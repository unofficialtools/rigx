"""C and C++ build phases (executable, static_library, shared_library)."""

from __future__ import annotations

from rigx.config import Project, Target, Variant
from rigx.nix.render import sh_join


def obj_name(source: str) -> str:
    """Map 'src/foo/bar.cpp' -> 'src_foo_bar.o'."""
    stem = source
    for ext in (".cpp", ".cxx", ".cc", ".c"):
        if stem.endswith(ext):
            stem = stem[: -len(ext)]
            break
    return stem.replace("/", "_") + ".o"


def effective_cxxflags(target: Target, variant: Variant | None) -> list[str]:
    flags = list(target.cxxflags)
    defines = dict(target.defines)
    if variant:
        flags += variant.cxxflags
        defines.update(variant.defines)
    for k, v in defines.items():
        flags.append(f"-D{k}={v}" if v else f"-D{k}")
    return flags


def effective_cflags(target: Target, variant: Variant | None) -> list[str]:
    flags = list(target.cflags)
    defines = dict(target.defines)
    if variant:
        flags += variant.cflags
        defines.update(variant.defines)
    for k, v in defines.items():
        flags.append(f"-D{k}={v}" if v else f"-D{k}")
    return flags


def effective_ldflags(target: Target, variant: Variant | None) -> list[str]:
    flags = list(target.ldflags)
    if variant:
        flags += variant.ldflags
    return flags


def _internal_includes(target: Target, project: Project) -> list[str]:
    # Inlined here (rather than importing from flake.py) to keep the
    # c-family module dependency-free against flake assembly.
    from rigx.nix.flake import internal_include_args
    return internal_include_args(target, project)


def _internal_links(target: Target, project: Project) -> list[str]:
    from rigx.nix.flake import internal_link_args
    return internal_link_args(target, project)


def build_phase_cxx_executable(
    target: Target, variant: Variant | None, project: Project
) -> str:
    cxxflags = effective_cxxflags(target, variant)
    ldflags = effective_ldflags(target, variant)
    includes = [f"-I{i}" for i in target.includes]
    internal_includes = _internal_includes(target, project)
    internal_links = _internal_links(target, project)

    parts: list[str] = ["$CXX"]
    parts += cxxflags
    parts += includes
    parts += internal_includes
    parts += target.sources
    parts += internal_links
    parts += ldflags
    parts += ["-o", target.name]
    # `$CXX` is left literal (not quoted) — stdenv expands the env var.
    cmd = "$CXX " + sh_join(parts[1:])

    return (
        "runHook preBuild\n"
        f"{cmd}\n"
        "runHook postBuild\n"
    )


def build_phase_c_executable(
    target: Target, variant: Variant | None, project: Project
) -> str:
    cflags = effective_cflags(target, variant)
    ldflags = effective_ldflags(target, variant)
    includes = [f"-I{i}" for i in target.includes]
    internal_includes = _internal_includes(target, project)
    internal_links = _internal_links(target, project)

    parts: list[str] = ["$CC"]
    parts += cflags
    parts += includes
    parts += internal_includes
    parts += target.sources
    parts += internal_links
    parts += ldflags
    parts += ["-o", target.name]
    cmd = "$CC " + sh_join(parts[1:])

    return (
        "runHook preBuild\n"
        f"{cmd}\n"
        "runHook postBuild\n"
    )


def build_phase_cxx_static_library(
    target: Target, variant: Variant | None, project: Project
) -> str:
    cxxflags = effective_cxxflags(target, variant)
    includes = [f"-I{i}" for i in target.includes]
    internal_includes = _internal_includes(target, project)

    lines = ["runHook preBuild"]
    obj_files: list[str] = []
    for src in target.sources:
        obj = obj_name(src)
        obj_files.append(obj)
        parts: list[str] = ["$CXX"]
        parts += cxxflags
        parts += ["-fPIC"]
        parts += includes
        parts += internal_includes
        parts += ["-c", src, "-o", obj]
        lines.append("$CXX " + sh_join(parts[1:]))
    lines.append("$AR rcs " + sh_join([f"lib{target.name}.a", *obj_files]))
    lines.append("runHook postBuild")
    return "\n".join(lines) + "\n"


def build_phase_c_static_library(
    target: Target, variant: Variant | None, project: Project
) -> str:
    cflags = effective_cflags(target, variant)
    includes = [f"-I{i}" for i in target.includes]
    internal_includes = _internal_includes(target, project)

    lines = ["runHook preBuild"]
    obj_files: list[str] = []
    for src in target.sources:
        obj = obj_name(src)
        obj_files.append(obj)
        parts: list[str] = ["$CC"]
        parts += cflags
        parts += ["-fPIC"]
        parts += includes
        parts += internal_includes
        parts += ["-c", src, "-o", obj]
        lines.append("$CC " + sh_join(parts[1:]))
    lines.append("$AR rcs " + sh_join([f"lib{target.name}.a", *obj_files]))
    lines.append("runHook postBuild")
    return "\n".join(lines) + "\n"


def build_phase_cxx_shared_library(
    target: Target, variant: Variant | None, project: Project
) -> str:
    cxxflags = effective_cxxflags(target, variant)
    ldflags = effective_ldflags(target, variant)
    includes = [f"-I{i}" for i in target.includes]
    internal_includes = _internal_includes(target, project)
    parts: list[str] = ["$CXX", "-shared", "-fPIC"]
    parts += cxxflags
    parts += includes
    parts += internal_includes
    parts += target.sources
    parts += ldflags
    parts += ["-o", f"lib{target.name}.so"]
    cmd = "$CXX " + sh_join(parts[1:])
    return (
        "runHook preBuild\n"
        f"{cmd}\n"
        "runHook postBuild\n"
    )


def build_phase_c_shared_library(
    target: Target, variant: Variant | None, project: Project
) -> str:
    cflags = effective_cflags(target, variant)
    ldflags = effective_ldflags(target, variant)
    includes = [f"-I{i}" for i in target.includes]
    internal_includes = _internal_includes(target, project)
    parts: list[str] = ["$CC", "-shared", "-fPIC"]
    parts += cflags
    parts += includes
    parts += internal_includes
    parts += target.sources
    parts += ldflags
    parts += ["-o", f"lib{target.name}.so"]
    cmd = "$CC " + sh_join(parts[1:])
    return (
        "runHook preBuild\n"
        f"{cmd}\n"
        "runHook postBuild\n"
    )
