"""Generate a Nix flake from a rigx Project."""

from __future__ import annotations

import re

from rigx.config import GitDep, Project, Target, Variant, canonicalize_qualified


def _nix_str(s: str) -> str:
    """Quote a Python string for Nix (double-quoted form)."""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$") + '"'


def _nix_id(qualified: str) -> str:
    """Map a possibly-dotted name ('frontend.app') to a Nix identifier
    ('frontend_app'). Used for `let` bindings and `rec` attrs — Nix accepts
    dots only in quoted attr keys, and they break `${name}` interpolation.

    Hyphens stay verbatim: they're valid Nix identifier characters
    (`hello-debug`, `actarus-test-runner` are legal `rec` attrs and
    `nix build .#…` accepts them)."""
    return qualified.replace(".", "_")


# Matches `${X.Y}` (and `${X.Y.Z}` etc.) where each segment is identifier-ish
# (hyphens allowed, since `lib-release` is a valid variant attr). Used to
# rewrite cross-flake interpolations in args and user scripts so
# `${frontend.app}` → `${frontend_app}`.
_INTERP_DOTTED = re.compile(
    r"\$\{([A-Za-z_][A-Za-z0-9_\-]*(?:\.[A-Za-z_][A-Za-z0-9_\-]*)+)\}"
)


def _rewrite_interp(s: str, project: Project) -> str:
    """Rewrite `${X.Y}` to `${X_Y}` when the dotted ref names either:
    - a cross-flake (`[dependencies.local.X]`) target, or
    - a `[modules]`-merged target (`X.Y` is a key in `project.targets`).
    Other dotted forms (e.g. `pkgs.foo`) are left alone — those are Nix
    attribute paths the user wrote intentionally."""
    def sub(m):
        qual = m.group(1)
        if qual.split(".", 1)[0] in project.local_deps:
            return "${" + _nix_id(qual) + "}"
        if qual in project.targets:
            return "${" + _nix_id(qual) + "}"
        return m.group(0)
    return _INTERP_DOTTED.sub(sub, s)


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


def _effective_cflags(target: Target, variant: Variant | None) -> list[str]:
    flags = list(target.cflags)
    defines = dict(target.defines)
    if variant:
        flags += variant.cflags
        defines.update(variant.defines)
    for k, v in defines.items():
        flags.append(f"-D{k}={v}" if v else f"-D{k}")
    return flags


def _effective_ldflags(target: Target, variant: Variant | None) -> list[str]:
    flags = list(target.ldflags)
    if variant:
        flags += variant.ldflags
    return flags


def _effective_goflags(target: Target, variant: Variant | None) -> list[str]:
    flags = list(target.goflags)
    if variant:
        flags += variant.goflags
    return flags


def _effective_rustflags(target: Target, variant: Variant | None) -> list[str]:
    flags = list(target.rustflags)
    if variant:
        flags += variant.rustflags
    return flags


def _effective_zigflags(target: Target, variant: Variant | None) -> list[str]:
    flags = list(target.zigflags)
    if variant:
        flags += variant.zigflags
    return flags


def _effective_compiler(target: Target, variant: Variant | None) -> str:
    if variant and variant.compiler:
        return variant.compiler
    return target.compiler


def _stdenv_attr(target: Target, variant: Variant | None) -> str:
    """Pick the nixpkgs stdenv attr for a c/cxx target.

    With a `target = …` set, route through `pkgsCross.<x>.stdenv` (or
    `pkgsCross.<x>.<compiler>Stdenv` for a non-default compiler). Otherwise:
    `compiler = "clang"` → `clangStdenv`; `"gcc13"` → `gcc13Stdenv`; empty
    / non-c/cxx returns the default `stdenv`."""
    triple = _effective_target(target, variant)
    if target.language in ("c", "cxx") and triple:
        info = _cross_info(triple)
        cross_attr = info.get("pkgsCross", triple)
        comp = _effective_compiler(target, variant)
        sub = "stdenv" if (not comp or comp == "gcc") else f"{comp}Stdenv"
        return f"pkgsCross.{cross_attr}.{sub}"
    if target.language not in ("c", "cxx"):
        return "stdenv"
    comp = _effective_compiler(target, variant)
    if not comp or comp == "gcc":
        return "stdenv"
    return f"{comp}Stdenv"


def _toolchain_pkgs(target: Target, variant: Variant | None) -> list[str]:
    """nixpkgs attrs auto-added to nativeBuildInputs for go/rust/zig/nim
    targets. C/cxx come from the stdenv directly, so this is empty.

    Cross-compiling Nim adds `zig` (used as the C compiler shim — see
    `_build_phase_nim_executable`)."""
    if target.language not in DEFAULT_COMPILER_FOR_LANG:
        return []
    comp = _effective_compiler(target, variant) or DEFAULT_COMPILER_FOR_LANG[target.language]
    pkgs = [comp]
    if target.language == "nim" and _effective_target(target, variant):
        pkgs.append("zig")
    return pkgs


# Mirrors `config.DEFAULT_COMPILER` but kept here so nix_gen doesn't have to
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


def _cross_info(triple: str) -> dict[str, str]:
    """Resolve a `target = …` value to the per-backend cross details. The
    raw string is also accepted as a fallback (caller is on their own to
    make sure the underlying tools recognize it)."""
    if not triple:
        return {}
    if triple in CROSS_TARGETS:
        return CROSS_TARGETS[triple]
    return {"pkgsCross": triple, "zig_triple": triple}


def _effective_target(target: Target, variant: Variant | None) -> str:
    if variant and variant.target:
        return variant.target
    return target.target


def _is_cross_flake_ref(d: str, project: Project) -> bool:
    """True if `d` is a `<localdep>.<target>` reference into a sibling flake."""
    if "." not in d:
        return False
    head = d.split(".", 1)[0]
    return head in project.local_deps


def _build_inputs(target: Target, project: Project) -> list[str]:
    exprs: list[str] = []
    for d in target.deps.nixpkgs:
        exprs.append(f"pkgs.{d}")
    for d in target.deps.git:
        dep = project.git_deps[d]
        attr = dep.attr or "default"
        exprs.append(f"inputs.{d}.packages.${{system}}.{attr}")
    for d in target.deps.internal:
        # Cross-flake refs and same-project refs both render as a single Nix
        # identifier — both end up in the parent's `rec` block (own targets
        # directly; cross-flake targets via re-export bindings).
        exprs.append(_nix_id(d))
    if target.run:
        run_canonical = canonicalize_qualified(target.run)
        if (
            (run_canonical in project.targets
             or _is_cross_flake_ref(run_canonical, project))
            and run_canonical not in target.deps.internal
        ):
            exprs.append(_nix_id(run_canonical))
    return exprs


def _internal_link_args(target: Target, project: Project) -> list[str]:
    """Positional linker args for internal static-library deps. Cross-flake
    refs are skipped — the parent has no metadata about the dep's kind, so it
    can't synthesize the `${dep}/lib/lib<dep>.a` line. Users wanting to link
    against a sibling-flake static_library should add a `custom` target that
    consumes the artifact explicitly.

    For B-merged deps (`frontend.greet`), the rec attr is sanitized
    (`frontend_greet`), but the actual library file uses the dep's *raw*
    target name (`libgreet.a`)."""
    args: list[str] = []
    for d in target.deps.internal:
        if _is_cross_flake_ref(d, project):
            continue
        dep_target = project.targets[d]
        if dep_target.kind == "static_library":
            args.append(f"${{{_nix_id(d)}}}/lib/lib{dep_target.name}.a")
    return args


def _internal_include_args(target: Target, project: Project) -> list[str]:
    """Include flags for same-project internal deps. Cross-flake refs are
    skipped (see `_internal_link_args`)."""
    args: list[str] = []
    for d in target.deps.internal:
        if _is_cross_flake_ref(d, project):
            continue
        args.append(f"-I${{{_nix_id(d)}}}/include")
    return args


def _build_phase_cxx_executable(
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


def _build_phase_c_executable(
    target: Target, variant: Variant | None, project: Project
) -> str:
    cflags = _effective_cflags(target, variant)
    ldflags = _effective_ldflags(target, variant)
    includes = [f"-I{i}" for i in target.includes]
    internal_includes = _internal_include_args(target, project)
    internal_links = _internal_link_args(target, project)

    parts = ["$CC"]
    parts += cflags
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


def _build_phase_go_executable(
    target: Target, variant: Variant | None, project: Project
) -> str:
    flags = _effective_goflags(target, variant)
    parts = ["go", "build"]
    parts += flags
    parts += ["-o", target.name]
    parts += target.sources
    cmd = " ".join(parts)
    triple = _effective_target(target, variant)
    cross_env = ""
    if triple:
        info = _cross_info(triple)
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


def _build_phase_rust_executable(
    target: Target, variant: Variant | None, project: Project
) -> str:
    if not target.sources:
        raise ValueError(f"rust executable {target.name!r} needs at least one source")
    entry = target.sources[0]
    flags = _effective_rustflags(target, variant)
    parts = ["rustc"]
    parts += flags
    parts += ["-o", target.name, entry]
    cmd = " ".join(parts)
    return (
        "runHook preBuild\n"
        "export HOME=$TMPDIR\n"
        f"{cmd}\n"
        "runHook postBuild\n"
    )


def _build_phase_zig_executable(
    target: Target, variant: Variant | None, project: Project
) -> str:
    if not target.sources:
        raise ValueError(f"zig executable {target.name!r} needs at least one source")
    entry = target.sources[0]
    flags = _effective_zigflags(target, variant)
    triple = _effective_target(target, variant)
    parts = ["zig", "build-exe"]
    if triple:
        parts += ["-target", _cross_info(triple).get("zig_triple", triple)]
    parts += flags
    parts += [f"-femit-bin={target.name}", entry]
    cmd = " ".join(parts)
    return (
        "runHook preBuild\n"
        "export HOME=$TMPDIR\n"
        # Zig caches under $XDG_CACHE_HOME or $HOME/.cache; ensure a writable
        # home is enough.
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


def _build_phase_cxx_static_library(
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


def _build_phase_c_static_library(
    target: Target, variant: Variant | None, project: Project
) -> str:
    cflags = _effective_cflags(target, variant)
    includes = [f"-I{i}" for i in target.includes]
    internal_includes = _internal_include_args(target, project)

    lines = ["runHook preBuild"]
    obj_files: list[str] = []
    for src in target.sources:
        obj = _obj_name(src)
        obj_files.append(obj)
        parts = ["$CC"]
        parts += cflags
        parts += ["-fPIC"]
        parts += includes
        parts += internal_includes
        parts += ["-c", src, "-o", obj]
        lines.append(" ".join(parts))
    lines.append(f"$AR rcs lib{target.name}.a {' '.join(obj_files)}")
    lines.append("runHook postBuild")
    return "\n".join(lines) + "\n"


def _build_phase_cxx_shared_library(
    target: Target, variant: Variant | None, project: Project
) -> str:
    cxxflags = _effective_cxxflags(target, variant)
    ldflags = _effective_ldflags(target, variant)
    includes = [f"-I{i}" for i in target.includes]
    internal_includes = _internal_include_args(target, project)
    parts = ["$CXX", "-shared", "-fPIC"]
    parts += cxxflags
    parts += includes
    parts += internal_includes
    parts += target.sources
    parts += ldflags
    parts += ["-o", f"lib{target.name}.so"]
    return (
        "runHook preBuild\n"
        f"{' '.join(parts)}\n"
        "runHook postBuild\n"
    )


def _build_phase_c_shared_library(
    target: Target, variant: Variant | None, project: Project
) -> str:
    cflags = _effective_cflags(target, variant)
    ldflags = _effective_ldflags(target, variant)
    includes = [f"-I{i}" for i in target.includes]
    internal_includes = _internal_include_args(target, project)
    parts = ["$CC", "-shared", "-fPIC"]
    parts += cflags
    parts += includes
    parts += internal_includes
    parts += target.sources
    parts += ldflags
    parts += ["-o", f"lib{target.name}.so"]
    return (
        "runHook preBuild\n"
        f"{' '.join(parts)}\n"
        "runHook postBuild\n"
    )


def _build_phase_rust_shared_library(
    target: Target, variant: Variant | None, project: Project
) -> str:
    if not target.sources:
        raise ValueError(f"rust shared_library {target.name!r} needs at least one source")
    entry = target.sources[0]
    flags = _effective_rustflags(target, variant)
    parts = ["rustc", "--crate-type=cdylib", f"--crate-name={target.name}"]
    parts += flags
    parts += ["-o", f"lib{target.name}.so", entry]
    return (
        "runHook preBuild\n"
        "export HOME=$TMPDIR\n"
        f"{' '.join(parts)}\n"
        "runHook postBuild\n"
    )


def _install_phase_shared_library(target: Target) -> str:
    lines = [
        "runHook preInstall",
        "mkdir -p $out/lib $out/include",
        f"cp lib{target.name}.so $out/lib/",
    ]
    for ph in target.public_headers:
        lines.append(f"cp -r {ph}/. $out/include/")
    lines.append("runHook postInstall")
    return "\n".join(lines) + "\n"


def _build_phase_rust_static_library(
    target: Target, variant: Variant | None, project: Project
) -> str:
    if not target.sources:
        raise ValueError(f"rust static_library {target.name!r} needs at least one source")
    entry = target.sources[0]
    flags = _effective_rustflags(target, variant)
    parts = ["rustc", "--crate-type=staticlib", f"--crate-name={target.name}"]
    parts += flags
    parts += ["-o", f"lib{target.name}.a", entry]
    cmd = " ".join(parts)
    return (
        "runHook preBuild\n"
        "export HOME=$TMPDIR\n"
        f"{cmd}\n"
        "runHook postBuild\n"
    )


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
        raise ValueError(f"nim executable {target.name!r} needs at least one source")
    entry = target.sources[0]
    flags = _effective_nim_flags(target, variant)
    triple = _effective_target(target, variant)

    parts = ["nim", "c"]
    if triple:
        # Auto-emit a zigcc shim and point Nim at it. The shim wraps
        # `zig cc -target <zig_triple>` so Nim's clang invocation produces
        # cross-compiled output. Recipe per the nim_zigcc guide.
        info = _cross_info(triple)
        nim_cpu = info.get("nim_cpu") or info.get("zig_triple", "").split("-")[0]
        nim_os = info.get("nim_os") or info.get("zig_triple", "").split("-")[1]
        parts += [
            "--cc:clang",
            "--clang.exe:$TMPDIR/bin/zigcc",
            "--clang.linkerexe:$TMPDIR/bin/zigcc",
            f"--cpu:{nim_cpu}",
            f"--os:{nim_os}",
        ]
    parts += flags
    parts += ["--nimcache:./nimcache", f"--out:{target.name}", entry]
    cmd = " ".join(parts)

    shim = ""
    if triple:
        zig_target = _cross_info(triple).get("zig_triple", triple)
        # Avoid heredocs here — `_indent` would break their terminator. Two
        # plain echos write the shim with no escaping pitfalls.
        shim = (
            "mkdir -p $TMPDIR/bin\n"
            "echo '#!/usr/bin/env bash' > $TMPDIR/bin/zigcc\n"
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


def _shell_quote(s: str) -> str:
    if not s or any(c in s for c in " \t\n\"'$\\`*?[]{}|&;<>()!#~"):
        return "'" + s.replace("'", "'\\''") + "'"
    return s


def _build_phase_run(target: Target, project: Project) -> str:
    assert target.run is not None
    # Try the canonical (`-` → `_`) form against project.targets first; that
    # catches dash-named refs that match an internal target. If neither
    # canonical nor raw resolves, fall back to a PATH command (where the
    # raw form is preserved — bare commands legitimately have dashes).
    run_canonical = canonicalize_qualified(target.run)
    if run_canonical in project.targets:
        # internal target: absolute path into its store output. For B-merged
        # targets (`frontend.greet`), the rec attr is sanitized but the
        # binary inside is named after the dep's raw target name.
        dep_target = project.targets[run_canonical]
        binary = f"${{{_nix_id(run_canonical)}}}/bin/{dep_target.name}"
    elif _is_cross_flake_ref(run_canonical, project):
        nix_ref = _nix_id(run_canonical)
        bin_name = run_canonical.rsplit(".", 1)[1]
        binary = f"${{{nix_ref}}}/bin/{bin_name}"
    else:
        # bare command name: resolved via PATH from deps.nixpkgs / deps.git
        binary = target.run
    quoted_args = " ".join(
        _shell_quote(_rewrite_interp(a, project)) for a in target.args
    )
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


def _venv_extra_pairs(target: Target) -> list[tuple[str, str]]:
    """Map each `python_venv_extra` entry to (src_in_flake, dest_in_FOD).

    Entries land in `target.python_venv_extra` as project-root-relative paths
    (the loader's expansion already prefixed them with `python_project`).
    The FOD source has `pyproject.toml` at its root, so destinations are
    re-rooted to be python_project-relative."""
    pdir = target.python_project.strip("/")
    pairs: list[tuple[str, str]] = []
    for entry in target.python_venv_extra:
        src = entry
        if pdir in (".", ""):
            dest = entry
        elif entry.startswith(pdir + "/"):
            dest = entry[len(pdir) + 1:]
        elif entry == pdir:
            dest = ""
        else:
            # Path that's not under python_project — shouldn't happen given
            # the loader's path-prefixing, but tolerate it by mirroring the
            # entry verbatim into the FOD.
            dest = entry
        pairs.append((src, dest))
    return pairs


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
    # python_venv_extra: each entry is a project-root-relative path. We copy
    # it into the FOD source at its python_project-relative location so
    # `pyproject.toml` can reach it via the same relative ref the user wrote
    # (e.g. `tool.uv.find-links = ["vendor"]`).
    for src_rel, dest_rel in _venv_extra_pairs(target):
        parent = "/".join(dest_rel.split("/")[:-1])
        if parent:
            lines.append(f"    mkdir -p $out/{parent}")
        lines.append(f"    cp -r ${{./{src_rel}}} $out/{dest_rel}")
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
    lines.append(f"  pname = {_nix_str(target.qualified_name)};")
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
        f"  pname = {_nix_str(target.qualified_name)};",
        f"  version = {_nix_str(project.version)};",
        "  inherit src;",
        f"  buildInputs = {_nix_list(build_inputs)};",
    ]
    if native:
        lines.append(f"  nativeBuildInputs = {_nix_list(native)};")
    lines.append("  dontConfigure = true;")

    if target.build_script:
        body = _rewrite_interp(target.build_script.strip("\n"), project)
        lines.append("  buildPhase = ''")
        lines.append("    runHook preBuild")
        lines.append(_indent(body, 4))
        lines.append("    runHook postBuild")
        lines.append("  '';")
    else:
        lines.append("  dontBuild = true;")

    install_body = _rewrite_interp((target.install_script or "").strip("\n"), project)
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

    pname = (
        target.qualified_name
        if variant is None
        else f"{target.qualified_name}-{variant.name}"
    )
    build_inputs = _build_inputs(target, project)
    extra_native: list[str] = []

    # `language` is normally populated by config._build_target's inference, but
    # tests and direct API users can construct Target without it — default to
    # cxx to match the pre-language behavior.
    language = target.language or "cxx"

    if target.kind == "executable":
        if language == "cxx":
            build_phase = _build_phase_cxx_executable(target, variant, project)
        elif language == "c":
            build_phase = _build_phase_c_executable(target, variant, project)
        elif language == "go":
            build_phase = _build_phase_go_executable(target, variant, project)
            extra_native = _toolchain_pkgs(target, variant)
        elif language == "rust":
            build_phase = _build_phase_rust_executable(target, variant, project)
            extra_native = _toolchain_pkgs(target, variant)
        elif language == "zig":
            build_phase = _build_phase_zig_executable(target, variant, project)
            extra_native = _toolchain_pkgs(target, variant)
        elif language == "nim":
            build_phase = _build_phase_nim_executable(target, variant, project)
            extra_native = _toolchain_pkgs(target, variant)
        else:
            raise ValueError(
                f"executable {target.name!r}: unsupported language "
                f"{target.language!r}"
            )
        install_phase = _install_phase_executable(target)
    elif target.kind == "static_library":
        if language == "cxx":
            build_phase = _build_phase_cxx_static_library(target, variant, project)
        elif language == "c":
            build_phase = _build_phase_c_static_library(target, variant, project)
        elif language == "rust":
            build_phase = _build_phase_rust_static_library(target, variant, project)
            extra_native = _toolchain_pkgs(target, variant)
        else:
            raise ValueError(
                f"static_library {target.name!r}: unsupported language "
                f"{target.language!r}"
            )
        install_phase = _install_phase_static_library(target)
    elif target.kind == "shared_library":
        if language == "cxx":
            build_phase = _build_phase_cxx_shared_library(target, variant, project)
        elif language == "c":
            build_phase = _build_phase_c_shared_library(target, variant, project)
        elif language == "rust":
            build_phase = _build_phase_rust_shared_library(target, variant, project)
            extra_native = _toolchain_pkgs(target, variant)
        else:
            raise ValueError(
                f"shared_library {target.name!r}: unsupported language "
                f"{target.language!r}"
            )
        install_phase = _install_phase_shared_library(target)
    elif target.kind == "run":
        build_phase = _build_phase_run(target, project)
        install_phase = _install_phase_run(target)
    else:
        raise ValueError(f"unknown kind {target.kind!r}")

    stdenv_attr = _stdenv_attr(target, variant)
    native_inputs = [f"pkgs.{n}" for n in extra_native]

    # Nix ''...'' multiline string. Inside it, ${foo} interpolates Nix; bash
    # vars like $CXX are left literal because they're not wrapped in ${}.
    lines = [
        f"pkgs.{stdenv_attr}.mkDerivation {{",
        f"  pname = {_nix_str(pname)};",
        f"  version = {_nix_str(project.version)};",
        "  inherit src;",
        f"  buildInputs = {_nix_list(build_inputs)};",
    ]
    if native_inputs:
        lines.append(f"  nativeBuildInputs = {_nix_list(native_inputs)};")
    lines.extend([
        "  dontConfigure = true;",
        "  buildPhase = ''",
        _indent(build_phase, 4).rstrip() + "\n",
        "  '';",
        "  installPhase = ''",
        _indent(install_phase, 4).rstrip() + "\n",
        "  '';",
        "}",
    ])
    return "\n".join(lines)


def _flake_attrs(project: Project) -> list[str]:
    """All attribute names this project's flake exposes under
    `packages.${system}`. Used by a parent flake to enumerate re-exports of a
    local-dep (`<lname>.<attr>` for each `<attr>` here).

    Variant attrs keep the hyphen (`hello-debug`) since that's what
    `_target_block` emits; B-merged target names are sanitized
    (`frontend_greet`) because dots aren't legal Nix identifier characters."""
    out: list[str] = []
    for tname, target in project.targets.items():
        if target.kind in ("script", "test"):
            continue
        attr_base = _nix_id(tname)
        if not target.variants:
            out.append(attr_base)
        else:
            for vname in sorted(target.variants.keys()):
                out.append(f"{attr_base}-{vname}")
            out.append(attr_base)
    for lname, ldep in project.local_deps.items():
        if not ldep.sub_project:
            continue
        for sub_attr in _flake_attrs(ldep.sub_project):
            out.append(_nix_id(f"{lname}_{sub_attr}"))
    return out


def _local_dep_url(project: Project, ldep) -> str:
    """Produce a Nix path: URL for a local-dep, preferring a path relative to
    the parent root (cleaner lockfile, portable across checkouts)."""
    parent_root = project.root.resolve()
    try:
        rel = ldep.path.relative_to(parent_root)
        return f"path:./{rel.as_posix()}"
    except ValueError:
        return f"path:{ldep.path}"


def _target_block(target: Target, project: Project) -> str:
    # `attr_base` is the rec-attr key. For B-merged targets it's the sanitized
    # qualified name (`frontend_greet`); for parent-owned targets it equals
    # `target.name` since `_nix_id` is a no-op on plain identifiers.
    attr_base = _nix_id(target.qualified_name)
    if not target.variants:
        body = _mk_derivation(target, None, project)
        return f"{attr_base} = {body};"

    lines: list[str] = []
    first_variant = sorted(target.variants.keys())[0]
    for vname in sorted(target.variants.keys()):
        variant = target.variants[vname]
        attr = f"{attr_base}-{vname}"
        body = _mk_derivation(target, variant, project)
        lines.append(f"{attr} = {body};")
    # Alias unqualified name to first variant
    lines.append(f"{attr_base} = {attr_base}-{first_variant};")
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
    for lname, ldep in project.local_deps.items():
        w(f"    {lname}.url = {_nix_str(_local_dep_url(project, ldep))};")
        w(f"    {lname}.flake = {'true' if ldep.flake else 'false'};")
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
        # `script` and `test` targets are host-side tasks; they don't become
        # Nix derivations (they run via `rigx run` / `rigx test`).
        if target.kind in ("script", "test"):
            continue
        block = _target_block(target, project)
        w(_indent(block, 10))

    # Re-export every attr exposed by each local-dep's flake so the parent
    # can `nix build path:.#frontend_app` and so `${frontend.app}` (rewritten
    # to `${frontend_app}`) interpolates inside the parent's `rec` scope.
    for lname, ldep in project.local_deps.items():
        if not ldep.sub_project:
            continue
        for sub_attr in _flake_attrs(ldep.sub_project):
            parent_attr = _nix_id(f"{lname}_{sub_attr}")
            w(_indent(
                f"{parent_attr} = inputs.{lname}.packages.${{system}}.{sub_attr};",
                10,
            ))

    w("        });")
    w("    };")
    w("}")
    return "\n".join(out) + "\n"
