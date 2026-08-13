"""Tests for `rigx.deploy.secrets` — secret source resolution + piping
over ssh into `systemd-creds encrypt`. `subprocess.run` is mocked; no
real ssh/systemd-creds involved."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from rigx.deploy import secrets
from rigx.deploy.bootstrap import DeployError, SSHTarget


def _completed(returncode=0, stdout=b"", stderr=b""):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


class ResolveSecretSource(unittest.TestCase):
    def test_env_source(self):
        with mock.patch.dict("os.environ", {"MY_SECRET": "hunter2"}):
            self.assertEqual(
                secrets.resolve_secret_source("env:MY_SECRET", project_root=Path(".")),
                b"hunter2",
            )

    def test_env_source_missing_raises(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(DeployError) as cm:
                secrets.resolve_secret_source("env:NOPE", project_root=Path("."))
        self.assertIn("NOPE", str(cm.exception))

    def test_file_source_relative_to_project_root(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "secret.txt").write_bytes(b"topsecret")
            self.assertEqual(
                secrets.resolve_secret_source("file:secret.txt", project_root=root),
                b"topsecret",
            )

    def test_file_source_absolute(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "s.txt"
            path.write_bytes(b"abs-secret")
            self.assertEqual(
                secrets.resolve_secret_source(f"file:{path}", project_root=Path("/nope")),
                b"abs-secret",
            )

    def test_file_source_missing_raises(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(DeployError):
                secrets.resolve_secret_source("file:missing.txt", project_root=Path(d))

    def test_unsupported_source_prefix(self):
        with self.assertRaises(DeployError) as cm:
            secrets.resolve_secret_source("vault:x", project_root=Path("."))
        self.assertIn("env:", str(cm.exception))
        self.assertIn("file:", str(cm.exception))


class PushSecret(unittest.TestCase):
    def test_pipes_value_as_stdin_and_targets_cred_path(self):
        target = SSHTarget(host="h", user="rigx")
        captured = {}

        def fake_run(argv, **kw):
            captured["argv"] = argv
            captured["input"] = kw.get("input")
            return _completed(0)

        with mock.patch.object(secrets.subprocess, "run", side_effect=fake_run):
            secrets.push_secret(target, "app", "api_key", b"sekret-bytes")

        self.assertEqual(captured["input"], b"sekret-bytes")
        cmd = captured["argv"][-1]
        self.assertIn("systemd-creds encrypt", cmd)
        self.assertIn("--name=api_key", cmd)
        self.assertIn("/var/lib/rigx/app/secrets/api_key.cred", cmd)
        self.assertIn("sudo", cmd)

    def test_raises_deployerror_on_failure(self):
        target = SSHTarget(host="h")
        with mock.patch.object(
            secrets.subprocess, "run",
            return_value=_completed(1, stderr=b"tpm unavailable"),
        ):
            with self.assertRaises(DeployError) as cm:
                secrets.push_secret(target, "app", "api_key", b"x")
        self.assertIn("tpm unavailable", str(cm.exception))
        self.assertIn("api_key", str(cm.exception))


class PushSecrets(unittest.TestCase):
    def test_pushes_each_declared_secret_in_name_order(self):
        target = SSHTarget(host="h")
        pushed = []

        def fake_push(t, testbed, name, value):
            pushed.append((testbed, name, value))

        with mock.patch.dict("os.environ", {"B_VAR": "b-val"}):
            with tempfile.TemporaryDirectory() as d:
                root = Path(d)
                (root / "a.txt").write_bytes(b"a-val")
                with mock.patch.object(secrets, "push_secret", side_effect=fake_push):
                    secrets.push_secrets(
                        target, "app",
                        {"b_secret": "env:B_VAR", "a_secret": "file:a.txt"},
                        project_root=root,
                    )

        self.assertEqual(
            pushed,
            [("app", "a_secret", b"a-val"), ("app", "b_secret", b"b-val")],
        )


if __name__ == "__main__":
    unittest.main()
