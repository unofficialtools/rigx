"""`rigx pkg` — run any binary from the project's pinned nixpkgs."""

from __future__ import annotations

import argparse

from rigx import builder
from rigx.commands.helpers import report_build_error


def cmd_pkg(args: argparse.Namespace) -> int:
    from rigx import cli
    project = cli._load(args)
    try:
        return builder.run_nixpkgs_tool(project, args.attr, args.pkg_args)
    except builder.BuildError as e:
        report_build_error(e)
        return 1
