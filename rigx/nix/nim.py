"""Nim build phase."""

from __future__ import annotations

from rigx.config import Project, Target, Variant
from rigx.nix.cross import cross_info, effective_target
from rigx.nix.render import sh_join


def effective_nim_flags(target: Target, variant: Variant | None) -> list[str]:
    flags = list(target.nim_flags)
    defines = dict(target.defines)
    if variant:
        flags += variant.nim_flags
        defines.update(variant.defines)
    for k, v in defines.items():
        flags.append(f"-d:{k}={v}" if v else f"-d:{k}")
    return flags


def build_phase_nim_executable(
    target: Target, variant: Variant | None, project: Project
) -> str:
    if not target.sources:
        raise ValueError(f"nim executable {target.name!r} needs at least one source")
    entry = target.sources[0]
    flags = effective_nim_flags(target, variant)
    triple = effective_target(target, variant)

    # Cross-compile args carry literal `$TMPDIR` references that the
    # build sandbox expands at runtime — `sh_join` would single-quote
    # them and Nim would receive the literal string `$TMPDIR/...`, then
    # treat the `$T` as a format placeholder and abort with
    # "invalid format string". So this prefix is built unquoted and
    # concatenated with the quoted tail.
    cross_prefix = ""
    if triple:
        info = cross_info(triple)
        nim_cpu = info.get("nim_cpu") or info.get("zig_triple", "").split("-")[0]
        nim_os = info.get("nim_os") or info.get("zig_triple", "").split("-")[1]
        cross_prefix = (
            " --cc:clang"
            " --clang.exe:$TMPDIR/bin/zigcc"
            " --clang.linkerexe:$TMPDIR/bin/zigcc"
            f" --cpu:{nim_cpu}"
            f" --os:{nim_os}"
        )

    tail_parts: list[str] = list(flags)
    tail_parts += ["--nimcache:./nimcache", f"--out:{target.name}", entry]
    cmd = "nim c" + cross_prefix
    if tail_parts:
        cmd += " " + sh_join(tail_parts)

    shim = ""
    if triple:
        zig_target = cross_info(triple).get("zig_triple", triple)
        # Use `#!/bin/sh` not `/usr/bin/env bash`: the strict Nix build
        # sandbox provides `/bin/sh` (bash) but no `/usr/bin/env`.
        shim = (
            "mkdir -p $TMPDIR/bin\n"
            "echo '#!/bin/sh' > $TMPDIR/bin/zigcc\n"
            f"echo 'exec zig cc -target {zig_target} \"$@\"' >> $TMPDIR/bin/zigcc\n"
            "chmod +x $TMPDIR/bin/zigcc\n"
        )

    # Nim wants a writable HOME (for nimcache defaults etc.); stdenv's default
    # /homeless-shelter is read-only.
    return (
        "runHook preBuild\n"
        "export HOME=$TMPDIR\n"
        f"{shim}"
        f"{cmd}\n"
        "runHook postBuild\n"
    )
