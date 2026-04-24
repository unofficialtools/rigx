"""Generate a Nix flake from a rigx Project."""

from __future__ import annotations

from rigx.config import GitDep, Project, Target, Variant


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
    # If `run` names an internal target, add it to buildInputs implicitly so
    # it resolves via ${name} interpolation. If it's a bare command, the user
    # must supply it via deps.nixpkgs/deps.git (on PATH in the sandbox).
    if (
        target.run
        and target.run in project.targets
        and target.run not in target.deps.internal
    ):
        exprs.append(target.run)
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


def _effective_nim_flags(target: Target, variant: Variant | None) -> list[str]:
    flags = list(target.nim_flags)
    defines = dict(target.defines)
    if variant:
        flags += variant.nim_flags
        defines.update(variant.defines)
    for k, v in defines.items():
        flags.append(f"-d:{k}={v}" if v else f"-d:{k}")
    return flags


def _build_phase_nim_executable(
    target: Target, variant: Variant | None, project: Project
) -> str:
    if not target.sources:
        raise ValueError(f"nim_executable {target.name!r} needs at least one source")
    entry = target.sources[0]
    flags = _effective_nim_flags(target, variant)

    parts = ["nim", "c"]
    parts += flags
    parts += ["--nimcache:./nimcache", f"--out:{target.name}", entry]
    cmd = " ".join(parts)

    # Nim wants a writable HOME (for nimcache defaults etc.); stdenv's default
    # /homeless-shelter is read-only.
    return (
        "runHook preBuild\n"
        "export HOME=$TMPDIR\n"
        f"{cmd}\n"
        "runHook postBuild\n"
    )


def _install_phase_nim_executable(target: Target) -> str:
    return (
        "runHook preInstall\n"
        "mkdir -p $out/bin\n"
        f"cp {target.name} $out/bin/\n"
        "runHook postInstall\n"
    )


def _shell_quote(s: str) -> str:
    if not s or any(c in s for c in " \t\n\"'$\\`*?[]{}|&;<>()!#~"):
        return "'" + s.replace("'", "'\\''") + "'"
    return s


def _build_phase_run(target: Target, project: Project) -> str:
    assert target.run is not None
    if target.run in project.targets:
        # internal target: absolute path into its store output
        binary = f"${{{target.run}}}/bin/{target.run}"
    else:
        # bare command name: resolved via PATH from deps.nixpkgs / deps.git
        binary = target.run
    quoted_args = " ".join(_shell_quote(a) for a in target.args)
    cmd = f"{binary} {quoted_args}".rstrip()
    return (
        "runHook preBuild\n"
        f"{cmd}\n"
        "runHook postBuild\n"
    )


def _install_phase_run(target: Target) -> str:
    lines = ["runHook preInstall", "mkdir -p $out"]
    for out in target.outputs:
        parent = "/".join(out.split("/")[:-1])
        if parent:
            lines.append(f"mkdir -p $out/{parent}")
        # cp -r handles both files and directories so outputs can be either.
        lines.append(f"cp -r {out} $out/{out}")
    lines.append("runHook postInstall")
    return "\n".join(lines) + "\n"


FAKE_SHA256 = "sha256-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="



def _python_pkg_attr(version: str) -> str:
    """'3.12' -> 'python312'."""
    major_minor = version.replace(".", "")
    return f"python{major_minor}"


def _mk_python_derivation(target: Target, project: Project) -> str:
    """Python scripts: build a uv-managed venv (FOD) and wrap the entry script.

    The venv is produced by a fixed-output derivation that runs
    `uv sync --frozen` against the user's pyproject.toml + uv.lock. Network is
    permitted (FODs can fetch); reproducibility is pinned by the outputHash.
    """
    if not target.sources:
        raise ValueError(f"python_script {target.name!r} needs at least one source")
    entry = target.sources[0]
    entry_dir = "/".join(entry.split("/")[:-1]) or "."

    # Resolve project-relative paths to Nix path literals
    pdir = target.python_project.strip("/")
    if pdir == "" or pdir == ".":
        pyproject_nix = "./pyproject.toml"
        uvlock_nix = "./uv.lock"
    else:
        pyproject_nix = f"./{pdir}/pyproject.toml"
        uvlock_nix = f"./{pdir}/uv.lock"

    venv_hash = target.python_venv_hash or FAKE_SHA256
    python_attr = _python_pkg_attr(target.python_version)

    copy_lines = ["mkdir -p $out/share/" + target.name, "mkdir -p $out/bin"]
    for src in target.sources:
        parent = "/".join(src.split("/")[:-1])
        if parent:
            copy_lines.append(f"mkdir -p $out/share/{target.name}/{parent}")
        copy_lines.append(f"cp {src} $out/share/{target.name}/{src}")

    site_pkgs = f"lib/python{target.python_version}/site-packages"

    # Wrapper uses pythonPkg (from downstream buildInputs) and prepends the
    # stripped venv's site-packages onto PYTHONPATH.
    install_phase = (
        "runHook preInstall\n"
        + "\n".join(copy_lines)
        + "\n"
        f"makeWrapper ${{pythonPkg}}/bin/python3 $out/bin/{target.name} \\\n"
        f"  --add-flags $out/share/{target.name}/{entry} \\\n"
        f"  --prefix PYTHONPATH : $out/share/{target.name}/{entry_dir} \\\n"
        f"  --prefix PYTHONPATH : ${{pythonVenv}}/{site_pkgs}\n"
        "runHook postInstall\n"
    )

    lines: list[str] = []
    lines.append("let")
    lines.append(f"  pythonPkg = pkgs.{python_attr};")
    lines.append("  venvSrc = pkgs.runCommand \"" + target.name + "-pysrc\" {} ''")
    lines.append("    mkdir -p $out")
    lines.append(f"    cp ${{{pyproject_nix}}} $out/pyproject.toml")
    lines.append(f"    cp ${{{uvlock_nix}}} $out/uv.lock")
    lines.append("  '';")
    lines.append("  pythonVenv = pkgs.stdenv.mkDerivation {")
    lines.append(f"    name = \"{target.name}-venv\";")
    lines.append("    src = venvSrc;")
    lines.append("    nativeBuildInputs = [ pkgs.uv pythonPkg pkgs.cacert ];")
    lines.append("    dontConfigure = true;")
    lines.append("    SSL_CERT_FILE = \"${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt\";")
    lines.append("    UV_PYTHON_DOWNLOADS = \"never\";")
    lines.append("    UV_PYTHON_PREFERENCE = \"only-system\";")
    lines.append("    UV_LINK_MODE = \"copy\";")
    lines.append("    UV_NO_CACHE = \"1\";")
    lines.append("    SOURCE_DATE_EPOCH = \"1\";")
    lines.append("    buildPhase = ''")
    lines.append("      runHook preBuild")
    lines.append("      export HOME=$TMPDIR")
    lines.append("      uv sync --frozen --no-install-project --python ${pythonPkg}/bin/python3")
    # Strip /nix/store references so the FOD output is self-contained.
    lines.append("      rm -f .venv/pyvenv.cfg")
    lines.append("      find .venv -type l | while IFS= read -r link; do")
    lines.append("        t=$(readlink \"$link\")")
    lines.append("        case \"$t\" in /nix/store/*) rm \"$link\" ;; esac")
    lines.append("      done")
    lines.append("      find .venv -type d -name __pycache__ -prune -exec rm -rf {} +")
    # Remove empty bin dir; it no longer contains anything useful.
    lines.append("      rm -rf .venv/bin")
    # Normalize timestamps for reproducibility of the FOD hash.
    lines.append("      find .venv -exec touch -h -d @1 {} +")
    lines.append("      runHook postBuild")
    lines.append("    '';")
    lines.append("    installPhase = ''")
    lines.append("      runHook preInstall")
    lines.append("      cp -r .venv/. $out")
    lines.append("      runHook postInstall")
    lines.append("    '';")
    lines.append("    outputHashMode = \"recursive\";")
    lines.append("    outputHashAlgo = \"sha256\";")
    lines.append(f"    outputHash = \"{venv_hash}\";")
    lines.append("  };")
    lines.append("in pkgs.stdenv.mkDerivation {")
    lines.append(f"  pname = {_nix_str(target.name)};")
    lines.append(f"  version = {_nix_str(project.version)};")
    lines.append("  inherit src;")
    lines.append("  nativeBuildInputs = [ pkgs.makeWrapper ];")
    lines.append("  buildInputs = [ pythonPkg ];")
    lines.append("  dontConfigure = true;")
    lines.append("  dontBuild = true;")
    lines.append("  installPhase = ''")
    lines.append(_indent(install_phase, 4).rstrip() + "\n")
    lines.append("  '';")
    lines.append("}")
    return "\n".join(lines)


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


def _mk_custom_derivation(target: Target, project: Project) -> str:
    """A pass-through derivation for user-supplied build/install scripts."""
    build_inputs = _build_inputs(target, project)
    native = [f"pkgs.{p}" for p in target.native_build_inputs]

    lines = [
        "pkgs.stdenv.mkDerivation {",
        f"  pname = {_nix_str(target.name)};",
        f"  version = {_nix_str(project.version)};",
        "  inherit src;",
        f"  buildInputs = {_nix_list(build_inputs)};",
    ]
    if native:
        lines.append(f"  nativeBuildInputs = {_nix_list(native)};")
    lines.append("  dontConfigure = true;")

    if target.build_script:
        body = target.build_script.strip("\n")
        lines.append("  buildPhase = ''")
        lines.append("    runHook preBuild")
        lines.append(_indent(body, 4))
        lines.append("    runHook postBuild")
        lines.append("  '';")
    else:
        lines.append("  dontBuild = true;")

    install_body = (target.install_script or "").strip("\n")
    lines.append("  installPhase = ''")
    lines.append("    runHook preInstall")
    lines.append(_indent(install_body, 4))
    lines.append("    runHook postInstall")
    lines.append("  '';")
    lines.append("}")
    return "\n".join(lines)


def _mk_derivation(
    target: Target, variant: Variant | None, project: Project
) -> str:
    if target.kind == "python_script":
        # variants on python_script are ignored for now (no compile step to differ).
        return _mk_python_derivation(target, project)
    if target.kind == "custom":
        return _mk_custom_derivation(target, project)

    pname = target.name if variant is None else f"{target.name}-{variant.name}"
    build_inputs = _build_inputs(target, project)

    if target.kind == "executable":
        build_phase = _build_phase_executable(target, variant, project)
        install_phase = _install_phase_executable(target)
    elif target.kind == "static_library":
        build_phase = _build_phase_static_library(target, variant, project)
        install_phase = _install_phase_static_library(target)
    elif target.kind == "nim_executable":
        build_phase = _build_phase_nim_executable(target, variant, project)
        install_phase = _install_phase_nim_executable(target)
    elif target.kind == "run":
        build_phase = _build_phase_run(target, project)
        install_phase = _install_phase_run(target)
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
    w(f"  description = {_nix_str(f'rigx build for {project.name}')};")
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
    w('            ".rigx" "output" ".git" "result"')
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
        # `script` targets are host-side tasks; they don't become Nix derivations.
        if target.kind == "script":
            continue
        block = _target_block(target, project)
        w(_indent(block, 10))

    w("        });")
    w("    };")
    w("}")
    return "\n".join(out) + "\n"
