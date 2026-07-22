"""Tests for `rigx.deploy.units` — systemd unit rendering from a
compiled manifest (see `rigx.deploy.plan`)."""

from __future__ import annotations

import unittest

from rigx.deploy import units


def _lines(text: str) -> list[str]:
    return [l for l in text.splitlines() if l.strip()]


class RenderUnit(unittest.TestCase):
    def test_minimal_service(self):
        svc = {
            "capsule": "web_capsule",
            "capsule_path": "/nix/store/aaa-web",
            "depends_on": [],
            "restart": "always",
            "ports": [],
            "volumes": {},
            "secrets": {},
            "healthcheck": None,
        }
        text = units.render_unit("app", "web", svc)
        self.assertIn("Restart=always", text)
        self.assertIn("ExecStart=/nix/store/aaa-web/bin/run-web_capsule", text)
        self.assertIn("Environment=RIGX_NAME=rigx-app-web", text)
        self.assertNotIn("RIGX_PUBLISH", text)
        self.assertNotIn("RIGX_VOLUMES", text)
        self.assertNotIn("LoadCredentialEncrypted", text)
        self.assertIn("WantedBy=multi-user.target", text)

    def test_depends_on_becomes_after_and_requires(self):
        svc = {
            "capsule": "web_capsule", "capsule_path": "/nix/store/x",
            "depends_on": ["db"], "restart": "always", "ports": [],
            "volumes": {}, "secrets": {}, "healthcheck": None,
        }
        text = units.render_unit("app", "web", svc)
        self.assertIn("rigx-app-db.service", text)
        after_line = next(l for l in _lines(text) if l.startswith("After="))
        requires_line = next(l for l in _lines(text) if l.startswith("Requires="))
        self.assertIn("rigx-app-db.service", after_line)
        self.assertEqual(requires_line, "Requires=rigx-app-db.service")

    def test_ports_become_rigx_publish(self):
        svc = {
            "capsule": "web_capsule", "capsule_path": "/nix/store/x",
            "depends_on": [], "restart": "always",
            "ports": [{"host": 8080, "container": 8080}, {"host": 9000, "container": 90}],
            "volumes": {}, "secrets": {}, "healthcheck": None,
        }
        text = units.render_unit("app", "web", svc)
        self.assertIn("Environment=RIGX_PUBLISH=8080:8080,9000:90", text)

    def test_volumes_become_rigx_volumes_env_when_no_secrets(self):
        svc = {
            "capsule": "db_capsule", "capsule_path": "/nix/store/x",
            "depends_on": [], "restart": "always", "ports": [],
            "volumes": {"data": "/var/lib/postgresql/data"},
            "secrets": {}, "healthcheck": None,
        }
        text = units.render_unit("app", "db", svc)
        self.assertIn(
            "Environment=RIGX_VOLUMES=/var/lib/rigx/app/volumes/data:"
            "/var/lib/postgresql/data:rw",
            text,
        )
        self.assertIn("ExecStart=/nix/store/x/bin/run-db_capsule", text)

    def test_secrets_use_load_credential_and_shell_wrapped_execstart(self):
        svc = {
            "capsule": "web_capsule", "capsule_path": "/nix/store/x",
            "depends_on": [], "restart": "always", "ports": [],
            "volumes": {"uploads": "/var/www/uploads"},
            "secrets": {"api_key": "/run/secrets/api_key"},
            "healthcheck": None,
        }
        text = units.render_unit("app", "web", svc)
        self.assertIn(
            "LoadCredentialEncrypted=api_key:/var/lib/rigx/app/secrets/api_key.cred",
            text,
        )
        exec_line = next(l for l in _lines(text) if l.startswith("ExecStart="))
        self.assertTrue(exec_line.startswith("ExecStart=/bin/sh -c "))
        self.assertIn("RIGX_VOLUMES=", exec_line)
        # both the static volume and the credential-derived mount are present
        self.assertIn("/var/lib/rigx/app/volumes/uploads:/var/www/uploads:rw", exec_line)
        self.assertIn(
            "${CREDENTIALS_DIRECTORY}/api_key:/run/secrets/api_key:ro", exec_line,
        )
        self.assertIn('exec "/nix/store/x/bin/run-web_capsule"', exec_line)

    def test_restart_policy_passthrough(self):
        for policy in ("always", "on-failure", "no"):
            svc = {
                "capsule": "c", "capsule_path": "/nix/store/x",
                "depends_on": [], "restart": policy, "ports": [],
                "volumes": {}, "secrets": {}, "healthcheck": None,
            }
            text = units.render_unit("app", "svc", svc)
            self.assertIn(f"Restart={policy}", text)


def _svc(**overrides):
    base = {
        "capsule": "c", "capsule_path": "/nix/store/x",
        "depends_on": [], "restart": "always", "ports": [],
        "volumes": {}, "secrets": {}, "healthcheck": None,
    }
    base.update(overrides)
    return base


class Confinement(unittest.TestCase):
    """Services run rootful on every backend. The old `DynamicUser=yes` +
    `SupplementaryGroups=docker` pairing bought nothing — docker-group
    membership is root-equivalent — and podman has no daemon socket to
    join in the first place."""

    def test_no_backend_gets_dynamic_user_or_a_socket_group(self):
        for backend in ("lite", "qemu", "nixos", ""):
            text = units.render_unit(
                "app", "web", _svc(capsule_backend=backend),
            )
            self.assertNotIn("DynamicUser", text, f"backend={backend!r}")
            self.assertNotIn("SupplementaryGroups", text, f"backend={backend!r}")
            self.assertNotIn("docker", text, f"backend={backend!r}")

    def test_missing_backend_key_still_renders(self):
        svc = _svc()
        svc.pop("capsule_backend", None)
        text = units.render_unit("app", "web", svc)
        self.assertNotIn("DynamicUser", text)


class Supervision(unittest.TestCase):
    """The unit has to be something systemd can actually supervise."""

    def test_runner_is_told_to_stay_in_foreground(self):
        # The runner's default is `-it`, which is a hard error when
        # stdin is /dev/null; `-d` would exit immediately and read as
        # the service having finished. Neither is supervisable.
        text = units.render_unit("app", "web", _svc())
        self.assertIn("Environment=RIGX_FOREGROUND=1", text)
        self.assertNotIn("RIGX_DETACH", text)

    def test_foreground_is_set_on_the_secrets_path_too(self):
        text = units.render_unit(
            "app", "web", _svc(secrets={"api_key": "/run/secrets/api_key"}),
        )
        self.assertIn("Environment=RIGX_FOREGROUND=1", text)

    def test_restart_always_is_not_capped_by_the_default_start_limit(self):
        # systemd's default (5 starts in 10s) would park a crash-looping
        # service in `failed` — i.e. `restart = "always"` would quietly
        # mean "always, for about ten seconds".
        text = units.render_unit("app", "web", _svc())
        self.assertIn("StartLimitIntervalSec=0", text)
        self.assertIn(f"RestartSec={units.RESTART_SEC}", text)

    def test_start_limit_override_is_in_the_unit_section(self):
        # StartLimitIntervalSec is a [Unit] directive; in [Service] it
        # is silently ignored, which would be a very quiet failure.
        text = units.render_unit("app", "web", _svc())
        unit_section = text.split("[Service]")[0]
        self.assertIn("StartLimitIntervalSec=0", unit_section)


class RestartPropagation(unittest.TestCase):
    def test_dependency_restart_propagates_to_dependents(self):
        # Requires= propagates stops but not restarts, so without
        # PartOf= a `db` restart leaves `web` talking to a dead socket.
        text = units.render_unit("app", "web", _svc(depends_on=["db"]))
        part_of = next(l for l in _lines(text) if l.startswith("PartOf="))
        self.assertEqual(part_of, "PartOf=rigx-app-db.service")

    def test_multiple_dependencies_all_propagate(self):
        text = units.render_unit("app", "web", _svc(depends_on=["db", "cache"]))
        part_of = next(l for l in _lines(text) if l.startswith("PartOf="))
        self.assertIn("rigx-app-db.service", part_of)
        self.assertIn("rigx-app-cache.service", part_of)

    def test_no_dependencies_means_no_partof(self):
        text = units.render_unit("app", "web", _svc())
        self.assertNotIn("PartOf=", text)


class HealthUnits(unittest.TestCase):
    HC = {"command": "curl -f http://127.0.0.1:8080/healthz",
          "interval": 2, "retries": 3}

    def test_service_without_healthcheck_owns_only_its_own_unit(self):
        self.assertEqual(
            units.service_unit_names("app", "web", _svc()),
            ["rigx-app-web.service"],
        )

    def test_service_with_healthcheck_owns_probe_and_timer(self):
        self.assertEqual(
            units.service_unit_names("app", "web", _svc(healthcheck=self.HC)),
            [
                "rigx-app-web.service",
                "rigx-app-web-health.service",
                "rigx-app-web-health.timer",
            ],
        )

    def test_main_unit_pulls_in_the_timer(self):
        text = units.render_unit("app", "web", _svc(healthcheck=self.HC))
        self.assertIn("Wants=rigx-app-web-health.timer", text)

    def test_no_timer_wanted_without_a_healthcheck(self):
        text = units.render_unit("app", "web", _svc())
        self.assertNotIn("health.timer", text)

    def test_probe_restarts_the_service_after_configured_retries(self):
        text = units.render_health_unit("app", "web", _svc(healthcheck=self.HC))
        self.assertIn("Type=oneshot", text)
        exec_line = next(l for l in _lines(text) if l.startswith("ExecStart="))
        self.assertIn("curl -f http://127.0.0.1:8080/healthz", exec_line)
        self.assertIn('[ "$i" -ge 3 ]', exec_line)
        self.assertIn("sleep 2", exec_line)
        # --no-block: a synchronous restart from inside a unit can
        # deadlock against the job it would be waiting on.
        self.assertIn("systemctl restart --no-block rigx-app-web.service", exec_line)

    def test_timer_is_bound_to_its_service(self):
        text = units.render_health_timer("app", "web", _svc(healthcheck=self.HC))
        # BindsTo= so the probe stops when the service does, instead of
        # probing (and restarting) something deliberately stopped.
        self.assertIn("BindsTo=rigx-app-web.service", text)
        self.assertIn("After=rigx-app-web.service", text)
        self.assertIn("OnUnitActiveSec=2", text)
        self.assertIn("Unit=rigx-app-web-health.service", text)


class RenderUnits(unittest.TestCase):
    def test_renders_all_services_in_start_order(self):
        manifest = {
            "testbed": "app",
            "start_order": ["db", "web"],
            "services": {
                "db": {
                    "capsule": "db_capsule", "capsule_path": "/nix/store/a",
                    "depends_on": [], "restart": "always", "ports": [],
                    "volumes": {}, "secrets": {}, "healthcheck": None,
                },
                "web": {
                    "capsule": "web_capsule", "capsule_path": "/nix/store/b",
                    "depends_on": ["db"], "restart": "always", "ports": [],
                    "volumes": {}, "secrets": {}, "healthcheck": None,
                },
            },
            "volumes": [], "secrets": [],
        }
        rendered = units.render_units(manifest)
        self.assertEqual(
            set(rendered.keys()), {"rigx-app-db.service", "rigx-app-web.service"},
        )
        self.assertIn("rigx-app-db.service", rendered["rigx-app-web.service"])

    def test_healthchecked_service_contributes_three_units(self):
        manifest = {
            "testbed": "app",
            "start_order": ["db", "web"],
            "services": {
                "db": _svc(capsule="db_capsule"),
                "web": _svc(
                    capsule="web_capsule", depends_on=["db"],
                    healthcheck={"command": "true", "interval": 1, "retries": 2},
                ),
            },
            "volumes": [], "secrets": [],
        }
        rendered = units.render_units(manifest)
        self.assertEqual(
            set(rendered.keys()),
            {
                "rigx-app-db.service",
                "rigx-app-web.service",
                "rigx-app-web-health.service",
                "rigx-app-web-health.timer",
            },
        )


if __name__ == "__main__":
    unittest.main()
