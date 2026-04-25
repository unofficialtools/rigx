"""Command-line interface for rigx."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from rigx import builder, config, fmt, graph, nix_gen, scaffold


def _find_project_root(start: Path) -> Path:
    cur = start.resolve()
    for candidate in [cur, *cur.parents]:
        if (candidate / "rigx.toml").is_file():
            return candidate
    raise SystemExit(
        "rigx.toml not found in the current directory or any parent"
    )


def _load(args: argparse.Namespace) -> config.Project:
    root = Path(args.project) if args.project else _find_project_root(Path.cwd())
    return config.load(root)


def _report_build_error(e: builder.BuildError) -> None:
    if isinstance(e, builder.NixNotFoundError):
        # The message is already multi-line and self-contained.
        print(str(e), file=sys.stderr)
    else:
        print(f"rigx: {e}", file=sys.stderr)


def cmd_build(args: argparse.Namespace) -> int:
    project = _load(args)
    try:
        results = builder.build(project, args.targets, jobs=args.jobs)
    except builder.BuildError as e:
        _report_build_error(e)
        return 1
    if args.json:
        # Machine-readable form for CI / scripts. One JSON array per call;
        # each element is `{attr, output}` (the symlink path under output/).
        print(json.dumps(
            [{"attr": a, "output": str(p)} for a, p in results],
            indent=2,
        ))
    else:
        for attr, link in results:
            print(f"  {attr} -> {link}")
    return 0


def cmd_lock(args: argparse.Namespace) -> int:
    project = _load(args)
    try:
        builder.update_lock(project)
    except builder.BuildError as e:
        _report_build_error(e)
        return 1
    print("lock updated")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    project = _load(args)
    kf = args.kind
    shown = 0
    for name, target in project.targets.items():
        if kf and target.kind != kf:
            continue
        if target.variants:
            variants = ", ".join(target.variant_names())
            print(f"  {name} [{target.kind}] variants: {variants}")
        else:
            print(f"  {name} [{target.kind}]")
        shown += 1
    # Cross-flake (local-dep) targets, recursively flattened. Shown with the
    # dotted CLI form so the user can copy-paste into `rigx build`.
    shown += _list_local_deps(project, prefix="", kind_filter=kf)
    if kf and shown == 0:
        print(f"  (no targets with kind={kf!r})", file=sys.stderr)
    return 0


def _list_local_deps(project, prefix: str, kind_filter: str | None = None) -> int:
    shown = 0
    for lname, ldep in project.local_deps.items():
        sub = ldep.sub_project
        if not sub:
            continue
        for tname, target in sub.targets.items():
            if target.kind == "script":
                continue
            if kind_filter and target.kind != kind_filter:
                continue
            qual = f"{prefix}{lname}.{tname}"
            if target.variants:
                variants = ", ".join(target.variant_names())
                print(f"  {qual} [{target.kind}, local-dep] variants: {variants}")
            else:
                print(f"  {qual} [{target.kind}, local-dep]")
            shown += 1
        shown += _list_local_deps(sub, prefix=f"{prefix}{lname}.", kind_filter=kind_filter)
    return shown


def cmd_clean(args: argparse.Namespace) -> int:
    project = _load(args)
    builder.clean(project)
    print("output/ cleaned")
    return 0


def cmd_flake(args: argparse.Namespace) -> int:
    """Print the generated flake.nix to stdout (useful for debugging)."""
    project = _load(args)
    sys.stdout.write(nix_gen.generate(project))
    return 0


def cmd_graph(args: argparse.Namespace) -> int:
    """Print a Mermaid `graph TD` for the dep tree of the given target."""
    project = _load(args)
    try:
        sys.stdout.write(graph.mermaid(project, args.target))
    except ValueError as e:
        print(f"rigx: {e}", file=sys.stderr)
        return 1
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    """Re-build target(s) whenever a source file changes. Polls every 0.5s,
    skipping output/, .git, flake.lock — keeps the implementation portable
    (no inotify/fsevents dependency). Exit with Ctrl-C."""
    import time
    project = _load(args)
    targets = args.targets
    print(f"[rigx] watching {project.root} (Ctrl-C to stop)")

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

    last = scan()
    # Trigger one build at startup so the user sees current state.
    try:
        results = builder.build(project, targets)
        for attr, link in results:
            print(f"  {attr} -> {link}")
        print("[rigx] watching for changes…")
    except builder.BuildError as e:
        _report_build_error(e)
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
                    _report_build_error(e)
    except KeyboardInterrupt:
        print()
        return 0


def cmd_new(args: argparse.Namespace) -> int:
    """Append a new `[targets.<name>]` block (and stub source files) to the
    current rigx.toml. Refuses to overwrite existing target names or files."""
    root = Path(args.project) if args.project else Path.cwd()
    toml_path = root / "rigx.toml"
    if not toml_path.is_file():
        print(
            f"rigx: no rigx.toml at {toml_path} — create one with a "
            f"[project] section first.",
            file=sys.stderr,
        )
        return 1
    # Cheap target-name collision check via TOML parse (we don't need the
    # full Project graph here — just to know which names are taken).
    import tomllib
    with toml_path.open("rb") as f:
        data = tomllib.load(f)
    if args.name in data.get("targets", {}):
        print(
            f"rigx: target {args.name!r} already exists in {toml_path}",
            file=sys.stderr,
        )
        return 1
    try:
        s = scaffold.scaffold(args.kind, args.name, args.language, args.run)
    except ValueError as e:
        print(f"rigx: {e}", file=sys.stderr)
        return 1
    for rel, body in s.files.items():
        path = root / rel
        if path.exists():
            print(f"rigx: refusing to overwrite existing file {path}", file=sys.stderr)
            return 1
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
        print(f"  wrote {rel}")
    with toml_path.open("a") as f:
        f.write(s.toml_block)
    print(f"  appended [targets.{args.name}] to rigx.toml")
    return 0


def cmd_fmt(args: argparse.Namespace) -> int:
    """Print a canonical reformat of rigx.toml to stdout. With `--write`,
    overwrite the file in place. Comments are not preserved (tomllib
    strips them on parse) — pipe through stdout to inspect first."""
    root = Path(args.project) if args.project else Path.cwd()
    toml_path = root / "rigx.toml"
    if not toml_path.is_file():
        print(f"rigx: no rigx.toml at {toml_path}", file=sys.stderr)
        return 1
    canonical = fmt.format_file(toml_path, write=args.write)
    if not args.write:
        sys.stdout.write(canonical)
    return 0


def cmd_test(args: argparse.Namespace) -> int:
    """Discover and run every `kind = "test"` target. Prints a per-test
    PASS/FAIL line plus a summary; exit code is the worst test exit code."""
    project = _load(args)
    try:
        results = builder.run_tests(project, args.targets or None)
    except builder.BuildError as e:
        _report_build_error(e)
        return 1
    if not results:
        msg = "no test targets found" if not args.targets else \
            f"no matching test targets: {args.targets}"
        print(f"rigx test: {msg}", file=sys.stderr)
        return 0 if not args.targets else 1
    passed = [n for n, rc in results if rc == 0]
    failed = [(n, rc) for n, rc in results if rc != 0]
    print()
    for n in passed:
        print(f"  PASS  {n}")
    for n, rc in failed:
        print(f"  FAIL  {n}  (exit {rc})")
    print(f"\n{len(passed)} passed, {len(failed)} failed")
    return 0 if not failed else max(rc for _, rc in failed)


def cmd_run(args: argparse.Namespace) -> int:
    """Execute a script-kind target (e.g. publish, deploy)."""
    project = _load(args)
    try:
        builder.run_named_script(project, args.target, args.script_args)
    except builder.BuildError as e:
        _report_build_error(e)
        return 1
    return 0


def cmd_pkg(args: argparse.Namespace) -> int:
    """Run any binary from the project's pinned nixpkgs.

    `rigx pkg <attr> [-- args…]` invokes `nix run nixpkgs/<ref>#<attr>` with
    the args after `--` (or directly after `<attr>`) forwarded verbatim.
    Useful for one-off tooling — `uv lock`, `jq …`, etc. — without having
    to declare a `kind = "script"` target."""
    project = _load(args)
    try:
        return builder.run_nixpkgs_tool(project, args.attr, args.pkg_args)
    except builder.BuildError as e:
        _report_build_error(e)
        return 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="rigx", description="Nix-backed build system")
    p.add_argument(
        "-C",
        "--project",
        help="path to project root (containing rigx.toml)",
        default=None,
    )
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("build", help="build targets (default: all)")
    sp.add_argument("targets", nargs="*", help="target[@variant] selectors")
    sp.add_argument(
        "--json",
        action="store_true",
        help="emit results as a JSON array (machine-readable; for CI/scripts)",
    )
    sp.add_argument(
        "-j",
        "--jobs",
        type=int,
        metavar="N",
        default=None,
        help="forward to `nix build --max-jobs N` (parallel derivations)",
    )
    sp.set_defaults(func=cmd_build)

    sp = sub.add_parser("lock", help="update lock file")
    sp.set_defaults(func=cmd_lock)

    sp = sub.add_parser("list", help="list targets")
    sp.add_argument(
        "--kind",
        choices=sorted(config.VALID_KINDS),
        help="show only targets of this kind",
    )
    sp.set_defaults(func=cmd_list)

    sp = sub.add_parser("clean", help="remove output/")
    sp.set_defaults(func=cmd_clean)

    sp = sub.add_parser("flake", help="print the generated flake.nix")
    sp.set_defaults(func=cmd_flake)

    sp = sub.add_parser(
        "graph",
        help="print a Mermaid dependency graph for a target",
    )
    sp.add_argument("target", help="target name (with optional @variant; ignored)")
    sp.set_defaults(func=cmd_graph)

    sp = sub.add_parser(
        "fmt",
        help="canonicalize rigx.toml (prints to stdout; comments are NOT preserved)",
    )
    sp.add_argument(
        "--write",
        action="store_true",
        help="overwrite rigx.toml in place (default: print to stdout)",
    )
    sp.set_defaults(func=cmd_fmt)

    sp = sub.add_parser(
        "test",
        help="discover and run all `kind = \"test\"` targets",
    )
    sp.add_argument(
        "targets",
        nargs="*",
        help="test target names to run (default: all kind=test targets)",
    )
    sp.set_defaults(func=cmd_test)

    sp = sub.add_parser(
        "watch",
        help="rebuild on source change (polling, Ctrl-C to stop)",
    )
    sp.add_argument("targets", nargs="*", help="target[@variant] selectors")
    sp.set_defaults(func=cmd_watch)

    sp = sub.add_parser(
        "new",
        help="scaffold a new target (appends to rigx.toml, writes stub sources)",
    )
    sp.add_argument(
        "kind",
        choices=[
            "executable", "static_library", "python_script",
            "custom", "script", "run", "test",
        ],
    )
    sp.add_argument("name", help="target name")
    sp.add_argument(
        "--language",
        default="cxx",
        choices=["c", "cxx", "go", "rust", "zig", "nim"],
        help="source language for executable/static_library (default cxx)",
    )
    sp.add_argument("--run", help="for `kind=run`: the target/command to invoke")
    sp.set_defaults(func=cmd_new)

    sp = sub.add_parser(
        "run",
        help="execute a script-kind target (e.g. `rigx run publish [-- args…]`)",
    )
    sp.add_argument("target", help="script target name")
    # No script_args declared here — `main()` slices argv on the first `--`
    # so any flags after it (e.g. `rigx run pub -- --foo`) pass through verbatim.
    sp.set_defaults(func=cmd_run, script_args=[])

    sp = sub.add_parser(
        "pkg",
        help="run a nixpkgs binary (e.g. `rigx pkg uv -- lock`)",
    )
    sp.add_argument("attr", help="nixpkgs attr name (uv, jq, ripgrep, …)")
    # `pkg_args` filled in by `main()` after the manual argv split so any
    # flags after `--` (or after `<attr>`) reach the underlying tool intact.
    sp.set_defaults(func=cmd_pkg, pkg_args=[])

    return p


def _split_pkg_passthrough(argv: list[str]) -> tuple[list[str], list[str]] | None:
    """If `rigx pkg <attr> …` is invoked, split off the trailing args for
    forwarding to the nixpkgs binary.

    Convention: everything after `<attr>` is forwarded; an optional `--`
    separator is consumed (so `rigx pkg uv -- lock` and `rigx pkg uv lock`
    are equivalent). Returns None if no `pkg` subcommand or no attr follows.
    """
    try:
        i = argv.index("pkg")
    except ValueError:
        return None
    prev = argv[i - 1] if i > 0 else ""
    if prev in {"-C", "--project"}:
        return None
    if i + 1 >= len(argv):
        return None  # `rigx pkg` with no attr — let argparse error normally.
    pre = argv[: i + 2]              # rigx … pkg <attr>
    forward = argv[i + 2 :]
    if forward and forward[0] == "--":
        forward = forward[1:]
    return pre, forward


def _split_run_passthrough(argv: list[str]) -> tuple[list[str], list[str]] | None:
    """If `rigx run TARGET -- …` is invoked, split off the post-`--` tail.

    Everything after the first `--` is forwarded to the script as `$1`, `$2`, …
    Returns None if there's no `run` subcommand or no `--` separator.
    """
    try:
        i = argv.index("run")
    except ValueError:
        return None
    prev = argv[i - 1] if i > 0 else ""
    value_taking_opts = {"-C", "--project"}
    if prev in value_taking_opts:
        return None
    try:
        j = argv.index("--", i + 1)
    except ValueError:
        return None
    return argv[:j], argv[j + 1 :]


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()

    pkg_split = _split_pkg_passthrough(raw)
    run_split = _split_run_passthrough(raw)
    if pkg_split is not None:
        pre, pkg_args = pkg_split
        args = parser.parse_args(pre)
        args.pkg_args = pkg_args
    elif run_split is not None:
        pre, script_args = run_split
        args = parser.parse_args(pre)
        args.script_args = script_args
    else:
        args = parser.parse_args(raw)

    try:
        return args.func(args)
    except config.ConfigError as e:
        print(f"rigx: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
