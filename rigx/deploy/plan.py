"""Testbed deployment plan compiler.

Pure Python, no Nix/network I/O — given a resolved `kind = "testbed"`
Target with a declarative `services` section (design decision #9) and
each referenced capsule's already-built Nix store path, computes:

  - `dependency_order()`: a deterministic topological sort of
    `depends_on` edges (raises on cycles)
  - `compile_manifest()`: the target-independent contract
    (`manifest.json`) that the unit renderer and `bin/activate` /
    `bin/rollback` scripts read (design decision #2)

Resolving capsule store paths (which requires actually building them
via `rigx.builder`) and packaging the manifest + rendered units into
an on-disk plan directory are separate steps — see
`rigx.deploy.pack`. This module only compiles the manifest's
*content*.
"""

from __future__ import annotations

from rigx.config import Target


class PlanError(RuntimeError):
    """Raised for any testbed plan-compilation failure (dependency
    cycles, missing capsule store paths, etc.)."""


def dependency_order(testbed: Target) -> list[str]:
    """Topologically sort `testbed.services` by `depends_on`.

    Deterministic: ties are broken by service name, so the same
    testbed always compiles to the same order (test-diffable, and the
    order downstream unit rendering/activation relies on). Raises
    `PlanError` on a dependency cycle — config-load validation already
    checked that every `depends_on` name refers to a declared sibling
    service, but not for cycles among them.
    """
    services = testbed.services
    order: list[str] = []
    state: dict[str, str] = {}  # name -> "visiting" | "done"

    def visit(name: str, chain: list[str]) -> None:
        cur = state.get(name)
        if cur == "done":
            return
        if cur == "visiting":
            cycle = " -> ".join([*chain, name])
            raise PlanError(f"dependency cycle in services: {cycle}")
        state[name] = "visiting"
        for dep in sorted(services[name].depends_on):
            visit(dep, [*chain, name])
        state[name] = "done"
        order.append(name)

    for name in sorted(services):
        visit(name, [])
    return order


def compile_manifest(
    testbed: Target,
    capsule_paths: dict[str, str],
    capsule_backends: dict[str, str] | None = None,
) -> dict:
    """Compile `testbed` into the manifest dict that becomes
    `manifest.json` in the deployment plan.

    `capsule_paths` maps each referenced capsule target name to its
    already-built Nix store path (`output/<capsule>` resolved) — the
    caller is responsible for having built them (see
    `rigx.deploy.pack.resolve_capsule_paths`); this function doesn't
    build anything itself. `capsule_backends` maps the same names to
    their `backend` field ("lite"/"qemu"/"nixos") — used by
    `rigx.deploy.units` to decide how much a service's unit can be
    confined (see design decision #11); optional/defaults to unknown
    ("") per capsule so existing callers that don't care still work.
    """
    capsule_backends = capsule_backends or {}
    if testbed.kind != "testbed":
        raise PlanError(f"{testbed.name!r} is kind={testbed.kind!r}, not 'testbed'")
    if not testbed.services:
        raise PlanError(
            f"testbed {testbed.name!r} has no declarative 'services' section "
            f"(script-only testbeds aren't deployable — see `rigx run` instead)"
        )

    order = dependency_order(testbed)  # also validates: raises PlanError on cycles

    services: dict[str, dict] = {}
    for sname in order:
        svc = testbed.services[sname]
        if svc.capsule not in capsule_paths:
            raise PlanError(
                f"service {sname!r}: no resolved store path for capsule "
                f"{svc.capsule!r} (build it before compiling the plan)"
            )
        services[sname] = {
            "capsule": svc.capsule,
            "capsule_path": capsule_paths[svc.capsule],
            "capsule_backend": capsule_backends.get(svc.capsule, ""),
            "depends_on": list(svc.depends_on),
            "restart": svc.restart,
            "ports": [
                {"host": p.host, "container": p.container} for p in svc.ports
            ],
            "volumes": dict(svc.volumes),
            "secrets": dict(svc.secrets),
            "healthcheck": (
                {
                    "command": svc.healthcheck.command,
                    "interval": svc.healthcheck.interval,
                    "retries": svc.healthcheck.retries,
                }
                if svc.healthcheck is not None
                else None
            ),
        }

    return {
        "testbed": testbed.name,
        "start_order": order,
        "services": services,
        "volumes": sorted(testbed.testbed_volumes),
        "secrets": sorted(testbed.testbed_secrets),
    }
