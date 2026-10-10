"""`rigx host` — inspect and clean up a deploy target, with no project.

Every other command in rigx starts from a `rigx.toml`. These two
deliberately don't, because the situations that need them are exactly
the ones where the config is not what it should be: the project was
lost, renamed, or moved to a different machine; a testbed was deployed
from a checkout nobody can find; or a host has accumulated deployments
from several projects and nothing local knows the whole list.

The information is all there on the host already — a testbed's Nix
profile under `/nix/var/nix/profiles/rigx-<name>` carries the manifest
of whichever generation is live, and the container engine knows what is
actually running. `rigx host ls` reads it back, and `rigx host destroy`
removes one testbed by name. Neither needs to be told anything a
`rigx.toml` would have said.

That matters more than convenience. `rigx remote <target> stop` can
only address a testbed the local project still declares, so before
this, a deployment whose project had gone missing could not be shut
down through rigx at all — the operator was left doing it by hand with
`systemctl` and `podman`, which is how half-removed deployments (unit
gone, container still holding the port) come about in the first place.

`destroy` keeps `/var/lib/rigx/<testbed>` unless `--purge` is passed:
removing a deployment from a host says nothing about whether its
database was wanted. See `rigx.deploy.hostinv.teardown`.
"""

from __future__ import annotations

import argparse
import sys

from rigx.deploy import bootstrap, hostinv
from rigx.deploy.bootstrap import DeployError


def _port_summary(svc: dict) -> str:
    """`80->8000, 127.0.0.1:5432->5432` for one manifest service entry."""
    out = []
    for p in svc.get("ports") or []:
        spec = p.get("publish") or f"{p.get('host')}:{p.get('container')}"
        host, _, container = spec.rpartition(":")
        out.append(f"{host}->{container}" if host else spec)
    return ", ".join(out)


def _container_summary(published) -> str:
    if not published:
        return ""
    return ", ".join(
        f"{addr}:{port}" if addr else str(port)
        for addr, port in sorted(published, key=lambda ap: (ap[1], ap[0]))
    )


def cmd_host_ls(args: argparse.Namespace) -> int:
    try:
        target = bootstrap.parse_ssh_url(args.ssh_url)
        testbeds, containers = hostinv.inventory(target)
    except DeployError as e:
        print(f"rigx host ls: {e}", file=sys.stderr)
        return 1

    by_name = {c.name: c for c in containers}
    print(f"{target.destination}: {len(testbeds)} testbed(s)")
    for info in testbeds:
        print()
        print(info.name)
        print(f"  generation  {info.generation or '(unreadable)'}")
        manifest = info.manifest
        if manifest is None:
            # A profile with no readable manifest is a real state, not a
            # bug to hide: an interrupted deploy, or a generation whose
            # store path has been garbage-collected out from under it.
            print("  (no readable manifest for the live generation)")
            continue
        for sname in manifest.get("start_order") or sorted(manifest.get("services", {})):
            svc = manifest["services"][sname]
            c = by_name.get(f"rigx-{info.name}-{sname}")
            state = "running" if (c and c.running) else (c.state if c else "no container")
            ports = _port_summary(svc)
            # Explicit separators, not just padding: "no container" is
            # exactly the pad width, so a padded field would run
            # straight into the ports and read as one token.
            print(f"  {sname:<12}  {state:<13} {ports}".rstrip())

    # Containers rigx named but no testbed profile claims. This is the
    # shape a half-removed deployment leaves behind, and the reason the
    # listing shows containers at all rather than just profiles.
    # Exact names from each testbed's live manifest; a name prefix would
    # hide testbed `app-staging`'s orphans behind testbed `app`. A
    # testbed whose manifest can't be read claims its prefix, as the
    # best guess available.
    owned: set[str] = set()
    unreadable: list[str] = []
    for info in testbeds:
        if info.manifest is None:
            unreadable.append(f"rigx-{info.name}-")
        else:
            owned |= hostinv.service_containers(
                info.name, info.manifest.get("services") or {},
            )
    orphans = [
        c for c in containers
        if c.name.startswith("rigx-") and c.name not in owned
        and not c.name.startswith(tuple(unreadable))
    ]
    if orphans:
        print()
        print("orphaned rigx containers (no testbed profile on this host):")
        for c in sorted(orphans, key=lambda c: c.name):
            ports = _container_summary(c.published)
            print(f"  {c.name:<40}  {c.state:<13} {ports}".rstrip())
        print()
        print(
            "These hold their published ports whether or not anything "
            "supervises them. Remove one with:\n"
            "  rigx host destroy <ssh_url> <testbed>"
        )
    return 0


def cmd_host_destroy(args: argparse.Namespace) -> int:
    try:
        target = bootstrap.parse_ssh_url(args.ssh_url)
        present = hostinv.list_testbeds(target)
        if args.testbed not in present:
            # Not an error — teardown is idempotent, and a testbed whose
            # profile is already gone can still have units or containers
            # left behind, which is precisely what needs cleaning up.
            print(
                f"rigx host destroy: {args.testbed!r} has no Nix profile on "
                f"{target.destination}"
                + (f" (found: {', '.join(present)})" if present else "")
                + " — cleaning up any leftover units and containers anyway",
                file=sys.stderr,
            )
        hostinv.teardown(target, args.testbed, purge=args.purge)
    except DeployError as e:
        print(f"rigx host destroy: {e}", file=sys.stderr)
        return 1

    kept = "" if args.purge else " (persistent volumes kept)"
    print(
        f"[rigx host destroy] {args.testbed!r} removed from "
        f"{target.destination}{kept}"
    )
    if not args.purge:
        print(
            f"[rigx host destroy] state left at "
            f"{hostinv.STATE_ROOT}/{args.testbed} — pass --purge to remove it"
        )
    return 0
