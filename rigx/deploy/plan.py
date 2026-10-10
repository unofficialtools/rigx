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


def network_name(testbed: str) -> str:
    """The container network every service in `testbed` joins.

    Implicit and unconfigurable on purpose. A flat per-testbed network
    needs no topology from the user — membership is "every service",
    and a peer's address is `<service>:<port>` where the port comes
    from the capsule's own `ports` declaration. That leaves nothing to
    declare, so there is no `networks` table and no `network` field:
    the same information that already describes the services describes
    the network.
    """
    return f"rigx-{testbed}"


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


def _resolve_artifacts(
    sname: str, svc, artifact_paths: dict[str, str],
) -> list[dict]:
    """Turn a service's `artifacts` table into resolved store-path mounts.

    Sorted by target name so the manifest is stable; `units` re-sorts by
    mount depth when it renders, since that is what mount ordering needs.
    """
    out = []
    for aname in sorted(svc.artifacts):
        if aname not in artifact_paths:
            raise PlanError(
                f"service {sname!r}: no resolved store path for artifact "
                f"{aname!r} (build it before compiling the plan)"
            )
        out.append({
            "target": aname,
            "store_path": artifact_paths[aname],
            "mount": svc.artifacts[aname],
        })
    return out


def compile_manifest(
    testbed: Target,
    capsule_paths: dict[str, str],
    capsule_backends: dict[str, str] | None = None,
    artifact_paths: dict[str, str] | None = None,
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
    artifact_paths = artifact_paths or {}
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
                {
                    "host": p.host,
                    "container": p.container,
                    "host_addr": p.host_addr,
                    "publish": p.publish_spec(),
                }
                for p in svc.ports
            ],
            "volumes": dict(svc.volumes),
            "artifacts": _resolve_artifacts(sname, svc, artifact_paths),
            "secrets": dict(svc.secrets),
            "env": dict(svc.env),
            "healthcheck": (
                {
                    "command": svc.healthcheck.command,
                    "interval": svc.healthcheck.interval,
                    "retries": svc.healthcheck.retries,
                    "scope": svc.healthcheck.scope,
                }
                if svc.healthcheck is not None
                else None
            ),
        }

    return {
        "testbed": testbed.name,
        "network": network_name(testbed.name),
        "start_order": order,
        "services": services,
        "volumes": sorted(testbed.testbed_volumes),
        "secrets": sorted(testbed.testbed_secrets),
    }
