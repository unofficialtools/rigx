"""Cross-compilation target aliases and per-target toolchain helpers."""

from __future__ import annotations

from rigx.config import Target, Variant


# Mirrors `config.DEFAULT_COMPILER` but kept here so this layer doesn't have to
# import it. If the table grows or moves, sync both sides.
DEFAULT_COMPILER_FOR_LANG = {
    "go":   "go",
    "rust": "rustc",
    "zig":  "zig",
    "nim":  "nim",
}


# Friendly cross-compilation target alias → details for each backend.
# `pkgsCross` is the nixpkgs `pkgsCross.<attr>` used for c/cxx (provides the
# cross stdenv). `zig_triple` is what `zig cc -target …` and `--cpu/--os`
# expect. `goos`/`goarch` set Go's environment.
CROSS_TARGETS: dict[str, dict[str, str]] = {
    "aarch64-linux": {
        "pkgsCross":  "aarch64-multiplatform",
        "zig_triple": "aarch64-linux-musl",
        "nim_cpu":    "arm64",
        "nim_os":     "linux",
        "goos":       "linux",
        "goarch":     "arm64",
    },
    "armv7-linux": {
        "pkgsCross":  "armv7l-hf-multiplatform",
        "zig_triple": "arm-linux-musleabihf",
        "nim_cpu":    "arm",
        "nim_os":     "linux",
        "goos":       "linux",
        "goarch":     "arm",
    },
    "x86_64-linux-musl": {
        "pkgsCross":  "musl64",
        "zig_triple": "x86_64-linux-musl",
        "nim_cpu":    "amd64",
        "nim_os":     "linux",
        "goos":       "linux",
        "goarch":     "amd64",
    },
    "x86_64-windows": {
        "pkgsCross":  "mingwW64",
        "zig_triple": "x86_64-windows-gnu",
        "nim_cpu":    "amd64",
        "nim_os":     "windows",
        "goos":       "windows",
        "goarch":     "amd64",
    },
}


def cross_info(triple: str) -> dict[str, str]:
    """Resolve a `target = …` value to the per-backend cross details. The
    raw string is also accepted as a fallback (caller is on their own to
    make sure the underlying tools recognize it)."""
    if not triple:
        return {}
    if triple in CROSS_TARGETS:
        return CROSS_TARGETS[triple]
    return {"pkgsCross": triple, "zig_triple": triple}


def effective_target(target: Target, variant: Variant | None) -> str:
    if variant and variant.target:
        return variant.target
    return target.target


def effective_compiler(target: Target, variant: Variant | None) -> str:
    if variant and variant.compiler:
        return variant.compiler
    return target.compiler


def stdenv_attr(target: Target, variant: Variant | None) -> str:
    """Pick the nixpkgs stdenv attr for a c/cxx target.

    With a `target = …` set, route through `pkgsCross.<x>.stdenv` (or
    `pkgsCross.<x>.<compiler>Stdenv` for a non-default compiler).
    Otherwise: `compiler = "clang"` → `clangStdenv`; `"gcc13"` →
    `gcc13Stdenv`; empty / non-c/cxx returns the default `stdenv`."""
    triple = effective_target(target, variant)
    if target.language in ("c", "cxx") and triple:
        info = cross_info(triple)
        cross_attr = info.get("pkgsCross", triple)
        comp = effective_compiler(target, variant)
        sub = "stdenv" if (not comp or comp == "gcc") else f"{comp}Stdenv"
        return f"pkgsCross.{cross_attr}.{sub}"
    if target.language not in ("c", "cxx"):
        return "stdenv"
    comp = effective_compiler(target, variant)
    if not comp or comp == "gcc":
        return "stdenv"
    return f"{comp}Stdenv"


def toolchain_pkgs(target: Target, variant: Variant | None) -> list[str]:
    """nixpkgs attrs auto-added to nativeBuildInputs for go/rust/zig/nim
    targets. C/cxx come from the stdenv directly, so this is empty.

    Cross-compiling Nim adds `zig` (used as the C compiler shim — see
    `_build_phase_nim_executable`)."""
    if target.language not in DEFAULT_COMPILER_FOR_LANG:
        return []
    comp = effective_compiler(target, variant) or DEFAULT_COMPILER_FOR_LANG[target.language]
    pkgs = [comp]
    if target.language == "nim" and effective_target(target, variant):
        pkgs.append("zig")
    return pkgs
