"""`rigx pkg` — run any binary from the project's pinned nixpkgs."""

from __future__ import annotations

import argparse
import sys

from rigx import builder
from rigx.commands.helpers import report_build_error


def cmd_pkg(args: argparse.Namespace) -> int:
    from rigx import cli
    project = cli._load(args)
    try:
        rc = builder.run_nixpkgs_tool(project, args.attr, args.pkg_args)
    except builder.BuildError as e:
        report_build_error(e)
        return 1
    if rc != 0:
        hint = builder.network_hint(project, offline_note=False)
        if hint:
            print(hint.lstrip("\n"), file=sys.stderr)
    return rc
