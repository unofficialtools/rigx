"""`rigx lock` — update flake.lock for the project's pinned inputs."""

from __future__ import annotations

import argparse

from rigx import builder
from rigx.commands.helpers import report_build_error


def cmd_lock(args: argparse.Namespace) -> int:
    from rigx import cli
    project = cli._load(args)
    try:
        if args.pin:
            rev = builder.pin_nixpkgs(project)
            print(f"pinned nixpkgs {project.nixpkgs_ref} to {rev} in rigx.toml")
        builder.update_lock(project)
    except builder.BuildError as e:
        report_build_error(e)
        return 1
    print("lock updated")
    return 0
