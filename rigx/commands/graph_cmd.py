"""`rigx graph` — print a Mermaid dependency graph for a target."""

from __future__ import annotations

import argparse
import sys

from rigx import graph


def cmd_graph(args: argparse.Namespace) -> int:
    from rigx import cli
    project = cli._load(args)
    try:
        sys.stdout.write(graph.mermaid(project, args.target))
    except ValueError as e:
        print(f"rigx: {e}", file=sys.stderr)
        return 1
    return 0
