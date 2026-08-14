"""Tests for `rigx.deploy.plan` — dependency ordering + manifest
compilation. Pure logic: builds `Target`/`TestbedService` objects
directly, no rigx.toml/Nix involved."""

from __future__ import annotations

import unittest

from rigx.config import Target, TestbedHealthcheck, TestbedPort, TestbedService
from rigx.deploy import plan


def _testbed(services: dict[str, TestbedService], **kw) -> Target:
    return Target(name=kw.pop("name", "app"), kind="testbed", services=services, **kw)


class DependencyOrder(unittest.TestCase):
    def test_linear_chain(self):
        tb = _testbed({
            "db":  TestbedService(name="db", capsule="db_capsule"),
            "web": TestbedService(name="web", capsule="web_capsule", depends_on=["db"]),
        })
        self.assertEqual(plan.dependency_order(tb), ["db", "web"])

    def test_independent_services_break_ties_by_name(self):
        tb = _testbed({
            "b": TestbedService(name="b", capsule="c_b"),
            "a": TestbedService(name="a", capsule="c_a"),
        })
        self.assertEqual(plan.dependency_order(tb), ["a", "b"])

    def test_diamond_dependency(self):
        tb = _testbed({
            "base":  TestbedService(name="base", capsule="c"),
            "left":  TestbedService(name="left", capsule="c", depends_on=["base"]),
            "right": TestbedService(name="right", capsule="c", depends_on=["base"]),
            "top":   TestbedService(name="top", capsule="c", depends_on=["left", "right"]),
        })
        order = plan.dependency_order(tb)
        self.assertLess(order.index("base"), order.index("left"))
        self.assertLess(order.index("base"), order.index("right"))
        self.assertLess(order.index("left"), order.index("top"))
        self.assertLess(order.index("right"), order.index("top"))

    def test_detects_two_node_cycle(self):
        tb = _testbed({
            "a": TestbedService(name="a", capsule="c", depends_on=["b"]),
            "b": TestbedService(name="b", capsule="c", depends_on=["a"]),
        })
        with self.assertRaises(plan.PlanError) as cm:
            plan.dependency_order(tb)
        self.assertIn("cycle", str(cm.exception))

    def test_detects_longer_cycle(self):
        tb = _testbed({
            "a": TestbedService(name="a", capsule="c", depends_on=["b"]),
            "b": TestbedService(name="b", capsule="c", depends_on=["c_svc"]),
            "c_svc": TestbedService(name="c_svc", capsule="c", depends_on=["a"]),
        })
        with self.assertRaises(plan.PlanError):
            plan.dependency_order(tb)


class CompileManifest(unittest.TestCase):
    def test_rejects_non_testbed_kind(self):
        t = Target(name="x", kind="capsule")
        with self.assertRaises(plan.PlanError):
            plan.compile_manifest(t, {})

    def test_rejects_no_services(self):
        t = Target(name="x", kind="testbed", script="python3 x.py")
        with self.assertRaises(plan.PlanError):
            plan.compile_manifest(t, {})

    def test_rejects_missing_capsule_path(self):
        tb = _testbed({"web": TestbedService(name="web", capsule="web_capsule")})
        with self.assertRaises(plan.PlanError) as cm:
            plan.compile_manifest(tb, {})
        self.assertIn("web_capsule", str(cm.exception))

    def test_full_manifest_shape(self):
        tb = _testbed(
            {
                "db": TestbedService(
                    name="db", capsule="db_capsule", restart="always",
                    volumes={"data": "/var/lib/postgresql/data"},
                ),
                "web": TestbedService(
                    name="web", capsule="web_capsule", depends_on=["db"],
                    restart="on-failure",
                    ports=[TestbedPort(host=8080, container=8080)],
                    secrets={"api_key": "/run/secrets/api_key"},
                    healthcheck=TestbedHealthcheck(
                        command="curl -f http://127.0.0.1:8080/healthz",
                        interval=2, retries=5,
                    ),
                ),
            },
            testbed_volumes=["data"],
            testbed_secrets={"api_key": "env:MYAPP_API_KEY"},
        )
        manifest = plan.compile_manifest(
            tb, {"db_capsule": "/nix/store/aaa-db", "web_capsule": "/nix/store/bbb-web"},
        )
        self.assertEqual(manifest["testbed"], "app")
        self.assertEqual(manifest["network"], "rigx-app")
        self.assertEqual(manifest["start_order"], ["db", "web"])
        self.assertEqual(manifest["volumes"], ["data"])
        self.assertEqual(manifest["secrets"], ["api_key"])

        db = manifest["services"]["db"]
        self.assertEqual(db["capsule_path"], "/nix/store/aaa-db")
        self.assertEqual(db["volumes"], {"data": "/var/lib/postgresql/data"})
        self.assertIsNone(db["healthcheck"])

        web = manifest["services"]["web"]
        self.assertEqual(web["capsule_path"], "/nix/store/bbb-web")
        self.assertEqual(web["depends_on"], ["db"])
        self.assertEqual(web["restart"], "on-failure")
        self.assertEqual(web["ports"], [{
            "host": 8080, "container": 8080,
            "host_addr": "", "publish": "8080:8080",
        }])
        self.assertEqual(web["env"], {})
        self.assertEqual(web["secrets"], {"api_key": "/run/secrets/api_key"})
        self.assertEqual(web["healthcheck"], {
            "command": "curl -f http://127.0.0.1:8080/healthz",
            "interval": 2,
            "retries": 5,
        })

    def test_capsule_backend_defaults_to_empty_string(self):
        tb = _testbed({"web": TestbedService(name="web", capsule="c")})
        manifest = plan.compile_manifest(tb, {"c": "/nix/store/x"})
        self.assertEqual(manifest["services"]["web"]["capsule_backend"], "")

    def test_capsule_backend_threaded_through(self):
        tb = _testbed({
            "web": TestbedService(name="web", capsule="web_capsule"),
            "db": TestbedService(name="db", capsule="db_capsule"),
        })
        manifest = plan.compile_manifest(
            tb,
            {"web_capsule": "/nix/store/a", "db_capsule": "/nix/store/b"},
            {"web_capsule": "lite", "db_capsule": "qemu"},
        )
        self.assertEqual(manifest["services"]["web"]["capsule_backend"], "lite")
        self.assertEqual(manifest["services"]["db"]["capsule_backend"], "qemu")

    def test_manifest_services_ordered_by_dependency(self):
        tb = _testbed({
            "web": TestbedService(name="web", capsule="c", depends_on=["db"]),
            "db":  TestbedService(name="db", capsule="c"),
        })
        manifest = plan.compile_manifest(tb, {"c": "/nix/store/x"})
        self.assertEqual(list(manifest["services"].keys()), ["db", "web"])


if __name__ == "__main__":
    unittest.main()
