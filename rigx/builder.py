"""Drive Nix to build targets and materialize outputs."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from rigx import nix_gen
from rigx.config import Project

OUTPUT_DIR = "output"
FLAKE_FILE = "flake.nix"
NIX_EXPERIMENTAL = ["--extra-experimental-features", "nix-command flakes"]


class BuildError(RuntimeError):
    pass


class NixNotFoundError(BuildError):
    """Raised when the `nix` binary is not on PATH."""


NIX_INSTALL_INSTRUCTIONS = """\
Nix is required but was not found on PATH.

rigx runs all builds inside Nix's sandbox. Install Nix, restart your shell,
then re-run `rigx`.

Install instructions
--------------------

macOS / Linux (official installer):
  sh <(curl -L https://nixos.org/nix/install) --daemon

macOS / Linux (Determinate Systems installer, recommended for most users):
  curl --proto '=https' --tlsv1.2 -sSf -L \\
    https://install.determinate.systems/nix | sh -s -- install

After installing, restart your shell or source:
  . /nix/var/nix/profiles/default/etc/profile.d/nix-daemon.sh

Docs: https://nixos.org/download
"""


def _flake_path(project: Project) -> Path:
    return project.root / FLAKE_FILE


def _output_dir(project: Project) -> Path:
    return project.root / OUTPUT_DIR


def _nix_bin() -> str:
    nix = shutil.which("nix")
    if not nix:
        raise NixNotFoundError(NIX_INSTALL_INSTRUCTIONS)
    return nix


def write_flake(project: Project) -> Path:
    """Regenerate flake.nix at the project root. Returns the file path."""
    flake_path = _flake_path(project)
    flake_path.write_text(nix_gen.generate(project))
    return flake_path


def _flake_ref(project: Project, attr: str | None = None) -> str:
    # Use the explicit "path:" scheme so Nix treats the directory as a flake
    # regardless of any enclosing git repo (which otherwise hides untracked
    # files like the generated flake.nix).
    ref = f"path:{project.root.resolve()}"
    return f"{ref}#{attr}" if attr else ref


def run_nixpkgs_tool(
    project: Project,
    attr: str,
    args: list[str],
    cwd: Path | None = None,
) -> int:
    """Invoke a binary from the project's pinned nixpkgs via `nix run`.

    Lets rigx wrap tools like `uv` without requiring the user to install them
    on their host — the project's `[nixpkgs].ref` (pinned via flake.lock)
    provides a reproducible binary.
    """
    nix = _nix_bin()
    cmd = [
        nix,
        *NIX_EXPERIMENTAL,
        "run",
        f"nixpkgs/{project.nixpkgs_ref}#{attr}",
        "--",
        *args,
    ]
    result = subprocess.run(cmd, cwd=cwd, check=False)
    return result.returncode


def update_lock(project: Project) -> None:
    write_flake(project)
    nix = _nix_bin()
    cmd = [nix, *NIX_EXPERIMENTAL, "flake", "lock", _flake_ref(project)]
    result = subprocess.run(cmd, check=False)
    if result.returncode != 0:
        raise BuildError(f"nix flake lock failed (exit {result.returncode})")


def _resolve_attr(project: Project, spec: str) -> str:
    """Map a user target spec ('hello' or 'hello@debug') to a Nix attr name."""
    if "@" in spec:
        name, variant = spec.split("@", 1)
    else:
        name, variant = spec, None
    if name not in project.targets:
        raise BuildError(f"no such target: {name}")
    target = project.targets[name]
    if variant is None:
        return name
    if variant not in target.variants:
        available = ", ".join(target.variant_names()) or "(none)"
        raise BuildError(
            f"target {name!r} has no variant {variant!r}; available: {available}"
        )
    return f"{name}-{variant}"


def _all_attrs(project: Project) -> list[str]:
    """Every build attribute (targets with variants expand to one per variant).

    `script`-kind targets are excluded so `rigx build` with no arguments does
    not inadvertently run side-effecting tasks (publish, deploy, etc.). Run
    them explicitly with `rigx build <name>`.
    """
    attrs: list[str] = []
    for name, target in project.targets.items():
        if target.kind == "script":
            continue
        if not target.variants:
            attrs.append(name)
        else:
            for v in target.variant_names():
                attrs.append(f"{name}-{v}")
    return attrs


def run_script_target(project: Project, target) -> int:
    """Execute a `kind = "script"` target via `nix shell` on the host.

    Tools listed in `deps.nixpkgs` are brought onto PATH from the project's
    pinned nixpkgs. Runs in the project root; no sandbox. The target's script
    is passed to `bash -eo pipefail`.
    """
    assert target.script is not None
    nix = _nix_bin()
    refs = [
        f"nixpkgs/{project.nixpkgs_ref}#{pkg}"
        for pkg in target.deps.nixpkgs
    ]
    cmd = [
        nix,
        *NIX_EXPERIMENTAL,
        "shell",
        *refs,
        "--command",
        "bash",
        "-eo",
        "pipefail",
        "-c",
        target.script,
    ]
    result = subprocess.run(cmd, cwd=project.root, check=False)
    return result.returncode


def run_named_script(project: Project, name: str) -> None:
    """CLI helper: look up a script target by name and execute it."""
    if name not in project.targets:
        raise BuildError(f"no such target: {name}")
    target = project.targets[name]
    if target.kind != "script":
        raise BuildError(
            f"target {name!r} (kind={target.kind!r}) is not a script target; "
            f"use `rigx build {name}` instead"
        )
    print(f"[rigx] running {name}")
    rc = run_script_target(project, target)
    if rc != 0:
        raise BuildError(f"script target {name!r} failed (exit {rc})")


def build(project: Project, specs: list[str]) -> list[tuple[str, Path]]:
    """Build the given target specs (empty list = all). Returns (attr, out_link).

    `script`-kind targets are NOT buildable — they produce no artifact. Name a
    script here and you'll get an error pointing at `rigx run`. `rigx build`
    with no args skips them (see `_all_attrs`).
    """
    if specs:
        for spec in specs:
            name = spec.split("@", 1)[0]
            if (
                name in project.targets
                and project.targets[name].kind == "script"
            ):
                raise BuildError(
                    f"target {name!r} is a script target (produces no artifact); "
                    f"use `rigx run {name}` instead"
                )
        attrs = [_resolve_attr(project, s) for s in specs]
    else:
        attrs = _all_attrs(project)

    if not attrs:
        return []

    write_flake(project)
    output_dir = _output_dir(project)
    output_dir.mkdir(exist_ok=True)

    nix = _nix_bin()
    results: list[tuple[str, Path]] = []

    for attr in attrs:
        out_link = output_dir / attr
        flake_ref = _flake_ref(project, attr)
        cmd = [
            nix,
            *NIX_EXPERIMENTAL,
            "build",
            flake_ref,
            "--out-link",
            str(out_link),
        ]
        print(f"[rigx] building {attr}")
        result = subprocess.run(cmd, check=False)
        if result.returncode != 0:
            raise BuildError(f"nix build {attr} failed (exit {result.returncode})")
        results.append((attr, out_link))

    return results


def clean(project: Project) -> None:
    output_dir = _output_dir(project)
    if output_dir.exists():
        # Remove symlinks and their contents (but not the Nix store paths themselves).
        for child in output_dir.iterdir():
            if child.is_symlink():
                child.unlink()
            elif child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
        output_dir.rmdir()
