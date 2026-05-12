"""`rigx clean` — remove the project's output/ tree."""

from __future__ import annotations

import argparse

from rigx import builder


def cmd_clean(args: argparse.Namespace) -> int:
    from rigx import cli
    project = cli._load(args)
    builder.clean(project)
    print("output/ cleaned")
    return 0
