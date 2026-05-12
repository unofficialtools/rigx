"""Zig build phase."""

from __future__ import annotations

from rigx.config import Project, Target, Variant
from rigx.nix.cross import cross_info, effective_target
from rigx.nix.render import sh_join


def effective_zigflags(target: Target, variant: Variant | None) -> list[str]:
    flags = list(target.zigflags)
    if variant:
        flags += variant.zigflags
    return flags


def build_phase_zig_executable(
    target: Target, variant: Variant | None, project: Project
) -> str:
    if not target.sources:
        raise ValueError(f"zig executable {target.name!r} needs at least one source")
    entry = target.sources[0]
    flags = effective_zigflags(target, variant)
    triple = effective_target(target, variant)
    parts: list[str] = ["zig", "build-exe"]
    if triple:
        parts += ["-target", cross_info(triple).get("zig_triple", triple)]
    parts += flags
    parts += [f"-femit-bin={target.name}", entry]
    cmd = sh_join(parts)
    return (
        "runHook preBuild\n"
        "export HOME=$TMPDIR\n"
        # Zig caches under $XDG_CACHE_HOME or $HOME/.cache; ensure a writable
        # home is enough.
        f"{cmd}\n"
        "runHook postBuild\n"
    )
