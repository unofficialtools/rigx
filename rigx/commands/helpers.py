"""Shared helpers across CLI command modules."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from rigx import builder, config


def find_project_root(start: Path) -> Path:
    cur = start.resolve()
    for candidate in [cur, *cur.parents]:
        if (candidate / "rigx.toml").is_file():
            return candidate
    raise SystemExit(
        "rigx.toml not found in the current directory or any parent"
    )


def load(args: argparse.Namespace) -> config.Project:
    root = Path(args.project) if args.project else find_project_root(Path.cwd())
    return config.load(root)


def report_build_error(e: builder.BuildError) -> None:
    if isinstance(e, builder.NixNotFoundError):
        print(str(e), file=sys.stderr)
    else:
        print(f"rigx: {e}", file=sys.stderr)


def attr_to_target(project: config.Project, attr: str) -> config.Target | None:
    """Reverse-map a Nix attr (`hello`, `hello-debug`, `frontend_app`) to
    the rigx Target it was built from."""
    for name, target in project.targets.items():
        sanitized = name.replace(".", "_")
        if sanitized == attr:
            return target
        for vname in target.variants:
            if f"{sanitized}-{vname}" == attr:
                return target
    return None


def build_hint_lines(
    project: config.Project, attr: str, link: Path
) -> list[str]:
    """Return per-kind "what to do with this artifact" lines printed
    underneath each `attr -> output/...` line."""
    target = attr_to_target(project, attr)
    if target is None:
        return []
    try:
        rel = Path(os.path.relpath(link, Path.cwd()))
    except ValueError:
        rel = link
    if target.kind == "executable":
        return [f"run:   {rel}/bin/{target.name}"]
    if target.kind == "python_script":
        return [f"run:   {rel}/bin/{target.name}"]
    if target.kind == "capsule":
        return [
            f"start: {rel}/bin/run-{target.name}",
            f"shell: {rel}/bin/shell-{target.name}",
        ]
    return []


def format_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024
    return f"{n} B"
