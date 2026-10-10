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
        self.assertEqual(web.healthcheck.scope, "container")  # the default

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


_PORTED_CAPSULE = """
    [targets.web_capsule]
    kind       = "capsule"
    backend    = "lite"
    entrypoint = "/bin/true"
    ports      = [8000, 9000]
"""


def _with_service(capsule_block: str, service_body: str) -> str:
    return f"""
    [project]
    name = "p"
    {capsule_block}
    [targets.app]
    kind = "testbed"

    [targets.app.services.web]
    capsule = "web_capsule"
    {service_body}
    """


class PortBindAddress(unittest.TestCase):
    """`host` may name an interface, so a port can be reachable from the
    host without being reachable from the world."""

    def _port(self, host_value: str):
        with TempProject(
            _with_service(_PORTED_CAPSULE, f"ports = [{{ host = {host_value}, container = 8000 }}]")
        ) as root:
            return config.load(root).targets["app"].services["web"].ports[0]

    def test_bare_integer_binds_everywhere(self):
        p = self._port("8000")
        self.assertEqual((p.host, p.container, p.host_addr), (8000, 8000, ""))
        self.assertEqual(p.publish_spec(), "8000:8000")

    def test_loopback_string_is_split(self):
        p = self._port('"127.0.0.1:8000"')
        self.assertEqual((p.host, p.container, p.host_addr), (8000, 8000, "127.0.0.1"))
        self.assertEqual(p.publish_spec(), "127.0.0.1:8000:8000")

    def test_ipv6_literal_keeps_its_brackets(self):
        p = self._port('"[::1]:8000"')
        self.assertEqual(p.host_addr, "[::1]")
        self.assertEqual(p.publish_spec(), "[::1]:8000:8000")

    def test_hostname_bind_address(self):
        p = self._port('"localhost:8000"')
        self.assertEqual(p.host_addr, "localhost")

    def test_rejects_string_without_a_port(self):
        with self.assertRaises(ConfigError) as cm:
            self._port('"127.0.0.1"')
        self.assertIn("addr:port", str(cm.exception))

    def test_rejects_non_integer_port(self):
        with self.assertRaises(ConfigError):
            self._port('"127.0.0.1:http"')

    def test_rejects_garbage_bind_address(self):
        with self.assertRaises(ConfigError):
            self._port('"not a host:8000"')

    def test_rejects_missing_host(self):
        with TempProject(
            _with_service(_PORTED_CAPSULE, "ports = [{ container = 8000 }]")
        ) as root:
            with self.assertRaises(ConfigError) as cm:
                config.load(root)
            self.assertIn("'host' is required", str(cm.exception))


class ServiceEnv(unittest.TestCase):
    """Per-service env, layered over the capsule's own — so one capsule
    can back services that differ only in configuration."""

    def _env(self, body: str):
        with TempProject(_with_service(_CAPSULE, body)) as root:
            return config.load(root).targets["app"].services["web"].env

    def test_absent_env_is_empty(self):
        self.assertEqual(self._env(""), {})

    def test_parses_string_table(self):
        self.assertEqual(
            self._env('env = { HOSTNAME = "example.com", LOG_LEVEL = "info" }'),
            {"HOSTNAME": "example.com", "LOG_LEVEL": "info"},
        )

    def test_rejects_unquoted_numbers(self):
        # Same rule as capsule env: a bare 8080 fails loudly instead of
        # silently becoming something else.
        with self.assertRaises(ConfigError) as cm:
            self._env("env = { PORT = 8080 }")
        self.assertIn("string", str(cm.exception))

    def test_rejects_invalid_env_var_name(self):
        with self.assertRaises(ConfigError) as cm:
            self._env('env = { "2BAD" = "x" }')
        self.assertIn("2BAD", str(cm.exception))


class ServiceArtifacts(unittest.TestCase):
    """`artifacts` mounts a rigx-built target read-only into a service."""

    CONTENT = """
    [targets.site]
    kind = "custom"
    install_script = "mkdir -p $out"
    """

    def _load(self, service_body: str, extra: str = ""):
        with TempProject(
            _with_service(_CAPSULE + self.CONTENT + extra, service_body)
        ) as root:
            return config.load(root).targets["app"].services["web"]

    def test_parses_target_to_mount_path(self):
        svc = self._load('artifacts = { site = "/usr/share/www" }')
        self.assertEqual(svc.artifacts, {"site": "/usr/share/www"})

    def test_rejects_relative_mount_path(self):
        with self.assertRaises(ConfigError) as cm:
            self._load('artifacts = { site = "usr/share/www" }')
        self.assertIn("absolute", str(cm.exception))

    def test_rejects_unknown_target(self):
        with self.assertRaises(ConfigError) as cm:
            self._load('artifacts = { nope = "/srv" }')
        self.assertIn("not a defined target", str(cm.exception))

    def test_rejects_a_target_that_produces_no_output(self):
        extra = """
    [targets.scenario]
    kind   = "script"
    script = "x.py"
    """
        with self.assertRaises(ConfigError) as cm:
            self._load('artifacts = { scenario = "/srv" }', extra=extra)
        self.assertIn("produces no output", str(cm.exception))

    def test_rejects_artifact_and_volume_at_the_same_path(self):
        # A volume may sit *under* an artifact; on top, whichever mounts
        # last silently wins and the state may be the loser.
        body = (
            'artifacts = { site = "/srv/data" }\n'
            '    volumes = { state = "/srv/data" }'
        )
        toml = _with_service(_CAPSULE + self.CONTENT, body).replace(
            "[targets.app]\n    kind = \"testbed\"",
            "[targets.app]\n    kind = \"testbed\"\n\n    [targets.app.volumes.state]",
        )
        with TempProject(toml) as root:
            with self.assertRaises(ConfigError) as cm:
                config.load(root)
            self.assertIn("both mount at", str(cm.exception))


class ServicePortsMatchCapsule(unittest.TestCase):
    """A published container port has to be one the capsule declares —
    otherwise the typo deploys cleanly and dies at runtime."""

    def test_accepts_a_declared_port(self):
        with TempProject(
            _with_service(
                _PORTED_CAPSULE, "ports = [{ host = 80, container = 8000 }]",
            )
        ) as root:
            self.assertEqual(
                config.load(root).targets["app"].services["web"].ports[0].container,
                8000,
            )

    def test_rejects_an_undeclared_port(self):
        with TempProject(
            _with_service(
                _PORTED_CAPSULE, "ports = [{ host = 80, container = 8080 }]",
            )
        ) as root:
            with self.assertRaises(ConfigError) as cm:
                config.load(root)
            msg = str(cm.exception)
            self.assertIn("8080", msg)
            self.assertIn("[8000, 9000]", msg)

    def test_capsule_declaring_no_ports_is_exempt(self):
        # `ports` is optional on a capsule; absence means unspecified,
        # not "none", so there's nothing to check against.
        with TempProject(
            _with_service(_CAPSULE, "ports = [{ host = 80, container = 8080 }]")
        ) as root:
            self.assertEqual(
                config.load(root).targets["app"].services["web"].ports[0].container,
                8080,
            )



class HealthcheckScope(unittest.TestCase):
    def test_scope_values(self):
        parse = config._parse_testbed_healthcheck
        self.assertEqual(parse({"command": "true"}, "x").scope, "container")
        self.assertEqual(parse({"command": "true", "scope": "host"}, "x").scope, "host")
        with self.assertRaises(ConfigError):
            parse({"command": "true", "scope": "network"}, "x")


class DeployNamesAndPaths(unittest.TestCase):
    """Service, volume and secret names reach generated sh, host paths
    and a remote command line, and mount paths a `-v` value."""

    def test_names(self):
        ok = config._check_deploy_name
        ok("web-1.a_b", "x", "service")
        for bad in ("a b", "../x", "-x", "a;b", ""):
            with self.subTest(bad=bad), self.assertRaises(ConfigError):
                ok(bad, "x", "service")

    def test_service_mount_paths(self):
        parse = config._parse_testbed_str_map
        self.assertEqual(parse({"data": "/srv/data"}, "x", field_name="volumes"),
                         {"data": "/srv/data"})
        for bad in ("rel/path", "/a:b", "/a,b"):
            with self.subTest(bad=bad), self.assertRaises(ConfigError):
                parse({"data": bad}, "x", field_name="volumes")


if __name__ == "__main__":
    unittest.main()
