"""Tests for `rigx.deploy.bootstrap` — SSH target bootstrap.

No real ssh/network access: `subprocess.run` is mocked throughout, so
these test the bootstrap decision logic (what gets run, in what order,
under which conditions), not an actual remote host.
"""

from __future__ import annotations

import subprocess
import unittest
from unittest import mock

from rigx.deploy import bootstrap


def _completed(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr
    )


class ParseSSHURL(unittest.TestCase):
    def test_user_host_port(self):
        t = bootstrap.parse_ssh_url("ssh://deploy@example.com:2222")
        self.assertEqual(t.host, "example.com")
        self.assertEqual(t.user, "deploy")
        self.assertEqual(t.port, 2222)
        self.assertEqual(t.destination, "deploy@example.com")

    def test_host_only(self):
        t = bootstrap.parse_ssh_url("ssh://example.com")
        self.assertIsNone(t.user)
        self.assertIsNone(t.port)
        self.assertEqual(t.destination, "example.com")

    def test_rejects_non_ssh_scheme(self):
        with self.assertRaises(bootstrap.DeployError):
            bootstrap.parse_ssh_url("https://example.com")

    def test_rejects_missing_host(self):
        with self.assertRaises(bootstrap.DeployError):
            bootstrap.parse_ssh_url("ssh://")


class SSHTargetArgv(unittest.TestCase):
    def test_includes_port_when_set(self):
        t = bootstrap.SSHTarget(host="h", user="u", port=2222)
        argv = t.ssh_argv()
        self.assertEqual(argv[-1], "u@h")
        self.assertIn("-p", argv)
        self.assertIn("2222", argv)

    def test_omits_port_when_unset(self):
        t = bootstrap.SSHTarget(host="h")
        self.assertNotIn("-p", t.ssh_argv())


class RunRemote(unittest.TestCase):
    def test_raises_on_nonzero_exit(self):
        target = bootstrap.SSHTarget(host="h")
        with mock.patch.object(
            bootstrap.subprocess, "run", return_value=_completed(1, stderr="boom")
        ):
            with self.assertRaises(bootstrap.DeployError) as cm:
                bootstrap.run_remote(target, "false")
        self.assertIn("boom", str(cm.exception))

    def test_check_false_returns_result_without_raising(self):
        target = bootstrap.SSHTarget(host="h")
        with mock.patch.object(
            bootstrap.subprocess, "run", return_value=_completed(1)
        ):
            proc = bootstrap.run_remote(target, "false", check=False)
        self.assertEqual(proc.returncode, 1)


class DetectHost(unittest.TestCase):
    def test_parses_uname_and_probes(self):
        target = bootstrap.SSHTarget(host="h")

        def fake_run(argv, **kw):
            cmd = argv[-1]
            if cmd.startswith("uname"):
                return _completed(0, stdout="Linux x86_64\n")
            return _completed(0)  # systemctl, nix, and engine probes all succeed

        with mock.patch.object(bootstrap.subprocess, "run", side_effect=fake_run):
            info = bootstrap.detect_host(target)

        self.assertEqual(info.os_name, "Linux")
        self.assertEqual(info.arch, "x86_64")
        self.assertTrue(info.has_systemd)
        self.assertTrue(info.has_nix)
        self.assertTrue(info.has_container_engine)

    def test_missing_systemd_nix_and_engine(self):
        target = bootstrap.SSHTarget(host="h")

        def fake_run(argv, **kw):
            cmd = argv[-1]
            if cmd.startswith("uname"):
                return _completed(0, stdout="Linux aarch64\n")
            return _completed(1)  # all probes fail

        with mock.patch.object(bootstrap.subprocess, "run", side_effect=fake_run):
            info = bootstrap.detect_host(target)

        self.assertFalse(info.has_systemd)
        self.assertFalse(info.has_nix)
        self.assertFalse(info.has_container_engine)

    def test_rejects_non_linux(self):
        target = bootstrap.SSHTarget(host="h")
        with mock.patch.object(
            bootstrap.subprocess, "run", return_value=_completed(0, stdout="Darwin arm64\n")
        ):
            with self.assertRaises(bootstrap.DeployError):
                bootstrap.detect_host(target)

    def test_malformed_uname_output_raises(self):
        target = bootstrap.SSHTarget(host="h")
        with mock.patch.object(
            bootstrap.subprocess, "run", return_value=_completed(0, stdout="garbage\n")
        ):
            with self.assertRaises(bootstrap.DeployError):
                bootstrap.detect_host(target)


class Bootstrap(unittest.TestCase):
    def _fake_run(self, calls, *, nix_present_before_install, engine_present_before_install=True):
        """Build a fake `subprocess.run` that tracks call order in
        `calls` and flips the nix/engine probes to "present" once their
        respective installer commands have actually run."""
        state = {
            "nix": nix_present_before_install,
            "engine": engine_present_before_install,
        }

        def fake_run(argv, **kw):
            cmd = argv[-1]
            calls.append(cmd)
            if cmd.startswith("uname"):
                return _completed(0, stdout="Linux x86_64\n")
            if "systemctl" in cmd:
                return _completed(0)
            if "install.determinate.systems" in cmd:
                state["nix"] = True
                return _completed(0)
            # The install command embeds the engine check as its own
            # guard, so match on something unique to the install body —
            # not on "podman", which the bare check also contains.
            if "apt-get install -y podman" in cmd:
                state["engine"] = True
                return _completed(0)
            if cmd == bootstrap._NIX_CHECK:
                return _completed(0 if state["nix"] else 1)
            if cmd == bootstrap._CONTAINER_ENGINE_CHECK:
                return _completed(0 if state["engine"] else 1)
            if "useradd" in cmd or "id -u" in cmd:
                return _completed(0)
            return _completed(0)

        return fake_run

    def test_installs_nix_when_missing_then_creates_user(self):
        calls: list[str] = []
        fake_run = self._fake_run(calls, nix_present_before_install=False)
        with mock.patch.object(bootstrap.subprocess, "run", side_effect=fake_run):
            info = bootstrap.bootstrap("ssh://h")

        self.assertTrue(info.has_nix)
        self.assertTrue(any("install.determinate.systems" in c for c in calls))
        self.assertTrue(any("useradd" in c or "id -u" in c for c in calls))
        # install happens before the user is (re-)created
        install_idx = next(i for i, c in enumerate(calls) if "install.determinate.systems" in c)
        user_idx = next(i for i, c in enumerate(calls) if "useradd" in c or "id -u" in c)
        self.assertLess(install_idx, user_idx)

    def test_skips_install_when_nix_already_present(self):
        calls: list[str] = []
        fake_run = self._fake_run(calls, nix_present_before_install=True)
        with mock.patch.object(bootstrap.subprocess, "run", side_effect=fake_run):
            info = bootstrap.bootstrap("ssh://h")

        self.assertTrue(info.has_nix)
        self.assertFalse(any("install.determinate.systems" in c for c in calls))

    def test_installs_podman_when_missing_then_creates_user(self):
        calls: list[str] = []
        fake_run = self._fake_run(
            calls, nix_present_before_install=True, engine_present_before_install=False,
        )
        with mock.patch.object(bootstrap.subprocess, "run", side_effect=fake_run):
            info = bootstrap.bootstrap("ssh://h")

        self.assertTrue(info.has_container_engine)
        self.assertTrue(any("apt-get install -y podman" in c for c in calls))
        # podman install happens before the user is (re-)created
        install_idx = next(
            i for i, c in enumerate(calls) if "apt-get install -y podman" in c
        )
        user_idx = next(i for i, c in enumerate(calls) if "useradd" in c or "id -u" in c)
        self.assertLess(install_idx, user_idx)

    def test_podman_install_dispatches_across_package_managers(self):
        """The install command has to cope with whatever package manager
        the target actually has — podman ships no equivalent of
        get.docker.com, so this is a distro dispatch, not one URL."""
        calls: list[str] = []
        fake_run = self._fake_run(
            calls, nix_present_before_install=True, engine_present_before_install=False,
        )
        with mock.patch.object(bootstrap.subprocess, "run", side_effect=fake_run):
            bootstrap.bootstrap("ssh://h")

        install = next(c for c in calls if "apt-get install -y podman" in c)
        for mgr in ("apt-get", "dnf", "yum", "zypper", "pacman", "apk"):
            self.assertIn(mgr, install, f"{mgr} branch missing from podman install")
        # Never reinstalls over an engine the host already has.
        self.assertTrue(install.startswith(bootstrap._CONTAINER_ENGINE_CHECK))

    def test_skips_podman_install_when_engine_already_present(self):
        """An existing docker install still satisfies bootstrap — podman
        is only what we *install*, not what we require."""
        calls: list[str] = []
        fake_run = self._fake_run(
            calls, nix_present_before_install=True, engine_present_before_install=True,
        )
        with mock.patch.object(bootstrap.subprocess, "run", side_effect=fake_run):
            info = bootstrap.bootstrap("ssh://h")

        self.assertTrue(info.has_container_engine)
        self.assertFalse(any("apt-get install -y podman" in c for c in calls))

    def test_raises_when_systemd_missing(self):
        def fake_run(argv, **kw):
            cmd = argv[-1]
            if cmd.startswith("uname"):
                return _completed(0, stdout="Linux x86_64\n")
            if "systemctl" in cmd:
                return _completed(1)
            return _completed(0)

        with mock.patch.object(bootstrap.subprocess, "run", side_effect=fake_run):
            with self.assertRaises(bootstrap.DeployError):
                bootstrap.bootstrap("ssh://h")

    def test_raises_when_nix_still_missing_after_install(self):
        def fake_run(argv, **kw):
            cmd = argv[-1]
            if cmd.startswith("uname"):
                return _completed(0, stdout="Linux x86_64\n")
            if "systemctl" in cmd:
                return _completed(0)
            if cmd == bootstrap._NIX_CHECK:
                return _completed(1)  # never reports success, even post-install
            return _completed(0)

        with mock.patch.object(bootstrap.subprocess, "run", side_effect=fake_run):
            with self.assertRaises(bootstrap.DeployError):
                bootstrap.bootstrap("ssh://h")

    def test_raises_when_engine_still_missing_after_install(self):
        def fake_run(argv, **kw):
            cmd = argv[-1]
            if cmd.startswith("uname"):
                return _completed(0, stdout="Linux x86_64\n")
            if "systemctl" in cmd:
                return _completed(0)
            if cmd == bootstrap._NIX_CHECK:
                return _completed(0)
            if cmd == bootstrap._CONTAINER_ENGINE_CHECK:
                return _completed(1)  # never reports success, even post-install
            return _completed(0)

        with mock.patch.object(bootstrap.subprocess, "run", side_effect=fake_run):
            with self.assertRaises(bootstrap.DeployError):
                bootstrap.bootstrap("ssh://h")


if __name__ == "__main__":
    unittest.main()
