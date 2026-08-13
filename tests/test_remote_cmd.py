"""Tests for `rigx.commands.remote_cmd.cmd_remote_deploy` — the full
orchestration (bootstrap -> plan -> secrets -> activate). Every
network/Nix-touching call (`bootstrap`, `pack`, `secrets`) is mocked;
this only tests the wiring/sequencing/error-propagation, each of which
already has its own direct unit tests."""

from __future__ import annotations

import argparse
import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from textwrap import dedent
from unittest import mock

from rigx import config
from rigx.commands import remote_cmd
from rigx.deploy.bootstrap import DeployError, HostInfo, SSHTarget
from rigx.deploy.plan import PlanError


class TempProject:
    def __init__(self, toml_body: str):
        self.toml_body = dedent(toml_body).lstrip()

    def __enter__(self) -> Path:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        (root / "rigx.toml").write_text(self.toml_body)
        return root

    def __exit__(self, *exc) -> None:
        self._tmp.cleanup()


_ONE_TESTBED = """
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
"""

_TWO_TESTBEDS = """
    [project]
    name = "p"

    [targets.web_capsule]
    kind       = "capsule"
    backend    = "lite"
    entrypoint = "/bin/true"

    [targets.app_a]
    kind = "testbed"

    [targets.app_a.services.web]
    capsule = "web_capsule"

    [targets.app_b]
    kind = "testbed"

    [targets.app_b.services.web]
    capsule = "web_capsule"
"""

_NO_TESTBED = """
    [project]
    name = "p"

    [targets.web_capsule]
    kind       = "capsule"
    backend    = "lite"
    entrypoint = "/bin/true"
"""

_HOST_INFO = HostInfo(
    os_name="Linux", arch="x86_64", has_systemd=True, has_nix=True,
    has_container_engine=True,
)


def _args(root: Path, target="app", ssh_url="ssh://h") -> argparse.Namespace:
    return argparse.Namespace(project=str(root), target=target, ssh_url=ssh_url)


class ResolveTarget(unittest.TestCase):
    def test_named_target_resolved(self):
        with TempProject(_ONE_TESTBED) as root:
            project = config.load(root)
        t = remote_cmd._resolve_target(project, "app")
        self.assertEqual(t.name, "app")

    def test_selects_among_multiple_by_name(self):
        with TempProject(_TWO_TESTBEDS) as root:
            project = config.load(root)
        t = remote_cmd._resolve_target(project, "app_b")
        self.assertEqual(t.name, "app_b")

    def test_unknown_target_raises(self):
        with TempProject(_NO_TESTBED) as root:
            project = config.load(root)
        with self.assertRaises(DeployError) as cm:
            remote_cmd._resolve_target(project, "app")
        self.assertIn("no such target", str(cm.exception))

    def test_wrong_kind_raises(self):
        with TempProject(_ONE_TESTBED) as root:
            project = config.load(root)
        with self.assertRaises(DeployError) as cm:
            remote_cmd._resolve_target(project, "web_capsule")
        self.assertIn("not 'testbed'", str(cm.exception))

    def test_ghost_target_raises(self):
        with TempProject(_ONE_TESTBED) as root:
            project = config.load(root)
        with self.assertRaises(DeployError):
            remote_cmd._resolve_target(project, "ghost")


class FetchPreviousManifest(unittest.TestCase):
    def test_returns_none_when_cat_fails(self):
        target = SSHTarget(host="h")
        with mock.patch.object(
            remote_cmd.bootstrap, "run_remote",
        ) as fake:
            fake.return_value = mock.Mock(returncode=1, stdout="")
            result = remote_cmd._fetch_previous_manifest(target, "app")
        self.assertIsNone(result)

    def test_parses_json_when_present(self):
        target = SSHTarget(host="h")
        manifest = {"testbed": "app", "services": {}}
        with mock.patch.object(remote_cmd.bootstrap, "run_remote") as fake:
            import json
            fake.return_value = mock.Mock(returncode=0, stdout=json.dumps(manifest))
            result = remote_cmd._fetch_previous_manifest(target, "app")
        self.assertEqual(result, manifest)

    def test_returns_none_on_bad_json(self):
        target = SSHTarget(host="h")
        with mock.patch.object(remote_cmd.bootstrap, "run_remote") as fake:
            fake.return_value = mock.Mock(returncode=0, stdout="not json")
            result = remote_cmd._fetch_previous_manifest(target, "app")
        self.assertIsNone(result)


class CmdRemoteDeploy(unittest.TestCase):
    def test_happy_path_no_secrets(self):
        with TempProject(_ONE_TESTBED) as root:
            args = _args(root)
            with mock.patch.object(
                remote_cmd.bootstrap, "bootstrap", return_value=_HOST_INFO,
            ) as fake_bootstrap, mock.patch.object(
                remote_cmd.bootstrap, "run_remote",
                return_value=mock.Mock(returncode=1, stdout=""),
            ) as fake_run_remote, mock.patch.object(
                remote_cmd.pack, "build_and_push_plan",
                return_value="/nix/store/plan",
            ) as fake_push, mock.patch.object(
                remote_cmd.secrets, "push_secrets",
            ) as fake_secrets:
                out, err = io.StringIO(), io.StringIO()
                with redirect_stdout(out), redirect_stderr(err):
                    rc = remote_cmd.cmd_remote_deploy(args)

        self.assertEqual(rc, 0, err.getvalue())
        fake_bootstrap.assert_called_once_with("ssh://h")
        fake_push.assert_called_once()
        _, kwargs = fake_push.call_args
        self.assertIsNone(kwargs["previous_manifest"])
        fake_secrets.assert_not_called()
        # last run_remote call activates the plan via sudo
        last_call = fake_run_remote.call_args_list[-1]
        self.assertIn("sudo /nix/store/plan/bin/activate", last_call.args[1])
        self.assertIn("deployed", out.getvalue())

    def test_pushes_secrets_when_declared(self):
        body = _ONE_TESTBED + dedent("""
            [targets.app.secrets.api_key]
            source = "env:API_KEY"
        """)
        with TempProject(body) as root:
            args = _args(root)
            with mock.patch.object(
                remote_cmd.bootstrap, "bootstrap", return_value=_HOST_INFO,
            ), mock.patch.object(
                remote_cmd.bootstrap, "run_remote",
                return_value=mock.Mock(returncode=1, stdout=""),
            ), mock.patch.object(
                remote_cmd.pack, "build_and_push_plan",
                return_value="/nix/store/plan",
            ), mock.patch.object(
                remote_cmd.secrets, "push_secrets",
            ) as fake_secrets:
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    rc = remote_cmd.cmd_remote_deploy(args)

        self.assertEqual(rc, 0)
        fake_secrets.assert_called_once()
        args_, kwargs = fake_secrets.call_args
        self.assertEqual(args_[1], "app")
        self.assertEqual(args_[2], {"api_key": "env:API_KEY"})

    def test_bootstrap_failure_returns_1(self):
        with TempProject(_ONE_TESTBED) as root:
            args = _args(root)
            with mock.patch.object(
                remote_cmd.bootstrap, "bootstrap",
                side_effect=DeployError("no route to host"),
            ):
                out, err = io.StringIO(), io.StringIO()
                with redirect_stdout(out), redirect_stderr(err):
                    rc = remote_cmd.cmd_remote_deploy(args)
        self.assertEqual(rc, 1)
        self.assertIn("no route to host", err.getvalue())

    def test_plan_error_returns_1(self):
        with TempProject(_ONE_TESTBED) as root:
            args = _args(root)
            with mock.patch.object(
                remote_cmd.bootstrap, "bootstrap", return_value=_HOST_INFO,
            ), mock.patch.object(
                remote_cmd.bootstrap, "run_remote",
                return_value=mock.Mock(returncode=1, stdout=""),
            ), mock.patch.object(
                remote_cmd.pack, "build_and_push_plan",
                side_effect=PlanError("dependency cycle in services: web -> web"),
            ):
                out, err = io.StringIO(), io.StringIO()
                with redirect_stdout(out), redirect_stderr(err):
                    rc = remote_cmd.cmd_remote_deploy(args)
        self.assertEqual(rc, 1)
        self.assertIn("dependency cycle", err.getvalue())

    def test_unresolvable_target_returns_1_without_touching_network(self):
        with TempProject(_NO_TESTBED) as root:
            args = _args(root)
            with mock.patch.object(remote_cmd.bootstrap, "bootstrap") as fake_bootstrap:
                out, err = io.StringIO(), io.StringIO()
                with redirect_stdout(out), redirect_stderr(err):
                    rc = remote_cmd.cmd_remote_deploy(args)
        self.assertEqual(rc, 1)
        fake_bootstrap.assert_not_called()
        self.assertIn("no such target", err.getvalue())


_TWO_SERVICE_MANIFEST = {
    "testbed": "app", "start_order": ["db", "web"],
    "services": {"db": {}, "web": {}}, "volumes": [], "secrets": [],
}


class Lifecycle(unittest.TestCase):
    def _run(self, func, root, **kw):
        args = _args(root, **kw)
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = func(args)
        return rc, out.getvalue(), err.getvalue()

    def test_start_uses_manifest_order(self):
        with TempProject(_ONE_TESTBED) as root:
            with mock.patch.object(
                remote_cmd, "_fetch_previous_manifest",
                return_value=_TWO_SERVICE_MANIFEST,
            ), mock.patch.object(remote_cmd.bootstrap, "run_remote") as fake_run:
                rc, out, err = self._run(remote_cmd.cmd_remote_start, root)
        self.assertEqual(rc, 0, err)
        calls = [c.args[1] for c in fake_run.call_args_list]
        self.assertEqual(calls, [
            "sudo systemctl start rigx-app-db.service",
            "sudo systemctl start rigx-app-web.service",
        ])

    def test_stop_uses_reversed_manifest_order(self):
        with TempProject(_ONE_TESTBED) as root:
            with mock.patch.object(
                remote_cmd, "_fetch_previous_manifest",
                return_value=_TWO_SERVICE_MANIFEST,
            ), mock.patch.object(remote_cmd.bootstrap, "run_remote") as fake_run:
                rc, out, err = self._run(remote_cmd.cmd_remote_stop, root)
        self.assertEqual(rc, 0, err)
        calls = [c.args[1] for c in fake_run.call_args_list]
        self.assertEqual(calls, [
            "sudo systemctl stop rigx-app-web.service",
            "sudo systemctl stop rigx-app-db.service",
        ])

    def test_restart_uses_manifest_order(self):
        with TempProject(_ONE_TESTBED) as root:
            with mock.patch.object(
                remote_cmd, "_fetch_previous_manifest",
                return_value=_TWO_SERVICE_MANIFEST,
            ), mock.patch.object(remote_cmd.bootstrap, "run_remote") as fake_run:
                rc, out, err = self._run(remote_cmd.cmd_remote_restart, root)
        self.assertEqual(rc, 0, err)
        calls = [c.args[1] for c in fake_run.call_args_list]
        self.assertEqual(calls, [
            "sudo systemctl restart rigx-app-db.service",
            "sudo systemctl restart rigx-app-web.service",
        ])

    def test_no_active_deployment_errors_clearly(self):
        with TempProject(_ONE_TESTBED) as root:
            with mock.patch.object(
                remote_cmd, "_fetch_previous_manifest", return_value=None,
            ), mock.patch.object(remote_cmd.bootstrap, "run_remote") as fake_run:
                rc, out, err = self._run(remote_cmd.cmd_remote_start, root)
        self.assertEqual(rc, 1)
        fake_run.assert_not_called()
        self.assertIn("no active deployment", err)

    def test_unresolvable_target_returns_1_without_ssh(self):
        with TempProject(_NO_TESTBED) as root:
            with mock.patch.object(remote_cmd.bootstrap, "run_remote") as fake_run:
                rc, out, err = self._run(remote_cmd.cmd_remote_stop, root)
        self.assertEqual(rc, 1)
        fake_run.assert_not_called()
        self.assertIn("no such target", err)


class Rollback(unittest.TestCase):
    def test_invokes_current_profiles_rollback_script(self):
        with TempProject(_ONE_TESTBED) as root:
            args = _args(root)
            with mock.patch.object(remote_cmd.bootstrap, "run_remote") as fake_run:
                out, err = io.StringIO(), io.StringIO()
                with redirect_stdout(out), redirect_stderr(err):
                    rc = remote_cmd.cmd_remote_rollback(args)
        self.assertEqual(rc, 0, err.getvalue())
        fake_run.assert_called_once()
        cmd = fake_run.call_args.args[1]
        self.assertEqual(cmd, "sudo /nix/var/nix/profiles/rigx-app/bin/rollback")
        self.assertIn("rolled back", out.getvalue())

    def test_unresolvable_target_returns_1_without_ssh(self):
        with TempProject(_NO_TESTBED) as root:
            args = _args(root)
            with mock.patch.object(remote_cmd.bootstrap, "run_remote") as fake_run:
                out, err = io.StringIO(), io.StringIO()
                with redirect_stdout(out), redirect_stderr(err):
                    rc = remote_cmd.cmd_remote_rollback(args)
        self.assertEqual(rc, 1)
        fake_run.assert_not_called()
        self.assertIn("no such target", err.getvalue())


if __name__ == "__main__":
    unittest.main()
