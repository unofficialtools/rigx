"""`rigx flake` — print the generated flake.nix to stdout."""

from __future__ import annotations

import argparse
import sys

from rigx import config, nix_gen


def cmd_flake(args: argparse.Namespace) -> int:
    from rigx import cli
    project = cli._load(args)
    # `rigx flake` prints the entire flake — every consumer is in
    # scope, so resolve every lazy `[external_inputs.*]` now.
    config.resolve_external_inputs(project, None)
    sys.stdout.write(nix_gen.generate(project))
    return 0
