"""`rigx new` — scaffold a new target (appends to rigx.toml, writes stubs)."""

from __future__ import annotations

import argparse
import sys
import tomllib
from pathlib import Path

from rigx import scaffold


def cmd_new(args: argparse.Namespace) -> int:
    root = Path(args.project) if args.project else Path.cwd()
    toml_path = root / "rigx.toml"
    if not toml_path.is_file():
        print(
            f"rigx: no rigx.toml at {toml_path} — create one with a "
            f"[project] section first.",
            file=sys.stderr,
        )
        return 1
    with toml_path.open("rb") as f:
        data = tomllib.load(f)
    if args.name in data.get("targets", {}):
        print(
            f"rigx: target {args.name!r} already exists in {toml_path}",
            file=sys.stderr,
        )
        return 1
    try:
        s = scaffold.scaffold(args.kind, args.name, args.language, args.run)
    except ValueError as e:
        print(f"rigx: {e}", file=sys.stderr)
        return 1
    for rel, body in s.files.items():
        path = root / rel
        if path.exists():
            print(f"rigx: refusing to overwrite existing file {path}", file=sys.stderr)
            return 1
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
        print(f"  wrote {rel}")
    with toml_path.open("a") as f:
        f.write(s.toml_block)
    print(f"  appended [targets.{args.name}] to rigx.toml")
    return 0
