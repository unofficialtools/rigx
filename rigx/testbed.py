"""Userspace network simulator for orchestrating capsules under faults.

Designed for application-protocol integration tests where you have
multiple processes (capsules) and want to inject faults — packet loss,
latency, corruption, partitions — between them. Sits at L4 (TCP);
intentionally not L2/L3.

Typical use:

    from rigx.capsule import start
    from rigx.testbed import Network

    with Network() as net:
        net.declare("sim", listens_on=[5000])
        net.declare("fc",  listens_on=[5001])
        net.link("sim", "fc", to_port=5001)   # sim → fc:5001

        with start("simulator",       **net.bindings("sim")) as sim, \\
             start("flight_computer", **net.bindings("fc"))  as fc:
            sim.wait_for_port(5000)
            fc.wait_for_port(5001)

            # Happy path — full bandwidth, no faults.
            ...

            # 50% drop on sim→fc
            with net.fault("sim", "fc", drop_rate=0.5):
                ...

            # Partition both directions
            with net.partition(["sim"], ["fc"]):
                ...

The testbed allocates ephemeral host ports up front (via `declare`),
binds proxy listeners between linked capsules (via `link`), and
forwards bytes through a per-link rule chain. Faults compose via
context managers that mutate the rule chain and restore on exit.
"""

from __future__ import annotations

import ipaddress
import os
import random
import shutil
import socket
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator


def _who_has_port(port: int, proto: str) -> str:
    """Best-effort: identify which process is holding `port` for `proto`
    ('tcp' or 'udp'). Walks /proc (Linux-only, which the testbed
    already requires). Purely informational — must never raise; returns
    "" if nothing useful can be determined.

    Strategy: read /proc/net/{tcp,tcp6,udp,udp6}, find rows whose local
    port matches, collect their socket inodes, then walk /proc/<pid>/fd
    looking for `socket:[<inode>]` symlinks. Without root we only see
    PIDs owned by the current user — we still return a useful hint in
    that case ('held by another user'), since at least the user knows
    it's not their own stale process."""
    try:
        inodes: set[str] = set()
        for fname in (proto, proto + "6"):
            try:
                with open(f"/proc/net/{fname}") as f:
                    next(f, None)  # header
                    for line in f:
                        parts = line.split()
                        if len(parts) < 10:
                            continue
                        local = parts[1]
                        if ":" not in local:
                            continue
                        try:
                            if int(local.rsplit(":", 1)[1], 16) != port:
                                continue
                        except ValueError:
                            continue
                        inodes.add(parts[9])
            except OSError:
                continue
        if not inodes:
            return ""
        hits: list[str] = []
        try:
            pids = os.listdir("/proc")
        except OSError:
            return ""
        for pid in pids:
            if not pid.isdigit():
                continue
            fddir = f"/proc/{pid}/fd"
            try:
                fds = os.listdir(fddir)
            except OSError:
                continue
            for fd in fds:
                try:
                    link = os.readlink(f"{fddir}/{fd}")
                except OSError:
                    continue
                if link.startswith("socket:[") and link.endswith("]"):
                    inode = link[len("socket:["):-1]
                    if inode in inodes:
                        try:
                            with open(f"/proc/{pid}/comm") as cf:
                                comm = cf.read().strip() or "?"
                        except OSError:
                            comm = "?"
                        hits.append(f"pid={pid} ({comm})")
                        break
        if hits:
            return "held by " + ", ".join(hits)
        # Inode found but no PID resolved → process owned by another user.
        return "held by a process owned by another user (re-run as root to identify)"
    except Exception:
        return ""


def _free_port(addr: str = "127.0.0.1", proto: str = "tcp") -> int:
    """Ask the OS for a free ephemeral port at `addr` for the given
    protocol. TCP and UDP have independent port spaces — a port may
    be free for one and busy for the other — so the helper takes a
    proto kwarg. Small race window before the testbed re-binds; in
    practice the kernel doesn't reassign that fast under test loads."""
    sock_type = socket.SOCK_DGRAM if proto == "udp" else socket.SOCK_STREAM
    with socket.socket(socket.AF_INET, sock_type) as s:
        s.bind((addr, 0))
        return s.getsockname()[1]


@dataclass
class RuleSet:
    """Per-link fault rules applied to bytes flowing through the proxy.

    All rules compose: a chunk that survives drop_rate may still be
    delayed, then corrupted, then forwarded. `blocked` is the partition
    primitive: when true, drops everything regardless of other rules.

    Mutated in place by `Network.fault()` and `Network.partition()`
    context managers; restored on context exit."""
    drop_rate: float = 0.0          # P(drop) per chunk
    delay_ms: float = 0.0           # constant delay per chunk
    jitter_ms: float = 0.0          # ± random jitter on top of delay
    corrupt_rate: float = 0.0       # P(flip one byte) per chunk
    blocked: bool = False           # partition: drop everything

    def apply(self, data: bytes) -> bytes | None:
        """Run a chunk through the rule chain. Returns None if dropped."""
        if self.blocked:
            return None
        if self.drop_rate > 0 and random.random() < self.drop_rate:
            return None
        if self.delay_ms > 0 or self.jitter_ms > 0:
            wait = self.delay_ms
            if self.jitter_ms > 0:
                wait += random.uniform(-self.jitter_ms, self.jitter_ms)
            time.sleep(max(0.0, wait) / 1000.0)
        if self.corrupt_rate > 0 and random.random() < self.corrupt_rate and data:
            ba = bytearray(data)
            idx = random.randrange(len(ba))
            ba[idx] ^= 0xFF
            data = bytes(ba)
        return data


@dataclass
class _CapsuleEntry:
    """Per-capsule address + pre-allocated host ports. With shared-
    loopback `Network()` (no subnet), `address` is `127.0.0.1` for
    every capsule and capsules are distinguished by port. With a
    `subnet` set, each capsule gets a distinct `127.0.X.Y` alias and
    its container ports map to host ports at *that* address; capsule
    code that asserts on source IPs sees real distinct peers.

    TCP and UDP port spaces are independent — same port number can
    be in use for one without conflicting with the other — so
    listening ports are stored in separate maps."""
    name: str
    address: str = "127.0.0.1"
    port_map: dict[int, int] = field(default_factory=dict)
    udp_port_map: dict[int, int] = field(default_factory=dict)
    # Container-port → list of `(host_addr, host_port)` extra
    # publishes. Set by `declare(expose=…)`; appended to each port's
    # canonical loopback-alias publish in `bindings()` so docker
    # binds the same container port on multiple host endpoints.
    expose: dict[int, list[tuple[str, int]]] = field(default_factory=dict)
    udp_expose: dict[int, list[tuple[str, int]]] = field(default_factory=dict)
    # Bind-mounts requested via `declare(volumes=…)`. Each entry is
    # `(handle, container_path, mode)`; the handle's `host_path` is
    # populated lazily at `__enter__` time when the testbed
    # allocates tempdirs for shared volumes.
    volumes: list[tuple["SharedVolume", str, str]] = field(default_factory=list)


@dataclass(eq=False)
class SharedVolume:
    """A writable host directory shared across capsules in a testbed.

    Returned by `Network.shared_volume(name)`. The host-side tempdir
    is allocated when the testbed's context manager enters and
    cleaned up on exit. Capsules attach via `declare(volumes={vol:
    "/path"})`; `bindings()` resolves the handle to a host path that
    `rigx.capsule.start()` wires up as a `-v` flag.

    The `name` is a human-readable label for diagnostics — multiple
    volumes can share a name without conflict.

    Identity-based equality (`eq=False`): two `SharedVolume` instances
    with the same `name` are still distinct handles, and we want them
    usable as dict keys in `declare(volumes={…})` even though
    `host_path` mutates over the testbed lifecycle.
    """
    name: str
    host_path: Path | None = None  # set by Network._start, cleared by _stop


@dataclass
class _Link:
    """A directional virtual edge `src → dst:dst_port`. The testbed
    listens on `listen_addr:listen_port` (TCP or UDP per `proto`);
    bytes / datagrams get forwarded to `dst_addr:<dst host port>`
    through the rule chain.

    For both protocols the proxy upstream socket binds on the source's
    address before `connect()`/`sendto()`, so the destination capsule
    sees the real source IP even though the proxy is in the middle.
    For TCP this is one socket per accepted connection; for UDP the
    forwarder maintains a session table keyed by (src_addr, src_port)
    — each session gets its own upstream socket so reply datagrams
    from the destination route back to the right sender."""
    src: str
    dst: str
    dst_port: int
    listen_addr: str
    listen_port: int
    proto: str = "tcp"
    rules: RuleSet = field(default_factory=RuleSet)


class Network:
    """A virtual network for capsule integration tests. Topology is
    declared up-front (`declare` + `link`) so capsule env / port
    bindings are deterministic before any container starts. Faults are
    applied at runtime via context managers.

    Lifecycle: build object → declare capsules → declare links →
    `with` block (binds proxy listeners, runs test logic, tears down).
    Use as a context manager.

    Limitations (v1):
      - TCP only. UDP simulation deferred.
      - L4 byte-level rules. No L2/L3 (no MTU, fragmentation, ARP, …).
      - Topology is fixed at `with`-entry; new links can't be added
        after the testbed is running. Re-enter with a fresh `Network`
        if you need a different shape.
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        *,
        subnet: str | None = None,
        verbose: bool = False,
    ):
        """Build a testbed.

        Args:
          host: default address used when `subnet` is None — every
                capsule shares it and is distinguished by port. Keeps
                the simple-test path working without ifconfig setup.
          subnet: when set, each capsule gets a distinct loopback
                alias auto-allocated from this CIDR (e.g.
                `"127.0.10.0/24"`). Linux routes all of `127.0.0.0/8`
                to `lo` automatically; macOS needs `sudo ifconfig lo0
                alias <addr>` per address (one-time). Capsules see
                real distinct peer IPs and code that asserts on
                source addresses works.
          verbose: stream proxy diagnostics to stderr.
        """
        self.host = host
        self.verbose = verbose
        self._capsules: dict[str, _CapsuleEntry] = {}
        # Keyed by (src, dst, dst_port) so multiple ports between the
        # same pair don't collide.
        # Keyed by (src, dst, dst_port, proto) so a TCP and a UDP link
        # between the same pair on the same port number can coexist.
        self._links: dict[tuple[str, str, int, str], _Link] = {}
        self._listeners: list[socket.socket] = []
        self._threads: list[threading.Thread] = []
        self._running = False
        self._stopping = False
        # Shared volumes — registered up-front via `shared_volume()`,
        # allocated on `__enter__`, deleted on `__exit__`. Stored as
        # a list because handle identity (not name) is what `declare`
        # references; multiple volumes can share a name.
        self._volumes: list[SharedVolume] = []

        self._subnet: ipaddress.IPv4Network | None = None
        self._addr_pool: Iterator[str] | None = None
        if subnet is not None:
            self._subnet = ipaddress.IPv4Network(subnet, strict=False)
            if not self._subnet.is_loopback:
                raise ValueError(
                    f"subnet {subnet!r} is not in 127.0.0.0/8 — only "
                    f"loopback subnets are supported in v1"
                )
            # Skip network/broadcast and the .1 host (commonly the
            # loopback default `127.0.0.1`); auto-allocate from .2
            # upward so explicit-address declarations in the same
            # subnet don't accidentally collide with the kernel's
            # default 127.0.0.1.
            self._addr_pool = (
                str(h) for h in self._subnet.hosts() if str(h) != "127.0.0.1"
            )

    # --- Topology declaration ------------------------------------------

    def shared_volume(self, name: str) -> SharedVolume:
        """Register a writable host directory shared across capsules.

        Returns a `SharedVolume` handle. Multiple capsules attach via
        `declare(volumes={handle: "/container/path"})`. The host-side
        tempdir is allocated on `__enter__` (so creating the handle is
        cheap and side-effect-free) and deleted on `__exit__`.

        Calling the testbed's `with` block before declaring all
        volumes is fine; calling `shared_volume` *after* entering is
        not — same lifecycle as `declare`."""
        if self._running:
            raise RuntimeError(
                "shared_volume() must be called before entering the testbed"
            )
        sv = SharedVolume(name=name)
        self._volumes.append(sv)
        return sv

    def declare(
        self,
        capsule: str,
        *,
        address: str | None = None,
        listens_on: list[int] | None = None,
        udp_listens_on: list[int] | None = None,
        expose: list[tuple[str, int]] | None = None,
        udp_expose: list[tuple[str, int]] | None = None,
        volumes: dict[SharedVolume, str | tuple[str, str]] | None = None,
    ) -> None:
        """Register a capsule, pre-allocating host-side ports for each
        of its declared listening container ports.

        Args:
          listens_on: TCP container ports the capsule listens on.
          udp_listens_on: UDP container ports the capsule listens on.
                  TCP and UDP port spaces are independent, so a
                  capsule may declare the same port number in both
                  lists and they map to distinct host ports.
          expose: extra `(host_addr, container_port)` publishes for
                  TCP. The capsule's port stays reachable on its
                  loopback alias for inter-capsule traffic; the
                  exposed endpoint is an *additional* docker `-p`
                  bind, useful for "open this in a browser" workflows
                  on `0.0.0.0:8000`. Each container port must already
                  appear in `listens_on`. Faults declared via
                  `link()` don't apply to traffic that arrives via
                  the exposed endpoint — the proxy never sees it.
          udp_expose: same shape, for UDP. Container ports must
                  appear in `udp_listens_on`.
          volumes: bind-mounts attached to this capsule, keyed by a
                  handle from `shared_volume()`. Value is a container
                  path (mode `"rw"`) or a `(container_path, mode)`
                  tuple. `bindings()` includes the resolved volumes
                  in the kwargs passed to `rigx.capsule.start()`.

        With no `subnet` on the Network and no explicit `address`,
        every capsule shares `host` (default `127.0.0.1`). Distinguish
        by port.

        With `subnet=`, capsules get a distinct loopback alias each.
        Pass `address=...` to pin one explicitly (must lie in the
        subnet); otherwise the testbed allocates from the subnet
        sequentially."""
        if self._running:
            raise RuntimeError("declare() must be called before entering the testbed")
        if capsule in self._capsules:
            raise ValueError(f"capsule {capsule!r} already declared")
        if address is None:
            if self._addr_pool is None:
                address = self.host
            else:
                try:
                    address = next(self._addr_pool)
                except StopIteration:
                    raise RuntimeError(
                        f"testbed: subnet {self._subnet} exhausted while "
                        f"declaring {capsule!r}; widen the subnet"
                    )
        else:
            if self._subnet is not None and (
                ipaddress.IPv4Address(address) not in self._subnet
            ):
                raise ValueError(
                    f"address {address!r} is outside the testbed subnet "
                    f"{self._subnet}"
                )
        port_map = {p: _free_port(address, "tcp") for p in (listens_on or [])}
        udp_port_map = {
            p: _free_port(address, "udp") for p in (udp_listens_on or [])
        }

        expose_map: dict[int, list[tuple[str, int]]] = {}
        for addr, port in (expose or []):
            if port not in port_map:
                raise ValueError(
                    f"expose entry ({addr!r}, {port}): port {port} is not "
                    f"in listens_on={sorted(port_map)} for capsule "
                    f"{capsule!r}"
                )
            expose_map.setdefault(port, []).append((addr, port))
        udp_expose_map: dict[int, list[tuple[str, int]]] = {}
        for addr, port in (udp_expose or []):
            if port not in udp_port_map:
                raise ValueError(
                    f"udp_expose entry ({addr!r}, {port}): port {port} is "
                    f"not in udp_listens_on={sorted(udp_port_map)} for "
                    f"capsule {capsule!r}"
                )
            udp_expose_map.setdefault(port, []).append((addr, port))

        vol_entries: list[tuple[SharedVolume, str, str]] = []
        for handle, spec in (volumes or {}).items():
            if not isinstance(handle, SharedVolume):
                raise TypeError(
                    f"volumes key must be a SharedVolume handle "
                    f"(got {type(handle).__name__}); use net.shared_volume()"
                )
            if handle not in self._volumes:
                raise ValueError(
                    f"volumes: handle {handle.name!r} was not produced by "
                    f"this testbed's shared_volume()"
                )
            if isinstance(spec, tuple):
                cont, mode = spec
            else:
                cont, mode = spec, "rw"
            if mode not in ("rw", "ro"):
                raise ValueError(
                    f"volumes[{handle.name!r}]: mode must be 'rw' or 'ro' "
                    f"(got {mode!r})"
                )
            vol_entries.append((handle, cont, mode))

        self._capsules[capsule] = _CapsuleEntry(
            name=capsule, address=address,
            port_map=port_map, udp_port_map=udp_port_map,
            expose=expose_map, udp_expose=udp_expose_map,
            volumes=vol_entries,
        )

    def link(
        self, src: str, dst: str, *, to_port: int, proto: str = "tcp"
    ) -> str:
        """Create a directional link `src → dst:to_port`. Returns the
        host:port endpoint that `src` should connect to. Pass it as an
        env var to the `src` capsule via `bindings()`.

        `proto` selects the transport: `"tcp"` (default) or `"udp"`.
        For UDP, the destination must be `udp_listens_on` for `to_port`
        in its declare. Both TCP and UDP links can coexist between the
        same pair of capsules, even on the same port number.

        With distinct addresses, the proxy listener binds on the
        source capsule's address: `src` connects to "its own" address
        at the proxy port, the proxy then forwards to `dst`'s real
        address — and crucially, binds the upstream socket on `src`'s
        address so `dst` sees the right source IP."""
        if self._running:
            raise RuntimeError("link() must be called before entering the testbed")
        if proto not in ("tcp", "udp"):
            raise ValueError(
                f"link proto must be 'tcp' or 'udp', got {proto!r}"
            )
        if src not in self._capsules:
            raise ValueError(f"declare {src!r} before linking from it")
        if dst not in self._capsules:
            raise ValueError(f"declare {dst!r} before linking to it")
        dst_map = (
            self._capsules[dst].udp_port_map if proto == "udp"
            else self._capsules[dst].port_map
        )
        if to_port not in dst_map:
            kind = "udp_listens_on" if proto == "udp" else "listens_on"
            raise ValueError(
                f"capsule {dst!r} was not declared with {kind}={to_port!r}; "
                f"declared {kind}: {sorted(dst_map)}"
            )
        key = (src, dst, to_port, proto)
        if key in self._links:
            raise ValueError(
                f"link {src!r}→{dst!r}:{to_port}/{proto} already declared"
            )
        src_addr = self._capsules[src].address
        link = _Link(
            src=src, dst=dst, dst_port=to_port,
            listen_addr=src_addr,
            listen_port=_free_port(src_addr, proto),
            proto=proto,
        )
        self._links[key] = link
        return f"{src_addr}:{link.listen_port}"

    def address(self, capsule: str) -> str:
        """The loopback address assigned to `capsule`. Useful for tests
        that want to verify a peer connection's source IP."""
        cap = self._capsules.get(capsule)
        if cap is None:
            raise ValueError(f"capsule {capsule!r} not declared")
        return cap.address

    def bindings(self, capsule: str) -> dict:
        """Kwargs ready to pass to `rigx.capsule.start(name, **bindings(name))`.

        Returns:
          publish: TCP listening ports forwarded to the capsule's
                   loopback alias as `(host_addr, host_port)` tuples.
                   When `declare(expose=…)` was set, the value
                   becomes a list — the canonical loopback alias
                   first, then each exposed `(addr, port)`. List
                   entries render to one docker `-p` flag each.
          publish_udp: same, for UDP listening ports. Always present;
                   empty dict if the capsule declared no UDP ports.
                   `rigx.capsule` formats both as docker `-p` flags
                   with the right `/udp` suffix.
          env: peer addresses for each outbound link the capsule has,
                   named `<DST>_<PORT>[_<PROTO>]_ADDR`. TCP links use
                   the bare form (`FC_5001_ADDR`) for backward compat;
                   UDP links append `_UDP` (`FC_5001_UDP_ADDR`).
          volumes: host-path → container-path mapping (with mode
                   recorded as the value when non-default — actually
                   keyed as `Path(host) -> container_path` in the
                   default `"rw"` case, or `Path(host) -> (cont,
                   mode)` if `"ro"`). Always present; empty dict if
                   the capsule declared no volumes. The testbed must
                   have entered (`__enter__`) so host paths are
                   resolved.
        """
        cap = self._capsules.get(capsule)
        if cap is None:
            raise ValueError(f"capsule {capsule!r} not declared")

        def _publish_value(
            cont: int, addr: str, host_port: int,
            extras: list[tuple[str, int]],
        ):
            """Single tuple if no extras; list when expose adds bindings."""
            if not extras:
                return (addr, host_port)
            return [(addr, host_port), *extras]

        publish = {
            cont: _publish_value(
                cont, cap.address, host, cap.expose.get(cont, []),
            )
            for cont, host in cap.port_map.items()
        }
        publish_udp = {
            cont: _publish_value(
                cont, cap.address, host, cap.udp_expose.get(cont, []),
            )
            for cont, host in cap.udp_port_map.items()
        }
        env: dict[str, str] = {}
        for (src, dst, port, proto), link in self._links.items():
            if src != capsule:
                continue
            suffix = "" if proto == "tcp" else f"_{proto.upper()}"
            env[f"{dst.upper()}_{port}{suffix}_ADDR"] = (
                f"{link.listen_addr}:{link.listen_port}"
            )

        volumes: dict[Path | str, str | tuple[str, str]] = {}
        for handle, container, mode in cap.volumes:
            if handle.host_path is None:
                raise RuntimeError(
                    f"capsule {capsule!r}: shared volume {handle.name!r} "
                    f"has no host path — call bindings() inside the "
                    f"testbed's `with` block"
                )
            volumes[handle.host_path] = (
                container if mode == "rw" else (container, mode)
            )

        return {
            "publish": publish,
            "publish_udp": publish_udp,
            "env": env,
            "volumes": volumes,
        }

    # --- Fault injection ------------------------------------------------

    @contextmanager
    def fault(
        self,
        src: str,
        dst: str,
        *,
        to_port: int | None = None,
        proto: str | None = None,
        **rules,
    ) -> Iterator[None]:
        """Apply fault rules to one or more `src → dst` links for the
        duration of the `with` block.

        Rules are keyword args matching `RuleSet` fields:
          drop_rate, delay_ms, jitter_ms, corrupt_rate, blocked

        `to_port=None` (default) applies to every src→dst link; pass
        an explicit port to scope to one. `proto=None` (default)
        applies to both TCP and UDP links between the pair; pass
        `"tcp"` or `"udp"` to scope to one. On exit, rules are
        restored to their pre-block values — faults nest cleanly."""
        affected = [
            link for (s, d, p, pr), link in self._links.items()
            if s == src and d == dst
            and (to_port is None or p == to_port)
            and (proto is None or pr == proto)
        ]
        if not affected:
            raise ValueError(
                f"no link matches src={src!r} dst={dst!r} "
                f"to_port={to_port!r}"
            )
        for k in rules:
            if not hasattr(RuleSet, k):
                raise ValueError(
                    f"unknown fault rule {k!r}; valid: "
                    f"drop_rate, delay_ms, jitter_ms, corrupt_rate, blocked"
                )
        snapshots = [(link, _snapshot(link.rules)) for link in affected]
        for link in affected:
            for k, v in rules.items():
                setattr(link.rules, k, v)
        try:
            yield
        finally:
            for link, snap in snapshots:
                link.rules = snap

    @contextmanager
    def partition(
        self, group_a: list[str], group_b: list[str]
    ) -> Iterator[None]:
        """Block every link between `group_a` and `group_b` (both
        directions) for the duration of the `with` block. Equivalent to
        `fault(..., blocked=True)` applied symmetrically across the
        relevant links."""
        a, b = set(group_a), set(group_b)
        affected = [
            link for (s, d, _, _), link in self._links.items()
            if (s in a and d in b) or (s in b and d in a)
        ]
        if not affected:
            raise ValueError(
                f"no links between {sorted(a)} and {sorted(b)}"
            )
        snapshots = [(link, _snapshot(link.rules)) for link in affected]
        for link in affected:
            link.rules.blocked = True
        try:
            yield
        finally:
            for link, snap in snapshots:
                link.rules = snap

    # --- Lifecycle ------------------------------------------------------

    def __enter__(self) -> "Network":
        self._start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop()

    def _start(self) -> None:
        if self._running:
            return
        self._running = True
        # Allocate per-volume tempdirs before starting proxies so a
        # later `bindings()` call has something to point capsules at.
        # Failure here aborts entry — partially-allocated dirs are
        # cleaned up via `_stop` since we already flipped `_running`.
        for sv in self._volumes:
            try:
                sv.host_path = Path(
                    tempfile.mkdtemp(prefix=f"rigx-vol-{sv.name}-")
                )
            except OSError as e:
                self._stop()
                raise RuntimeError(
                    f"testbed: cannot allocate shared volume "
                    f"{sv.name!r}: {e}"
                ) from e
        for link in self._links.values():
            if link.proto == "udp":
                self._start_udp_link(link)
            else:
                self._start_tcp_link(link)

    def _start_tcp_link(self, link: _Link) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((link.listen_addr, link.listen_port))
        except OSError as e:
            sock.close()
            hint = _who_has_port(link.listen_port, "tcp")
            hint_msg = f" — {hint}" if hint else ""
            raise RuntimeError(
                f"testbed: cannot bind {link.listen_addr}:"
                f"{link.listen_port} for link {link.src!r}→{link.dst!r}: "
                f"{e}{hint_msg}. On macOS, loopback aliases need "
                f"`sudo ifconfig lo0 alias {link.listen_addr}` first."
            ) from e
        sock.listen(8)
        self._listeners.append(sock)
        t = threading.Thread(
            target=self._accept_loop,
            args=(sock, link),
            daemon=True,
            name=f"rigx-testbed-tcp-{link.src}-to-{link.dst}",
        )
        t.start()
        self._threads.append(t)

    def _start_udp_link(self, link: _Link) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((link.listen_addr, link.listen_port))
        except OSError as e:
            sock.close()
            hint = _who_has_port(link.listen_port, "udp")
            hint_msg = f" — {hint}" if hint else ""
            raise RuntimeError(
                f"testbed: cannot bind UDP {link.listen_addr}:"
                f"{link.listen_port} for link {link.src!r}→{link.dst!r}: {e}{hint_msg}"
            ) from e
        self._listeners.append(sock)
        t = threading.Thread(
            target=self._udp_forwarder,
            args=(sock, link),
            daemon=True,
            name=f"rigx-testbed-udp-{link.src}-to-{link.dst}",
        )
        t.start()
        self._threads.append(t)

    def _stop(self) -> None:
        self._stopping = True
        for sock in self._listeners:
            try:
                sock.close()
            except OSError:
                pass
        for t in self._threads:
            t.join(timeout=1.0)
        self._listeners.clear()
        self._threads.clear()
        for sv in self._volumes:
            if sv.host_path is not None:
                shutil.rmtree(sv.host_path, ignore_errors=True)
                sv.host_path = None
        self._running = False

    # --- Internal proxy plumbing ---------------------------------------

    def _accept_loop(self, listener: socket.socket, link: _Link) -> None:
        # Set timeout once before the loop. Re-setting it inside the
        # loop races with `close()` from another thread — the listener
        # fd may be already closed when settimeout runs, raising
        # OSError(EBADF) and printing a noisy traceback.
        try:
            listener.settimeout(0.5)
        except OSError:
            return
        while not self._stopping:
            try:
                client, _ = listener.accept()
            except (socket.timeout, BlockingIOError):
                continue
            except OSError:
                return
            t = threading.Thread(
                target=self._proxy_one,
                args=(client, link),
                daemon=True,
            )
            t.start()

    def _proxy_one(self, client: socket.socket, link: _Link) -> None:
        try:
            dst_cap = self._capsules[link.dst]
            host_port = dst_cap.port_map[link.dst_port]
        except KeyError:
            client.close()
            return
        # Bind the upstream socket on the source's address before
        # connect. With distinct addresses, this makes the destination
        # see the real source IP — the proxy is otherwise transparent
        # at the L4-source level. With shared loopback, src==dst==host
        # so the bind is a no-op equivalent.
        src_addr = self._capsules[link.src].address
        upstream = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            upstream.bind((src_addr, 0))   # 0 = let kernel pick source port
            upstream.settimeout(5.0)
            upstream.connect((dst_cap.address, host_port))
            upstream.settimeout(None)
        except OSError as e:
            if self.verbose:
                print(
                    f"[testbed] {link.src}→{link.dst}: "
                    f"upstream connect failed: {e}",
                    file=sys.stderr,
                )
            try:
                upstream.close()
            except OSError:
                pass
            client.close()
            return

        def pump(src: socket.socket, dst: socket.socket, label: str) -> None:
            try:
                while True:
                    data = src.recv(4096)
                    if not data:
                        break
                    out = link.rules.apply(data)
                    if out is None:
                        # Dropped chunk; keep pumping subsequent chunks.
                        continue
                    dst.sendall(out)
            except OSError:
                pass
            finally:
                try:
                    dst.shutdown(socket.SHUT_WR)
                except OSError:
                    pass

        t1 = threading.Thread(
            target=pump,
            args=(client, upstream, f"{link.src}→{link.dst}"),
            daemon=True,
        )
        t2 = threading.Thread(
            target=pump,
            args=(upstream, client, f"{link.dst}→{link.src}"),
            daemon=True,
        )
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        client.close()
        upstream.close()

    # --- UDP forwarder --------------------------------------------------
    #
    # UDP doesn't have connections, so the proxy works differently from
    # TCP: a single listener socket receives every datagram from any
    # source, and we maintain a per-(src_addr, src_port) session table
    # so reply datagrams from the destination route back to the right
    # sender. Each session has its own upstream socket bound on the
    # source capsule's address — same source-IP-visibility trick the
    # TCP path uses.
    #
    # Limitation acknowledged in v1: a chunk's `delay_ms` blocks the
    # listener thread head-of-line. Acceptable for tests where delay
    # is modest; switch to a heap-based scheduler if it bites.
    _UDP_SESSION_IDLE_TIMEOUT = 60.0  # seconds before a quiet session is GC'd

    def _udp_forwarder(self, listener: socket.socket, link: _Link) -> None:
        sessions: dict[tuple[str, int], _UdpSession] = {}
        sessions_lock = threading.Lock()

        def close_sessions() -> None:
            # All sessions own an upstream socket bound on the source's
            # address; close them on teardown so reply pumps wake up
            # and the kernel reclaims the fds. Without this, ResourceWarning
            # noise during test teardown — and a real fd leak in long
            # test runs.
            with sessions_lock:
                for sess in sessions.values():
                    try:
                        sess.upstream.close()
                    except OSError:
                        pass
                sessions.clear()

        try:
            listener.settimeout(0.5)
        except OSError:
            close_sessions()
            return
        try:
            dst_cap = self._capsules[link.dst]
            dst_host_port = dst_cap.udp_port_map[link.dst_port]
        except KeyError:
            close_sessions()
            return
        src_addr = self._capsules[link.src].address

        def gc_idle() -> None:
            now = time.monotonic()
            stale = [
                key for key, sess in sessions.items()
                if now - sess.last_seen > self._UDP_SESSION_IDLE_TIMEOUT
            ]
            for key in stale:
                sess = sessions.pop(key)
                try:
                    sess.upstream.close()
                except OSError:
                    pass

        try:
            while not self._stopping:
                try:
                    payload, peer = listener.recvfrom(65535)
                except (socket.timeout, BlockingIOError):
                    with sessions_lock:
                        gc_idle()
                    continue
                except OSError:
                    return
                out = link.rules.apply(payload)
                if out is None:
                    # Dropped (drop_rate or blocked). Don't allocate
                    # a session for traffic we're discarding.
                    continue
                with sessions_lock:
                    sess = sessions.get(peer)
                    if sess is None:
                        upstream = socket.socket(
                            socket.AF_INET, socket.SOCK_DGRAM
                        )
                        try:
                            upstream.bind((src_addr, 0))
                        except OSError as e:
                            if self.verbose:
                                print(
                                    f"[testbed] udp {link.src}→{link.dst}: "
                                    f"upstream bind on {src_addr} "
                                    f"failed: {e}",
                                    file=sys.stderr,
                                )
                            upstream.close()
                            continue
                        sess = _UdpSession(
                            peer=peer, upstream=upstream,
                            last_seen=time.monotonic(),
                        )
                        sessions[peer] = sess
                        # Spawn a reply pump for this session — reads
                        # datagrams the destination sends back and
                        # forwards them to the original peer through
                        # the listener socket. Reply packets carry the
                        # listener's address as their source; symmetric
                        # src-IP visibility on the reply path is option
                        # (2) territory and not in v1.
                        t = threading.Thread(
                            target=self._udp_reply_pump,
                            args=(listener, sess, link),
                            daemon=True,
                        )
                        t.start()
                        self._threads.append(t)
                    else:
                        sess.last_seen = time.monotonic()
                    upstream = sess.upstream
                try:
                    upstream.sendto(
                        out, (dst_cap.address, dst_host_port)
                    )
                except OSError as e:
                    if self.verbose:
                        print(
                            f"[testbed] udp {link.src}→{link.dst}: "
                            f"forward failed: {e}",
                            file=sys.stderr,
                        )
        finally:
            close_sessions()

    def _udp_reply_pump(
        self, listener: socket.socket, sess: "_UdpSession", link: _Link
    ) -> None:
        """One thread per session. Reads reply datagrams the destination
        sends to the upstream socket and forwards them back to the
        original sender via the listener socket. Same fault rules apply
        on the reply leg — if `blocked=True`, replies vanish too."""
        try:
            sess.upstream.settimeout(0.5)
        except OSError:
            return
        while not self._stopping:
            try:
                payload, _ = sess.upstream.recvfrom(65535)
            except (socket.timeout, BlockingIOError):
                continue
            except OSError:
                return
            out = link.rules.apply(payload)
            if out is None:
                continue
            sess.last_seen = time.monotonic()
            try:
                listener.sendto(out, sess.peer)
            except OSError:
                return


@dataclass
class _UdpSession:
    """One UDP src→dst session. The upstream socket is bound on the
    source capsule's address so destinations see the real source IP.
    `last_seen` drives idle eviction so long-running tests don't leak
    sockets."""
    peer: tuple[str, int]
    upstream: socket.socket
    last_seen: float


def _snapshot(rules: RuleSet) -> RuleSet:
    """Shallow copy of a RuleSet for fault-context save/restore."""
    return RuleSet(
        drop_rate=rules.drop_rate,
        delay_ms=rules.delay_ms,
        jitter_ms=rules.jitter_ms,
        corrupt_rate=rules.corrupt_rate,
        blocked=rules.blocked,
    )
