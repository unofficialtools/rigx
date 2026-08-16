"""Tests for `rigx host ls` / `rigx host destroy`.

The defining property under test is what these commands *don't* need: no
`rigx.toml`, no target name, nothing local at all. That is not a
convenience — before this, a testbed whose project had been lost could
not be shut down through rigx at all, and the hand cleanup that
replaced it is how half-removed deployments come about.
"""

from __future__ import annotations

import argparse
import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from rigx import cli
from rigx.commands import host_cmd
from rigx.deploy import hostinv
from rigx.deploy.bootstrap import DeployError

_MANIFEST = {
    "testbed": "app",
    "start_order": ["db", "web"],
    "services": {
        "db": {"ports": []},
        "web": {"ports": [{"host": 80, "container": 8000, "publish": "80:8000"}]},
    },
}


def _args(**kw) -> argparse.Namespace:
    base = {"ssh_url": "ssh://root@h", "testbed": "app", "purge": False}
    base.update(kw)
    return argparse.Namespace(**base)


def _run(func, args):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = func(args)
    return rc, out.getvalue(), err.getvalue()


class HostLs(unittest.TestCase):
    def _ls(self, testbeds, containers):
        with mock.patch.object(
            host_cmd.hostinv, "inventory", return_value=(testbeds, containers),
        ):
            return _run(host_cmd.cmd_host_ls, _args())

    def test_lists_services_and_their_container_state(self):
        rc, out, err = self._ls(
            [hostinv.TestbedInfo("app", "/nix/store/gen-app", _MANIFEST)],
            [hostinv.Container(
                name="rigx-app-web", state="running",
                published=hostinv.parse_published("0.0.0.0:80->8000/tcp"),
            )],
        )
        self.assertEqual(rc, 0, err)
        self.assertIn("app", out)
        self.assertIn("/nix/store/gen-app", out)
        self.assertIn("running", out)
        self.assertIn("80->8000", out)
        # db has no container at all — that has to be visible, not blank.
        self.assertIn("no container", out)

    def test_reports_a_profile_with_no_readable_manifest(self):
        # An interrupted deploy, or a generation garbage-collected out
        # from under its profile. A real state, not something to hide.
        rc, out, err = self._ls(
            [hostinv.TestbedInfo("app", "/nix/store/gen", None)], [],
        )
        self.assertEqual(rc, 0, err)
        self.assertIn("no readable manifest", out)

    def test_flags_orphaned_rigx_containers(self):
        # The exact shape a half-removed deployment leaves behind, and
        # the reason the listing shows containers rather than only
        # profiles: this container still holds port 80.
        rc, out, err = self._ls(
            [hostinv.TestbedInfo("app", "/nix/store/gen", _MANIFEST)],
            [hostinv.Container(
                name="rigx-ghost-web", state="running",
                published=hostinv.parse_published("0.0.0.0:80->8080/tcp"),
            )],
        )
        self.assertEqual(rc, 0, err)
        self.assertIn("orphaned rigx containers", out)
        self.assertIn("rigx-ghost-web", out)
        self.assertIn("rigx host destroy", out)

    def test_containers_of_a_known_testbed_are_not_orphans(self):
        rc, out, err = self._ls(
            [hostinv.TestbedInfo("app", "/nix/store/gen", _MANIFEST)],
            [hostinv.Container(name="rigx-app-web", state="running")],
        )
        self.assertNotIn("orphaned", out)

    def test_non_rigx_containers_are_not_listed_as_orphans(self):
        rc, out, err = self._ls(
            [], [hostinv.Container(name="nginx", state="running")],
        )
        self.assertNotIn("orphaned", out)

    def test_empty_host(self):
        rc, out, err = self._ls([], [])
        self.assertEqual(rc, 0, err)
        self.assertIn("0 testbed(s)", out)

    def test_bad_ssh_url_is_a_clean_error(self):
        rc, out, err = _run(host_cmd.cmd_host_ls, _args(ssh_url="https://x"))
        self.assertEqual(rc, 1)
        self.assertIn("ssh://", err)


class HostDestroy(unittest.TestCase):
    def _destroy(self, present, **kw):
        with mock.patch.object(
            host_cmd.hostinv, "list_testbeds", return_value=present,
        ), mock.patch.object(host_cmd.hostinv, "teardown") as fake:
            rc, out, err = _run(host_cmd.cmd_host_destroy, _args(**kw))
        return rc, out, err, fake

    def test_removes_the_named_testbed_keeping_state(self):
        rc, out, err, fake = self._destroy(["app", "other"])
        self.assertEqual(rc, 0, err)
        fake.assert_called_once()
        self.assertEqual(fake.call_args.args[1], "app")
        self.assertFalse(fake.call_args.kwargs["purge"])
        self.assertIn("persistent volumes kept", out)
        self.assertIn("--purge", out)

    def test_purge_is_explicit(self):
        rc, out, err, fake = self._destroy(["app"], purge=True)
        self.assertEqual(rc, 0, err)
        self.assertTrue(fake.call_args.kwargs["purge"])
        self.assertNotIn("volumes kept", out)

    def test_unknown_testbed_still_cleans_up_leftovers(self):
        # A testbed whose profile is already gone can still have units
        # and containers behind it — which is precisely the mess this
        # command exists to clear.
        rc, out, err, fake = self._destroy(["other"])
        self.assertEqual(rc, 0, err)
        fake.assert_called_once()
        self.assertIn("no Nix profile", err)
        self.assertIn("other", err)

    def test_unsafe_name_is_refused(self):
        with mock.patch.object(
            host_cmd.hostinv, "list_testbeds", return_value=[],
        ), mock.patch.object(
            host_cmd.hostinv, "teardown",
            side_effect=DeployError("refusing to act on testbed name"),
        ):
            rc, out, err = _run(host_cmd.cmd_host_destroy, _args(testbed="a;rm -rf /"))
        self.assertEqual(rc, 1)
        self.assertIn("refusing", err)


class NoProjectNeeded(unittest.TestCase):
    """The whole point: these run where there is no rigx.toml to find."""

    def test_ls_runs_outside_any_project(self):
        cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                self.assertFalse((Path(tmp) / "rigx.toml").exists())
                with mock.patch.object(
                    host_cmd.hostinv, "inventory", return_value=([], []),
                ), mock.patch.object(cli, "_load", side_effect=AssertionError(
                    "rigx host must not load a project"
                )):
                    rc, out, err = _run(host_cmd.cmd_host_ls, _args())
            finally:
                os.chdir(cwd)
        self.assertEqual(rc, 0, err)

    def test_cli_parses_host_commands(self):
        p = cli.build_parser()
        a = p.parse_args(["host", "ls", "ssh://root@h"])
        self.assertIs(a.func, host_cmd.cmd_host_ls)
        a = p.parse_args(["host", "destroy", "ssh://root@h", "old", "--purge"])
        self.assertIs(a.func, host_cmd.cmd_host_destroy)
        self.assertEqual(a.testbed, "old")
        self.assertTrue(a.purge)

    def test_purge_is_off_unless_asked_for(self):
        a = cli.build_parser().parse_args(["host", "destroy", "ssh://root@h", "old"])
        self.assertFalse(a.purge)
