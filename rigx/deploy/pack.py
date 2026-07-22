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


def copy_to_target(store_path: str, target: SSHTarget) -> None:
    """`nix copy` the plan's full transitive closure — including every
    referenced capsule — to `target`. Substituter negotiation means
    paths the target already has (e.g. an unchanged capsule from a
    previous deploy) aren't retransferred."""
    proc = subprocess.run(
        ["nix", "copy", "--to", f"ssh://{target.destination}", store_path],
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
    manifest = plan.compile_manifest(testbed, capsule_paths, capsule_backends)
    with tempfile.TemporaryDirectory() as d:
        plan_dir = assemble_plan_dir(Path(d) / testbed.name, manifest, previous_manifest)
        store_path = add_to_local_store(plan_dir)
    copy_to_target(store_path, target)
    return store_path
