"""Parse procton.toml into typed dataclasses."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

VALID_KINDS = {"executable", "static_library", "shared_library"}


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


def load(root: Path) -> Project:
    toml_path = root / "procton.toml"
    if not toml_path.is_file():
        raise ConfigError(f"no procton.toml found at {toml_path}")
    with toml_path.open("rb") as f:
        data = tomllib.load(f)

    proj = data.get("project")
    if not proj or "name" not in proj:
        raise ConfigError("missing [project] section with 'name'")
    name = proj["name"]
    version = proj.get("version", "0.0.0")

    nixpkgs = data.get("nixpkgs", {})
    nixpkgs_ref = nixpkgs.get("ref", "nixos-24.11")

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
            internal=list(deps_conf.get("internal", [])),
            nixpkgs=list(deps_conf.get("nixpkgs", [])),
            git=list(deps_conf.get("git", [])),
        )
        for g in deps.git:
            if g not in git_deps:
                raise ConfigError(
                    f"target {tname}: deps.git references undefined dependency '{g}'"
                )

        variants: dict[str, Variant] = {}
        for vname, vconf in tconf.get("variants", {}).items():
            variants[vname] = Variant(
                name=vname,
                cxxflags=list(vconf.get("cxxflags", [])),
                defines=dict(vconf.get("defines", {})),
                ldflags=list(vconf.get("ldflags", [])),
            )

        targets[tname] = Target(
            name=tname,
            kind=kind,
            sources=list(tconf.get("sources", [])),
            includes=list(tconf.get("includes", [])),
            public_headers=list(tconf.get("public_headers", [])),
            deps=deps,
            cxxflags=list(tconf.get("cxxflags", [])),
            ldflags=list(tconf.get("ldflags", [])),
            defines=dict(tconf.get("defines", {})),
            variants=variants,
        )

    for tname, target in targets.items():
        for dep in target.deps.internal:
            if dep not in targets:
                raise ConfigError(
                    f"target {tname}: internal dep '{dep}' is not a defined target"
                )

    return Project(
        name=name,
        version=version,
        nixpkgs_ref=nixpkgs_ref,
        git_deps=git_deps,
        targets=targets,
        root=root,
    )
