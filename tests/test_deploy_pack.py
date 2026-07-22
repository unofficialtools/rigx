"""Tests for `rigx.deploy.pack` — plan assembly, capsule resolution,
and the `nix store add` / `nix copy` steps. Nix and `rigx.builder` are
mocked throughout; `assemble_plan_dir` is exercised for real against a
temp directory."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from rigx.config import Target, TestbedService
from rigx.deploy import pack
from rigx.deploy.bootstrap import DeployError, SSHTarget


def _completed(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


class ResolveCapsuleBackends(unittest.TestCase):
    def test_maps_service_capsules_to_their_backend_no_build_needed(self):
        testbed = Target(name="app", kind="testbed", services={
            "web": TestbedService(name="web", capsule="web_capsule"),
            "web2": TestbedService(name="web2", capsule="web_capsule"),
            "db": TestbedService(name="db", capsule="db_capsule"),
        })
        project = mock.Mock(targets={
            "web_capsule": Target(name="web_capsule", kind="capsule", backend="lite"),
            "db_capsule": Target(name="db_capsule", kind="capsule", backend="qemu"),
        })
        with mock.patch.object(pack.builder, "build") as fake_build:
            backends = pack.resolve_capsule_backends(project, testbed)
        fake_build.assert_not_called()
        self.assertEqual(backends, {"web_capsule": "lite", "db_capsule": "qemu"})


class ResolveCapsulePaths(unittest.TestCase):
    def test_builds_unique_referenced_capsules(self):
        testbed = Target(name="app", kind="testbed", services={
            "web": TestbedService(name="web", capsule="web_capsule"),
            "web2": TestbedService(name="web2", capsule="web_capsule"),  # shared capsule
            "db": TestbedService(name="db", capsule="db_capsule"),
        })
        calls = []

        def fake_build(project, names):
            calls.append(sorted(names))
            return [(n, Path(f"/fake/output/{n}")) for n in names]

        with mock.patch.object(pack.builder, "build", side_effect=fake_build):
            paths = pack.resolve_capsule_paths(project=object(), testbed=testbed)

        self.assertEqual(calls, [["db_capsule", "web_capsule"]])
        self.assertEqual(set(paths), {"db_capsule", "web_capsule"})

    def test_no_services_builds_nothing(self):
        testbed = Target(name="app", kind="testbed", services={})
        with mock.patch.object(pack.builder, "build") as fake_build:
            paths = pack.resolve_capsule_paths(project=object(), testbed=testbed)
        fake_build.assert_not_called()
        self.assertEqual(paths, {})

    def test_missing_output_link_raises(self):
        testbed = Target(name="app", kind="testbed", services={
            "web": TestbedService(name="web", capsule="web_capsule"),
        })
        with mock.patch.object(pack.builder, "build", return_value=[]):
            with self.assertRaises(DeployError) as cm:
                pack.resolve_capsule_paths(project=object(), testbed=testbed)
        self.assertIn("web_capsule", str(cm.exception))


class AssemblePlanDir(unittest.TestCase):
    def test_writes_manifest_units_and_scripts(self):
        manifest = {
            "testbed": "app", "start_order": ["web"],
            "services": {
                "web": {
                    "capsule": "web_capsule", "capsule_path": "/nix/store/x",
                    "depends_on": [], "restart": "always", "ports": [],
                    "volumes": {}, "secrets": {}, "healthcheck": None,
                },
            },
            "volumes": [], "secrets": [],
        }
        with tempfile.TemporaryDirectory() as d:
            dest = Path(d) / "plan"
            pack.assemble_plan_dir(dest, manifest)

            self.assertEqual(
                json.loads((dest / "manifest.json").read_text()), manifest,
            )
            unit_file = dest / "units" / "rigx-app-web.service"
            self.assertTrue(unit_file.exists())
            self.assertIn("web_capsule", unit_file.read_text())

            activate_path = dest / "bin" / "activate"
            rollback_path = dest / "bin" / "rollback"
            self.assertTrue(activate_path.exists())
            self.assertTrue(rollback_path.exists())
            self.assertTrue(activate_path.stat().st_mode & 0o111)
            self.assertTrue(rollback_path.stat().st_mode & 0o111)


class NixStoreSteps(unittest.TestCase):
    def test_add_to_local_store_returns_stdout_path(self):
        with mock.patch.object(
            pack.subprocess, "run",
            return_value=_completed(0, stdout="/nix/store/abc-plan\n"),
        ) as fake_run:
            path = pack.add_to_local_store(Path("/tmp/plan"))
        self.assertEqual(path, "/nix/store/abc-plan")
        self.assertEqual(fake_run.call_args[0][0][:3], ["nix", "store", "add"])

    def test_add_to_local_store_raises_on_failure(self):
        with mock.patch.object(
            pack.subprocess, "run", return_value=_completed(1, stderr="boom"),
        ):
            with self.assertRaises(DeployError) as cm:
                pack.add_to_local_store(Path("/tmp/plan"))
        self.assertIn("boom", str(cm.exception))

    def test_copy_to_target_invokes_nix_copy_with_ssh_destination(self):
        target = SSHTarget(host="example.com", user="rigx")
        with mock.patch.object(
            pack.subprocess, "run", return_value=_completed(0),
        ) as fake_run:
            pack.copy_to_target("/nix/store/abc-plan", target)
        argv = fake_run.call_args[0][0]
        self.assertEqual(argv[:2], ["nix", "copy"])
        self.assertIn("--to", argv)
        self.assertIn("ssh://rigx@example.com", argv)
        self.assertIn("/nix/store/abc-plan", argv)

    def test_copy_to_target_raises_on_failure(self):
        target = SSHTarget(host="h")
        with mock.patch.object(
            pack.subprocess, "run", return_value=_completed(1, stderr="no route"),
        ):
            with self.assertRaises(DeployError) as cm:
                pack.copy_to_target("/nix/store/x", target)
        self.assertIn("no route", str(cm.exception))


class BuildAndPushPlan(unittest.TestCase):
    def test_end_to_end_wiring(self):
        testbed = Target(name="app", kind="testbed", services={
            "web": TestbedService(name="web", capsule="web_capsule"),
        })
        target = SSHTarget(host="h")

        with mock.patch.object(
            pack, "resolve_capsule_paths", return_value={"web_capsule": "/nix/store/cap"},
        ) as fake_resolve, mock.patch.object(
            pack, "resolve_capsule_backends", return_value={"web_capsule": "lite"},
        ) as fake_backends, mock.patch.object(
            pack, "add_to_local_store", return_value="/nix/store/plan",
        ) as fake_add, mock.patch.object(
            pack, "copy_to_target",
        ) as fake_copy:
            result = pack.build_and_push_plan(object(), testbed, target)

        fake_resolve.assert_called_once()
        fake_backends.assert_called_once()
        fake_add.assert_called_once()
        fake_copy.assert_called_once_with("/nix/store/plan", target)
        self.assertEqual(result, "/nix/store/plan")


if __name__ == "__main__":
    unittest.main()
