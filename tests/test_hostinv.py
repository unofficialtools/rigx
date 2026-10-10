"""Tests for `rigx.deploy.hostinv` — host-level inventory, port-conflict
detection and teardown.

The parsing/matching half is pure and tested directly. The half that
talks to a host is tested by mocking `run_remote` (the same seam
test_remote_cmd.py uses), plus one behavioral test that executes the
generated teardown script under bash against a real temp filesystem
with fake `systemctl`/`podman` on PATH — the approach
test_deploy_activate.py takes, because a teardown script that merely
*looks* right is exactly the failure this whole change is about.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from textwrap import dedent
from unittest import mock

from rigx import config
from rigx.deploy import hostinv
from rigx.deploy.bootstrap import DeployError, SSHTarget

_BASH = shutil.which("bash") or "/bin/bash"
_TARGET = SSHTarget(host="h", user="root")


def _proc(stdout="", returncode=0):
    return mock.Mock(stdout=stdout, stderr="", returncode=returncode)


class ParsePublished(unittest.TestCase):
    """`{{.Ports}}` is the only place either engine says which host
    ports a container actually holds, and it has more shapes than it
    looks like."""

    def test_ipv4_publish(self):
        self.assertEqual(
            hostinv.parse_published("0.0.0.0:80->8000/tcp"),
            frozenset({("0.0.0.0", 80)}),
        )

    def test_ipv6_forms(self):
        self.assertEqual(
            hostinv.parse_published(":::80->8000/tcp"),
            frozenset({("::", 80)}),
        )
        self.assertEqual(
            hostinv.parse_published("[::1]:80->8000/tcp"),
            frozenset({("[::1]", 80)}),
        )

    def test_several_entries(self):
        self.assertEqual(
            hostinv.parse_published("0.0.0.0:80->8000/tcp, :::80->8000/tcp"),
            frozenset({("0.0.0.0", 80), ("::", 80)}),
        )

    def test_host_range(self):
        self.assertEqual(
            hostinv.parse_published("0.0.0.0:8000-8002->8000-8002/tcp"),
            frozenset({("0.0.0.0", 8000), ("0.0.0.0", 8001), ("0.0.0.0", 8002)}),
        )

    def test_exposed_but_not_published_is_no_host_port(self):
        # `8000/tcp` with no `->` is EXPOSE, not -p: it binds nothing on
        # the host and therefore cannot collide with anything.
        self.assertEqual(hostinv.parse_published("8000/tcp"), frozenset())

    def test_empty_and_garbage(self):
        self.assertEqual(hostinv.parse_published(""), frozenset())
        self.assertEqual(hostinv.parse_published("nonsense->x/tcp"), frozenset())


_OURS = hostinv.service_containers("app", ["web"])


class ContainerNaming(unittest.TestCase):
    def test_ownership_is_by_exact_name(self):
        # `app` and `app-staging` share a name prefix; `app`'s web
        # service is exactly one container name, never a prefix match.
        self.assertEqual(
            hostinv.service_containers("app", ["web", "db"]),
            {"rigx-app-web", "rigx-app-db"},
        )
        self.assertNotIn(
            "rigx-app-staging-web", hostinv.service_containers("app", ["web"]),
        )

    def test_non_rigx_container_has_no_testbed(self):
        self.assertIsNone(hostinv.Container(name="nginx", state="running").testbed())

    def test_running_covers_both_engines_wording(self):
        self.assertTrue(hostinv.Container(name="c", state="running").running)
        self.assertTrue(hostinv.Container(name="c", state="Up 2 days").running)
        self.assertFalse(hostinv.Container(name="c", state="exited").running)


class ListContainers(unittest.TestCase):
    def test_parses_engine_output(self):
        out = (
            "rigx-hello-web|running|0.0.0.0:80->8080/tcp\n"
            "rigx-app-db|exited|\n"
            "nginx|running|0.0.0.0:443->443/tcp\n"
        )
        with mock.patch.object(hostinv, "run_remote", return_value=_proc(out)):
            cs = hostinv.list_containers(_TARGET)
        self.assertEqual([c.name for c in cs], ["rigx-hello-web", "rigx-app-db", "nginx"])
        self.assertEqual(cs[0].published, frozenset({("0.0.0.0", 80)}))
        self.assertFalse(cs[1].running)

    def test_ignores_blank_and_short_lines(self):
        with mock.patch.object(hostinv, "run_remote", return_value=_proc("\n|\nx|y\n")):
            self.assertEqual(hostinv.list_containers(_TARGET), [])

    def test_engine_missing_is_empty_not_an_error(self):
        # The `|| true` tail means a host with no engine returns nothing
        # rather than failing the deploy before it has said anything
        # useful.
        with mock.patch.object(hostinv, "run_remote", return_value=_proc("", 0)):
            self.assertEqual(hostinv.list_containers(_TARGET), [])


_TESTBED_WITH_PORTS = """
    [project]
    name = "p"

    [targets.web_capsule]
    kind       = "capsule"
    backend    = "lite"
    entrypoint = "/bin/true"

    [targets.app]
    kind = "testbed"

    [targets.app.services.web]
    capsule = "web_capsule"
    ports   = [{ host = 80, container = 8000 }]
"""


def _testbed(toml_body: str, name="app"):
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "rigx.toml").write_text(dedent(toml_body).lstrip())
        return config.load(root).targets[name]


class PortConflicts(unittest.TestCase):
    """The check that turns 'failed healthcheck after 20 tries' into
    'port 80 is held by container X'."""

    def setUp(self):
        self.t = _testbed(_TESTBED_WITH_PORTS)
        self.wanted = hostinv.declared_host_ports(self.t)

    def _c(self, name, state="running", ports="0.0.0.0:80->8080/tcp"):
        return hostinv.Container(
            name=name, state=state, published=hostinv.parse_published(ports),
        )

    def test_another_testbeds_running_container_conflicts(self):
        cs = [self._c("rigx-hello-web")]
        conflicts = hostinv.find_port_conflicts(cs, _OURS, self.wanted)
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0].port, 80)
        self.assertEqual(conflicts[0].holder, "rigx-hello-web")
        self.assertEqual(conflicts[0].holder_testbed, "hello")

    def test_our_own_container_does_not_conflict_with_us(self):
        # A redeploy always finds the generation it is replacing still
        # holding the port; restarting the unit is what frees it.
        cs = [self._c("rigx-app-web")]
        self.assertEqual(hostinv.find_port_conflicts(cs, _OURS, self.wanted), [])

    def test_stopped_container_holds_nothing(self):
        cs = [self._c("rigx-hello-web", state="exited")]
        self.assertEqual(hostinv.find_port_conflicts(cs, _OURS, self.wanted), [])

    def test_non_rigx_container_still_conflicts(self):
        # A hand-started nginx breaks the deploy just as thoroughly, and
        # naming it is the whole point of the check.
        cs = [self._c("nginx")]
        conflicts = hostinv.find_port_conflicts(cs, _OURS, self.wanted)
        self.assertEqual(len(conflicts), 1)
        self.assertIsNone(conflicts[0].holder_testbed)

    def test_different_port_does_not_conflict(self):
        cs = [self._c("rigx-hello-web", ports="0.0.0.0:8080->8080/tcp")]
        self.assertEqual(hostinv.find_port_conflicts(cs, _OURS, self.wanted), [])

    def test_disjoint_bind_addresses_do_not_conflict(self):
        # Two services can share a port number on different interfaces.
        # Reporting that as a conflict would block a legitimate deploy.
        t = _testbed(
            _TESTBED_WITH_PORTS.replace(
                'ports   = [{ host = 80, container = 8000 }]',
                'ports   = [{ host = "127.0.0.1:80", container = 8000 }]',
            )
        )
        wanted = hostinv.declared_host_ports(t)
        cs = [self._c("rigx-hello-web", ports="192.168.1.5:80->8080/tcp")]
        self.assertEqual(hostinv.find_port_conflicts(cs, _OURS, wanted), [])

    def test_wildcard_bind_conflicts_with_a_specific_one(self):
        t = _testbed(
            _TESTBED_WITH_PORTS.replace(
                'ports   = [{ host = 80, container = 8000 }]',
                'ports   = [{ host = "127.0.0.1:80", container = 8000 }]',
            )
        )
        wanted = hostinv.declared_host_ports(t)
        cs = [self._c("rigx-hello-web", ports="0.0.0.0:80->8080/tcp")]
        self.assertEqual(len(hostinv.find_port_conflicts(cs, _OURS, wanted)), 1)

    def test_no_declared_ports_means_nothing_to_check(self):
        t = _testbed(
            _TESTBED_WITH_PORTS.replace(
                'ports   = [{ host = 80, container = 8000 }]', ""
            )
        )
        self.assertEqual(hostinv.declared_host_ports(t), {})

    def test_message_names_holder_and_the_way_out(self):
        cs = [self._c("rigx-hello-web")]
        msg = hostinv.format_conflicts(
            "app", hostinv.find_port_conflicts(cs, _OURS, self.wanted),
        )
        self.assertIn("port 80", msg)
        self.assertIn("rigx-hello-web", msg)
        self.assertIn("rigx host destroy", msg)
        self.assertIn("host_exclusive", msg)


class ProfileListing(unittest.TestCase):
    def test_generation_links_are_not_testbeds(self):
        listing = "rigx-app\nrigx-app-1-link\nrigx-app-2-link\nrigx-hello\ndefault\n"
        self.assertEqual(hostinv.parse_profile_names(listing), ["app", "hello"])

    def test_non_rigx_profiles_ignored(self):
        self.assertEqual(hostinv.parse_profile_names("default\nper-user\n"), [])

    def test_list_testbeds_reads_the_profile_dir(self):
        with mock.patch.object(
            hostinv, "run_remote", return_value=_proc("rigx-a\nrigx-b\n"),
        ):
            self.assertEqual(hostinv.list_testbeds(_TARGET), ["a", "b"])


class NameSafety(unittest.TestCase):
    """Testbed names reach a remote root shell as glob fragments and as
    an engine `--filter name=` regex. They are checked, not trusted."""

    def test_rejects_shell_metacharacters(self):
        for bad in ("a b", "a;rm -rf /", "$(id)", "../../etc", "a'b", ""):
            with self.assertRaises(DeployError, msg=bad):
                hostinv.teardown_script(bad)

    def test_accepts_ordinary_target_names(self):
        for good in ("app", "hello_py4web_prod", "web-1", "a.b"):
            self.assertIn(f"T={good}", hostinv.teardown_script(good))


class TeardownScript(unittest.TestCase):
    def test_keeps_state_by_default(self):
        script = hostinv.teardown_script("app")
        self.assertNotIn('rm -rf "/var/lib/rigx/$T"', script)

    def test_purge_removes_state(self):
        script = hostinv.teardown_script("app", purge=True)
        self.assertIn('rm -rf "/var/lib/rigx/$T"', script)

    def test_profile_is_unlinked_last(self):
        # An interrupted teardown must leave the testbed still visible to
        # `rigx host ls`, not invisible and half-alive.
        script = hostinv.teardown_script("app")
        self.assertGreater(
            script.index('rm -f "$P/rigx-$T"\n'),
            script.index("podman rm -f"),
        )

    def test_teardown_feeds_the_script_on_stdin(self):
        # Not shell-quoted into an argument: the script contains quoting
        # of its own, and `sh -s` sidesteps the whole question.
        with mock.patch.object(hostinv, "run_remote") as fake:
            hostinv.teardown(_TARGET, "app")
        fake.assert_called_once()
        self.assertEqual(fake.call_args.args[1], "sudo sh -s")
        self.assertIn("T=app", fake.call_args.kwargs["stdin"])


class TeardownExecution(unittest.TestCase):
    """Runs the real generated script under bash against a temp
    filesystem, with fake `systemctl`/`podman` recording their calls."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.root = root
        self.bindir = root / "bin"
        self.bindir.mkdir()
        self.log = root / "calls.log"
        for name in ("systemctl", "podman"):
            p = self.bindir / name
            p.write_text(f'#!{_BASH}\necho "{name} $*" >> "$CALL_LOG"\nexit 0\n')
            p.chmod(0o755)
        self.units = root / "etc"
        self.units.mkdir()
        self.profiles = root / "profiles"
        self.profiles.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, script: str):
        # Point the script's absolute paths at the temp tree.
        script = script.replace("/etc/systemd/system", str(self.units))
        script = script.replace("/nix/var/nix/profiles", str(self.profiles))
        # Never the real host paths, even ones a test run can't write.
        script = script.replace("/nix/var/nix/gcroots", str(self.root / "gcroots"))
        script = script.replace("/var/lib/rigx", str(self.root / "state"))
        # Prepend to the inherited PATH rather than naming /usr/bin:/bin.
        # The suite also runs inside a Nix build sandbox, which has
        # neither — the real `rm`/`ls` the script uses live in stdenv's
        # coreutils, wherever PATH says that is.
        env = dict(os.environ)
        env["PATH"] = f"{self.bindir}:{env['PATH']}"
        env["CALL_LOG"] = str(self.log)
        return subprocess.run(
            [_BASH, "-c", script], env=env, capture_output=True, text=True,
        )

    def _unit(self, testbed: str, name: str, what: str = "service web") -> None:
        (self.units / name).write_text(
            f"[Unit]\nDescription=rigx testbed {testbed} {what}\n"
        )

    def test_removes_units_containers_and_profile(self):
        self._unit("app", "rigx-app-web.service")
        self._unit("app", "rigx-app-web-health.timer", "health timer for web")
        (self.profiles / "rigx-app").write_text("x")
        (self.profiles / "rigx-app-1-link").write_text("x")
        # A different testbed sharing the name prefix must survive.
        self._unit("app_prod", "rigx-app_prod-web.service")
        (self.profiles / "rigx-app_prod").write_text("x")

        r = self._run(hostinv.teardown_script("app"))
        self.assertEqual(r.returncode, 0, r.stderr)

        self.assertFalse((self.units / "rigx-app-web.service").exists())
        self.assertFalse((self.units / "rigx-app-web-health.timer").exists())
        self.assertFalse((self.profiles / "rigx-app").exists())
        self.assertFalse((self.profiles / "rigx-app-1-link").exists())
        self.assertTrue((self.units / "rigx-app_prod-web.service").exists())
        self.assertTrue((self.profiles / "rigx-app_prod").exists())

        calls = self.log.read_text()
        self.assertIn("systemctl disable --now rigx-app-web.service", calls)
        self.assertIn("systemctl daemon-reload", calls)
        self.assertIn("podman network rm rigx-app", calls)

    def test_a_dashed_testbed_sharing_the_prefix_survives(self):
        """`app-staging`'s units, containers and generations match every
        `rigx-app-*` glob; tearing down `app` must leave them alone."""
        self._unit("app", "rigx-app-web.service")
        self._unit("app-staging", "rigx-app-staging-web.service")
        (self.profiles / "rigx-app-3-link").write_text("x")
        (self.profiles / "rigx-app-staging").write_text("x")
        (self.profiles / "rigx-app-staging-2-link").write_text("x")

        r = self._run(hostinv.teardown_script("app"))
        self.assertEqual(r.returncode, 0, r.stderr)

        self.assertFalse((self.units / "rigx-app-web.service").exists())
        self.assertFalse((self.profiles / "rigx-app-3-link").exists())
        self.assertTrue((self.units / "rigx-app-staging-web.service").exists())
        self.assertTrue((self.profiles / "rigx-app-staging").exists())
        self.assertTrue((self.profiles / "rigx-app-staging-2-link").exists())
        calls = self.log.read_text()
        self.assertIn("podman rm -f rigx-app-web", calls)
        self.assertNotIn("rigx-app-staging-web", calls)

    def test_containers_of_uninstalled_units_come_from_the_plans(self):
        plan = self.root / "plan"
        (plan / "units").mkdir(parents=True)
        (plan / "units" / "rigx-app-worker.service").write_text("x")
        (self.profiles / "rigx-app").symlink_to(plan)
        r = self._run(hostinv.teardown_script("app"))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("podman rm -f rigx-app-worker", self.log.read_text())

    def test_succeeds_on_a_host_with_nothing_to_remove(self):
        # Idempotence isn't cosmetic here: `rigx host destroy` is the
        # tool you reach for when you don't know what state the host is
        # in, so it has to work on a host that is already clean.
        r = self._run(hostinv.teardown_script("app"))
        self.assertEqual(r.returncode, 0, r.stderr)
