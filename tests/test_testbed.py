"""Tests for `rigx.testbed.Network` — userspace TCP proxy + fault rules.

Strategy: stand up a real local TCP echo server in lieu of a "capsule"
(it's a process listening on a port, which is all the testbed needs),
then drive traffic through the testbed's proxy and assert on what
arrives at the far end. This exercises the actual proxy threads, rule
chain, and fault context managers — no mocks for the data path.
"""

import os
import random
import socket
import threading
import time
import unittest
from contextlib import contextmanager
from pathlib import Path

from rigx import testbed


@contextmanager
def echo_server(port: int):
    """Tiny echo server bound to a fixed port (used to stand in for a
    capsule listening on its container port). Closes on context exit."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen(8)
    stop = threading.Event()

    def serve():
        srv.settimeout(0.1)
        while not stop.is_set():
            try:
                client, _ = srv.accept()
            except (socket.timeout, BlockingIOError):
                continue
            except OSError:
                return

            def handle(c=client):
                try:
                    while True:
                        data = c.recv(4096)
                        if not data:
                            break
                        c.sendall(data)
                except OSError:
                    pass
                finally:
                    c.close()

            threading.Thread(target=handle, daemon=True).start()

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    try:
        yield port
    finally:
        stop.set()
        try:
            srv.close()
        except OSError:
            pass
        t.join(timeout=1.0)


def _send_recv(host: str, port: int, payload: bytes, timeout: float = 1.0) -> bytes:
    with socket.create_connection((host, port), timeout=timeout) as s:
        s.sendall(payload)
        s.shutdown(socket.SHUT_WR)
        chunks = []
        while True:
            try:
                d = s.recv(4096)
            except socket.timeout:
                break
            if not d:
                break
            chunks.append(d)
        return b"".join(chunks)


def _two_capsules(net: testbed.Network) -> tuple[int, int]:
    """Helper: declare two capsules and one src→dst link. Returns
    (src_listen_port, dst_host_port). Caller stands up an echo server
    on `127.0.0.1:dst_host_port` to stand in for fc."""
    net.declare("sim", listens_on=[])
    net.declare("fc", listens_on=[5001])
    src_endpoint = net.link("sim", "fc", to_port=5001)
    listen_port = int(src_endpoint.split(":")[1])
    # bindings() returns (addr, port) tuples now (or default-host
    # tuples in the no-subnet case). Extract the port for the echo
    # server.
    _, dst_host_port = net.bindings("fc")["publish"][5001]
    return listen_port, dst_host_port


class HappyPath(unittest.TestCase):
    def test_byte_for_byte_proxy_with_no_rules(self):
        net = testbed.Network()
        listen_port, dst_port = _two_capsules(net)
        with echo_server(dst_port), net:
            received = _send_recv("127.0.0.1", listen_port, b"hello\n")
        self.assertEqual(received, b"hello\n")

    def test_bindings_returns_publish_and_env(self):
        """`bindings()` is the orchestrator-facing contract. Pin the
        shape so tests using it don't need to inspect internals."""
        net = testbed.Network()
        net.declare("sim", listens_on=[5000])
        net.declare("fc", listens_on=[5001])
        net.link("sim", "fc", to_port=5001)
        sim_b = net.bindings("sim")
        self.assertIn("publish", sim_b)
        self.assertEqual(set(sim_b["publish"].keys()), {5000})
        self.assertIn("env", sim_b)
        # The env var name encodes peer + port so multi-link topologies
        # don't collide.
        self.assertEqual(set(sim_b["env"].keys()), {"FC_5001_ADDR"})


class FaultRules(unittest.TestCase):
    def test_blocked_drops_everything(self):
        net = testbed.Network()
        listen_port, dst_port = _two_capsules(net)
        with echo_server(dst_port), net:
            with net.fault("sim", "fc", blocked=True):
                with self.assertRaises(OSError):
                    # The proxy accepts the connection then drops bytes;
                    # depending on timing, either send or recv fails.
                    # Either way, no data flows through.
                    received = _send_recv(
                        "127.0.0.1", listen_port, b"hello\n", timeout=0.3
                    )
                    self.assertEqual(received, b"")
                    raise OSError("explicit fall-through marker")
            # Sanity: after the fault block, traffic flows again.
            received = _send_recv("127.0.0.1", listen_port, b"world\n")
            self.assertEqual(received, b"world\n")

    def test_drop_rate_one_drops_all_chunks(self):
        net = testbed.Network()
        listen_port, dst_port = _two_capsules(net)
        with echo_server(dst_port), net:
            with net.fault("sim", "fc", drop_rate=1.0):
                # Every chunk gets dropped; echo server returns nothing.
                received = _send_recv(
                    "127.0.0.1", listen_port, b"hello\n", timeout=0.3
                )
                self.assertEqual(received, b"")

    def test_delay_introduces_measurable_latency(self):
        net = testbed.Network()
        listen_port, dst_port = _two_capsules(net)
        with echo_server(dst_port), net:
            t0 = time.monotonic()
            with net.fault("sim", "fc", delay_ms=120):
                _send_recv("127.0.0.1", listen_port, b"hi\n", timeout=2.0)
            elapsed = (time.monotonic() - t0) * 1000
        # 120ms each way × 2 directions plus echo round trip overhead.
        # Allow generous slack — CI is noisy.
        self.assertGreater(elapsed, 100, f"elapsed={elapsed}ms")

    def test_partition_blocks_both_directions(self):
        """Symmetric block: anything from a → b OR b → a is dropped."""
        net = testbed.Network()
        net.declare("a", listens_on=[100])
        net.declare("b", listens_on=[200])
        a_to_b = net.link("a", "b", to_port=200)
        b_to_a = net.link("b", "a", to_port=100)
        a_listen = int(a_to_b.split(":")[1])
        b_listen = int(b_to_a.split(":")[1])
        _, a_dst = net.bindings("b")["publish"][200]
        _, b_dst = net.bindings("a")["publish"][100]
        with echo_server(a_dst), echo_server(b_dst), net:
            with net.partition(["a"], ["b"]):
                # Both directions blocked.
                self.assertEqual(
                    _send_recv("127.0.0.1", a_listen, b"x", timeout=0.3),
                    b"",
                )
                self.assertEqual(
                    _send_recv("127.0.0.1", b_listen, b"y", timeout=0.3),
                    b"",
                )
            # Healed: both directions back to byte-for-byte.
            self.assertEqual(
                _send_recv("127.0.0.1", a_listen, b"x"),
                b"x",
            )
            self.assertEqual(
                _send_recv("127.0.0.1", b_listen, b"y"),
                b"y",
            )

    def test_fault_restores_on_exception(self):
        """If the body of `with net.fault(...)` raises, rules still
        revert. Otherwise a failed assertion poisons subsequent tests
        in the same Network."""
        net = testbed.Network()
        listen_port, dst_port = _two_capsules(net)
        with echo_server(dst_port), net:
            try:
                with net.fault("sim", "fc", blocked=True):
                    raise RuntimeError("boom")
            except RuntimeError:
                pass
            # Restored.
            received = _send_recv("127.0.0.1", listen_port, b"ok\n")
            self.assertEqual(received, b"ok\n")

    def test_unknown_fault_rule_raises(self):
        net = testbed.Network()
        net.declare("a")
        net.declare("b", listens_on=[1])
        net.link("a", "b", to_port=1)
        with net:
            with self.assertRaises(ValueError) as ctx:
                with net.fault("a", "b", banana=True):
                    pass
            self.assertIn("banana", str(ctx.exception))

    def test_fault_on_undeclared_link_raises(self):
        net = testbed.Network()
        net.declare("a")
        net.declare("b", listens_on=[1])
        net.link("a", "b", to_port=1)
        with net:
            with self.assertRaises(ValueError):
                with net.fault("c", "b"):
                    pass


class TopologyValidation(unittest.TestCase):
    def test_link_to_undeclared_capsule_raises(self):
        net = testbed.Network()
        net.declare("a")
        with self.assertRaises(ValueError):
            net.link("a", "missing", to_port=1)

    def test_link_to_undeclared_port_raises(self):
        net = testbed.Network()
        net.declare("a")
        net.declare("b", listens_on=[1])
        with self.assertRaises(ValueError):
            net.link("a", "b", to_port=999)

    def test_declare_after_start_rejected(self):
        net = testbed.Network()
        net.declare("a", listens_on=[1])
        with net:
            with self.assertRaises(RuntimeError):
                net.declare("b", listens_on=[2])


class DistinctAddresses(unittest.TestCase):
    """`Network(subnet=...)` allocates a per-capsule loopback alias
    from `127.0.0.0/8`. The proxy upstream socket binds on the
    source's address before connect, so destination capsules see the
    real source IP — code that hardcodes IPs or asserts on source IPs
    works as it would on a real network."""

    def test_default_constructor_keeps_shared_loopback(self):
        """Backward compat: `Network()` (no subnet) shares 127.0.0.1
        across capsules. Pre-existing tests rely on this and should
        keep passing without modification."""
        net = testbed.Network()
        net.declare("a")
        net.declare("b", listens_on=[1])
        self.assertEqual(net.address("a"), "127.0.0.1")
        self.assertEqual(net.address("b"), "127.0.0.1")

    def test_subnet_auto_allocates_distinct_addresses(self):
        net = testbed.Network(subnet="127.0.10.0/24")
        net.declare("sim", listens_on=[5000])
        net.declare("fc", listens_on=[5001])
        self.assertNotEqual(net.address("sim"), net.address("fc"))
        # Both fall in the requested subnet, neither is the kernel's
        # default 127.0.0.1 — auto-allocation skips that address.
        for cap in ("sim", "fc"):
            self.assertTrue(net.address(cap).startswith("127.0.10."))
            self.assertNotEqual(net.address(cap), "127.0.0.1")

    def test_explicit_address_overrides_auto_allocation(self):
        net = testbed.Network(subnet="127.0.10.0/24")
        net.declare("sim", address="127.0.10.50", listens_on=[5000])
        self.assertEqual(net.address("sim"), "127.0.10.50")

    def test_explicit_address_outside_subnet_rejected(self):
        net = testbed.Network(subnet="127.0.10.0/24")
        with self.assertRaises(ValueError) as ctx:
            net.declare("sim", address="127.0.99.1", listens_on=[5000])
        self.assertIn("outside the testbed subnet", str(ctx.exception))

    def test_non_loopback_subnet_rejected(self):
        # 10.0.0.0/8 isn't loopback; option 1 only handles loopback
        # aliases. Larger network simulators belong in netns / docker
        # land, not the userspace proxy.
        with self.assertRaises(ValueError) as ctx:
            testbed.Network(subnet="10.0.0.0/24")
        self.assertIn("loopback", str(ctx.exception))

    def test_bindings_publish_uses_capsule_address(self):
        """`bindings()` returns publish values as `(addr, port)`
        tuples in subnet mode so `rigx.capsule.start()` can render
        them as `addr:host_port:cont_port` for `docker -p`."""
        net = testbed.Network(subnet="127.0.10.0/24")
        net.declare("fc", listens_on=[5001])
        b = net.bindings("fc")
        self.assertEqual(set(b["publish"].keys()), {5001})
        addr, port = b["publish"][5001]
        self.assertTrue(addr.startswith("127.0.10."))
        self.assertIsInstance(port, int)

    def test_bindings_publish_default_host_in_no_subnet(self):
        """No subnet → publish values are `(127.0.0.1, port)` tuples
        with the default host. Caller can still uniformly destructure
        as `(addr, port)` whether subnet is set or not."""
        net = testbed.Network()
        net.declare("fc", listens_on=[5001])
        addr, _ = net.bindings("fc")["publish"][5001]
        self.assertEqual(addr, "127.0.0.1")

    def test_link_endpoint_uses_source_address(self):
        """The proxy listener binds on `src`'s address, so the env
        var injected into `src` resolves to its own loopback alias —
        the proxy lives 'with' the source. This is what makes the
        upstream-bind trick (preserving real source IP) coherent."""
        net = testbed.Network(subnet="127.0.10.0/24")
        net.declare("sim", listens_on=[])
        net.declare("fc", listens_on=[5001])
        endpoint = net.link("sim", "fc", to_port=5001)
        self.assertEqual(endpoint.split(":")[0], net.address("sim"))

    def test_destination_sees_source_address_through_proxy(self):
        """End-to-end: with distinct addresses, an echo server bound
        to fc's address sees connections coming from sim's address
        (because the proxy binds upstream on sim's address before
        connect). Without the upstream-bind, dst would see the
        proxy's address and the test would fail."""
        net = testbed.Network(subnet="127.0.10.0/24")
        net.declare("sim", listens_on=[])
        net.declare("fc", listens_on=[5001])
        listen_endpoint = net.link("sim", "fc", to_port=5001)
        listen_addr, listen_port = listen_endpoint.split(":")
        listen_port = int(listen_port)
        fc_addr = net.address("fc")
        _, fc_host_port = net.bindings("fc")["publish"][5001]

        observed_peer: list[str] = []
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((fc_addr, fc_host_port))
        srv.listen(1)

        def serve():
            try:
                srv.settimeout(2.0)
                client, peer = srv.accept()
                observed_peer.append(peer[0])
                try:
                    client.recv(1024)
                except OSError:
                    pass
                client.close()
            except (socket.timeout, OSError):
                pass
            finally:
                srv.close()

        t = threading.Thread(target=serve, daemon=True)
        t.start()
        try:
            with net:
                with socket.create_connection(
                    (listen_addr, listen_port), timeout=2.0
                ) as s:
                    s.sendall(b"hello\n")
                    time.sleep(0.1)
        finally:
            t.join(timeout=2.0)

        self.assertEqual(observed_peer, [net.address("sim")])


@contextmanager
def udp_echo_server(addr: str, port: int):
    """UDP server bound to (addr, port). Echoes datagrams back to
    sender; remembers the source addr/port of each received datagram
    for tests that assert on source-IP visibility."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((addr, port))
    received: list[tuple[bytes, tuple[str, int]]] = []
    stop = threading.Event()

    def serve():
        srv.settimeout(0.1)
        while not stop.is_set():
            try:
                data, peer = srv.recvfrom(65535)
            except (socket.timeout, BlockingIOError):
                continue
            except OSError:
                return
            received.append((data, peer))
            try:
                srv.sendto(data, peer)
            except OSError:
                pass

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    try:
        yield received
    finally:
        stop.set()
        try:
            srv.close()
        except OSError:
            pass
        t.join(timeout=1.0)


class UdpForwarding(unittest.TestCase):
    """`link(proto="udp")` proxies UDP datagrams. Each (src_addr,
    src_port) pair gets its own session with an upstream socket bound
    on the source's address — so destinations see the real source IP,
    same as TCP."""

    def _two_capsules_udp(self, net: testbed.Network) -> tuple[str, int, int]:
        """Declare sim → fc:5001/udp. Returns (listen_addr, listen_port,
        dst_host_port). Caller stands up a UDP echo server on
        (fc.address, dst_host_port)."""
        net.declare("sim", listens_on=[])
        net.declare("fc", udp_listens_on=[5001])
        endpoint = net.link("sim", "fc", to_port=5001, proto="udp")
        listen_addr, listen_port = endpoint.split(":")
        listen_port = int(listen_port)
        _, dst_host_port = net.bindings("fc")["publish_udp"][5001]
        return listen_addr, listen_port, dst_host_port

    def test_datagram_forwarded_through_proxy(self):
        net = testbed.Network()
        listen_addr, listen_port, dst_port = self._two_capsules_udp(net)
        fc_addr = net.address("fc")
        with udp_echo_server(fc_addr, dst_port) as received, net:
            with socket.socket(
                socket.AF_INET, socket.SOCK_DGRAM
            ) as s:
                s.sendto(b"hello\n", (listen_addr, listen_port))
                s.settimeout(2.0)
                reply, _ = s.recvfrom(4096)
        self.assertEqual(reply, b"hello\n")
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0][0], b"hello\n")

    def test_destination_sees_source_address(self):
        """End-to-end source-IP visibility on the UDP forward path:
        the destination's recvfrom returns the *source capsule's*
        address (because the proxy upstream socket is bound on
        sim.address before sendto), not the proxy's listener address."""
        net = testbed.Network(subnet="127.0.10.0/24")
        listen_addr, listen_port, dst_port = self._two_capsules_udp(net)
        fc_addr = net.address("fc")
        sim_addr = net.address("sim")
        with udp_echo_server(fc_addr, dst_port) as received, net:
            # Bind the sender on sim's address so the kernel actually
            # routes the outgoing datagram from sim_addr — without this,
            # the kernel picks an arbitrary loopback source and the
            # proxy's upstream-bind is the only address visible.
            sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                sender.sendto(b"ping", (listen_addr, listen_port))
                time.sleep(0.2)
            finally:
                sender.close()
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0][1][0], sim_addr)

    def test_drop_rate_one_drops_all_datagrams(self):
        net = testbed.Network()
        listen_addr, listen_port, dst_port = self._two_capsules_udp(net)
        fc_addr = net.address("fc")
        with udp_echo_server(fc_addr, dst_port) as received, net:
            with net.fault("sim", "fc", drop_rate=1.0):
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                    s.sendto(b"a", (listen_addr, listen_port))
                    s.sendto(b"b", (listen_addr, listen_port))
                    s.sendto(b"c", (listen_addr, listen_port))
                    time.sleep(0.2)
        self.assertEqual(received, [])

    def test_blocked_via_partition(self):
        net = testbed.Network()
        listen_addr, listen_port, dst_port = self._two_capsules_udp(net)
        fc_addr = net.address("fc")
        with udp_echo_server(fc_addr, dst_port) as received, net:
            with net.partition(["sim"], ["fc"]):
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                    s.sendto(b"x", (listen_addr, listen_port))
                    time.sleep(0.2)
            self.assertEqual(received, [])
            # Healed → traffic flows again
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.sendto(b"y", (listen_addr, listen_port))
                time.sleep(0.2)
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0][0], b"y")

    def test_reply_path_routes_back_to_sender(self):
        """The session table is keyed on the original sender's
        (addr, port). When the destination replies via the upstream
        socket, the proxy forwards it back to that exact peer."""
        net = testbed.Network()
        listen_addr, listen_port, dst_port = self._two_capsules_udp(net)
        fc_addr = net.address("fc")
        with udp_echo_server(fc_addr, dst_port), net:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.bind(("127.0.0.1", 0))
                s.sendto(b"req", (listen_addr, listen_port))
                s.settimeout(2.0)
                reply, _ = s.recvfrom(4096)
                self.assertEqual(reply, b"req")

    def test_link_to_udp_port_not_declared_rejected(self):
        """`fc` declared TCP 5001 only; UDP link to 5001 should fail
        with a clear pointer at `udp_listens_on`."""
        net = testbed.Network()
        net.declare("sim")
        net.declare("fc", listens_on=[5001])  # TCP only
        with self.assertRaises(ValueError) as ctx:
            net.link("sim", "fc", to_port=5001, proto="udp")
        self.assertIn("udp_listens_on", str(ctx.exception))

    def test_invalid_proto_rejected(self):
        net = testbed.Network()
        net.declare("a")
        net.declare("b", listens_on=[1])
        with self.assertRaises(ValueError) as ctx:
            net.link("a", "b", to_port=1, proto="sctp")
        self.assertIn("'tcp' or 'udp'", str(ctx.exception))

    def test_tcp_and_udp_can_coexist_between_same_pair(self):
        """TCP and UDP port spaces are independent. A capsule that
        speaks both protocols on the same port number, and a pair
        with both TCP and UDP links between them, must work."""
        net = testbed.Network()
        net.declare("sim")
        net.declare("fc", listens_on=[5001], udp_listens_on=[5001])
        tcp_endpoint = net.link("sim", "fc", to_port=5001, proto="tcp")
        udp_endpoint = net.link("sim", "fc", to_port=5001, proto="udp")
        self.assertNotEqual(tcp_endpoint, udp_endpoint)
        env = net.bindings("sim")["env"]
        self.assertIn("FC_5001_ADDR", env)       # TCP — bare form
        self.assertIn("FC_5001_UDP_ADDR", env)   # UDP — suffixed


class RuleSet(unittest.TestCase):
    """Tests for the standalone RuleSet logic — no proxy needed."""

    def test_drop_rate_zero_passes_through(self):
        rs = testbed.RuleSet()
        self.assertEqual(rs.apply(b"hi"), b"hi")

    def test_blocked_returns_none(self):
        rs = testbed.RuleSet(blocked=True)
        self.assertIsNone(rs.apply(b"hi"))

    def test_corrupt_rate_one_flips_a_byte(self):
        random.seed(42)
        rs = testbed.RuleSet(corrupt_rate=1.0)
        out = rs.apply(b"hello")
        self.assertEqual(len(out), len(b"hello"))
        self.assertNotEqual(out, b"hello")

    def test_drop_rate_one_returns_none(self):
        rs = testbed.RuleSet(drop_rate=1.0)
        self.assertIsNone(rs.apply(b"hi"))


class WhoHasPort(unittest.TestCase):
    """The `_who_has_port` helper is a best-effort /proc walker that
    annotates bind-failure errors with the holding process. Linux only."""

    def setUp(self):
        if not Path("/proc/net/tcp").is_file():
            self.skipTest("/proc/net/tcp not present (not a Linux host)")

    def test_returns_empty_for_unused_port(self):
        # Bind-and-release pattern: ask the kernel for a free port,
        # release it, then immediately query. Tiny race window, but the
        # kernel won't reassign in microseconds.
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        self.assertEqual(testbed._who_has_port(port, "tcp"), "")

    def test_identifies_self_as_tcp_holder(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        try:
            port = s.getsockname()[1]
            hint = testbed._who_has_port(port, "tcp")
            self.assertIn(f"pid={os.getpid()}", hint)
        finally:
            s.close()

    def test_identifies_self_as_udp_holder(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", 0))
        try:
            port = s.getsockname()[1]
            hint = testbed._who_has_port(port, "udp")
            self.assertIn(f"pid={os.getpid()}", hint)
        finally:
            s.close()

    def test_bind_failure_message_includes_holder_hint(self):
        # Bind a TCP port ourselves, then ask the testbed to bind the
        # same one and assert the resulting RuntimeError names our PID.
        squat = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        squat.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        squat.bind(("127.0.0.1", 0))
        squat.listen(1)
        try:
            busy_port = squat.getsockname()[1]
            net = testbed.Network()
            net.declare("sim", listens_on=[])
            net.declare("fc", listens_on=[5001])
            net.link("sim", "fc", to_port=5001)
            # Force the testbed listener onto the port we're already
            # holding. Reach into the link record to override the
            # auto-assigned listener port.
            link = next(iter(net._links.values()))
            link.listen_port = busy_port
            with self.assertRaises(RuntimeError) as cm:
                net.__enter__()
            msg = str(cm.exception)
            self.assertIn(f"pid={os.getpid()}", msg)
        finally:
            squat.close()


if __name__ == "__main__":
    unittest.main()
