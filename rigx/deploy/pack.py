"""Assemble a testbed deployment plan and get it onto the target host.

**Not yet wired through rigx's own flake.nix/build pipeline** — see the
"flake.nix integration" open item in the design memory. This module
assembles `manifest.json` + `units/` + `bin/activate` + `bin/rollback`
directly as a plain directory, then content-addresses it into the
*local* Nix store via `nix store add` so `nix copy --to ssh://` (and
`nix-env --profile ... --set`, invoked from inside `bin/activate`
itself — see `rigx.deploy.activate`) have a real store path to work
with. The capsules referenced by the testbed's services ARE built for
real, through rigx's existing `rigx.builder.build` — only the plan
*container* around them is Python-assembled rather than Nix-assembled.
Once flake.nix integration lands, `assemble_plan_dir` and
`add_to_local_store` are what a `kind = "testbed"` Nix derivation's
build phase would replace.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from collections.abc import Sequence
from pathlib import Path

from rigx import builder
from rigx.config import Project, Target
from rigx.deploy import activate, plan, units
from rigx.deploy.bootstrap import DeployError, SSHTarget


def resolve_capsule_backends(project: Project, testbed: Target) -> dict[str, str]:
    """Map each capsule referenced by `testbed.services` to its
    `backend` field ("lite"/"qemu"/"nixos") — a static config property,
    no build needed. Used by `rigx.deploy.units` to decide unit
    confinement (design decision #11)."""
    return {
        svc.capsule: project.targets[svc.capsule].backend
        for svc in testbed.services.values()
    }


def resolve_artifact_paths(project: Project, testbed: Target) -> dict[str, str]:
    """Build every target named in a service's `artifacts` and return
    target name -> its resolved Nix store path.

    These are ordinary rigx targets, built the same way capsules are —
    which is the point: mounting a store path is what makes deployed
    content immutable and makes a local deletion propagate (the next
    generation is a different path, not an edited directory)."""
    names = sorted({
        aname for svc in testbed.services.values() for aname in svc.artifacts
    })
    if not names:
        return {}
    built = dict(builder.build(project, names))
    paths: dict[str, str] = {}
    for name in names:
        link = built.get(name)
        if link is None:
            raise DeployError(f"artifact {name!r} did not build (no output link)")
        paths[name] = str(Path(link).resolve())
    return paths


def resolve_capsule_paths(project: Project, testbed: Target) -> dict[str, str]:
    """Build every capsule referenced by `testbed.services` (for real,
    via `rigx.builder.build`) and return capsule target name -> its
    resolved Nix store path."""
    capsule_names = sorted({svc.capsule for svc in testbed.services.values()})
    if not capsule_names:
        return {}
    built = dict(builder.build(project, capsule_names))
    paths: dict[str, str] = {}
    for name in capsule_names:
        link = built.get(name)
        if link is None:
            raise DeployError(f"capsule {name!r} did not build (no output link)")
        paths[name] = str(Path(link).resolve())
    return paths


def assemble_plan_dir(
    dest: Path, manifest: dict, previous_manifest: dict | None = None,
) -> Path:
    """Write `manifest.json` + `units/` + `bin/activate` +
    `bin/rollback` under `dest`. `dest` must not already exist."""
    (dest / "units").mkdir(parents=True)
    (dest / "bin").mkdir()
    (dest / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True))
    for name, text in units.render_units(manifest).items():
        (dest / "units" / name).write_text(text)
    activate_path = dest / "bin" / "activate"
    rollback_path = dest / "bin" / "rollback"
    activate_path.write_text(activate.render_activate(manifest, previous_manifest))
    rollback_path.write_text(activate.render_rollback(manifest))
    activate_path.chmod(0o755)
    rollback_path.chmod(0o755)
    return dest


def add_to_local_store(plan_dir: Path) -> str:
    """Content-address `plan_dir` into the local Nix store (`nix store
    add`), return the resulting store path."""
    proc = subprocess.run(
        ["nix", "store", "add", str(plan_dir)],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise DeployError(f"nix store add {plan_dir}: {proc.stderr.strip()}")
    return proc.stdout.strip()


def copy_to_target(
    store_path: str, target: SSHTarget, extra_paths: Sequence[str] = (),
) -> None:
    """`nix copy` the plan and everything it names to `target`.

    `extra_paths` has to be passed explicitly, and getting this wrong is
    not subtle: **a path added with `nix store add` has an empty
    reference set.** Nix computes references by scanning *derivation
    outputs*; a manually added path records none, however many store
    paths appear in the files inside it. So the plan's "closure" is the
    plan alone — `nix copy`ing it ships the units and scripts and none
    of the capsules they exec, and the deployment fails on the target
    with systemd status 203/EXEC (cannot execute ExecStart).

    This is a direct consequence of the plan not yet being a real
    derivation (see the module docstring). Once it is, its references
    are computed by Nix and this parameter goes away.

    The capsules themselves *are* derivation outputs, so passing each
    one pulls its own closure — interpreter, libraries, image tarball —
    transitively. Substituter negotiation still means paths the target
    already has aren't retransferred.
    """
    paths = [store_path, *extra_paths]
    proc = subprocess.run(
        ["nix", "copy", "--to", f"ssh://{target.destination}", *paths],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise DeployError(f"nix copy to {target.destination}: {proc.stderr.strip()}")


def build_and_push_plan(
    project: Project,
    testbed: Target,
    target: SSHTarget,
    *,
    previous_manifest: dict | None = None,
) -> str:
    """End to end: build the referenced capsules, compile the
    manifest, assemble + content-address the plan, `nix copy` it to
    `target`. Returns the plan's store path (identical on both ends —
    Nix store paths are content-addressed and host-independent)."""
    capsule_paths = resolve_capsule_paths(project, testbed)
    capsule_backends = resolve_capsule_backends(project, testbed)
    artifact_paths = resolve_artifact_paths(project, testbed)
    manifest = plan.compile_manifest(
        testbed, capsule_paths, capsule_backends, artifact_paths,
    )
    with tempfile.TemporaryDirectory() as d:
        plan_dir = assemble_plan_dir(Path(d) / testbed.name, manifest, previous_manifest)
        store_path = add_to_local_store(plan_dir)
    # Everything the plan's units and scripts name by absolute path.
    # The plan itself records no references — see `copy_to_target`.
    referenced = sorted({*capsule_paths.values(), *artifact_paths.values()})
    copy_to_target(store_path, target, referenced)
    return store_path
