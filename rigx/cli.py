"""Command-line interface for rigx."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from rigx import builder, config, nix_gen


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
        results = builder.build(project, args.targets)
    except builder.BuildError as e:
        _report_build_error(e)
        return 1
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
    for name, target in project.targets.items():
        if target.variants:
            variants = ", ".join(target.variant_names())
            print(f"  {name} [{target.kind}] variants: {variants}")
        else:
            print(f"  {name} [{target.kind}]")
    # Cross-flake (local-dep) targets, recursively flattened. Shown with the
    # dotted CLI form so the user can copy-paste into `rigx build`.
    _list_local_deps(project, prefix="")
    return 0


def _list_local_deps(project, prefix: str) -> None:
    for lname, ldep in project.local_deps.items():
        sub = ldep.sub_project
        if not sub:
            continue
        for tname, target in sub.targets.items():
            if target.kind == "script":
                continue
            qual = f"{prefix}{lname}.{tname}"
            if target.variants:
                variants = ", ".join(target.variant_names())
                print(f"  {qual} [{target.kind}, local-dep] variants: {variants}")
            else:
                print(f"  {qual} [{target.kind}, local-dep]")
        _list_local_deps(sub, prefix=f"{prefix}{lname}.")


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


def cmd_run(args: argparse.Namespace) -> int:
    """Execute a script-kind target (e.g. publish, deploy)."""
    project = _load(args)
    try:
        builder.run_named_script(project, args.target, args.script_args)
    except builder.BuildError as e:
        _report_build_error(e)
        return 1
    return 0


def cmd_uv(args: argparse.Namespace) -> int:
    """Run uv via the project's pinned nixpkgs (no host uv install needed)."""
    project = _load(args)
    try:
        return builder.run_nixpkgs_tool(project, "uv", args.uv_args)
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
    sp.set_defaults(func=cmd_build)

    sp = sub.add_parser("lock", help="update lock file")
    sp.set_defaults(func=cmd_lock)

    sp = sub.add_parser("list", help="list targets")
    sp.set_defaults(func=cmd_list)

    sp = sub.add_parser("clean", help="remove output/")
    sp.set_defaults(func=cmd_clean)

    sp = sub.add_parser("flake", help="print the generated flake.nix")
    sp.set_defaults(func=cmd_flake)

    sp = sub.add_parser(
        "run",
        help="execute a script-kind target (e.g. `rigx run publish [-- args…]`)",
    )
    sp.add_argument("target", help="script target name")
    # No script_args declared here — `main()` slices argv on the first `--`
    # so any flags after it (e.g. `rigx run pub -- --foo`) pass through verbatim.
    sp.set_defaults(func=cmd_run, script_args=[])

    sp = sub.add_parser(
        "uv",
        help="run uv via the project's pinned nixpkgs (e.g. `rigx uv lock`)",
    )
    # No arguments declared here — `main()` slices argv manually so leading
    # flags (e.g. `rigx uv --version`) pass through verbatim.
    sp.set_defaults(func=cmd_uv)

    return p


def _split_uv_passthrough(argv: list[str]) -> tuple[list[str], list[str]] | None:
    """If `uv` appears as a subcommand, split argv into (pre, uv_args).

    argparse.REMAINDER is finicky about leading flags in subparsers, so we
    pre-slice here: everything after the first `uv` token is forwarded to uv
    verbatim. Returns None if there's no `uv` subcommand in argv.
    """
    try:
        i = argv.index("uv")
    except ValueError:
        return None
    # Make sure `uv` isn't the *value* of a preceding option like `-C uv`.
    prev = argv[i - 1] if i > 0 else ""
    value_taking_opts = {"-C", "--project"}
    if prev in value_taking_opts:
        return None
    return argv[: i + 1], argv[i + 1 :]


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

    uv_split = _split_uv_passthrough(raw)
    run_split = _split_run_passthrough(raw)
    if uv_split is not None:
        pre, uv_args = uv_split
        args = parser.parse_args(pre)
        args.uv_args = uv_args
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
