"""`rigx fmt` — canonicalize rigx.toml."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from rigx import fmt


def cmd_fmt(args: argparse.Namespace) -> int:
    root = Path(args.project) if args.project else Path.cwd()
    toml_path = root / "rigx.toml"
    if not toml_path.is_file():
        print(f"rigx: no rigx.toml at {toml_path}", file=sys.stderr)
        return 1
    try:
        canonical = fmt.format_file(toml_path, write=args.write)
    except fmt.FmtError as e:
        print(f"rigx: {toml_path}: {e}", file=sys.stderr)
        return 1
    if not args.write:
        sys.stdout.write(canonical)
    return 0
