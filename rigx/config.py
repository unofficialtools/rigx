"""Parse rigx.toml into typed dataclasses."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

# `"$vars.<name>"` references in list fields expand to the named entry of
# `[vars]`. Anchored so embedded substrings ("path/$vars.x") don't match —
# expansion is whole-element only.
_VARS_REF = re.compile(r"^\$vars\.([A-Za-z_][A-Za-z0-9_]*)$")

# Source-extension → language. Anchors `executable` / `static_library`'s
# language inference so a `.cpp` source picks the C++ build phase, a `.go`
# source picks the Go path, etc. Mixed extensions (without an explicit
# `language = …`) are rejected at config-load time.
EXT_TO_LANG = {
    ".c":   "c",
    ".cpp": "cxx", ".cxx": "cxx", ".cc": "cxx", ".C": "cxx",
    ".go":  "go",
    ".rs":  "rust",
    ".zig": "zig",
    ".nim": "nim",
}

# Which `kind` accepts which `language`. `executable` works for everything;
# `static_library` is limited to languages with a stable archive convention
# (`.a` / rlib). Go and Zig static libraries exist but the conventions are
# noisier — out of scope for v1.
KIND_LANGUAGES = {
    "executable":     {"c", "cxx", "go", "rust", "zig", "nim"},
    "static_library": {"c", "cxx", "rust"},
    "shared_library": {"c", "cxx", "rust"},
}

# Default toolchain nixpkgs attr per language (used when `compiler` is unset).
# For c/cxx we ride the default `pkgs.stdenv`; for the others we pull the
# compiler explicitly into nativeBuildInputs.
DEFAULT_COMPILER = {
    "go":   "go",
    "rust": "rustc",
    "zig":  "zig",
    "nim":  "nim",
}

VALID_KINDS = {
    "executable",
    "static_library",
    "shared_library",
    "python_script",
    "run",
    "custom",
    "script",
    "test",
}


@dataclass
class GitDep:
    name: str
    url: str
    rev: str = "HEAD"
    flake: bool = True
    attr: str | None = None  # attr path inside the flake, e.g. "packages.default"


@dataclass
class LocalDep:
    """A sibling rigx project pulled in as a path flake input.

    The sub-project is loaded recursively (so its targets can be enumerated
    and re-exported) but is otherwise opaque: the parent depends on its
    *built outputs*, never its raw sources.
    """
    name: str
    path: Path                         # absolute, resolved against parent root
    flake: bool = True
    sub_project: "Project | None" = None  # populated during recursive load


@dataclass
class TargetDeps:
    internal: list[str] = field(default_factory=list)
    nixpkgs: list[str] = field(default_factory=list)
    git: list[str] = field(default_factory=list)


@dataclass
class Variant:
    name: str
    cxxflags: list[str] = field(default_factory=list)
    defines: dict[str, str] = field(default_factory=dict)
    ldflags: list[str] = field(default_factory=list)
    nim_flags: list[str] = field(default_factory=list)
    cflags: list[str] = field(default_factory=list)
    goflags: list[str] = field(default_factory=list)
    rustflags: list[str] = field(default_factory=list)
    zigflags: list[str] = field(default_factory=list)
    compiler: str = ""        # variant-level toolchain override
    target: str = ""          # cross-compilation triple override


@dataclass
class Target:
    name: str
    kind: str
    sources: list[str] = field(default_factory=list)
    includes: list[str] = field(default_factory=list)
    public_headers: list[str] = field(default_factory=list)
    deps: TargetDeps = field(default_factory=TargetDeps)
    cxxflags: list[str] = field(default_factory=list)
    ldflags: list[str] = field(default_factory=list)
    defines: dict[str, str] = field(default_factory=dict)
    nim_flags: list[str] = field(default_factory=list)
    cflags: list[str] = field(default_factory=list)
    goflags: list[str] = field(default_factory=list)
    rustflags: list[str] = field(default_factory=list)
    zigflags: list[str] = field(default_factory=list)
    # Language for `executable`/`static_library` kinds. Inferred from source
    # extensions if omitted. Mixed-extension sources require an explicit value.
    language: str = ""
    # Toolchain selector. For c/cxx, names a stdenv variant
    # (`""` → default stdenv, `"clang"` → clangStdenv, `"gcc13"` → gcc13Stdenv).
    # For go/rust/zig, names the nixpkgs attr providing the compiler binary
    # (`""` → "go" / "rustc" / "zig"; `"go_1_21"` to pin a specific version).
    compiler: str = ""
    # Cross-compilation target triple. Friendly alias (e.g. "aarch64-linux"),
    # the full Zig-style triple ("aarch64-linux-musl"), or a `pkgsCross.<x>`
    # attr for c/cxx routing. See nix_gen.CROSS_TARGET_ALIASES for the
    # mapping. Default ("") = same as the build host.
    target: str = ""
    # Python (kind = "python_script")
    python_version: str = "3.12"
    python_project: str = "."
    python_venv_hash: str | None = None
    # Extra files to include in the venv FOD source (alongside pyproject.toml
    # + uv.lock). Paths are relative to `python_project`. Anything you list
    # here re-runs `uv sync` (and shifts the venv hash) when it changes —
    # exactly what you want for vendored wheels / path-deps.
    python_venv_extra: list[str] = field(default_factory=list)
    run: str | None = None                          # for kind = "run"
    args: list[str] = field(default_factory=list)   # for kind = "run"
    outputs: list[str] = field(default_factory=list)  # for kind = "run"
    build_script: str | None = None                 # for kind = "custom"
    install_script: str | None = None               # for kind = "custom"
    native_build_inputs: list[str] = field(default_factory=list)  # nixpkgs attrs
    script: str | None = None                       # for kind = "script"
    variants: dict[str, Variant] = field(default_factory=dict)
    # Module namespace (`[modules]` form). Empty for parent-owned targets.
    # The full identity is `namespace.name` when namespace is set; this is
    # what populates `project.targets` keys and Nix attr names.
    namespace: str = ""

    def variant_names(self) -> list[str]:
        return sorted(self.variants.keys())

    @property
    def qualified_name(self) -> str:
        return f"{self.namespace}.{self.name}" if self.namespace else self.name


@dataclass
class Project:
    name: str
    version: str
    nixpkgs_ref: str
    git_deps: dict[str, GitDep]
    targets: dict[str, Target]
    root: Path
    local_deps: dict[str, "LocalDep"] = field(default_factory=dict)

    def find_target(self, qualified: str) -> tuple["Project", str] | None:
        """Resolve a possibly-qualified target name (e.g. 'frontend.app') to
        (owning_project, target_name_in_that_project). Returns None if the
        name doesn't resolve. Used to render cross-flake `deps.internal`
        refs and to expand `${frontend.app}` interpolations."""
        if "." in qualified:
            head, tail = qualified.split(".", 1)
            if head in self.local_deps and self.local_deps[head].sub_project:
                sub = self.local_deps[head].sub_project
                # Recurse so 'a.b.c' walks through nested local-deps.
                return sub.find_target(tail) if "." in tail else (
                    (sub, tail) if tail in sub.targets else None
                )
            return None
        return (self, qualified) if qualified in self.targets else None


class ConfigError(ValueError):
    pass


def _load_vars(
    data: dict, base_path: Path | None = None, _visited: set[Path] | None = None
) -> dict[str, list[str]]:
    """Read `[vars]` as `dict[str, list[str]]`. Each value must be a list of
    strings — vars are only useful for sharing list fields between targets,
    and rejecting other shapes upfront keeps the expansion rules simple.

    The reserved key `extends = ["../path/to/rigx.toml", …]` pulls vars from
    other TOML files (resolved against `base_path`). Extended files'
    `[vars]` tables are merged in first; collisions are config errors;
    cycles are detected via `_visited`."""
    raw = data.get("vars", {})
    if not isinstance(raw, dict):
        raise ConfigError("[vars] must be a table")
    out: dict[str, list[str]] = {}
    if _visited is None:
        _visited = set()

    extends = raw.get("extends", [])
    if extends and not isinstance(extends, list):
        raise ConfigError("[vars].extends must be a list of paths")
    for entry in extends:
        if not isinstance(entry, str):
            raise ConfigError(
                f"[vars].extends: entries must be strings, got {entry!r}"
            )
        if base_path is None:
            raise ConfigError(
                "[vars].extends: base_path required to resolve relative paths"
            )
        ext_path = (base_path / entry).resolve()
        if ext_path in _visited:
            chain = " -> ".join(str(p) for p in [*_visited, ext_path])
            raise ConfigError(f"[vars].extends cycle: {chain}")
        if not ext_path.is_file():
            raise ConfigError(f"[vars].extends: file not found: {ext_path}")
        with ext_path.open("rb") as f:
            ext_data = tomllib.load(f)
        ext_vars = _load_vars(ext_data, ext_path.parent, _visited | {ext_path})
        for k, v in ext_vars.items():
            if k in out:
                raise ConfigError(
                    f"[vars].extends: '{k}' from {ext_path} collides with another extended file"
                )
            out[k] = v

    for vname, vval in raw.items():
        if vname == "extends":
            continue
        if not isinstance(vval, list) or not all(isinstance(x, str) for x in vval):
            raise ConfigError(
                f"vars.{vname}: must be a list of strings"
            )
        for x in vval:
            if _VARS_REF.match(x):
                raise ConfigError(
                    f"vars.{vname}: nested $vars references are not supported "
                    f"(found {x!r})"
                )
        if vname in out:
            raise ConfigError(
                f"vars.{vname}: collides with vars inherited via extends"
            )
        out[vname] = list(vval)
    return out


def _expand_list(items, vars: dict[str, list[str]], ctx: str) -> list[str]:
    """Expand `"$vars.<name>"` entries in a list field. Non-matching items
    pass through verbatim. `ctx` is used in error messages (e.g.
    'target hello: sources')."""
    if items is None:
        return []
    out: list[str] = []
    for item in items:
        if not isinstance(item, str):
            raise ConfigError(f"{ctx}: list entries must be strings, got {item!r}")
        m = _VARS_REF.match(item)
        if m:
            vname = m.group(1)
            if vname not in vars:
                raise ConfigError(f"{ctx}: undefined var '$vars.{vname}'")
            out.extend(vars[vname])
        else:
            out.append(item)
    return out


def _expand_globs(
    items: list[str], root: Path, ctx: str, *, output_prefix: str = ""
) -> list[str]:
    """Resolve glob patterns (`*`, `**`, `?`, `[…]`) in a path list against
    `root`. Non-glob entries pass through verbatim; glob entries are replaced
    by their sorted matches. A glob that matches no files is a config error —
    silent zero-match globs hide typos and produce empty derivations.

    `output_prefix` is prepended to every resolved path (literal or globbed).
    Used by `[modules]` so a module's `src/main.cpp` ends up as
    `frontend/src/main.cpp` relative to the parent root."""
    out: list[str] = []
    for item in items:
        if not any(c in item for c in "*?["):
            out.append(output_prefix + item)
            continue
        # Path.glob handles `**` natively. Sort for deterministic Nix derivation
        # hashes and stable build commands.
        matches = sorted(
            output_prefix + p.relative_to(root).as_posix()
            for p in root.glob(item)
            if p.is_file()
        )
        if not matches:
            raise ConfigError(f"{ctx}: glob {item!r} matched no files under {root}")
        out.extend(matches)
    return out


def _infer_language(
    sources: list[str], explicit: str, kind: str, ctx: str
) -> str:
    """Resolve a target's `language` field.

    With `explicit` set: validates it's one of the known languages.
    Otherwise: scans source extensions; rejects mixed-language source lists
    (forces an explicit override) and empty source lists (`executable`/
    `static_library` always need at least one source)."""
    if explicit:
        if explicit not in {"c", "cxx", "go", "rust", "zig", "nim"}:
            raise ConfigError(
                f"{ctx}: language must be one of c, cxx, go, rust, zig, nim "
                f"(got {explicit!r})"
            )
        return explicit
    if not sources:
        raise ConfigError(
            f"{ctx}: cannot infer language — `sources` is empty"
        )
    seen: set[str] = set()
    for s in sources:
        for ext, lang in EXT_TO_LANG.items():
            if s.endswith(ext):
                seen.add(lang)
                break
    if not seen:
        raise ConfigError(
            f"{ctx}: cannot infer language from sources {sources!r}; "
            f"set `language = …` explicitly"
        )
    if len(seen) > 1:
        raise ConfigError(
            f"{ctx}: mixed source languages {sorted(seen)}; "
            f"set `language = …` to disambiguate"
        )
    return next(iter(seen))


def _build_target(
    tname: str,
    tconf: dict,
    vars_table: dict[str, list[str]],
    git_deps: dict[str, "GitDep"],
    glob_root: Path,
    *,
    namespace: str = "",
    path_prefix: str = "",
) -> Target:
    """Build a single Target from its TOML table.

    `glob_root` is where source globs are resolved (parent root for parent
    targets, module root for `[modules]`-merged targets). `path_prefix` is
    a POSIX path fragment (with trailing `/`) prepended to every resolved
    path so module-relative paths stay valid relative to the parent root.
    `namespace` populates `target.namespace`; same-namespace `deps.internal`
    refs are auto-qualified (`greet` → `frontend.greet`)."""
    kind = tconf.get("kind")
    if kind not in VALID_KINDS:
        raise ConfigError(
            f"target {tname}: 'kind' must be one of {sorted(VALID_KINDS)}"
        )

    deps_conf = tconf.get("deps", {})
    for unknown in deps_conf.keys() - {"internal", "nixpkgs", "git"}:
        raise ConfigError(f"target {tname}: unknown deps key '{unknown}'")
    deps = TargetDeps(
        internal=_expand_list(
            deps_conf.get("internal", []), vars_table, f"target {tname}: deps.internal"
        ),
        nixpkgs=_expand_list(
            deps_conf.get("nixpkgs", []), vars_table, f"target {tname}: deps.nixpkgs"
        ),
        git=_expand_list(
            deps_conf.get("git", []), vars_table, f"target {tname}: deps.git"
        ),
    )
    for g in deps.git:
        if g not in git_deps:
            raise ConfigError(
                f"target {tname}: deps.git references undefined dependency '{g}'"
            )
    # Auto-qualify intra-module deps so module-author writes `greet` and the
    # merged scope sees `frontend.greet`. Already-qualified refs and own-
    # project parent refs (no namespace) pass through.
    if namespace:
        deps.internal = [
            d if "." in d else f"{namespace}.{d}"
            for d in deps.internal
        ]

    variants: dict[str, Variant] = {}
    for vname, vconf in tconf.get("variants", {}).items():
        vctx = f"target {tname}: variants.{vname}"
        variants[vname] = Variant(
            name=vname,
            cxxflags=_expand_list(vconf.get("cxxflags", []), vars_table, f"{vctx}.cxxflags"),
            defines=dict(vconf.get("defines", {})),
            ldflags=_expand_list(vconf.get("ldflags", []), vars_table, f"{vctx}.ldflags"),
            nim_flags=_expand_list(vconf.get("nim_flags", []), vars_table, f"{vctx}.nim_flags"),
            cflags=_expand_list(vconf.get("cflags", []), vars_table, f"{vctx}.cflags"),
            goflags=_expand_list(vconf.get("goflags", []), vars_table, f"{vctx}.goflags"),
            rustflags=_expand_list(vconf.get("rustflags", []), vars_table, f"{vctx}.rustflags"),
            zigflags=_expand_list(vconf.get("zigflags", []), vars_table, f"{vctx}.zigflags"),
            compiler=str(vconf.get("compiler", "")),
            target=str(vconf.get("target", "")),
        )

    tctx = f"target {tname}"
    sources = _expand_globs(
        _expand_list(tconf.get("sources", []), vars_table, f"{tctx}.sources"),
        glob_root,
        f"{tctx}.sources",
        output_prefix=path_prefix,
    )
    includes = [
        path_prefix + i
        for i in _expand_list(tconf.get("includes", []), vars_table, f"{tctx}.includes")
    ]
    public_headers = [
        path_prefix + p
        for p in _expand_list(
            tconf.get("public_headers", []), vars_table, f"{tctx}.public_headers"
        )
    ]
    raw_python_project = str(tconf.get("python_project", "."))
    python_project = raw_python_project
    if path_prefix and python_project not in (".", ""):
        # python_project is a path relative to the declaring rigx.toml; rewrite
        # so it stays valid from the parent's root.
        python_project = path_prefix.rstrip("/") + "/" + python_project.lstrip("./")
    elif path_prefix and python_project in (".", ""):
        python_project = path_prefix.rstrip("/")

    # python_venv_extra: paths the user types relative to `python_project`
    # (i.e. relative to where their `pyproject.toml` lives). We glob against
    # that directory on disk and emit project-root-relative paths so the
    # nix_gen layer can reach them via `${./<path>}`.
    if raw_python_project in (".", ""):
        venv_glob_root = glob_root
        venv_output_prefix = path_prefix
    else:
        venv_glob_root = glob_root / raw_python_project
        venv_output_prefix = (python_project.rstrip("/") + "/") if python_project not in (".", "") else ""
    python_venv_extra = _expand_globs(
        _expand_list(
            tconf.get("python_venv_extra", []),
            vars_table,
            f"{tctx}.python_venv_extra",
        ),
        venv_glob_root,
        f"{tctx}.python_venv_extra",
        output_prefix=venv_output_prefix,
    )

    # Resolve language + validate compiler for executable/static_library.
    language = ""
    if kind in KIND_LANGUAGES:
        language = _infer_language(
            sources, str(tconf.get("language", "")), kind, tctx,
        )
        if language not in KIND_LANGUAGES[kind]:
            raise ConfigError(
                f"{tctx}: kind={kind!r} does not support language {language!r}; "
                f"allowed: {sorted(KIND_LANGUAGES[kind])}"
            )
    compiler = str(tconf.get("compiler", ""))
    target_triple = str(tconf.get("target", ""))

    return Target(
        name=tname,
        kind=kind,
        sources=sources,
        includes=includes,
        public_headers=public_headers,
        deps=deps,
        cxxflags=_expand_list(tconf.get("cxxflags", []), vars_table, f"{tctx}.cxxflags"),
        ldflags=_expand_list(tconf.get("ldflags", []), vars_table, f"{tctx}.ldflags"),
        defines=dict(tconf.get("defines", {})),
        nim_flags=_expand_list(tconf.get("nim_flags", []), vars_table, f"{tctx}.nim_flags"),
        cflags=_expand_list(tconf.get("cflags", []), vars_table, f"{tctx}.cflags"),
        goflags=_expand_list(tconf.get("goflags", []), vars_table, f"{tctx}.goflags"),
        rustflags=_expand_list(tconf.get("rustflags", []), vars_table, f"{tctx}.rustflags"),
        zigflags=_expand_list(tconf.get("zigflags", []), vars_table, f"{tctx}.zigflags"),
        language=language,
        compiler=compiler,
        target=target_triple,
        python_version=str(tconf.get("python_version", "3.12")),
        python_project=python_project,
        python_venv_hash=tconf.get("python_venv_hash"),
        python_venv_extra=python_venv_extra,
        run=tconf.get("run"),
        args=_expand_list(tconf.get("args", []), vars_table, f"{tctx}.args"),
        outputs=_expand_list(tconf.get("outputs", []), vars_table, f"{tctx}.outputs"),
        build_script=tconf.get("build_script"),
        install_script=tconf.get("install_script"),
        native_build_inputs=_expand_list(
            tconf.get("native_build_inputs", []), vars_table, f"{tctx}.native_build_inputs"
        ),
        script=tconf.get("script"),
        variants=variants,
        namespace=namespace,
    )


def _enumerate_modules(
    data: dict, base_root: Path, ns_prefix: str = ""
):
    """Yield (namespace, module_root, raw_data) for each `[modules].include`
    entry, recursing into nested module trees. Validates that modules don't
    declare `[project]` or `[nixpkgs]` — those are reserved for the parent."""
    section = data.get("modules", {})
    if not isinstance(section, dict):
        raise ConfigError("[modules] must be a table")
    includes = section.get("include", [])
    if not isinstance(includes, list):
        raise ConfigError("[modules].include must be a list of paths")
    for entry in includes:
        if not isinstance(entry, str):
            raise ConfigError(
                f"[modules].include: entries must be strings, got {entry!r}"
            )
        mod_root = (base_root / entry).resolve()
        mod_basename = Path(entry).name
        ns = f"{ns_prefix}.{mod_basename}" if ns_prefix else mod_basename
        if not (mod_root / "rigx.toml").is_file():
            raise ConfigError(f"module {ns}: no rigx.toml at {mod_root}")
        with (mod_root / "rigx.toml").open("rb") as f:
            mod_data = tomllib.load(f)
        if "project" in mod_data:
            raise ConfigError(
                f"module {ns}: must not contain [project] (parent owns identity)"
            )
        if "nixpkgs" in mod_data:
            raise ConfigError(
                f"module {ns}: must not contain [nixpkgs] (parent's nixpkgs ref is shared)"
            )
        yield (ns, mod_root, mod_data)
        yield from _enumerate_modules(mod_data, mod_root, ns)


def load(root: Path) -> Project:
    return _load(root.resolve(), _visited=set())


def _parse_git_deps(section: dict, source_label: str) -> dict[str, GitDep]:
    out: dict[str, GitDep] = {}
    for gname, gconf in section.items():
        if "url" not in gconf:
            raise ConfigError(f"{source_label}dependencies.git.{gname}: missing 'url'")
        out[gname] = GitDep(
            name=gname,
            url=gconf["url"],
            rev=gconf.get("rev", "HEAD"),
            flake=gconf.get("flake", True),
            attr=gconf.get("attr"),
        )
    return out


def _parse_local_deps(
    section: dict,
    declared_root: Path,
    git_deps: dict[str, GitDep],
    _visited: set[Path],
    source_label: str,
) -> dict[str, LocalDep]:
    out: dict[str, LocalDep] = {}
    for lname, lconf in section.items():
        if "path" not in lconf:
            raise ConfigError(
                f"{source_label}dependencies.local.{lname}: missing 'path'"
            )
        if lname in git_deps:
            raise ConfigError(
                f"{source_label}dependencies.local.{lname}: "
                f"name collides with dependencies.git.{lname}"
            )
        sub_root = (declared_root / lconf["path"]).resolve()
        if not (sub_root / "rigx.toml").is_file():
            raise ConfigError(
                f"{source_label}dependencies.local.{lname}: no rigx.toml at {sub_root}"
            )
        sub_project = _load(sub_root, _visited)
        out[lname] = LocalDep(
            name=lname,
            path=sub_root,
            flake=lconf.get("flake", True),
            sub_project=sub_project,
        )
    return out


def _merge_into(
    base: dict, new: dict, kind: str, source_label: str
) -> None:
    for k, v in new.items():
        if k in base:
            raise ConfigError(
                f"{source_label}: {kind}.{k} collides with another module or parent"
            )
        base[k] = v


def _load(root: Path, _visited: set[Path]) -> Project:
    if root in _visited:
        chain = " -> ".join(str(p) for p in [*_visited, root])
        raise ConfigError(f"local-dep cycle detected: {chain}")
    _visited = _visited | {root}

    toml_path = root / "rigx.toml"
    if not toml_path.is_file():
        raise ConfigError(f"no rigx.toml found at {toml_path}")
    with toml_path.open("rb") as f:
        data = tomllib.load(f)

    proj = data.get("project")
    if not proj or "name" not in proj:
        raise ConfigError("missing [project] section with 'name'")
    name = proj["name"]
    version = proj.get("version", "0.0.0")

    nixpkgs = data.get("nixpkgs", {})
    nixpkgs_ref = nixpkgs.get("ref", "nixos-24.11")

    # Phase 1: gather modules. Each module's TOML is read and validated for
    # the `no [project]` / `no [nixpkgs]` rules during enumeration.
    modules = list(_enumerate_modules(data, root))

    # Phase 2: merge top-level tables (vars, git_deps, local_deps) across the
    # parent and every module. Collisions are config errors so the user
    # always knows which module owns what.
    vars_table = _load_vars(data, base_path=root)
    git_deps = _parse_git_deps(data.get("dependencies", {}).get("git", {}), "")
    local_deps = _parse_local_deps(
        data.get("dependencies", {}).get("local", {}),
        root, git_deps, _visited, "",
    )
    for ns, mod_root, mod_data in modules:
        label = f"module {ns}"
        _merge_into(vars_table, _load_vars(mod_data, base_path=mod_root), "vars", label)
        _merge_into(
            git_deps,
            _parse_git_deps(mod_data.get("dependencies", {}).get("git", {}), f"{label}: "),
            "dependencies.git", label,
        )
        _merge_into(
            local_deps,
            _parse_local_deps(
                mod_data.get("dependencies", {}).get("local", {}),
                mod_root, git_deps, _visited, f"{label}: ",
            ),
            "dependencies.local", label,
        )

    # Phase 3: build targets. Parent's first (no namespace), then each module
    # with its namespace and a path prefix so source paths stay valid relative
    # to the parent root.
    targets: dict[str, Target] = {}
    for tname, tconf in data.get("targets", {}).items():
        targets[tname] = _build_target(
            tname, tconf, vars_table, git_deps, root,
            namespace="", path_prefix="",
        )
    for ns, mod_root, mod_data in modules:
        try:
            rel = mod_root.relative_to(root)
            prefix = f"{rel.as_posix()}/"
        except ValueError:
            raise ConfigError(
                f"module {ns}: path {mod_root} is not under parent root {root}"
            )
        for tname, tconf in mod_data.get("targets", {}).items():
            qual = f"{ns}.{tname}"
            if qual in targets:
                raise ConfigError(
                    f"module {ns}: target name collision on '{qual}'"
                )
            targets[qual] = _build_target(
                tname, tconf, vars_table, git_deps, mod_root,
                namespace=ns, path_prefix=prefix,
            )

    # Phase 4: validate deps.internal across the merged set. A ref is valid
    # if it names (a) a target in this flake (parent or merged module), or
    # (b) a `<localdep>.<target>` cross-flake ref into a sibling project.
    for qname, target in targets.items():
        for dep in target.deps.internal:
            if dep in targets:
                continue
            if "." in dep:
                head, tail = dep.split(".", 1)
                if head in local_deps:
                    sub = local_deps[head].sub_project
                    resolved = sub.find_target(tail) if sub else None
                    if resolved is None:
                        raise ConfigError(
                            f"target {qname}: internal dep '{dep}' — target "
                            f"'{tail}' not found in local-dep '{head}'"
                        )
                    continue
            raise ConfigError(
                f"target {qname}: internal dep '{dep}' is not a defined target "
                f"(neither a sibling target in this flake nor a known local-dep ref)"
            )
        if target.kind == "custom":
            if not target.install_script:
                raise ConfigError(
                    f"target {tname}: kind='custom' requires 'install_script'"
                )
        if target.kind == "script":
            if not target.script:
                raise ConfigError(
                    f"target {tname}: kind='script' requires 'script'"
                )
        if target.kind == "test":
            if not target.script:
                raise ConfigError(
                    f"target {tname}: kind='test' requires 'script'"
                )
        if target.kind == "run":
            if not target.run:
                raise ConfigError(f"target {tname}: kind='run' requires 'run = <name>'")
            # If `run` matches an internal target, require it to be runnable.
            # Otherwise, treat `run` as a bare command name resolved via PATH
            # (supplied by deps.nixpkgs / deps.git).
            if target.run in targets:
                runnable_kinds = {"executable"}
                if targets[target.run].kind not in runnable_kinds:
                    raise ConfigError(
                        f"target {tname}: run target '{target.run}' must be one of "
                        f"{sorted(runnable_kinds)}, got {targets[target.run].kind!r}"
                    )
            if not target.outputs:
                raise ConfigError(
                    f"target {tname}: kind='run' requires 'outputs' (files to capture)"
                )

    return Project(
        name=name,
        version=version,
        nixpkgs_ref=nixpkgs_ref,
        git_deps=git_deps,
        local_deps=local_deps,
        targets=targets,
        root=root,
    )
