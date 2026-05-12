"""`rigx watch` — rebuild on source change (polling)."""

from __future__ import annotations

import argparse
import time
from pathlib import Path

from rigx import builder, sources
from rigx.commands.helpers import report_build_error


def _declared_input_files(project, targets: list[str] | None) -> set[Path]:
    """Resolve the set of files watch should poll under the narrow
    default: rigx.toml itself, plus everything each declared target
    consumes (sources, includes, public_headers, scripts, env files…).

    For projects without `[project].sources`, the per-target file
    resolver isn't available, so we fall back to the union of
    `target.sources`, `target.includes`, `target.public_headers`,
    `target.python_project` / `python_venv_extra`, and module file
    paths (`nixos_modules`, `volumes[].host`). Cross-flake local-dep
    rigx.toml files are included so a sibling-flake change re-triggers.
    """
    files: set[Path] = set()
    root = project.root
    # The project's own rigx.toml.
    toml = root / "rigx.toml"
    if toml.is_file():
        files.add(toml)
    # Modules / included files are loaded by the config layer; their
    # paths land in `project.included_files` (when the loader tracks
    # them). Best-effort.
    for path in getattr(project, "included_files", []) or []:
        p = Path(path)
        if p.exists():
            files.add(p)
    # Lockfile relevant for python_script venv hashes.
    for cand in ("uv.lock", "pyproject.toml"):
        p = root / cand
        if p.is_file():
            files.add(p)
    selected: list[str] = []
    if targets:
        for spec in targets:
            base = spec.split("@", 1)[0]
            if base in project.targets:
                selected.append(base)
    else:
        selected = list(project.targets.keys())
    for tname in selected:
        target = project.targets[tname]
        # python_project sub-trees (pyproject + uv.lock + extras).
        if target.kind == "python_script":
            pdir = target.python_project.strip("/")
            base = root / pdir if pdir not in ("", ".") else root
            for cand in ("pyproject.toml", "uv.lock"):
                p = base / cand
                if p.is_file():
                    files.add(p)
            for extra in target.python_venv_extra:
                p = root / extra
                if p.exists() and p.is_file():
                    files.add(p)
        # Source files / includes / public headers.
        for s in target.sources:
            p = root / s
            if p.is_file():
                files.add(p)
        for inc in target.includes:
            p = root / inc
            if p.is_dir():
                for sub in p.rglob("*"):
                    if sub.is_file():
                        files.add(sub)
            elif p.is_file():
                files.add(p)
        for ph in target.public_headers:
            p = root / ph
            if p.is_dir():
                for sub in p.rglob("*"):
                    if sub.is_file():
                        files.add(sub)
            elif p.is_file():
                files.add(p)
        for mod in getattr(target, "nixos_modules", []) or []:
            p = root / mod
            if p.is_file():
                files.add(p)
        for v in getattr(target, "volumes", []) or []:
            host = getattr(v, "host", None)
            if host:
                p = root / host
                if p.is_file():
                    files.add(p)
    # Cross-flake local-deps: watch their rigx.toml + their declared
    # inputs (one level deep — re-entering would be unbounded).
    for ldep in project.local_deps.values():
        sub = ldep.sub_project
        if not sub:
            continue
        sub_toml = sub.root / "rigx.toml"
        if sub_toml.is_file():
            files.add(sub_toml)
    # Project-level source filtering: include every declared file.
    if sources.project_filtering_enabled(project):
        try:
            for rel in sources.compute_project_files(project):
                p = root / rel
                if p.is_file():
                    files.add(p)
        except Exception:
            pass
    return files


def cmd_watch(args: argparse.Namespace) -> int:
    """Re-build target(s) whenever a source file changes. Polls every 0.5s.

    Default: watches each target's declared inputs (sources, includes,
    public_headers, lockfiles, rigx.toml). Use `--watch-all` to revert
    to the legacy 'scan the whole project tree (minus output/, .git,
    result, .rigx, flake.lock)' behavior — useful when files outside
    declared inputs (generated headers, ad-hoc data) should also
    trigger rebuilds.
    """
    from rigx import cli
    project = cli._load(args)
    targets = args.targets
    watch_all = getattr(args, "watch_all", False)
    mode = "all files" if watch_all else "declared inputs"
    print(f"[rigx] watching {project.root} [{mode}] (Ctrl-C to stop)")

    if watch_all:
        def scan() -> float:
            latest = 0.0
            skip = {".git", "output", "result", ".rigx"}
            skip_files = {"flake.lock"}
            for path in project.root.rglob("*"):
                if any(part in skip for part in path.relative_to(project.root).parts):
                    continue
                if path.name in skip_files:
                    continue
                if path.is_file():
                    m = path.stat().st_mtime
                    if m > latest:
                        latest = m
            return latest
    else:
        # Resolve the file set up-front. If TOML changes, we'll
        # re-resolve at next iteration so newly-declared inputs start
        # being watched without a `rigx watch` restart.
        tracked = _declared_input_files(project, targets)
        last_toml_mtime = (project.root / "rigx.toml").stat().st_mtime \
            if (project.root / "rigx.toml").is_file() else 0.0

        def scan() -> float:
            nonlocal tracked, last_toml_mtime
            # If rigx.toml changed, reload the project + tracked set so
            # newly added sources etc. start being watched.
            toml_p = project.root / "rigx.toml"
            if toml_p.is_file():
                tm = toml_p.stat().st_mtime
                if tm != last_toml_mtime:
                    last_toml_mtime = tm
                    try:
                        from rigx import config
                        proj2 = config.load(project.root)
                        tracked = _declared_input_files(proj2, targets)
                    except Exception:
                        pass
            latest = 0.0
            for path in list(tracked):
                try:
                    m = path.stat().st_mtime
                except OSError:
                    continue
                if m > latest:
                    latest = m
            return latest

    last = scan()
    try:
        results = builder.build(project, targets)
        for attr, link in results:
            print(f"  {attr} -> {link}")
        print("[rigx] watching for changes…")
    except builder.BuildError as e:
        report_build_error(e)
        print("[rigx] (still watching — fix the error to retry)")
    try:
        while True:
            time.sleep(0.5)
            now = scan()
            if now > last:
                last = now
                print("[rigx] change detected, rebuilding…")
                try:
                    results = builder.build(project, targets)
                    for attr, link in results:
                        print(f"  {attr} -> {link}")
                    print("[rigx] OK")
                except builder.BuildError as e:
                    report_build_error(e)
    except KeyboardInterrupt:
        print()
        return 0
