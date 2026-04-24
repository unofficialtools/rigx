"""Generate a Nix flake from a procton Project."""

from __future__ import annotations

from procton.config import GitDep, Project, Target, Variant


def _nix_str(s: str) -> str:
    """Quote a Python string for Nix (double-quoted form)."""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$") + '"'


def _nix_list(exprs: list[str]) -> str:
    if not exprs:
        return "[ ]"
    return "[ " + " ".join(exprs) + " ]"


def _git_input_url(dep: GitDep) -> str:
    # Nix flake URL. If rev looks like a commit sha, use rev=; otherwise ref=.
    is_sha = len(dep.rev) == 40 and all(c in "0123456789abcdef" for c in dep.rev.lower())
    qual = f"rev={dep.rev}" if is_sha else f"ref={dep.rev}"
    return f"git+{dep.url}?{qual}"


def _obj_name(source: str) -> str:
    # Map "src/foo/bar.cpp" -> "src_foo_bar.o"
    stem = source
    for ext in (".cpp", ".cxx", ".cc", ".c"):
        if stem.endswith(ext):
            stem = stem[: -len(ext)]
            break
    return stem.replace("/", "_") + ".o"


def _effective_cxxflags(target: Target, variant: Variant | None) -> list[str]:
    flags = list(target.cxxflags)
    defines = dict(target.defines)
    if variant:
        flags += variant.cxxflags
        defines.update(variant.defines)
    for k, v in defines.items():
        flags.append(f"-D{k}={v}" if v else f"-D{k}")
    return flags


def _effective_ldflags(target: Target, variant: Variant | None) -> list[str]:
    flags = list(target.ldflags)
    if variant:
        flags += variant.ldflags
    return flags


def _build_inputs(target: Target, project: Project) -> list[str]:
    exprs: list[str] = []
    for d in target.deps.nixpkgs:
        exprs.append(f"pkgs.{d}")
    for d in target.deps.git:
        dep = project.git_deps[d]
        attr = dep.attr or "default"
        exprs.append(f"inputs.{d}.packages.${{system}}.{attr}")
    for d in target.deps.internal:
        exprs.append(d)
    return exprs


def _internal_link_args(target: Target, project: Project) -> list[str]:
    """Positional linker args for internal static-library deps."""
    args: list[str] = []
    for d in target.deps.internal:
        dep_target = project.targets[d]
        if dep_target.kind == "static_library":
            args.append(f"${{{d}}}/lib/lib{d}.a")
    return args


def _internal_include_args(target: Target, project: Project) -> list[str]:
    args: list[str] = []
    for d in target.deps.internal:
        args.append(f"-I${{{d}}}/include")
    return args


def _build_phase_executable(
    target: Target, variant: Variant | None, project: Project
) -> str:
    cxxflags = _effective_cxxflags(target, variant)
    ldflags = _effective_ldflags(target, variant)
    includes = [f"-I{i}" for i in target.includes]
    internal_includes = _internal_include_args(target, project)
    internal_links = _internal_link_args(target, project)

    parts = ["$CXX"]
    parts += cxxflags
    parts += includes
    parts += internal_includes
    parts += target.sources
    parts += internal_links
    parts += ldflags
    parts += ["-o", target.name]
    cmd = " ".join(parts)

    return (
        "runHook preBuild\n"
        f"{cmd}\n"
        "runHook postBuild\n"
    )


def _install_phase_executable(target: Target) -> str:
    return (
        "runHook preInstall\n"
        "mkdir -p $out/bin\n"
        f"cp {target.name} $out/bin/\n"
        "runHook postInstall\n"
    )


def _build_phase_static_library(
    target: Target, variant: Variant | None, project: Project
) -> str:
    cxxflags = _effective_cxxflags(target, variant)
    includes = [f"-I{i}" for i in target.includes]
    internal_includes = _internal_include_args(target, project)

    lines = ["runHook preBuild"]
    obj_files: list[str] = []
    for src in target.sources:
        obj = _obj_name(src)
        obj_files.append(obj)
        parts = ["$CXX"]
        parts += cxxflags
        parts += ["-fPIC"]
        parts += includes
        parts += internal_includes
        parts += ["-c", src, "-o", obj]
        lines.append(" ".join(parts))
    lines.append(f"$AR rcs lib{target.name}.a {' '.join(obj_files)}")
    lines.append("runHook postBuild")
    return "\n".join(lines) + "\n"


def _install_phase_static_library(target: Target) -> str:
    lines = [
        "runHook preInstall",
        "mkdir -p $out/lib $out/include",
        f"cp lib{target.name}.a $out/lib/",
    ]
    for ph in target.public_headers:
        lines.append(f"cp -r {ph}/. $out/include/")
    lines.append("runHook postInstall")
    return "\n".join(lines) + "\n"


def _indent(text: str, n: int) -> str:
    prefix = " " * n
    return "".join(
        (prefix + line) if line.strip() else line
        for line in text.splitlines(keepends=True)
    )


def _mk_derivation(
    target: Target, variant: Variant | None, project: Project
) -> str:
    pname = target.name if variant is None else f"{target.name}-{variant.name}"
    build_inputs = _build_inputs(target, project)

    if target.kind == "executable":
        build_phase = _build_phase_executable(target, variant, project)
        install_phase = _install_phase_executable(target)
    elif target.kind == "static_library":
        build_phase = _build_phase_static_library(target, variant, project)
        install_phase = _install_phase_static_library(target)
    elif target.kind == "shared_library":
        raise NotImplementedError("shared_library kind not yet supported")
    else:
        raise ValueError(f"unknown kind {target.kind!r}")

    # Nix ''...'' multiline string. Inside it, ${foo} interpolates Nix; bash
    # vars like $CXX are left literal because they're not wrapped in ${}.
    lines = [
        "pkgs.stdenv.mkDerivation {",
        f"  pname = {_nix_str(pname)};",
        f"  version = {_nix_str(project.version)};",
        "  inherit src;",
        f"  buildInputs = {_nix_list(build_inputs)};",
        "  dontConfigure = true;",
        "  buildPhase = ''",
        _indent(build_phase, 4).rstrip() + "\n",
        "  '';",
        "  installPhase = ''",
        _indent(install_phase, 4).rstrip() + "\n",
        "  '';",
        "}",
    ]
    return "\n".join(lines)


def _target_block(target: Target, project: Project) -> str:
    if not target.variants:
        body = _mk_derivation(target, None, project)
        return f"{target.name} = {body};"

    lines: list[str] = []
    first_variant = sorted(target.variants.keys())[0]
    for vname in sorted(target.variants.keys()):
        variant = target.variants[vname]
        attr = f"{target.name}-{vname}"
        body = _mk_derivation(target, variant, project)
        lines.append(f"{attr} = {body};")
    # Alias unqualified name to first variant
    lines.append(f"{target.name} = {target.name}-{first_variant};")
    return "\n".join(lines)


def generate(project: Project) -> str:
    """Return the contents of flake.nix for the given project."""
    out: list[str] = []
    w = out.append

    w("{")
    w(f"  description = {_nix_str(f'procton build for {project.name}')};")
    w("")
    w("  inputs = {")
    w(f"    nixpkgs.url = {_nix_str(f'github:NixOS/nixpkgs/{project.nixpkgs_ref}')};")
    for name, dep in project.git_deps.items():
        w(f"    {name}.url = {_nix_str(_git_input_url(dep))};")
        w(f"    {name}.flake = {'true' if dep.flake else 'false'};")
    w("  };")
    w("")
    w("  outputs = { self, nixpkgs, ... }@inputs:")
    w("    let")
    w('      systems = [ "x86_64-linux" "aarch64-linux" "x86_64-darwin" "aarch64-darwin" ];')
    w("      forAll = f: nixpkgs.lib.genAttrs systems f;")
    w("      srcRoot = builtins.path {")
    w("        path = ./.;")
    w('        name = "source";')
    w("        filter = path: type:")
    w("          let base = baseNameOf (toString path); in")
    w('          !(builtins.elem base [')
    w('            ".procton" "output" ".git" "result"')
    w('            "flake.nix" "flake.lock"')
    w('          ]);')
    w("      };")
    w("    in {")
    w("      packages = forAll (system:")
    w("        let")
    w("          pkgs = import nixpkgs { inherit system; };")
    w("          src = srcRoot;")
    w("        in rec {")

    for tname, target in project.targets.items():
        block = _target_block(target, project)
        w(_indent(block, 10))

    w("        });")
    w("    };")
    w("}")
    return "\n".join(out) + "\n"
