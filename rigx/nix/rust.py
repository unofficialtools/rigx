"""Rust build phases (executable, static_library, shared_library)."""

from __future__ import annotations

from rigx.config import Project, Target, Variant
from rigx.nix.render import sh_join


def effective_rustflags(target: Target, variant: Variant | None) -> list[str]:
    flags = list(target.rustflags)
    if variant:
        flags += variant.rustflags
    return flags


def build_phase_rust_executable(
    target: Target, variant: Variant | None, project: Project
) -> str:
    if not target.sources:
        raise ValueError(f"rust executable {target.name!r} needs at least one source")
    entry = target.sources[0]
    flags = effective_rustflags(target, variant)
    parts: list[str] = ["rustc"]
    parts += flags
    parts += ["-o", target.name, entry]
    cmd = sh_join(parts)
    return (
        "runHook preBuild\n"
        "export HOME=$TMPDIR\n"
        f"{cmd}\n"
        "runHook postBuild\n"
    )


def build_phase_rust_static_library(
    target: Target, variant: Variant | None, project: Project
) -> str:
    if not target.sources:
        raise ValueError(f"rust static_library {target.name!r} needs at least one source")
    entry = target.sources[0]
    flags = effective_rustflags(target, variant)
    parts: list[str] = ["rustc", "--crate-type=staticlib", f"--crate-name={target.name}"]
    parts += flags
    parts += ["-o", f"lib{target.name}.a", entry]
    cmd = sh_join(parts)
    return (
        "runHook preBuild\n"
        "export HOME=$TMPDIR\n"
        f"{cmd}\n"
        "runHook postBuild\n"
    )


def build_phase_rust_shared_library(
    target: Target, variant: Variant | None, project: Project
) -> str:
    if not target.sources:
        raise ValueError(f"rust shared_library {target.name!r} needs at least one source")
    entry = target.sources[0]
    flags = effective_rustflags(target, variant)
    parts: list[str] = ["rustc", "--crate-type=cdylib", f"--crate-name={target.name}"]
    parts += flags
    parts += ["-o", f"lib{target.name}.so", entry]
    cmd = sh_join(parts)
    return (
        "runHook preBuild\n"
        "export HOME=$TMPDIR\n"
        f"{cmd}\n"
        "runHook postBuild\n"
    )
