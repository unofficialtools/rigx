"""Command-line interface for rigx — argparse wiring only.

Each `cmd_<name>` lives in `rigx.commands.<name>`. They are re-exported
here so existing callers (`rigx.cli.cmd_build`, `rigx.cli._load`,
`rigx.cli._build_hint_lines`, `rigx.cli._format_bytes`) continue to work.
"""

from __future__ import annotations

import argparse
import sys

from rigx import __version__, config
from rigx.commands.build import cmd_build
from rigx.commands.clean import cmd_clean
from rigx.commands.flake_cmd import cmd_flake
from rigx.commands.fmt_cmd import cmd_fmt
from rigx.commands.graph_cmd import cmd_graph
from rigx.commands.helpers import (
    attr_to_target as _attr_to_target,
    build_hint_lines as _build_hint_lines,
    find_project_root as _find_project_root,
    format_bytes as _format_bytes,
    report_build_error as _report_build_error,
)


def _load(args):
    """Resolve `args.project` to a loaded `Project`.

    Defined here (rather than re-exported from `commands.helpers`) so
    `mock.patch("rigx.cli._load", ...)` in tests overrides every command
    handler's project load — each `commands/<x>.cmd_*` does a late
    `from rigx import cli; cli._load(args)` so the lookup hits this
    module's namespace at call time, not import time."""
    from pathlib import Path
    root = Path(args.project) if args.project else _find_project_root(Path.cwd())
    return config.load(root)
from rigx.commands.host_cmd import cmd_host_destroy, cmd_host_ls
from rigx.commands.list_cmd import cmd_list
from rigx.commands.lock import cmd_lock
from rigx.commands.ls_source import cmd_ls_source
from rigx.commands.new import cmd_new
from rigx.commands.pkg import cmd_pkg
from rigx.commands.remote_cmd import (
    cmd_remote_deploy,
    cmd_remote_restart,
    cmd_remote_rollback,
    cmd_remote_start,
    cmd_remote_stop,
)
from rigx.commands.run_cmd import cmd_run
from rigx.commands.test_cmd import cmd_test
from rigx.commands.version_cmd import cmd_version
from rigx.commands.watch import cmd_watch


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="rigx", description="Nix-backed build system")
    p.add_argument(
        "-C",
        "--project",
        help="path to project root (containing rigx.toml)",
        default=None,
    )
    p.add_argument(
        "-V",
        "--version",
        action="version",
        version=f"rigx {__version__}",
        help="print the rigx version and exit",
    )
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("version", help="print the rigx version")
    sp.set_defaults(func=cmd_version)

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
        help="run up to N targets concurrently (each via its own `nix build` "
             "call; the Nix daemon dedupes shared deps)",
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
    sp.add_argument(
        "-j",
        "--jobs",
        type=int,
        default=None,
        metavar="N",
        help="run up to N tests concurrently; tests with `exclusive = true` "
             "still run alone (default: sequential)",
    )
    sp.set_defaults(func=cmd_test)

    sp = sub.add_parser(
        "watch",
        help="rebuild on source change (polling, Ctrl-C to stop)",
    )
    sp.add_argument("targets", nargs="*", help="target[@variant] selectors")
    sp.add_argument(
        "--watch-all",
        action="store_true",
        dest="watch_all",
        help="watch every file under the project root (legacy behavior); "
             "by default rigx only watches each target's declared inputs",
    )
    sp.set_defaults(func=cmd_watch)

    sp = sub.add_parser(
        "new",
        help="scaffold a new target (appends to rigx.toml, writes stub sources)",
    )
    sp.add_argument(
        "kind",
        choices=[
            "executable", "static_library", "python_script",
            "custom", "script", "run", "test", "testbed",
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
        help="execute a script or testbed target "
             "(e.g. `rigx run publish [-- args…]`)",
    )
    sp.add_argument("target", help="script or testbed target name")
    sp.set_defaults(func=cmd_run, script_args=[])

    sp = sub.add_parser(
        "ls-source",
        help="print the resolved file list for a target's `src` "
             "(requires [project].sources to be set)",
    )
    sp.add_argument("target", help="target name")
    sp.set_defaults(func=cmd_ls_source)

    sp = sub.add_parser(
        "pkg",
        help="run a nixpkgs binary (e.g. `rigx pkg uv -- lock`)",
    )
    sp.add_argument("attr", help="nixpkgs attr name (uv, jq, ripgrep, …)")
    sp.set_defaults(func=cmd_pkg, pkg_args=[])

    sp = sub.add_parser(
        "remote",
        help="manage a declarative testbed deployment (experimental)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  rigx remote myapp deploy      # uses ssh_url from rigx.toml\n"
            "  rigx remote myapp stop\n"
            "  rigx remote myapp start\n"
            "  rigx remote myapp rollback\n"
            "  rigx remote myapp deploy ssh://deploy@203.0.113.10  # override"
        ),
    )
    sp.add_argument(
        "target",
        help="testbed target name (kind='testbed' with a 'services' section)",
    )
    remote_sub = sp.add_subparsers(dest="remote_command", required=True)

    def _add_ssh_url_arg(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "ssh_url",
            nargs="?",
            default=None,
            help=(
                "ssh://[user@]host[:port] deploy destination. Optional: "
                "overrides the target's `ssh_url` in rigx.toml, and is "
                "required only when the target doesn't declare one"
            ),
        )

    dp = remote_sub.add_parser(
        "deploy",
        help="bootstrap a target host and deploy a testbed to it",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  rigx remote myapp deploy      # uses ssh_url from rigx.toml\n"
            "  rigx remote myapp deploy ssh://deploy@203.0.113.10\n"
            "  rigx remote myapp deploy ssh://deploy@203.0.113.10:2222"
        ),
    )
    _add_ssh_url_arg(dp)
    dp.set_defaults(func=cmd_remote_deploy)

    for action, func in (
        ("start", cmd_remote_start),
        ("stop", cmd_remote_stop),
        ("restart", cmd_remote_restart),
    ):
        lp = remote_sub.add_parser(
            action,
            help=f"{action} an already-deployed testbed's services",
            formatter_class=argparse.RawDescriptionHelpFormatter,
            epilog=(
                "examples:\n"
                f"  rigx remote myapp {action}\n"
                f"  rigx remote myapp {action} ssh://deploy@203.0.113.10"
            ),
        )
        _add_ssh_url_arg(lp)
        lp.set_defaults(func=func)

    rp = remote_sub.add_parser(
        "rollback",
        help="roll an already-deployed testbed back to its previous generation",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  rigx remote myapp rollback\n"
            "  rigx remote myapp rollback ssh://deploy@203.0.113.10"
        ),
    )
    _add_ssh_url_arg(rp)
    rp.set_defaults(func=cmd_remote_rollback)

    # `host` rather than another `remote` action: these address a
    # machine, not a target, so they take no target name and read no
    # rigx.toml — which is the point, since the case that needs them is
    # a host whose project is missing. See rigx.commands.host_cmd.
    sp = sub.add_parser(
        "host",
        help="inspect or clean up a deploy target (no rigx.toml needed)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  rigx host ls ssh://root@example.com\n"
            "  rigx host destroy ssh://root@example.com old_app\n"
            "  rigx host destroy ssh://root@example.com old_app --purge"
        ),
    )
    host_sub = sp.add_subparsers(dest="host_command", required=True)

    hp = host_sub.add_parser(
        "ls",
        help="list the testbeds and rigx containers a host is carrying",
    )
    hp.add_argument("ssh_url", help="ssh://[user@]host[:port]")
    hp.set_defaults(func=cmd_host_ls)

    hp = host_sub.add_parser(
        "destroy",
        help="remove one testbed (units, containers, network, profile) from a host",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Persistent volumes under /var/lib/rigx/<testbed> are kept "
            "unless --purge is given."
        ),
    )
    hp.add_argument("ssh_url", help="ssh://[user@]host[:port]")
    hp.add_argument("testbed", help="testbed name as `rigx host ls` reports it")
    hp.add_argument(
        "--purge",
        action="store_true",
        help="also delete the testbed's persistent volumes (irreversible)",
    )
    hp.set_defaults(func=cmd_host_destroy)

    return p


def _subcommand_index(argv: list[str]) -> int | None:
    """Index of the actual subcommand token: the first token that is
    neither a global flag nor a global flag's value (`-C`/`--project`
    take a value). Returns None when argv holds no subcommand."""
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok in ("-C", "--project"):
            i += 2  # skip the flag and its value
            continue
        if tok.startswith("-") and tok != "-":
            # -V/--version/-h/--help, or inline forms -Cdir / --project=dir.
            i += 1
            continue
        return i
    return None


def _split_pkg_passthrough(argv: list[str]) -> tuple[list[str], list[str]] | None:
    """If `rigx pkg <attr> …` is invoked, split off the trailing args for
    forwarding to the nixpkgs binary."""
    i = _subcommand_index(argv)
    if i is None or argv[i] != "pkg":
        return None
    if i + 1 >= len(argv):
        return None  # `rigx pkg` with no attr — let argparse error normally.
    pre = argv[: i + 2]              # rigx … pkg <attr>
    forward = argv[i + 2 :]
    if forward and forward[0] == "--":
        forward = forward[1:]
    return pre, forward


def _split_run_passthrough(argv: list[str]) -> tuple[list[str], list[str]] | None:
    """If `rigx run TARGET -- …` is invoked, split off the post-`--` tail."""
    i = _subcommand_index(argv)
    if i is None or argv[i] != "run":
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
    except KeyboardInterrupt:
        print("\n[rigx] interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
