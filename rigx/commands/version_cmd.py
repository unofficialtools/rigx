"""`rigx version` — print the package version."""

from __future__ import annotations

import argparse

from rigx import __version__


def cmd_version(args: argparse.Namespace) -> int:
    print(f"rigx {__version__}")
    return 0
