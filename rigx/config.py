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

VALID_KINDS = {
    "executable",
    "static_library",
    "shared_library",
    "nim_executable",
    "python_script",
    "run",
    "custom",
    "script",
}


@dataclass
class GitDep:
    name: str
    url: str
    rev: str = "HEAD"
    flake: bool = True
    attr: str | None = None  # attr path inside the flake, e.g. "packages.default"


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
    # Python (kind = "python_script")
    python_version: str = "3.12"
    python_project: str = "."
    python_venv_hash: str | None = None
    run: str | None = None                          # for kind = "run"
    args: list[str] = field(default_factory=list)   # for kind = "run"
    outputs: list[str] = field(default_factory=list)  # for kind = "run"
    build_script: str | None = None                 # for kind = "custom"
    install_script: str | None = None               # for kind = "custom"
    native_build_inputs: list[str] = field(default_factory=list)  # nixpkgs attrs
    script: str | None = None                       # for kind = "script"
    variants: dict[str, Variant] = field(default_factory=dict)

    def variant_names(self) -> list[str]:
        return sorted(self.variants.keys())


@dataclass
class Project:
    name: str
    version: str
    nixpkgs_ref: str
    git_deps: dict[str, GitDep]
    targets: dict[str, Target]
    root: Path


class ConfigError(ValueError):
    pass


def _load_vars(data: dict) -> dict[str, list[str]]:
    """Read `[vars]` as `dict[str, list[str]]`. Each value must be a list of
    strings — vars are only useful for sharing list fields between targets,
    and rejecting other shapes upfront keeps the expansion rules simple."""
    raw = data.get("vars", {})
    if not isinstance(raw, dict):
        raise ConfigError("[vars] must be a table")
    out: dict[str, list[str]] = {}
    for vname, vval in raw.items():
        if not isinstance(vval, list) or not all(isinstance(x, str) for x in vval):
            raise ConfigError(
                f"vars.{vname}: must be a list of strings"
            )
        # Vars referencing other vars are not supported — keeps resolution
        # one-pass and rules out cycles. Fail loudly so users notice.
        for x in vval:
            if _VARS_REF.match(x):
                raise ConfigError(
                    f"vars.{vname}: nested $vars references are not supported "
                    f"(found {x!r})"
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


def _expand_globs(items: list[str], root: Path, ctx: str) -> list[str]:
    """Resolve glob patterns (`*`, `**`, `?`, `[…]`) in a path list against
    `root`. Non-glob entries pass through verbatim; glob entries are replaced
    by their sorted matches. A glob that matches no files is a config error —
    silent zero-match globs hide typos and produce empty derivations."""
    out: list[str] = []
    for item in items:
        if not any(c in item for c in "*?["):
            out.append(item)
            continue
        # Path.glob handles `**` natively. Sort for deterministic Nix derivation
        # hashes and stable build commands.
        matches = sorted(
            p.relative_to(root).as_posix()
            for p in root.glob(item)
            if p.is_file()
        )
        if not matches:
            raise ConfigError(f"{ctx}: glob {item!r} matched no files under {root}")
        out.extend(matches)
    return out


def load(root: Path) -> Project:
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

    vars_table = _load_vars(data)

    git_deps: dict[str, GitDep] = {}
    deps_section = data.get("dependencies", {}).get("git", {})
    for gname, gconf in deps_section.items():
        if "url" not in gconf:
            raise ConfigError(f"dependencies.git.{gname}: missing 'url'")
        git_deps[gname] = GitDep(
            name=gname,
            url=gconf["url"],
            rev=gconf.get("rev", "HEAD"),
            flake=gconf.get("flake", True),
            attr=gconf.get("attr"),
        )

    targets: dict[str, Target] = {}
    for tname, tconf in data.get("targets", {}).items():
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

        variants: dict[str, Variant] = {}
        for vname, vconf in tconf.get("variants", {}).items():
            vctx = f"target {tname}: variants.{vname}"
            variants[vname] = Variant(
                name=vname,
                cxxflags=_expand_list(vconf.get("cxxflags", []), vars_table, f"{vctx}.cxxflags"),
                defines=dict(vconf.get("defines", {})),
                ldflags=_expand_list(vconf.get("ldflags", []), vars_table, f"{vctx}.ldflags"),
                nim_flags=_expand_list(vconf.get("nim_flags", []), vars_table, f"{vctx}.nim_flags"),
            )

        tctx = f"target {tname}"
        sources = _expand_globs(
            _expand_list(tconf.get("sources", []), vars_table, f"{tctx}.sources"),
            root,
            f"{tctx}.sources",
        )
        targets[tname] = Target(
            name=tname,
            kind=kind,
            sources=sources,
            includes=_expand_list(tconf.get("includes", []), vars_table, f"{tctx}.includes"),
            public_headers=_expand_list(
                tconf.get("public_headers", []), vars_table, f"{tctx}.public_headers"
            ),
            deps=deps,
            cxxflags=_expand_list(tconf.get("cxxflags", []), vars_table, f"{tctx}.cxxflags"),
            ldflags=_expand_list(tconf.get("ldflags", []), vars_table, f"{tctx}.ldflags"),
            defines=dict(tconf.get("defines", {})),
            nim_flags=_expand_list(tconf.get("nim_flags", []), vars_table, f"{tctx}.nim_flags"),
            python_version=str(tconf.get("python_version", "3.12")),
            python_project=str(tconf.get("python_project", ".")),
            python_venv_hash=tconf.get("python_venv_hash"),
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
        )

    for tname, target in targets.items():
        for dep in target.deps.internal:
            if dep not in targets:
                raise ConfigError(
                    f"target {tname}: internal dep '{dep}' is not a defined target"
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
        if target.kind == "run":
            if not target.run:
                raise ConfigError(f"target {tname}: kind='run' requires 'run = <name>'")
            # If `run` matches an internal target, require it to be runnable.
            # Otherwise, treat `run` as a bare command name resolved via PATH
            # (supplied by deps.nixpkgs / deps.git).
            if target.run in targets:
                runnable_kinds = {"executable", "nim_executable"}
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
        targets=targets,
        root=root,
    )
