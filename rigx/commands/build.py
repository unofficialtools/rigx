"""`rigx build` — build one or more targets."""

from __future__ import annotations

import argparse
import json

from rigx import builder
from rigx.commands.helpers import build_hint_lines, report_build_error


def cmd_build(args: argparse.Namespace) -> int:
    from rigx import cli  # late import: avoid cli ↔ commands cycle
    project = cli._load(args)
    try:
        results = builder.build(project, args.targets, jobs=args.jobs)
    except builder.BuildError as e:
        report_build_error(e)
        return 1
    if args.json:
        print(json.dumps(
            [{"attr": a, "output": str(p)} for a, p in results],
            indent=2,
        ))
    else:
        for attr, link in results:
            print(f"  {attr} -> {link}")
            for hint in build_hint_lines(project, attr, link):
                print(f"      {hint}")
    return 0
