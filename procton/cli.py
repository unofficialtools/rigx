"""Command-line interface for procton."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from procton import builder, config, nix_gen


def _find_project_root(start: Path) -> Path:
    cur = start.resolve()
    for candidate in [cur, *cur.parents]:
        if (candidate / "procton.toml").is_file():
            return candidate
    raise SystemExit(
        "procton.toml not found in the current directory or any parent"
    )


def _load(args: argparse.Namespace) -> config.Project:
    root = Path(args.project) if args.project else _find_project_root(Path.cwd())
    return config.load(root)


def cmd_build(args: argparse.Namespace) -> int:
    project = _load(args)
    try:
        results = builder.build(project, args.targets)
    except builder.BuildError as e:
        print(f"procton: {e}", file=sys.stderr)
        return 1
    for attr, link in results:
        print(f"  {attr} -> {link}")
    return 0


def cmd_lock(args: argparse.Namespace) -> int:
    project = _load(args)
    try:
        builder.update_lock(project)
    except builder.BuildError as e:
        print(f"procton: {e}", file=sys.stderr)
        return 1
    print("lock updated")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    project = _load(args)
    for name, target in project.targets.items():
        if target.variants:
            variants = ", ".join(target.variant_names())
            print(f"  {name} [{target.kind}] variants: {variants}")
        else:
            print(f"  {name} [{target.kind}]")
    return 0


def cmd_clean(args: argparse.Namespace) -> int:
    project = _load(args)
    builder.clean(project)
    print("output/ cleaned")
    return 0


def cmd_flake(args: argparse.Namespace) -> int:
    """Print the generated flake.nix to stdout (useful for debugging)."""
    project = _load(args)
    sys.stdout.write(nix_gen.generate(project))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="procton", description="Nix-backed build system")
    p.add_argument(
        "-C",
        "--project",
        help="path to project root (containing procton.toml)",
        default=None,
    )
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("build", help="build targets (default: all)")
    sp.add_argument("targets", nargs="*", help="target[@variant] selectors")
    sp.set_defaults(func=cmd_build)

    sp = sub.add_parser("lock", help="update lock file")
    sp.set_defaults(func=cmd_lock)

    sp = sub.add_parser("list", help="list targets")
    sp.set_defaults(func=cmd_list)

    sp = sub.add_parser("clean", help="remove output/")
    sp.set_defaults(func=cmd_clean)

    sp = sub.add_parser("flake", help="print the generated flake.nix")
    sp.set_defaults(func=cmd_flake)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except config.ConfigError as e:
        print(f"procton: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
