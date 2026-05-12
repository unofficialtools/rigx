"""Go build phase."""

from __future__ import annotations

from rigx.config import Project, Target, Variant
from rigx.nix.cross import cross_info, effective_target
from rigx.nix.render import sh_join


def effective_goflags(target: Target, variant: Variant | None) -> list[str]:
    flags = list(target.goflags)
    if variant:
        flags += variant.goflags
    return flags


def build_phase_go_executable(
    target: Target, variant: Variant | None, project: Project
) -> str:
    flags = effective_goflags(target, variant)
    parts: list[str] = ["go", "build"]
    parts += flags
    parts += ["-o", target.name]
    parts += target.sources
    cmd = sh_join(parts)
    triple = effective_target(target, variant)
    cross_env = ""
    if triple:
        info = cross_info(triple)
        if "goos" in info and "goarch" in info:
            cross_env = (
                f"export GOOS={info['goos']}\n"
                f"export GOARCH={info['goarch']}\n"
                f"export CGO_ENABLED=0\n"
            )
    # Go wants writable HOME and module/cache dirs; stdenv's defaults are
    # read-only or unset.
    return (
        "runHook preBuild\n"
        "export HOME=$TMPDIR\n"
        "export GOCACHE=$TMPDIR/go-cache\n"
        "export GOPATH=$TMPDIR/go\n"
        f"{cross_env}"
        f"{cmd}\n"
        "runHook postBuild\n"
    )
