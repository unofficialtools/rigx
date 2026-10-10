"""Host-level inventory and teardown for rigx deployments.

Everything else under `rigx.deploy` answers questions about a
*project*: this testbed, its services, its plan. This module answers
questions about a *host* — which testbeds it carries, which containers
are actually running on it, and who currently holds a given host port —
and it does so without needing the `rigx.toml` that put them there.

That independence is the point. A testbed's name, its units, its
containers and its Nix profile all live on the target; the local
project is only ever the thing that *wrote* them. Recovering a host
whose project has been lost, renamed, or moved to another machine
should not require reconstructing the config first, so every function
here is driven by what the host reports about itself.

Three problems motivated it, all observed together on one host:

  * `rigx remote <t> stop` stopped the units but left the containers
    running. Killing an attached `podman run` does not reliably stop
    the container it started (the same asymmetry
    `rigx.nix.capsule_common`'s runner already guards against on the
    *start* side), so `--rm` never fired and the published ports stayed
    bound by a container whose supervisor was long gone.

  * A second testbed deploying to the same host then failed to bind its
    ports. The container engine reports that as a plain "address
    already in use" at *runtime*, so the deploy surfaced it as twenty
    failed health-check attempts against a service that was in fact
    perfectly healthy — the least informative form the failure could
    possibly take. `find_port_conflicts` turns it into a preflight
    error that names the container holding the port.

  * The reverse case is worse and is the reason the check is not merely
    cosmetic: a host-side health probe hits a port, not a process. If
    the stale container had answered the probe successfully, the deploy
    would have gone *green* while the previous generation was still the
    one serving traffic.

`teardown` is the shared primitive behind both `rigx host destroy` and
`host_exclusive` deploys. It deliberately does not touch
`/var/lib/rigx/<testbed>/volumes` — evicting a deployment is a
statement about what should be *running*, never about the state it
accumulated (see the content-vs-state split in `rigx.deploy.units`).
Callers that really do want the data gone pass `purge=True`.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from rigx.deploy.bootstrap import DeployError, SSHTarget, run_remote

PROFILE_DIR = "/nix/var/nix/profiles"
STATE_ROOT = "/var/lib/rigx"

# Addresses that mean "every interface", and therefore collide with any
# other bind on the same port rather than only with an identical one.
_WILDCARD_ADDRS = {"", "0.0.0.0", "::", "[::]", "*"}

# Generation links a Nix profile keeps beside the profile symlink
# itself (`rigx-app-4-link`). Only the bare `rigx-<name>` symlink names
# a testbed.
_GENERATION_LINK = re.compile(r"-\d+-link$")

# Testbed names reach a remote root shell (as glob fragments and as an
# engine `--filter name=` regex), so they are checked rather than
# trusted. rigx target names are already this alphabet; this is the
# assertion that they stay that way.
_SAFE_NAME = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_.-]*\Z")


def _check_name(name: str) -> str:
    if not _SAFE_NAME.match(name):
        raise DeployError(
            f"refusing to act on testbed name {name!r}: expected only "
            f"letters, digits, '_', '.' and '-'"
        )
    return name


# ---------------------------------------------------------------------------
# Containers
# ---------------------------------------------------------------------------

# `|` rather than a tab: podman and docker both post-process `\t` in
# --format strings, but neither can produce a literal `|` inside a
# container name or a port list, so splitting is unambiguous without
# relying on that post-processing.
_PS_FORMAT = "{{.Names}}|{{.State}}|{{.Ports}}"
_PS_COMMAND = (
    f"sudo podman ps -a --format '{_PS_FORMAT}' 2>/dev/null || "
    f"sudo docker ps -a --format '{_PS_FORMAT}' 2>/dev/null || true"
)


@dataclass(frozen=True)
class Container:
    """One container as the engine reports it."""

    name: str
    state: str
    # (address, port) pairs published to the host. The address is "" when
    # the engine reported none, which means every interface.
    published: frozenset[tuple[str, int]] = field(default_factory=frozenset)

    @property
    def running(self) -> bool:
        return self.state.lower().startswith(("running", "up"))

    def testbed(self) -> str | None:
        """The testbed this container belongs to, or None if it isn't a
        rigx service container.

        Deliberately not derived by splitting on `-`: a container is
        named `rigx-<testbed>-<service>` and both halves may contain
        dashes, so the split is ambiguous in general. Callers ask
        `belongs_to(name)` instead, which is the question they actually
        have; this is only for display.
        """
        if not self.name.startswith("rigx-"):
            return None
        rest = self.name[len("rigx-"):]
        return rest.rsplit("-", 1)[0] if "-" in rest else None

    def belongs_to(self, testbed: str) -> bool:
        return self.name.startswith(f"rigx-{testbed}-")


def parse_published(ports_field: str) -> frozenset[tuple[str, int]]:
    """Parse an engine `{{.Ports}}` field into published (addr, port) pairs.

    Handles the shapes both engines emit: `0.0.0.0:80->8000/tcp`,
    `[::]:80->8000/tcp`, `:::80->8000/tcp`, host ranges
    (`0.0.0.0:8000-8002->8000-8002/tcp`), several entries separated by
    commas, and bare `8000/tcp` exposures — which publish nothing to the
    host and are therefore skipped, since they cannot collide.
    """
    out: set[tuple[str, int]] = set()
    for chunk in ports_field.split(","):
        chunk = chunk.strip()
        if "->" not in chunk:
            continue  # exposed-only, not published: no host port to clash on
        left = chunk.split("->", 1)[0]
        addr, sep, hostpart = left.rpartition(":")
        if not sep:
            addr, hostpart = "", left
        lo_s, _, hi_s = hostpart.partition("-")
        try:
            lo = int(lo_s)
            hi = int(hi_s) if hi_s else lo
        except ValueError:
            continue
        if not (0 < lo <= hi <= 65535):
            continue
        for port in range(lo, hi + 1):
            out.add((addr, port))
    return frozenset(out)


def list_containers(target: SSHTarget) -> list[Container]:
    """Every container on `target`, rigx-owned or not.

    Not filtered to rigx: a port held by somebody's hand-started nginx
    breaks a deploy exactly as thoroughly as one held by a stale rigx
    container, and naming the actual holder is the whole value of the
    check.
    """
    proc = run_remote(target, _PS_COMMAND, check=False)
    out: list[Container] = []
    for line in proc.stdout.splitlines():
        parts = line.split("|")
        if len(parts) < 3:
            continue
        name, state, ports = parts[0].strip(), parts[1].strip(), parts[2].strip()
        if not name:
            continue
        out.append(Container(name=name, state=state, published=parse_published(ports)))
    return out


# ---------------------------------------------------------------------------
# Port conflicts
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PortConflict:
    port: int
    service: str        # our service that wants the port
    holder: str         # the container name already holding it
    holder_testbed: str | None


def _addrs_overlap(a: str, b: str) -> bool:
    if a in _WILDCARD_ADDRS or b in _WILDCARD_ADDRS:
        return True
    return a.strip("[]") == b.strip("[]")


def declared_host_ports(testbed) -> dict[str, list[tuple[str, int]]]:
    """The (addr, port) pairs each of `testbed`'s services publishes to
    the host, keyed by service name."""
    out: dict[str, list[tuple[str, int]]] = {}
    for sname, svc in testbed.services.items():
        pairs = [(p.host_addr or "", p.host) for p in svc.ports]
        if pairs:
            out[sname] = pairs
    return out


def find_port_conflicts(
    containers: list[Container], testbed_name: str, wanted: dict[str, list[tuple[str, int]]],
) -> list[PortConflict]:
    """Which of `wanted`'s host ports are already held by a container
    that isn't ours.

    Only *running* containers count. A stopped container holds no port,
    and a redeploy routinely finds its own previous generation's
    container in that state — erroring on it would block the most
    ordinary operation there is.

    Our own running containers are skipped too, for the same reason from
    the other direction: on a redeploy the service being replaced still
    holds the port at preflight time, and `activate` restarting the unit
    is precisely what frees it.
    """
    conflicts: list[PortConflict] = []
    for c in containers:
        if not c.running or c.belongs_to(testbed_name):
            continue
        for sname, pairs in sorted(wanted.items()):
            for addr, port in pairs:
                for held_addr, held_port in sorted(c.published):
                    if held_port == port and _addrs_overlap(addr, held_addr):
                        conflicts.append(PortConflict(
                            port=port, service=sname, holder=c.name,
                            holder_testbed=c.testbed(),
                        ))
                        break
    return conflicts


def format_conflicts(testbed_name: str, conflicts: list[PortConflict]) -> str:
    """Render conflicts as the error a deploy dies with.

    Says who holds the port and what to do about it, because the
    alternative this replaces — a health check timing out against a
    service that never got to bind — says neither.
    """
    lines = [
        f"testbed {testbed_name!r} cannot deploy: host port(s) already in "
        f"use on the target",
    ]
    for c in sorted(conflicts, key=lambda c: (c.port, c.service)):
        owner = (
            f"testbed {c.holder_testbed!r}" if c.holder_testbed
            else "a container not managed by rigx"
        )
        lines.append(
            f"  port {c.port} (service {c.service!r}) is held by container "
            f"{c.holder!r} — {owner}"
        )
    others = sorted({c.holder_testbed for c in conflicts if c.holder_testbed})
    if others:
        lines.append("")
        lines.append(
            "Free the port by taking the other deployment down "
            + ", ".join(f"(`rigx host destroy <ssh_url> {n}`)" for n in others)
            + f", or set `host_exclusive = true` on [targets.{testbed_name}] "
            f"to have deploys evict other rigx testbeds automatically."
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Testbed inventory
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TestbedInfo:
    name: str
    generation: str            # store path the profile points at ("" if unreadable)
    manifest: dict | None      # the live generation's manifest.json, if readable


def parse_profile_names(listing: str) -> list[str]:
    """Testbed names out of a `ls /nix/var/nix/profiles` listing.

    Keeps `rigx-<name>` and drops the `rigx-<name>-<n>-link` generation
    links that sit beside it.
    """
    names = []
    for entry in listing.split():
        if not entry.startswith("rigx-") or _GENERATION_LINK.search(entry):
            continue
        name = entry[len("rigx-"):]
        if name:
            names.append(name)
    return sorted(set(names))


def list_testbeds(target: SSHTarget) -> list[str]:
    """Every testbed with a Nix profile on `target` — the host's own
    record of what has been deployed to it, needing no local project."""
    proc = run_remote(target, f"ls -1 {PROFILE_DIR} 2>/dev/null || true", check=False)
    return parse_profile_names(proc.stdout)


def read_manifest(target: SSHTarget, testbed_name: str) -> dict | None:
    """The live generation's `manifest.json` for one testbed, or None."""
    _check_name(testbed_name)
    proc = run_remote(
        target, f"cat {PROFILE_DIR}/rigx-{testbed_name}/manifest.json 2>/dev/null",
        check=False,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None


def inventory(target: SSHTarget) -> tuple[list[TestbedInfo], list[Container]]:
    """Everything rigx put on `target`: its testbeds (with the manifest
    of whichever generation is live) and every container on the host."""
    infos = []
    for name in list_testbeds(target):
        gen = run_remote(
            target, f"readlink -f {PROFILE_DIR}/rigx-{name} 2>/dev/null || true",
            check=False,
        ).stdout.strip()
        infos.append(TestbedInfo(
            name=name, generation=gen, manifest=read_manifest(target, name),
        ))
    return infos, list_containers(target)


# ---------------------------------------------------------------------------
# Teardown
# ---------------------------------------------------------------------------


def teardown_script(testbed_name: str, *, purge: bool = False) -> str:
    """The sh that removes one testbed from a host.

    Ordered so nothing can restart behind us: units are disabled (which
    also stops them) before their files go, containers are reaped after
    the supervisor that would have restarted them is gone, and the Nix
    profile — the marker `rigx remote` reads to decide a testbed is
    deployed at all — is unlinked last, so an interrupted teardown
    leaves the testbed still visible to `rigx host ls` rather than
    invisible and half-alive.

    `purge` additionally removes `/var/lib/rigx/<testbed>` — the
    persistent volumes. Off by default: those outlive generations and
    rollbacks by design, and a deployment being evicted from a host is
    not a reason to believe its database is unwanted.
    """
    t = _check_name(testbed_name)
    lines = [
        "set -u",
        f"T={t}",
        'for u in "/etc/systemd/system/rigx-$T-"*.service '
        '"/etc/systemd/system/rigx-$T-"*.timer; do',
        '  [ -e "$u" ] || continue',
        '  n=${u##*/}',
        '  systemctl disable --now "$n" >/dev/null 2>&1 || true',
        '  rm -f "$u"',
        "done",
        'rm -f "/etc/systemd/system/multi-user.target.wants/rigx-$T-"*.service',
        "systemctl daemon-reload >/dev/null 2>&1 || true",
        # Reaped by name rather than by `--rm`: the container can outlive
        # the runner that started it, which is the whole reason this
        # module exists.
        'for c in $(podman ps -aq --filter "name=^rigx-$T-" 2>/dev/null || '
        'docker ps -aq --filter "name=^rigx-$T-" 2>/dev/null || true); do',
        '  podman rm -f "$c" >/dev/null 2>&1 || '
        'docker rm -f "$c" >/dev/null 2>&1 || true',
        "done",
        'podman network rm "rigx-$T" >/dev/null 2>&1 || '
        'docker network rm "rigx-$T" >/dev/null 2>&1 || true',
    ]
    if purge:
        lines.append(f'rm -rf "{STATE_ROOT}/$T"')
    lines += [
        f'rm -f "{PROFILE_DIR}/rigx-$T"',
        f'rm -f "{PROFILE_DIR}/rigx-$T-"*-link',
    ]
    return "\n".join(lines) + "\n"


def teardown(target: SSHTarget, testbed_name: str, *, purge: bool = False) -> None:
    """Remove `testbed_name` from `target`. Idempotent: tearing down a
    testbed that isn't there does nothing and succeeds."""
    run_remote(
        target, "sudo sh -s", stdin=teardown_script(testbed_name, purge=purge),
    )
