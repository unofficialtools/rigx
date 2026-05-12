"""`rigx test` — discover and run `kind = "test"` targets."""

from __future__ import annotations

import argparse
import sys

from rigx import builder
from rigx.commands.helpers import report_build_error


def cmd_test(args: argparse.Namespace) -> int:
    from rigx import cli
    project = cli._load(args)
    try:
        results = builder.run_tests(
            project, args.targets or None, jobs=args.jobs,
        )
    except builder.BuildError as e:
        report_build_error(e)
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
