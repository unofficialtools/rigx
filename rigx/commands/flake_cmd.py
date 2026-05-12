"""`rigx flake` — print the generated flake.nix to stdout."""

from __future__ import annotations

import argparse
import sys

from rigx import nix_gen


def cmd_flake(args: argparse.Namespace) -> int:
    from rigx import cli
    project = cli._load(args)
    sys.stdout.write(nix_gen.generate(project))
    return 0
