"""Top-level flake assembly: dependency wiring, install phases, dispatch."""

from __future__ import annotations

from rigx import sources
from rigx.config import GitDep, Project, Target, Variant
from rigx.nix import c_family, go, nim, python, rust, zig
from rigx.nix.capsule_lite import mk_lite_capsule_derivation
from rigx.nix.capsule_nixos import mk_nixos_capsule_derivation
from rigx.nix.capsule_qemu import mk_qemu_capsule_derivation
from rigx.nix.cross import stdenv_attr, toolchain_pkgs
from rigx.nix.render import (
    _external_bucket_var,
    indent,
    nix_id,
    nix_list,
    nix_str,
    rewrite_interp,
    sh_join,
    shell_quote,
)
from rigx.nix.tests import mk_test_derivation


def git_input_url(dep: GitDep) -> str:
    # Nix flake URL. If rev looks like a commit sha, use rev=; otherwise ref=.
    is_sha = len(dep.rev) == 40 and all(c in "0123456789abcdef" for c in dep.rev.lower())
    qual = f"rev={dep.rev}" if is_sha else f"ref={dep.rev}"
    return f"git+{dep.url}?{qual}"


def is_cross_flake_ref(d: str, project: Project) -> bool:
    """True if `d` is a `<localdep>.<target>` reference into a sibling flake."""
    if "." not in d:
        return False
    head = d.split(".", 1)[0]
    return head in project.local_deps


def build_inputs(target: Target, project: Project) -> list[str]:
    exprs: list[str] = []
    for d in target.deps.nixpkgs:
        exprs.append(f"pkgs.{d}")
    for d in target.deps.git:
        dep = project.git_deps[d]
        attr = dep.attr or "default"
        exprs.append(f"inputs.{d}.packages.${{system}}.{attr}")
    for d in target.deps.internal:
        exprs.append(nix_id(d))
    # External inputs land as path-typed buildInputs so any tool that walks
    # the input chain (e.g. `nix-store --query --references`) sees them.
    # The substitution machinery (`${name.<bucket>}`) is what actually
    # drops paths into the build command line.
    for d in target.deps.external:
        ext = project.external_inputs.get(d)
        if ext is None:
            continue
        for bname in ext.buckets:
            exprs.append(_external_bucket_var(d, bname))
    if (
        target.run
        and (target.run in project.targets or is_cross_flake_ref(target.run, project))
        and target.run not in target.deps.internal
    ):
        exprs.append(nix_id(target.run))
    return exprs


def internal_link_args(target: Target, project: Project) -> list[str]:
    """Positional linker args for internal static-library deps. Cross-flake
    refs are skipped — the parent has no metadata about the dep's kind."""
    args: list[str] = []
    for d in target.deps.internal:
        if is_cross_flake_ref(d, project):
            continue
        dep_target = project.targets[d]
        if dep_target.kind == "static_library":
            args.append(f"${{{nix_id(d)}}}/lib/lib{dep_target.name}.a")
    return args


def internal_include_args(target: Target, project: Project) -> list[str]:
    """Include flags for same-project internal deps. Cross-flake refs are
    skipped (see `internal_link_args`)."""
    args: list[str] = []
    for d in target.deps.internal:
        if is_cross_flake_ref(d, project):
            continue
        args.append(f"-I${{{nix_id(d)}}}/include")
    return args


def install_phase_executable(target: Target) -> str:
    return (
        "runHook preInstall\n"
        "mkdir -p $out/bin\n"
        f"cp {target.name} $out/bin/\n"
        "runHook postInstall\n"
    )


def install_phase_shared_library(target: Target) -> str:
    lines = [
        "runHook preInstall",
        "mkdir -p $out/lib $out/include",
        f"cp lib{target.name}.so $out/lib/",
    ]
    for ph in target.public_headers:
        lines.append(f"cp -r {ph}/. $out/include/")
    lines.append("runHook postInstall")
    return "\n".join(lines) + "\n"


def install_phase_static_library(target: Target) -> str:
    lines = [
        "runHook preInstall",
        "mkdir -p $out/lib $out/include",
        f"cp lib{target.name}.a $out/lib/",
    ]
    for ph in target.public_headers:
        lines.append(f"cp -r {ph}/. $out/include/")
    lines.append("runHook postInstall")
    return "\n".join(lines) + "\n"


def build_phase_run(target: Target, project: Project) -> str:
    assert target.run is not None
    if target.run in project.targets:
        dep_target = project.targets[target.run]
        binary = f"${{{nix_id(target.run)}}}/bin/{dep_target.name}"
    elif is_cross_flake_ref(target.run, project):
        nix_ref = nix_id(target.run)
        bin_name = target.run.rsplit(".", 1)[1]
        binary = f"${{{nix_ref}}}/bin/{bin_name}"
    else:
        binary = target.run
    quoted_args = " ".join(
        shell_quote(rewrite_interp(a, project)) for a in target.args
    )
    cmd = f"{binary} {quoted_args}".rstrip()
    return (
        "runHook preBuild\n"
        f"{cmd}\n"
        "runHook postBuild\n"
    )


def install_phase_run(target: Target) -> str:
    lines = ["runHook preInstall", "mkdir -p $out"]
    for out in target.outputs:
        parent = "/".join(out.split("/")[:-1])
        if parent:
            lines.append(f"mkdir -p $out/{parent}")
        lines.append(f"cp -r {out} $out/{out}")
    lines.append("runHook postInstall")
    return "\n".join(lines) + "\n"


def mk_custom_derivation(target: Target, project: Project) -> str:
    """Pass-through derivation for user-supplied build/install scripts."""
    inputs = build_inputs(target, project)
    native = [f"pkgs.{p}" for p in target.native_build_inputs]

    lines = [
        "pkgs.stdenv.mkDerivation {",
        f"  pname = {nix_str(target.qualified_name)};",
        f"  version = {nix_str(project.version)};",
        "  inherit src;",
        f"  buildInputs = {nix_list(inputs)};",
    ]
    if native:
        lines.append(f"  nativeBuildInputs = {nix_list(native)};")
    lines.append("  dontConfigure = true;")

    if target.build_script:
        body = rewrite_interp(target.build_script.strip("\n"), project)
        lines.append("  buildPhase = ''")
        lines.append("    runHook preBuild")
        lines.append(indent(body, 4))
        lines.append("    runHook postBuild")
        lines.append("  '';")
    else:
        lines.append("  dontBuild = true;")

    install_body = rewrite_interp((target.install_script or "").strip("\n"), project)
    lines.append("  installPhase = ''")
    lines.append("    runHook preInstall")
    lines.append(indent(install_body, 4))
    lines.append("    runHook postInstall")
    lines.append("  '';")
    lines.append("}")
    return "\n".join(lines)


def _external_input_bindings(project: Project) -> list[str]:
    """Emit one `let`-binding per declared `[external_inputs.<name>]`
    bucket. The binding is a `builtins.path { ... }` that copies the
    host-side directory into the Nix store at eval time — sandboxed
    builds see only the in-store copy. When the bucket has a `sha256`
    entry, Nix verifies the content hash and fails loudly on drift;
    otherwise it hashes whatever it finds (the loader already WARNed)."""
    lines: list[str] = []
    for name, ext in project.external_inputs.items():
        for bucket in ext.buckets:
            host_path = ext.bucket_paths.get(bucket, "")
            if not host_path:
                continue
            var = _external_bucket_var(name, bucket)
            attrs = [
                f"path = {nix_str(host_path)};",
                f"name = {nix_str(f'{name}-{bucket}')};",
            ]
            bucket_sha = ext.sha256.get(bucket, "")
            if bucket_sha:
                attrs.append(f"sha256 = {nix_str(bucket_sha)};")
            joined = " ".join(attrs)
            lines.append(f"{var} = builtins.path {{ {joined} }};")
    return lines


def mk_generated_source_derivation(target: Target, project: Project) -> str:
    """Pass-through derivation that runs a user command to produce files.

    The command sees `$inputs` (space-joined input paths, relative to the
    derivation's `$src`) and `$out` (the output store directory the command
    should populate). Internal/external deps interpolate via `${dep}` /
    `${dep.bucket}`. After the command runs, every entry in `outputs` is
    verified to exist under `$out` — a missing one fails the build with a
    clear pointer at what wasn't produced."""
    if not target.command:
        raise ValueError(
            f"generated_source {target.qualified_name!r}: missing command"
        )
    inputs_exprs = build_inputs(target, project)
    native = [f"pkgs.{p}" for p in target.native_build_inputs]
    # Build the inputs assignment as a single bash-double-quoted string so
    # `$inputs` (unquoted) word-splits across the input paths inside the
    # user's command. Paths with spaces aren't supported here — every other
    # `sources`-style field in rigx makes the same assumption.
    quoted_inputs = " ".join(target.inputs)
    cmd_body = rewrite_interp(target.command.strip("\n"), project)

    lines = [
        "pkgs.stdenv.mkDerivation {",
        f"  pname = {nix_str(target.qualified_name)};",
        f"  version = {nix_str(project.version)};",
        "  inherit src;",
        f"  buildInputs = {nix_list(inputs_exprs)};",
    ]
    if native:
        lines.append(f"  nativeBuildInputs = {nix_list(native)};")
    lines.extend([
        "  dontConfigure = true;",
        "  dontBuild = true;",
        "  installPhase = ''",
        "    runHook preInstall",
        "    mkdir -p $out",
        f'    inputs="{quoted_inputs}"',
        f"    {cmd_body}",
    ])
    for out in target.outputs:
        lines.append(
            f"    test -e $out/{out} || "
            f"{{ echo \"generated_source {target.qualified_name}: "
            f"command did not produce expected output {out!r}\" >&2; "
            f"exit 1; }}"
        )
    lines.extend([
        "    runHook postInstall",
        "  '';",
        "}",
    ])
    return "\n".join(lines)


def mk_capsule_derivation(target: Target, project: Project) -> str:
    """Dispatch a `kind = "capsule"` target to its backend-specific builder."""
    if target.backend == "lite":
        return mk_lite_capsule_derivation(target, project)
    if target.backend == "nixos":
        return mk_nixos_capsule_derivation(target, project)
    if target.backend == "qemu":
        return mk_qemu_capsule_derivation(target, project)
    raise ValueError(
        f"capsule {target.qualified_name!r}: unknown backend "
        f"{target.backend!r} (supported: 'lite', 'nixos', 'qemu')"
    )


def mk_derivation(
    target: Target, variant: Variant | None, project: Project
) -> str:
    if target.kind == "python_script":
        return python.mk_python_derivation(target, project)
    if target.kind == "test":
        return mk_test_derivation(target, project)
    if target.kind == "custom":
        return mk_custom_derivation(target, project)
    if target.kind == "capsule":
        return mk_capsule_derivation(target, project)
    if target.kind == "generated_source":
        return mk_generated_source_derivation(target, project)

    pname = (
        target.qualified_name
        if variant is None
        else f"{target.qualified_name}-{variant.name}"
    )
    inputs = build_inputs(target, project)
    extra_native: list[str] = []

    language = target.language or "cxx"

    if target.kind == "executable":
        if language == "cxx":
            build_phase = c_family.build_phase_cxx_executable(target, variant, project)
        elif language == "c":
            build_phase = c_family.build_phase_c_executable(target, variant, project)
        elif language == "go":
            build_phase = go.build_phase_go_executable(target, variant, project)
            extra_native = toolchain_pkgs(target, variant)
        elif language == "rust":
            build_phase = rust.build_phase_rust_executable(target, variant, project)
            extra_native = toolchain_pkgs(target, variant)
        elif language == "zig":
            build_phase = zig.build_phase_zig_executable(target, variant, project)
            extra_native = toolchain_pkgs(target, variant)
        elif language == "nim":
            build_phase = nim.build_phase_nim_executable(target, variant, project)
            extra_native = toolchain_pkgs(target, variant)
        else:
            raise ValueError(
                f"executable {target.name!r}: unsupported language "
                f"{target.language!r}"
            )
        install_phase = install_phase_executable(target)
    elif target.kind == "static_library":
        if language == "cxx":
            build_phase = c_family.build_phase_cxx_static_library(target, variant, project)
        elif language == "c":
            build_phase = c_family.build_phase_c_static_library(target, variant, project)
        elif language == "rust":
            build_phase = rust.build_phase_rust_static_library(target, variant, project)
            extra_native = toolchain_pkgs(target, variant)
        else:
            raise ValueError(
                f"static_library {target.name!r}: unsupported language "
                f"{target.language!r}"
            )
        install_phase = install_phase_static_library(target)
    elif target.kind == "shared_library":
        if language == "cxx":
            build_phase = c_family.build_phase_cxx_shared_library(target, variant, project)
        elif language == "c":
            build_phase = c_family.build_phase_c_shared_library(target, variant, project)
        elif language == "rust":
            build_phase = rust.build_phase_rust_shared_library(target, variant, project)
            extra_native = toolchain_pkgs(target, variant)
        else:
            raise ValueError(
                f"shared_library {target.name!r}: unsupported language "
                f"{target.language!r}"
            )
        install_phase = install_phase_shared_library(target)
    elif target.kind == "run":
        build_phase = build_phase_run(target, project)
        install_phase = install_phase_run(target)
    else:
        raise ValueError(f"unknown kind {target.kind!r}")

    sa = stdenv_attr(target, variant)
    native_inputs = [f"pkgs.{n}" for n in extra_native]

    lines = [
        f"pkgs.{sa}.mkDerivation {{",
        f"  pname = {nix_str(pname)};",
        f"  version = {nix_str(project.version)};",
        "  inherit src;",
        f"  buildInputs = {nix_list(inputs)};",
    ]
    if native_inputs:
        lines.append(f"  nativeBuildInputs = {nix_list(native_inputs)};")
    # Rewrite dotted interpolations (`${local-dep.t}`, `${ext-input.lib}`,
    # `${module.t}`) into the underscore-form rec attrs the surrounding
    # `rec { ... }` actually binds. Applied to the assembled phase string
    # so per-language builders don't each have to remember to do it.
    build_phase = rewrite_interp(build_phase, project)
    install_phase = rewrite_interp(install_phase, project)
    lines.extend([
        "  dontConfigure = true;",
        "  buildPhase = ''",
        indent(build_phase, 4).rstrip() + "\n",
        "  '';",
        "  installPhase = ''",
        indent(install_phase, 4).rstrip() + "\n",
        "  '';",
        "}",
    ])
    return "\n".join(lines)


def flake_attrs(project: Project) -> list[str]:
    """All attribute names this project's flake exposes under
    `packages.${system}`. Used by a parent flake to enumerate re-exports of a
    local-dep (`<lname>.<attr>` for each `<attr>` here)."""
    out: list[str] = []
    for tname, target in project.targets.items():
        if target.kind in ("script", "testbed"):
            continue
        if target.kind == "test" and not target.sandbox:
            continue
        attr_base = nix_id(tname)
        if not target.variants:
            out.append(attr_base)
        else:
            for vname in sorted(target.variants.keys()):
                out.append(f"{attr_base}-{vname}")
            out.append(attr_base)
    for lname, ldep in project.local_deps.items():
        if not ldep.sub_project:
            continue
        for sub_attr in flake_attrs(ldep.sub_project):
            out.append(nix_id(f"{lname}_{sub_attr}"))
    return out


def local_dep_url(project: Project, ldep) -> str:
    """Produce a Nix path: URL for a local-dep, preferring a path relative to
    the parent root (cleaner lockfile, portable across checkouts)."""
    parent_root = project.root.resolve()
    try:
        rel = ldep.path.relative_to(parent_root)
        return f"path:./{rel.as_posix()}"
    except ValueError:
        return f"path:{ldep.path}"


def _emitted_targets(project: Project) -> list[Target]:
    """Targets that appear under `packages.${system}` and therefore need a
    `src` binding. Mirrors the filtering done in `generate()`."""
    out: list[Target] = []
    for target in project.targets.values():
        if target.kind in ("script", "testbed"):
            continue
        if target.kind == "test" and not target.sandbox:
            continue
        out.append(target)
    return out


def collect_src_bindings(
    project: Project,
) -> tuple[list[tuple[str, str, list[str]]], dict[str, str]]:
    """Plan the `src` derivations for the outer let-binding.

    Returns `(bindings, target_ref)`:
      * `bindings` is a list of `(nix_var, display_name, rels)` tuples to
        emit as `nix_var = mkSrc "display_name" [ ... ];` in the outer
        let. Always starts with the project baseline (`projectSrc`); a
        per-target binding is added only when the target's file set
        differs from the baseline.
      * `target_ref` maps each emitted target's qualified name to the
        nix variable its `src` should resolve to. Targets that match the
        baseline map to `"projectSrc"` so they share one derivation.

    Both are empty when `[project].sources` filtering is disabled —
    callers fall back to the legacy `srcRoot` path.
    """
    if not sources.project_filtering_enabled(project):
        return [], {}

    base = sources.compute_project_files(project)
    base_set = set(base)
    target_ref: dict[str, str] = {}
    per_target: list[tuple[str, str, list[str]]] = []
    baseline_used = False

    for target in _emitted_targets(project):
        files = sources.compute_target_files(project, target)
        if set(files) == base_set:
            target_ref[target.qualified_name] = "projectSrc"
            baseline_used = True
            continue
        rels = sorted(set(files) | set(sources.ancestor_dirs(files)))
        var = "srcs__" + nix_id(target.qualified_name)
        per_target.append((var, target.qualified_name, rels))
        target_ref[target.qualified_name] = var

    bindings: list[tuple[str, str, list[str]]] = []
    if baseline_used:
        base_rels = sorted(base_set | set(sources.ancestor_dirs(base)))
        bindings.append(("projectSrc", "project", base_rels))
    bindings.extend(per_target)
    return bindings, target_ref


def target_block(
    target: Target,
    project: Project,
    src_ref: str = "",
) -> str:
    """Render a target's attrset entries. `src_ref` is the outer-let
    variable that this target's `src` should bind to (e.g. `"projectSrc"`
    or `"srcs__exe"`); empty when source-filtering is disabled, in which
    case `src` comes from the enclosing `packages.${system}` let."""
    attr_base = nix_id(target.qualified_name)
    src_let = f"let src = {src_ref}; in " if src_ref else ""
    if not target.variants:
        body = mk_derivation(target, None, project)
        return f"{attr_base} = {src_let}{body};"

    lines: list[str] = []
    first_variant = sorted(target.variants.keys())[0]
    for vname in sorted(target.variants.keys()):
        variant = target.variants[vname]
        attr = f"{attr_base}-{vname}"
        body = mk_derivation(target, variant, project)
        lines.append(f"{attr} = {src_let}{body};")
    lines.append(f"{attr_base} = {attr_base}-{first_variant};")
    return "\n".join(lines)


def generate(project: Project) -> str:
    """Return the contents of flake.nix for the given project."""
    out: list[str] = []
    w = out.append

    w("{")
    desc = project.description or f"rigx build for {project.name}"
    w(f"  description = {nix_str(desc)};")
    w("")
    w("  inputs = {")
    w(f"    nixpkgs.url = {nix_str(f'github:NixOS/nixpkgs/{project.nixpkgs_ref}')};")
    for name, dep in project.git_deps.items():
        w(f"    {name}.url = {nix_str(git_input_url(dep))};")
        w(f"    {name}.flake = {'true' if dep.flake else 'false'};")
    for lname, ldep in project.local_deps.items():
        w(f"    {lname}.url = {nix_str(local_dep_url(project, ldep))};")
        w(f"    {lname}.flake = {'true' if ldep.flake else 'false'};")
    w("  };")
    w("")
    w("  outputs = { self, nixpkgs, ... }@inputs:")
    w("    let")
    w('      systems = [ "x86_64-linux" "aarch64-linux" "x86_64-darwin" "aarch64-darwin" ];')
    w("      forAll = f: nixpkgs.lib.genAttrs systems f;")
    filtering_enabled = sources.project_filtering_enabled(project)
    src_bindings, target_ref = collect_src_bindings(project)
    if filtering_enabled:
        w("      mkSrc = name: rels:")
        w("        let")
        w("          allowed = builtins.listToAttrs (")
        w("            map (p: { name = p; value = true; }) rels")
        w("          );")
        w("          rootStr = toString ./.;")
        w("          prefix = rootStr + \"/\";")
        w("          prefixLen = builtins.stringLength prefix;")
        w("        in builtins.path {")
        w("          path = ./.;")
        w('          name = "rigx-src-" + name;')
        w("          filter = path: type:")
        w("            let")
        w("              ps = toString path;")
        w("              rel = if ps == rootStr")
        w("                    then \"\"")
        w("                    else builtins.substring prefixLen (-1) ps;")
        w("            in rel == \"\" || builtins.hasAttr rel allowed;")
        w("        };")
        for var, display, rels in src_bindings:
            quoted = " ".join(nix_str(r) for r in rels)
            w(f"      {var} = mkSrc {nix_str(display)} [ {quoted} ];")
    else:
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
    for line in _external_input_bindings(project):
        w(f"      {line}")
    w("    in {")
    w("      packages = forAll (system:")
    w("        let")
    w("          pkgs = import nixpkgs { inherit system; };")
    if not filtering_enabled:
        w("          src = srcRoot;")
    w("        in rec {")

    for tname, target in project.targets.items():
        if target.kind in ("script", "testbed"):
            continue
        if target.kind == "test" and not target.sandbox:
            continue
        block = target_block(target, project, target_ref.get(target.qualified_name, ""))
        w(indent(block, 10))

    for lname, ldep in project.local_deps.items():
        if not ldep.sub_project:
            continue
        for sub_attr in flake_attrs(ldep.sub_project):
            parent_attr = nix_id(f"{lname}_{sub_attr}")
            w(indent(
                f"{parent_attr} = inputs.{lname}.packages.${{system}}.{sub_attr};",
                10,
            ))

    w("        });")
    w("    };")
    w("}")
    return "\n".join(out) + "\n"
