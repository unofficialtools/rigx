"""Per-target source-set computation.

When `[project].sources` is set in `rigx.toml`, every target's `src`
derivation input is narrowed to the intersection of:

  1. `[project].sources`             — repo-wide include globs (baseline)
  2. `[targets.X].sources`           — per-target include globs
                                       (only narrows when present)
  3. minus `[project].excludes`      — globs subtracted from the include set
  4. minus `git ls-files` ignores    — when `respect_gitignore = true`
                                       and the project root is a git checkout

Globs are POSIX, path-aware:
    `*`   any chars except `/`
    `?`   single non-`/` char
    `**`  zero or more path components (only meaningful between separators)
    `[…]` character class

This module is the single source of truth: `nix_gen` consumes
`compute_target_files` to bake per-target allow-lists into flake.nix,
and `rigx ls-source <target>` prints the same list.
"""

from __future__ import annotations

import re
import subprocess
from functools import lru_cache
from pathlib import Path

from rigx.config import Project, Target


# Directories that are never walked. Mirrors the names baked into the legacy
# `srcRoot` filter so behavior stays consistent for projects that don't opt
# into the new include model.
_ALWAYS_SKIP_DIRS = frozenset({".git", "output", "result", ".rigx", "__pycache__"})


def _glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Translate a path-aware glob to a regex anchored against a relative
    POSIX path (no leading `/`).

    `**` is recognized only at path-component boundaries (between separators
    or at the start/end of the pattern). Inside a single component, `*` and
    `?` apply per-character.
    """
    if pattern.startswith("./"):
        pattern = pattern[2:]

    out: list[str] = ["^"]
    i = 0
    n = len(pattern)
    while i < n:
        if pattern[i:i + 2] == "**":
            before_is_sep = (i == 0) or pattern[i - 1] == "/"
            j = i + 2
            after_is_sep = (j == n) or pattern[j] == "/"
            if before_is_sep and after_is_sep:
                if j == n:
                    if i > 0 and out[-1] == "/":
                        out.pop()
                        out.append("(?:/.*)?")
                    else:
                        out.append(".*")
                    i = j
                else:
                    if i > 0 and out[-1] == "/":
                        out.pop()
                        out.append("(?:/.*)?/")
                    else:
                        out.append("(?:.*/)?")
                    i = j + 1
                continue

        c = pattern[i]
        if c == "*":
            out.append("[^/]*")
            i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        elif c == "[":
            try:
                k = pattern.index("]", i + 1)
                cls = pattern[i:k + 1]
                out.append(cls)
                i = k + 1
            except ValueError:
                out.append("\\[")
                i += 1
        elif c == "/":
            out.append("/")
            i += 1
        elif c in r".+(){}^$|\\":
            out.append("\\" + c)
            i += 1
        else:
            out.append(c)
            i += 1
    out.append("$")
    return re.compile("".join(out))


def _compile_globs(patterns: list[str]) -> list[re.Pattern[str]]:
    return [_glob_to_regex(p) for p in patterns]


def _matches_any(rel: str, regexes: list[re.Pattern[str]]) -> bool:
    return any(rx.match(rel) is not None for rx in regexes)


def _walk_files(root: Path) -> list[str]:
    """Yield every regular file under `root` as a POSIX-relative path,
    skipping `_ALWAYS_SKIP_DIRS` at any depth. Symlinks are resolved by
    `is_file()`; broken symlinks are silently dropped (Nix wouldn't be
    able to hash them either)."""
    out: list[str] = []
    root = root.resolve()

    def walk(d: Path, rel_parts: tuple[str, ...]) -> None:
        try:
            entries = sorted(d.iterdir(), key=lambda p: p.name)
        except (PermissionError, OSError):
            return
        for entry in entries:
            name = entry.name
            if entry.is_dir() and not entry.is_symlink():
                if name in _ALWAYS_SKIP_DIRS:
                    continue
                walk(entry, rel_parts + (name,))
            elif entry.is_file():
                out.append("/".join(rel_parts + (name,)))

    walk(root, ())
    return out


@lru_cache(maxsize=None)
def _git_tracked_or_unignored_cached(root_str: str) -> tuple[str, ...] | None:
    """Process-lifetime cache of the gitignore-aware file list. Without
    this every Target evaluation re-spawned git ls-files; on a 30+
    target project with a slow filesystem that's the dominant build
    overhead. Returns None when git is unavailable or `root` isn't a
    checkout — caller treats that as "don't filter by gitignore"."""
    try:
        proc = subprocess.run(
            [
                "git", "-C", root_str,
                "ls-files",
                "--cached", "--others", "--exclude-standard",
                "-z",
            ],
            capture_output=True,
            check=False,
        )
    except (FileNotFoundError, OSError):
        return None
    if proc.returncode != 0:
        return None
    raw = proc.stdout.decode("utf-8", errors="replace")
    return tuple(p for p in raw.split("\x00") if p)


def _git_tracked_or_unignored(root: Path) -> set[str] | None:
    """Return the set of paths git considers in-tree (tracked or
    untracked-and-not-ignored), POSIX-relative to `root`. Returns None
    if `root` is not a git checkout or git is unavailable — caller
    treats that as "don't apply gitignore filtering"."""
    cached = _git_tracked_or_unignored_cached(str(root))
    return None if cached is None else set(cached)


@lru_cache(maxsize=None)
def _project_files_cached(root_str: str) -> tuple[str, ...]:
    """Cache the disk walk per project root; the same Project is queried
    once per target during flake-gen."""
    return tuple(_walk_files(Path(root_str)))


def _project_files(project: Project) -> list[str]:
    return list(_project_files_cached(str(project.root.resolve())))


def project_filtering_enabled(project: Project) -> bool:
    """True iff the user opted into the include-only source-filter model
    by setting `[project].sources`. When false, callers should fall back
    to the legacy whole-tree-with-basename-blacklist `srcRoot` behavior."""
    return bool(project.sources)


def _apply_filters(
    files: list[str],
    include_globs: list[str],
    exclude_globs: list[str],
    *,
    gitignore_set: set[str] | None,
) -> list[str]:
    inc = _compile_globs(include_globs)
    exc = _compile_globs(exclude_globs) if exclude_globs else []

    def keep(rel: str) -> bool:
        if not _matches_any(rel, inc):
            return False
        if exc and _matches_any(rel, exc):
            return False
        if gitignore_set is not None and rel not in gitignore_set:
            return False
        return True

    return [r for r in files if keep(r)]


@lru_cache(maxsize=None)
def _compute_project_files_cached(
    root_str: str,
    sources: tuple[str, ...],
    excludes: tuple[str, ...],
    respect_gitignore: bool,
) -> tuple[str, ...]:
    files = list(_project_files_cached(root_str))
    gitignore_set = (
        _git_tracked_or_unignored(Path(root_str))
        if respect_gitignore else None
    )
    return tuple(sorted(_apply_filters(
        files,
        list(sources),
        list(excludes),
        gitignore_set=gitignore_set,
    )))


def compute_project_files(project: Project) -> list[str]:
    """Files matching `[project].sources` minus `[project].excludes`
    (and minus gitignore when respected). Sorted, POSIX, root-relative.

    Empty result is returned as-is — callers decide whether to error.
    Caller should check `project_filtering_enabled` first; this function
    returns an empty list when the project hasn't opted in.
    """
    if not project.sources:
        return []
    return list(_compute_project_files_cached(
        str(project.root.resolve()),
        tuple(project.sources),
        tuple(project.excludes),
        project.respect_gitignore,
    ))


def compute_target_files(project: Project, target: Target) -> list[str]:
    """Files that should be the `src` input for `target`'s derivation.

    When the target has its own `sources`: per-target `src` is the union
    of the target's listed source files and every project-baseline file
    that sits under any directory the target declares as `includes` or
    `public_headers`. The latter is what makes C/C++ ergonomic — users
    declare `includes = ["include"]` once and any `**/*.h` in the project
    baseline lands automatically.

    When the target has no `sources`: it gets the full project baseline.

    Raises ValueError if a path listed in `target.sources` isn't part of
    the project baseline (typo / missing extension / forgotten exclude).
    """
    if not project.sources:
        # Caller is expected to fall back to the legacy `srcRoot` path
        # in this case; the empty-list return is a defensive default.
        return []
    base = compute_project_files(project)
    if not target.sources and not target.inputs:
        return base

    # Sources that start with `${...}` are Nix-interpolations into the rec
    # scope (a `generated_source` reference like `${gen}/foo.nim`), not
    # paths on disk. They contribute nothing to the on-disk src filter
    # and need to be excluded from the baseline-validation check.
    on_disk_sources = [s for s in target.sources if not s.startswith("${")]
    base_set = set(base)
    missing = [s for s in on_disk_sources if s not in base_set]
    if missing:
        raise ValueError(
            f"target {target.qualified_name!r}: sources are not in the "
            f"[project].sources baseline: {missing}\n"
            f"  Add a glob covering them to [project].sources, or remove "
            f"them from this target."
        )
    # `generated_source` targets carry their source-file list under
    # `inputs` (not `sources`), so the per-target src filter has to
    # include them too — otherwise the derivation's $src is missing the
    # files the generator command tries to read.
    missing_inputs = [s for s in target.inputs if s not in base_set]
    if missing_inputs:
        raise ValueError(
            f"target {target.qualified_name!r}: inputs are not in the "
            f"[project].sources baseline: {missing_inputs}\n"
            f"  Add a glob covering them to [project].sources, or remove "
            f"them from this target."
        )

    keep: set[str] = set(on_disk_sources) | set(target.inputs)
    # Capsule `nixos_modules` are referenced via `(src + "/path")` in the
    # generated flake — they must land in the per-target src too.
    missing_modules = [
        m for m in target.nixos_modules if m not in base_set
    ]
    if missing_modules:
        raise ValueError(
            f"target {target.qualified_name!r}: nixos_modules are not in "
            f"the [project].sources baseline: {missing_modules}\n"
            f"  Add a glob covering them to [project].sources."
        )
    keep.update(target.nixos_modules)
    for inc in list(target.includes) + list(target.public_headers):
        prefix = inc.rstrip("/") + "/"
        for f in base:
            if f.startswith(prefix):
                keep.add(f)
    return sorted(keep)


def ancestor_dirs(rels: list[str]) -> list[str]:
    """Every ancestor directory of every entry in `rels`, as POSIX-relative
    paths. Used to build the Nix filter's `allowed` attrset — Nix needs
    directories to be allowed for the walk to descend into them."""
    seen: set[str] = set()
    for rel in rels:
        parts = rel.split("/")
        for i in range(1, len(parts)):
            seen.add("/".join(parts[:i]))
    return sorted(seen)
