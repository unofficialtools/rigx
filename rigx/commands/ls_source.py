"""`rigx ls-source` — print the resolved file set for a target's src."""

from __future__ import annotations

import argparse
import sys

from rigx import sources
from rigx.commands.helpers import format_bytes


def cmd_ls_source(args: argparse.Namespace) -> int:
    """Print the resolved per-target source set, one path per line, with
    a `<count> files, <bytes> total` summary on stderr.
    """
    from rigx import cli
    project = cli._load(args)
    if not sources.project_filtering_enabled(project):
        print(
            "rigx: ls-source requires `[project].sources = [...]` to be set "
            "in rigx.toml — without it, every target's src is the whole "
            "tree minus a basename blacklist (legacy behavior).",
            file=sys.stderr,
        )
        return 1
    if args.target not in project.targets:
        print(
            f"rigx: target {args.target!r} not found in project",
            file=sys.stderr,
        )
        return 1
    target = project.targets[args.target]
    try:
        files = sources.compute_target_files(project, target)
    except ValueError as e:
        print(f"rigx: {e}", file=sys.stderr)
        return 1
    total_bytes = 0
    for rel in files:
        print(rel)
        try:
            total_bytes += (project.root / rel).stat().st_size
        except OSError:
            pass
    print(
        f"\n{len(files)} files, {format_bytes(total_bytes)} total.",
        file=sys.stderr,
    )
    return 0
