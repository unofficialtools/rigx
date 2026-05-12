"""`rigx list` — list project (and local-dep) targets."""

from __future__ import annotations

import argparse
import sys

def _list_local_deps(project, prefix: str, kind_filter: str | None = None) -> int:
    shown = 0
    for lname, ldep in project.local_deps.items():
        sub = ldep.sub_project
        if not sub:
            continue
        for tname, target in sub.targets.items():
            if target.kind in ("script", "testbed"):
                continue
            if kind_filter and target.kind != kind_filter:
                continue
            qual = f"{prefix}{lname}.{tname}"
            if target.variants:
                variants = ", ".join(target.variant_names())
                print(f"  {qual} [{target.kind}, local-dep] variants: {variants}")
            else:
                print(f"  {qual} [{target.kind}, local-dep]")
            shown += 1
        shown += _list_local_deps(sub, prefix=f"{prefix}{lname}.", kind_filter=kind_filter)
    return shown


def cmd_list(args: argparse.Namespace) -> int:
    from rigx import cli
    project = cli._load(args)
    kf = args.kind
    shown = 0
    for name, target in project.targets.items():
        if kf and target.kind != kf:
            continue
        if target.variants:
            variants = ", ".join(target.variant_names())
            print(f"  {name} [{target.kind}] variants: {variants}")
        else:
            print(f"  {name} [{target.kind}]")
        shown += 1
    shown += _list_local_deps(project, prefix="", kind_filter=kf)
    if kf and shown == 0:
        print(f"  (no targets with kind={kf!r})", file=sys.stderr)
    return 0
