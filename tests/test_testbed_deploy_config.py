"""Tests for the declarative testbed deployment schema (`kind =
"testbed"` `services`/`volumes`/`secrets` — design decision #9), as
distinct from the existing interactive `script` mode."""

import tempfile
import unittest
from pathlib import Path
from textwrap import dedent

from rigx import config
from rigx.config import ConfigError


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


_CAPSULE = """
    [targets.web_capsule]
    kind       = "capsule"
    backend    = "lite"
    entrypoint = "/bin/true"

    [targets.db_capsule]
    kind       = "capsule"
    backend    = "lite"
    entrypoint = "/bin/true"
"""


class RequiresScriptOrServices(unittest.TestCase):
    def test_rejects_testbed_with_neither(self):
        body = f"""
            [project]
            name = "p"

            [targets.app]
            kind = "testbed"
        """
        with TempProject(body) as root:
            with self.assertRaises(ConfigError) as cm:
                config.load(root)
        self.assertIn("requires 'script'", str(cm.exception))

    def test_script_only_still_works(self):
        body = f"""
            [project]
            name = "p"

            [targets.app]
            kind   = "testbed"
            script = "python3 testbeds/demo.py"
        """
        with TempProject(body) as root:
            proj = config.load(root)
        self.assertEqual(proj.targets["app"].services, {})

    def test_services_only_needs_no_script(self):
        body = f"""
            [project]
            name = "p"
            {_CAPSULE}
            [targets.app]
            kind = "testbed"

            [targets.app.services.web]
            capsule = "web_capsule"
        """
        with TempProject(body) as root:
            proj = config.load(root)
        self.assertIn("web", proj.targets["app"].services)


class ServiceParsing(unittest.TestCase):
    def test_full_service_shape(self):
        body = f"""
            [project]
            name = "p"
            {_CAPSULE}
            [targets.app]
            kind = "testbed"

            [targets.app.volumes.uploads]
            [targets.app.volumes.data]

            [targets.app.secrets.api_key]
            source = "env:MYAPP_API_KEY"

            [targets.app.services.web]
            capsule    = "web_capsule"
            depends_on = ["db"]
            restart    = "always"
            ports      = [{{ host = 8080, container = 8080 }}]

            [targets.app.services.web.volumes]
            uploads = "/var/www/uploads"

            [targets.app.services.web.secrets]
            api_key = "/run/secrets/api_key"

            [targets.app.services.web.healthcheck]
            command  = "curl -f http://127.0.0.1:8080/healthz"
            interval = 2
            retries  = 5

            [targets.app.services.db]
            capsule = "db_capsule"
            restart = "always"

            [targets.app.services.db.volumes]
            data = "/var/lib/postgresql/data"
        """
        with TempProject(body) as root:
            proj = config.load(root)
        app = proj.targets["app"]
        self.assertEqual(sorted(app.testbed_volumes), ["data", "uploads"])
        self.assertEqual(app.testbed_secrets, {"api_key": "env:MYAPP_API_KEY"})

        web = app.services["web"]
        self.assertEqual(web.capsule, "web_capsule")
        self.assertEqual(web.depends_on, ["db"])
        self.assertEqual(web.restart, "always")
        self.assertEqual(web.ports[0].host, 8080)
        self.assertEqual(web.ports[0].container, 8080)
        self.assertEqual(web.volumes, {"uploads": "/var/www/uploads"})
        self.assertEqual(web.secrets, {"api_key": "/run/secrets/api_key"})
        self.assertEqual(web.healthcheck.command, "curl -f http://127.0.0.1:8080/healthz")
        self.assertEqual(web.healthcheck.interval, 2)
        self.assertEqual(web.healthcheck.retries, 5)

        db = app.services["db"]
        self.assertEqual(db.depends_on, [])
        self.assertIsNone(db.healthcheck)

    def test_default_restart_is_always(self):
        body = f"""
            [project]
            name = "p"
            {_CAPSULE}
            [targets.app]
            kind = "testbed"

            [targets.app.services.web]
            capsule = "web_capsule"
        """
        with TempProject(body) as root:
            proj = config.load(root)
        self.assertEqual(proj.targets["app"].services["web"].restart, "always")

    def test_rejects_bad_restart_value(self):
        body = f"""
            [project]
            name = "p"
            {_CAPSULE}
            [targets.app]
            kind = "testbed"

            [targets.app.services.web]
            capsule = "web_capsule"
            restart = "sometimes"
        """
        with TempProject(body) as root:
            with self.assertRaises(ConfigError) as cm:
                config.load(root)
        self.assertIn("restart", str(cm.exception))

    def test_rejects_unknown_service_key(self):
        body = f"""
            [project]
            name = "p"
            {_CAPSULE}
            [targets.app]
            kind = "testbed"

            [targets.app.services.web]
            capsule = "web_capsule"
            bogus   = "x"
        """
        with TempProject(body) as root:
            with self.assertRaises(ConfigError) as cm:
                config.load(root)
        self.assertIn("unknown keys", str(cm.exception))

    def test_rejects_missing_capsule(self):
        body = f"""
            [project]
            name = "p"
            {_CAPSULE}
            [targets.app]
            kind = "testbed"

            [targets.app.services.web]
            depends_on = ["db"]
        """
        with TempProject(body) as root:
            with self.assertRaises(ConfigError) as cm:
                config.load(root)
        self.assertIn("'capsule'", str(cm.exception))


class CrossValidation(unittest.TestCase):
    def test_capsule_reference_must_exist(self):
        body = f"""
            [project]
            name = "p"
            {_CAPSULE}
            [targets.app]
            kind = "testbed"

            [targets.app.services.web]
            capsule = "does_not_exist"
        """
        with TempProject(body) as root:
            with self.assertRaises(ConfigError) as cm:
                config.load(root)
        self.assertIn("not a defined target", str(cm.exception))

    def test_capsule_reference_must_be_capsule_kind(self):
        body = f"""
            [project]
            name = "p"
            {_CAPSULE}
            [targets.not_a_capsule]
            kind   = "script"
            script = "true"

            [targets.app]
            kind = "testbed"

            [targets.app.services.web]
            capsule = "not_a_capsule"
        """
        with TempProject(body) as root:
            with self.assertRaises(ConfigError) as cm:
                config.load(root)
        self.assertIn("must be kind='capsule'", str(cm.exception))

    def test_depends_on_must_reference_declared_service(self):
        body = f"""
            [project]
            name = "p"
            {_CAPSULE}
            [targets.app]
            kind = "testbed"

            [targets.app.services.web]
            capsule    = "web_capsule"
            depends_on = ["ghost"]
        """
        with TempProject(body) as root:
            with self.assertRaises(ConfigError) as cm:
                config.load(root)
        self.assertIn("undeclared service", str(cm.exception))

    def test_depends_on_self_rejected(self):
        body = f"""
            [project]
            name = "p"
            {_CAPSULE}
            [targets.app]
            kind = "testbed"

            [targets.app.services.web]
            capsule    = "web_capsule"
            depends_on = ["web"]
        """
        with TempProject(body) as root:
            with self.assertRaises(ConfigError) as cm:
                config.load(root)
        self.assertIn("cannot reference itself", str(cm.exception))

    def test_service_volume_must_be_declared_at_testbed_level(self):
        body = f"""
            [project]
            name = "p"
            {_CAPSULE}
            [targets.app]
            kind = "testbed"

            [targets.app.services.web]
            capsule = "web_capsule"

            [targets.app.services.web.volumes]
            uploads = "/var/www/uploads"
        """
        with TempProject(body) as root:
            with self.assertRaises(ConfigError) as cm:
                config.load(root)
        self.assertIn("undeclared volume", str(cm.exception))

    def test_service_secret_must_be_declared_at_testbed_level(self):
        body = f"""
            [project]
            name = "p"
            {_CAPSULE}
            [targets.app]
            kind = "testbed"

            [targets.app.services.web]
            capsule = "web_capsule"

            [targets.app.services.web.secrets]
            api_key = "/run/secrets/api_key"
        """
        with TempProject(body) as root:
            with self.assertRaises(ConfigError) as cm:
                config.load(root)
        self.assertIn("undeclared secret", str(cm.exception))


class SecretSourceValidation(unittest.TestCase):
    def test_rejects_bad_source_prefix(self):
        body = f"""
            [project]
            name = "p"
            {_CAPSULE}
            [targets.app]
            kind = "testbed"

            [targets.app.secrets.api_key]
            source = "vault:api_key"

            [targets.app.services.web]
            capsule = "web_capsule"
        """
        with TempProject(body) as root:
            with self.assertRaises(ConfigError) as cm:
                config.load(root)
        self.assertIn("'env:' or 'file:'", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
