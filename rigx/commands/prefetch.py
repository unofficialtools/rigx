"""`rigx prefetch` — fetch everything an offline build needs."""

from __future__ import annotations

import argparse

from rigx import builder
from rigx.commands.helpers import report_build_error


def cmd_prefetch(args: argparse.Namespace) -> int:
    from rigx import cli
    project = cli._load(args)
    try:
        n = builder.prefetch(project)
    except builder.BuildError as e:
        report_build_error(e)
        return 1
    print(
        f"prefetched {n} store path(s); `rigx --offline ...` can now build "
        f"this project without a network"
    )
    return 0
