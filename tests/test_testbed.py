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
from unittest import mock

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


    def test_env_var_names_sanitized_for_posix(self):
        """Capsule names with '-' or '.' (my-svc, mod.svc) would emit
        env var names invalid under POSIX (MY-SVC_..., MOD.SVC_...)
        that shells silently drop. Non-[A-Z0-9_] chars map to '_'."""
        net = testbed.Network()
        net.declare("sim")
        net.declare("my-svc", listens_on=[5001])
        net.declare("mod.svc", listens_on=[5002])
        net.link("sim", "my-svc", to_port=5001)
        net.link("sim", "mod.svc", to_port=5002)
        env = net.bindings("sim")["env"]
        self.assertEqual(
            set(env), {"MY_SVC_5001_ADDR", "MOD_SVC_5002_ADDR"}
        )

    def test_env_var_collision_after_sanitization_raises(self):
        """'fc' and 'FC' are distinct capsules but produce the same env
        var — silent overwrite would point one link at the wrong proxy."""
        net = testbed.Network()
        net.declare("sim")
        net.declare("fc", listens_on=[5001])
        net.declare("FC", listens_on=[5001])
        net.link("sim", "fc", to_port=5001)
        net.link("sim", "FC", to_port=5001)
        with self.assertRaises(ValueError) as ctx:
            net.bindings("sim")
        self.assertIn("FC_5001_ADDR", str(ctx.exception))


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

    def test_fault_rejects_ruleset_method_names(self):
        """`hasattr(RuleSet, k)` would accept method names ('apply'),
        letting setattr shadow the method and TypeError later inside a
        pump thread. Only dataclass fields are valid rules."""
        net = testbed.Network()
        net.declare("a")
        net.declare("b", listens_on=[1])
        net.link("a", "b", to_port=1)
        with self.assertRaises(ValueError) as ctx:
            with net.fault("a", "b", apply=0.5):
                pass
        self.assertIn("apply", str(ctx.exception))

    def test_fault_rejects_bad_rule_values(self):
        """Bad values must fail at fault() time, not mid-pump (where
        they'd hang the connection instead of failing the test)."""
        net = testbed.Network()
        net.declare("a")
        net.declare("b", listens_on=[1])
        net.link("a", "b", to_port=1)
        for bad in (
            {"drop_rate": "0.5"},     # not a number
            {"drop_rate": 1.5},       # probability out of range
            {"delay_ms": -10},        # negative
            {"blocked": "yes"},       # not a bool
            {"corrupt_rate": True},   # bool is not a rate
        ):
            with self.assertRaises(ValueError, msg=f"rules={bad}"):
                with net.fault("a", "b", **bad):
                    pass

    def test_fault_validates_proto_and_reports_it_on_no_match(self):
        net = testbed.Network()
        net.declare("a")
        net.declare("b", listens_on=[1])
        net.link("a", "b", to_port=1)  # TCP only
        with self.assertRaises(ValueError) as ctx:
            with net.fault("a", "b", proto="sctp", blocked=True):
                pass
        self.assertIn("'tcp' or 'udp'", str(ctx.exception))
        # A valid proto with no matching link names the proto in the
        # error so the tcp/udp mixup is visible.
        with self.assertRaises(ValueError) as ctx:
            with net.fault("a", "b", proto="udp", blocked=True):
                pass
        self.assertIn("proto='udp'", str(ctx.exception))

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


class LifecycleRobustness(unittest.TestCase):
    def test_failed_enter_cleans_up_partial_state(self):
        """If the Nth link fails to bind, the listeners already started
        (and volume tempdirs) are torn down and `_running` resets, so
        the failure doesn't leak and the Network can be retried."""
        net = testbed.Network(gateway=None)  # one listener per link
        vol = net.shared_volume("data")
        net.declare("sim", listens_on=[])
        net.declare("fc", listens_on=[5001])
        net.declare("gs", listens_on=[5002])
        net.link("sim", "fc", to_port=5001)
        net.link("sim", "gs", to_port=5002)
        # Pre-bind the *second* link's port so the first listener has
        # already started when the second bind fails.
        squat = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        squat.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        squat.bind(("127.0.0.1", 0))
        squat.listen(1)
        second_link = list(net._links.values())[-1]
        try:
            second_link.listen_port = squat.getsockname()[1]
            with self.assertRaises(RuntimeError):
                net.__enter__()
        finally:
            squat.close()
        self.assertFalse(net._running)
        self.assertEqual(net._listeners, [])
        self.assertEqual(net._threads, [])
        self.assertIsNone(vol.host_path)
        # Retry after freeing the port: entry now succeeds cleanly.
        second_link.listen_port = testbed._free_port()
        with net:
            self.assertTrue(net._running)
            self.assertEqual(len(net._listeners), 2)

    def test_exit_closes_inflight_tcp_connections(self):
        """`_stop()` must terminate proxied connections that are still
        open — otherwise the pump threads sit in recv() forever and the
        sockets leak across tests. The client should observe EOF (or a
        reset) promptly after the testbed exits."""
        net = testbed.Network()
        listen_port, dst_port = _two_capsules(net)
        with echo_server(dst_port):
            net.__enter__()
            client = socket.create_connection(
                ("127.0.0.1", listen_port), timeout=2.0
            )
            try:
                client.sendall(b"ping")
                self.assertEqual(client.recv(4096), b"ping")
                # Connection is established and idle: the proxy tracks
                # its two sockets (client-side + upstream).
                with net._conns_lock:
                    self.assertEqual(len(net._conns), 2)
                net.__exit__(None, None, None)
                client.settimeout(1.0)
                try:
                    data = client.recv(4096)
                except socket.timeout:
                    self.fail(
                        "proxied connection not closed by testbed teardown"
                    )
                except OSError:
                    data = b""  # reset is fine too — connection is dead
                self.assertEqual(data, b"")
            finally:
                client.close()
        with net._conns_lock:
            self.assertEqual(net._conns, set())

    def test_network_can_be_reentered_after_exit(self):
        """Exiting and re-entering the same Network must serve traffic
        again — `_stop()` latches `_stopping`, which `_start()` has to
        clear or the second run's proxy loops exit instantly."""
        net = testbed.Network()
        listen_port, dst_port = _two_capsules(net)
        with echo_server(dst_port):
            with net:
                self.assertEqual(
                    _send_recv("127.0.0.1", listen_port, b"one\n"),
                    b"one\n",
                )
            with net:
                self.assertEqual(
                    _send_recv("127.0.0.1", listen_port, b"two\n",
                               timeout=0.5),
                    b"two\n",
                )


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

    def test_delay_holds_each_datagram_not_the_link(self):
        """A UDP delay applies per datagram: five datagrams sent back to
        back under a 300 ms delay all arrive ~300 ms later, not one every
        300 ms (1.5 s) as when the forwarder slept head-of-line."""
        net = testbed.Network()
        listen_addr, listen_port, dst_port = self._two_capsules_udp(net)
        fc_addr = net.address("fc")
        with udp_echo_server(fc_addr, dst_port) as received, net:
            with net.fault("sim", "fc", delay_ms=300):
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                    t0 = time.monotonic()
                    for i in range(5):
                        s.sendto(b"d%d" % i, (listen_addr, listen_port))
                    deadline = t0 + 3.0
                    while len(received) < 5 and time.monotonic() < deadline:
                        time.sleep(0.01)
                    elapsed = time.monotonic() - t0
        self.assertEqual(sorted(r[0] for r in received),
                         [b"d0", b"d1", b"d2", b"d3", b"d4"])
        self.assertGreaterEqual(elapsed, 0.29)
        self.assertLess(elapsed, 0.9)

    def test_delayed_reply_reaches_the_sender(self):
        """Replies take the same per-datagram delay path."""
        net = testbed.Network()
        listen_addr, listen_port, dst_port = self._two_capsules_udp(net)
        fc_addr = net.address("fc")
        with udp_echo_server(fc_addr, dst_port), net:
            with net.fault("sim", "fc", delay_ms=100):
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                    t0 = time.monotonic()
                    s.sendto(b"req", (listen_addr, listen_port))
                    s.settimeout(2.0)
                    reply, _ = s.recvfrom(4096)
                    elapsed = time.monotonic() - t0
        self.assertEqual(reply, b"req")
        self.assertGreaterEqual(elapsed, 0.19)  # delayed both ways

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

    def test_idle_sessions_evicted_under_continuous_traffic(self):
        """Session GC must run on a timer, not only on the recv-timeout
        branch — under continuous traffic that branch never fires, so
        idle sessions (and their upstream sockets + reply pumps) would
        pile up forever. Shrink the timers, let sender `a` idle past
        the timeout while `b` keeps the forwarder busy, and check `a`'s
        next datagram arrives via a *fresh* upstream socket (different
        source port at the destination)."""
        net = testbed.Network()
        listen_addr, listen_port, dst_port = self._two_capsules_udp(net)
        fc_addr = net.address("fc")
        net._UDP_SESSION_IDLE_TIMEOUT = 0.15
        net._UDP_GC_INTERVAL = 0.05
        with udp_echo_server(fc_addr, dst_port) as received, net:
            a = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            b = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                a.sendto(b"a1", (listen_addr, listen_port))
                # Keep traffic flowing so recvfrom never times out
                # while `a` sits idle past its session timeout.
                deadline = time.monotonic() + 0.5
                while time.monotonic() < deadline:
                    b.sendto(b"bb", (listen_addr, listen_port))
                    time.sleep(0.02)
                a.sendto(b"a2", (listen_addr, listen_port))
                time.sleep(0.2)
            finally:
                a.close()
                b.close()
        a_peers = [peer for data, peer in received if data.startswith(b"a")]
        self.assertEqual(len(a_peers), 2)
        self.assertNotEqual(
            a_peers[0][1], a_peers[1][1],
            "idle session was never evicted — both datagrams used the "
            "same upstream socket",
        )

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


class LifecycleAndFaultLayers(unittest.TestCase):
    def test_nested_with_keeps_the_outer_testbed_running(self):
        net = testbed.Network(gateway=None)
        listen_port, dst_port = _two_capsules(net)
        with echo_server(dst_port), net:
            with net:
                pass
            # The inner exit must not have stopped the proxies.
            self.assertEqual(_send_recv("127.0.0.1", listen_port, b"hi\n"), b"hi\n")
        self.assertFalse(net._running)

    def test_faults_can_end_in_any_order(self):
        net = testbed.Network(gateway=None)
        _two_capsules(net)
        rules = next(iter(net._links.values())).rules
        f1 = net.fault("sim", "fc", drop_rate=0.5)
        f2 = net.fault("sim", "fc", drop_rate=0.9, delay_ms=10)
        f1.__enter__()
        f2.__enter__()
        self.assertEqual((rules.drop_rate, rules.delay_ms), (0.9, 10))
        f1.__exit__(None, None, None)       # f2 is still active
        self.assertEqual((rules.drop_rate, rules.delay_ms), (0.9, 10))
        f2.__exit__(None, None, None)
        self.assertEqual((rules.drop_rate, rules.delay_ms), (0.0, 0.0))

    def test_partition_inside_a_fault_restores_the_fault(self):
        net = testbed.Network(gateway=None)
        _two_capsules(net)
        rules = next(iter(net._links.values())).rules
        with net.fault("sim", "fc", delay_ms=5):
            with net.partition(["sim"], ["fc"]):
                self.assertTrue(rules.blocked)
            self.assertFalse(rules.blocked)
            self.assertEqual(rules.delay_ms, 5)

    def test_tcp_delay_is_latency_not_a_stall_per_chunk(self):
        """1 MB through a 100 ms delay: one chunk per delay would take
        ~25 s; as latency it takes about 0.1 s more than without."""
        net = testbed.Network(gateway=None)
        listen_port, dst_port = _two_capsules(net)
        payload = b"x" * (1 << 20)
        with echo_server(dst_port), net:
            with net.fault("sim", "fc", delay_ms=100):
                t0 = time.monotonic()
                got = _send_recv("127.0.0.1", listen_port, payload, timeout=10)
                elapsed = time.monotonic() - t0
        self.assertEqual(len(got), len(payload))
        self.assertGreaterEqual(elapsed, 0.19)   # 100 ms each way
        self.assertLess(elapsed, 3.0)


class ContainerGateway(unittest.TestCase):
    """Capsules run on the container bridge, where 127.0.0.1 is the
    container itself, so each link also listens on the bridge gateway
    and `bindings()` hands capsules that endpoint. These tests use
    127.0.0.1 as a stand-in gateway so they don't depend on docker."""

    GW = "127.0.0.1"

    def test_capsule_env_gets_the_gateway_endpoint(self):
        net = testbed.Network(gateway=self.GW)
        net.declare("sim")
        net.declare("fc", listens_on=[5001])
        host_ep = net.link("sim", "fc", to_port=5001)
        link = next(iter(net._links.values()))
        env = net.bindings("sim")["env"]
        self.assertEqual(env["FC_5001_ADDR"], f"{self.GW}:{link.gateway_port}")
        # link() still returns the host-side endpoint.
        self.assertEqual(host_ep, f"127.0.0.1:{link.listen_port}")
        self.assertNotEqual(link.gateway_port, link.listen_port)

    def test_without_a_gateway_env_is_the_loopback_endpoint(self):
        net = testbed.Network(gateway=None)
        net.declare("sim")
        net.declare("fc", listens_on=[5001])
        host_ep = net.link("sim", "fc", to_port=5001)
        self.assertEqual(net.bindings("sim")["env"]["FC_5001_ADDR"], host_ep)

    def test_gateway_endpoint_forwards_under_the_same_rules(self):
        net = testbed.Network(gateway=self.GW)
        _, dst_port = _two_capsules(net)
        link = next(iter(net._links.values()))
        with echo_server(dst_port), net:
            self.assertEqual(
                _send_recv(self.GW, link.gateway_port, b"hi\n"), b"hi\n",
            )
            with net.partition(["sim"], ["fc"]):
                self.assertEqual(
                    _send_recv(self.GW, link.gateway_port, b"x\n", timeout=0.3),
                    b"",
                )

    def test_udp_gateway_endpoint_replies_to_the_sender(self):
        net = testbed.Network(gateway=self.GW)
        net.declare("sim")
        net.declare("fc", udp_listens_on=[5001])
        net.link("sim", "fc", to_port=5001, proto="udp")
        link = next(iter(net._links.values()))
        _, dst_port = net.bindings("fc")["publish_udp"][5001]
        with udp_echo_server(net.address("fc"), dst_port), net:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.settimeout(2.0)
                s.sendto(b"ping", (self.GW, link.gateway_port))
                reply, _ = s.recvfrom(4096)
        self.assertEqual(reply, b"ping")

    def test_environment_override(self):
        with mock.patch.dict(os.environ, {"RIGX_TESTBED_GATEWAY": "10.1.2.3"}):
            self.assertEqual(testbed._container_gateway(), "10.1.2.3")
        with mock.patch.dict(os.environ, {"RIGX_TESTBED_GATEWAY": ""}):
            self.assertIsNone(testbed._container_gateway())

    def test_discovery_skips_addresses_it_cannot_bind(self):
        testbed._discover_gateway.cache_clear()
        self.addCleanup(testbed._discover_gateway.cache_clear)
        out = mock.Mock(stdout="192.0.2.1 127.0.0.1\n")
        with mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch("rigx.testbed.shutil.which",
                        side_effect=lambda b: "/usr/bin/docker" if b == "docker" else None), \
             mock.patch("rigx.testbed.subprocess.run", return_value=out):
            # 192.0.2.1 (TEST-NET) isn't on this host; 127.0.0.1 is.
            self.assertEqual(testbed._container_gateway(), "127.0.0.1")


class RuleSet(unittest.TestCase):
    """Tests for the standalone RuleSet logic — no proxy needed."""

    def test_drop_rate_zero_passes_through(self):
        rs = testbed.RuleSet()
        self.assertEqual(rs.decide(b"hi")[0], b"hi")

    def test_blocked_returns_none(self):
        rs = testbed.RuleSet(blocked=True)
        self.assertIsNone(rs.decide(b"hi")[0])

    def test_corrupt_rate_one_flips_a_byte(self):
        random.seed(42)
        rs = testbed.RuleSet(corrupt_rate=1.0)
        out = rs.decide(b"hello")[0]
        self.assertEqual(len(out), len(b"hello"))
        self.assertNotEqual(out, b"hello")

    def test_drop_rate_one_returns_none(self):
        rs = testbed.RuleSet(drop_rate=1.0)
        self.assertIsNone(rs.decide(b"hi")[0])


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
