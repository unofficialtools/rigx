"""`rigx run` — execute a script or testbed target."""

from __future__ import annotations

import argparse

from rigx import builder
from rigx.commands.helpers import report_build_error


def cmd_run(args: argparse.Namespace) -> int:
    from rigx import cli
    project = cli._load(args)
    try:
        builder.run_named_script(project, args.target, args.script_args)
    except builder.BuildError as e:
        report_build_error(e)
        return 1
    return 0
