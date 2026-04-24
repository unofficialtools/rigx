"""Drive Nix to build targets and materialize outputs."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from procton import nix_gen
from procton.config import Project

OUTPUT_DIR = "output"
FLAKE_FILE = "flake.nix"
NIX_EXPERIMENTAL = ["--extra-experimental-features", "nix-command flakes"]


class BuildError(RuntimeError):
    pass


def _flake_path(project: Project) -> Path:
    return project.root / FLAKE_FILE


def _output_dir(project: Project) -> Path:
    return project.root / OUTPUT_DIR


def _nix_bin() -> str:
    nix = shutil.which("nix")
    if not nix:
        raise BuildError(
            "nix is not installed or not on PATH; install Nix from https://nixos.org/download"
        )
    return nix


def write_flake(project: Project) -> Path:
    """Regenerate flake.nix at the project root. Returns the file path."""
    flake_path = _flake_path(project)
    flake_path.write_text(nix_gen.generate(project))
    return flake_path


def update_lock(project: Project) -> None:
    write_flake(project)
    nix = _nix_bin()
    cmd = [nix, *NIX_EXPERIMENTAL, "flake", "lock", str(project.root)]
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
    """Every build attribute (targets with variants expand to one per variant)."""
    attrs: list[str] = []
    for name, target in project.targets.items():
        if not target.variants:
            attrs.append(name)
        else:
            for v in target.variant_names():
                attrs.append(f"{name}-{v}")
    return attrs


def build(project: Project, specs: list[str]) -> list[tuple[str, Path]]:
    """Build the given target specs (empty list = all). Returns (attr, out_link)."""
    write_flake(project)

    if specs:
        attrs = [_resolve_attr(project, s) for s in specs]
    else:
        attrs = _all_attrs(project)

    output_dir = _output_dir(project)
    output_dir.mkdir(exist_ok=True)

    nix = _nix_bin()
    results: list[tuple[str, Path]] = []

    for attr in attrs:
        out_link = output_dir / attr
        flake_ref = f"{project.root}#{attr}"
        cmd = [
            nix,
            *NIX_EXPERIMENTAL,
            "build",
            flake_ref,
            "--out-link",
            str(out_link),
        ]
        print(f"[procton] building {attr}")
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
