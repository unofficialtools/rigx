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
        self.assertIn('Environment="RIGX_NAME=rigx-app-web"', text)
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
        self.assertIn('Environment="RIGX_PUBLISH=8080:8080,9000:90"', text)

    def test_volumes_become_rigx_volumes_env_when_no_secrets(self):
        svc = {
            "capsule": "db_capsule", "capsule_path": "/nix/store/x",
            "depends_on": [], "restart": "always", "ports": [],
            "volumes": {"data": "/var/lib/postgresql/data"},
            "secrets": {}, "healthcheck": None,
        }
        text = units.render_unit("app", "db", svc)
        self.assertIn(
            'Environment="RIGX_VOLUMES=/var/lib/rigx/app/volumes/data:'
            '/var/lib/postgresql/data:rw"',
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
        "volumes": {}, "artifacts": [], "secrets": {}, "env": {},
        "healthcheck": None,
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
            # "docker" on its own appears legitimately in ExecStopPost=,
            # which names both engines to reap the container. What must
            # not come back is joining the socket group.
            self.assertNotIn("=docker", text, f"backend={backend!r}")

    def test_missing_backend_key_still_renders(self):
        svc = _svc()
        svc.pop("capsule_backend", None)
        text = units.render_unit("app", "web", svc)
        self.assertNotIn("DynamicUser", text)


class ContainerReaping(unittest.TestCase):
    """Stopping the unit has to stop the container. `ExecStart` is an
    attached `podman run`, and killing an attached run does not
    reliably take its container down — so without this the container
    outlives the unit and keeps its published ports."""

    def test_unit_reaps_its_own_container_on_stop(self):
        text = units.render_unit("app", "web", _svc())
        self.assertIn("ExecStopPost=", text)
        self.assertIn("rigx-app-web", text.split("ExecStopPost=")[1])

    def test_reaping_is_present_on_the_secrets_path_too(self):
        text = units.render_unit(
            "app", "web", _svc(secrets={"api_key": "/run/secrets/api_key"}),
        )
        self.assertIn("ExecStopPost=", text)

    def test_reap_command_contains_no_dollar_sign(self):
        # systemd expands $VAR/${VAR} inside Exec* lines before the shell
        # sees them, and an unset name expands to EMPTY rather than
        # erroring — so a "$ENGINE" here would silently become `"" rm -f`.
        self.assertNotIn("$", units.reap_command("rigx-app-web"))

    def test_reap_tries_both_engines(self):
        cmd = units.reap_command("c")
        self.assertIn("podman rm -f c", cmd)
        self.assertIn("docker rm -f c", cmd)
        # Never fail the stop over it: a missing container is the normal
        # case, and a failing ExecStopPost= would mark the unit failed.
        self.assertTrue(cmd.rstrip().endswith("|| true"))

    def test_container_name_matches_the_name_the_runner_is_given(self):
        text = units.render_unit("app", "web", _svc())
        self.assertIn(
            f'Environment="RIGX_NAME={units.container_name("app", "web")}"', text,
        )


class Supervision(unittest.TestCase):
    """The unit has to be something systemd can actually supervise."""

    def test_runner_is_told_to_stay_in_foreground(self):
        # The runner's default is `-it`, which is a hard error when
        # stdin is /dev/null; `-d` would exit immediately and read as
        # the service having finished. Neither is supervisable.
        text = units.render_unit("app", "web", _svc())
        self.assertIn('Environment="RIGX_FOREGROUND=1"', text)
        self.assertNotIn("RIGX_DETACH", text)

    def test_foreground_is_set_on_the_secrets_path_too(self):
        text = units.render_unit(
            "app", "web", _svc(secrets={"api_key": "/run/secrets/api_key"}),
        )
        self.assertIn('Environment="RIGX_FOREGROUND=1"', text)

    def test_unit_provides_a_home(self):
        # systemd gives system services no HOME, and the lite runner
        # needs one for its per-container profile/gcroot dirs.
        text = units.render_unit("app", "web", _svc())
        self.assertIn('Environment="HOME=/var/lib/rigx/app/home"', text)

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


class Networking(unittest.TestCase):
    """Peers reach each other at `<service>:<port>` over the testbed's
    implicit network; publishing to the host is a separate, explicit
    act."""

    def test_service_joins_the_network_under_its_own_name(self):
        text = units.render_unit("app", "web", _svc(), network="rigx-app")
        self.assertIn('Environment="RIGX_NETWORK=rigx-app"', text)
        # The alias, not RIGX_NAME, is what a peer dials — RIGX_NAME has
        # to be unique and is therefore too long to be an address.
        self.assertIn('Environment="RIGX_NETWORK_ALIAS=web"', text)
        self.assertIn('Environment="RIGX_NAME=rigx-app-web"', text)

    def test_no_network_wiring_when_none_given(self):
        text = units.render_unit("app", "web", _svc())
        self.assertNotIn("RIGX_NETWORK", text)

    def test_service_without_ports_publishes_nothing(self):
        # The `expose:` equivalent: reachable by peers, invisible from
        # the host, with nothing extra to declare.
        text = units.render_unit("app", "web", _svc(), network="rigx-app")
        self.assertNotIn("RIGX_PUBLISH", text)

    def test_bind_address_is_carried_into_publish(self):
        svc = _svc(ports=[{
            "host": 8000, "container": 8000,
            "host_addr": "127.0.0.1", "publish": "127.0.0.1:8000:8000",
        }])
        text = units.render_unit("app", "web", svc, network="rigx-app")
        self.assertIn('Environment="RIGX_PUBLISH=127.0.0.1:8000:8000"', text)

    def test_falls_back_when_manifest_predates_publish_field(self):
        svc = _svc(ports=[{"host": 80, "container": 8080}])
        text = units.render_unit("app", "web", svc)
        self.assertIn('Environment="RIGX_PUBLISH=80:8080"', text)

    def test_render_units_threads_the_network_through(self):
        manifest = {
            "testbed": "app", "network": "rigx-app",
            "start_order": ["web"],
            "services": {"web": _svc()},
            "volumes": [], "secrets": [],
        }
        text = units.render_units(manifest)["rigx-app-web.service"]
        self.assertIn('Environment="RIGX_NETWORK=rigx-app"', text)


class Artifacts(unittest.TestCase):
    """Built content is mounted read-only from the store; state volumes
    mount read-write at paths underneath it."""

    def test_artifact_is_mounted_read_only(self):
        svc = _svc(artifacts=[
            {"target": "apps", "store_path": "/nix/store/x-apps", "mount": "/srv/apps"},
        ])
        text = units.render_unit("app", "web", svc)
        self.assertIn('Environment="RIGX_VOLUMES=/nix/store/x-apps:/srv/apps:ro"', text)

    def test_state_volumes_mount_over_a_read_only_artifact(self):
        # The whole point: an immutable tree with writable holes. The
        # artifact must mount first or it buries the state under it.
        svc = _svc(
            artifacts=[{
                "target": "apps", "store_path": "/nix/store/x-apps",
                "mount": "/srv/apps",
            }],
            volumes={"db": "/srv/apps/access/databases", "svc": "/srv/apps/.service"},
        )
        text = units.render_unit("app", "web", svc)
        line = next(l for l in _lines(text) if "RIGX_VOLUMES" in l)
        # Environment= values are quoted, so drop the closing quote
        # before splitting or the last mode carries it.
        specs = line.split("RIGX_VOLUMES=", 1)[1].rstrip('"').split(",")
        self.assertEqual(specs[0], "/nix/store/x-apps:/srv/apps:ro")
        self.assertTrue(specs[1].endswith(":/srv/apps/.service:rw"))
        self.assertTrue(specs[2].endswith(":/srv/apps/access/databases:rw"))

    def test_artifacts_and_volumes_interleave_by_depth_not_by_kind(self):
        svc = _svc(
            artifacts=[
                {"target": "a", "store_path": "/nix/store/a", "mount": "/srv/x/deep"},
                {"target": "b", "store_path": "/nix/store/b", "mount": "/srv"},
            ],
            volumes={"v": "/srv/x"},
        )
        text = units.render_unit("app", "web", svc)
        line = next(l for l in _lines(text) if "RIGX_VOLUMES" in l)
        dests = [s.rsplit(":", 1)[0].split(":", 1)[1] for s in
                 line.split("RIGX_VOLUMES=", 1)[1].split(",")]
        self.assertEqual(dests, ["/srv", "/srv/x", "/srv/x/deep"])

    def test_no_artifacts_key_is_tolerated(self):
        svc = _svc()
        svc.pop("artifacts", None)
        self.assertNotIn("RIGX_VOLUMES", units.render_unit("app", "web", svc))

    def test_artifacts_reach_the_secrets_execstart_path_too(self):
        svc = _svc(
            artifacts=[{
                "target": "apps", "store_path": "/nix/store/x", "mount": "/srv/apps",
            }],
            secrets={"k": "/run/secrets/k"},
        )
        exec_line = next(
            l for l in _lines(units.render_unit("app", "web", svc))
            if l.startswith("ExecStart=")
        )
        self.assertIn("/nix/store/x:/srv/apps:ro", exec_line)


class NestedVolumeOrdering(unittest.TestCase):
    def test_parent_mounts_come_before_the_volumes_nested_in_them(self):
        # A writable app tree with per-app state underneath is the
        # motivating case: mounting /srv/apps after /srv/apps/x/db would
        # hide the latter. Volume *names* here sort the wrong way on
        # purpose, so name-ordering would fail this test.
        svc = _svc(volumes={
            "zz_state": "/srv/apps/access/databases",
            "aa_tree": "/srv/apps",
            "mm_service": "/srv/apps/.service",
        })
        text = units.render_unit("app", "web", svc)
        line = next(l for l in _lines(text) if "RIGX_VOLUMES" in l)
        mounts = line.split("RIGX_VOLUMES=", 1)[1].split(",")
        dests = [m.split(":")[1] for m in mounts]
        self.assertEqual(dests[0], "/srv/apps")
        self.assertLess(dests.index("/srv/apps"), dests.index("/srv/apps/.service"))
        self.assertLess(
            dests.index("/srv/apps"), dests.index("/srv/apps/access/databases"),
        )


class ServiceEnv(unittest.TestCase):
    def test_env_becomes_rigx_env(self):
        text = units.render_unit(
            "app", "web", _svc(env={"LOG_LEVEL": "info", "HOSTNAME": "x.com"}),
        )
        # Sorted, so the unit text is stable and `activate`'s content
        # diff doesn't see a spurious change on every render.
        self.assertIn('Environment="RIGX_ENV=HOSTNAME=x.com,LOG_LEVEL=info"', text)

    def test_no_env_line_when_empty(self):
        self.assertNotIn("RIGX_ENV", units.render_unit("app", "web", _svc()))

    def test_missing_env_key_is_tolerated(self):
        svc = _svc()
        svc.pop("env", None)
        self.assertNotIn("RIGX_ENV", units.render_unit("app", "web", svc))


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
        self.assertIn('[ "$$i" -ge 3 ]', exec_line)
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



class InContainerProbe(unittest.TestCase):
    """`scope = "container"` runs the healthcheck inside the service
    container's network namespace, so only that container can answer."""

    HC = {"command": "curl -f http://127.0.0.1:8000/", "interval": 2,
          "retries": 3, "scope": "container"}

    def test_container_scope_enters_the_service_netns(self):
        probe = units.probe_command("app", "web", self.HC)
        self.assertIn("inspect -f '{{.State.Pid}}' rigx-app-web", probe)
        self.assertIn("nsenter -t \"$pid\" -n /bin/sh -c "
                      "'curl -f http://127.0.0.1:8000/'", probe)
        # systemd expands `${VAR}` even inside quotes.
        self.assertNotIn("${", probe)

    def test_host_scope_and_old_manifests_run_the_command_as_is(self):
        # In a shell of its own: a `# comment` or `exit` in the command
        # must not reach the caller's `until …; do`.
        expected = "/bin/sh -c 'curl -f http://127.0.0.1:8000/'"
        self.assertEqual(
            units.probe_command("app", "web", {**self.HC, "scope": "host"}),
            expected,
        )
        old = {k: v for k, v in self.HC.items() if k != "scope"}
        self.assertEqual(units.probe_command("app", "web", old), expected)

    def test_health_unit_escapes_dollars_for_systemd(self):
        hc = {**self.HC, "command": "curl http://127.0.0.1:${PORT}/"}
        text = units.render_health_unit("app", "web", _svc(healthcheck=hc))
        exec_line = next(l for l in text.splitlines() if l.startswith("ExecStart="))
        # systemd turns `$$` into `$`; no lone `$` may reach it.
        import re
        self.assertIsNone(re.search(r"(?<!\$)\$(?!\$)", exec_line.replace("$$", "")))
        self.assertIn("$${PORT}", exec_line)

    def test_environment_escapes_specifiers(self):
        text = units.render_unit("app", "web", _svc(env={"FMT": "%Y-%m"}))
        self.assertIn("FMT=%%Y-%%m", text)

    def test_health_unit_uses_the_probe_and_escapes_specifiers(self):
        hc = {**self.HC, "command": "curl -s -w '%{http_code}' http://127.0.0.1:8000/"}
        text = units.render_health_unit("app", "web", _svc(healthcheck=hc))
        self.assertIn("nsenter", text)
        self.assertIn("%%{http_code}", text)
        self.assertNotIn("'%{http_code}", text.replace("%%", ""))

    def _run_probe(self, pid: str) -> int:
        """Run the rendered probe with stand-ins: `podman inspect` prints
        `pid`, and `nsenter -t PID -n /bin/sh -c CMD` runs CMD."""
        import shutil
        import subprocess
        import tempfile
        from pathlib import Path
        sh = shutil.which("sh") or shutil.which("bash")
        with tempfile.TemporaryDirectory() as d:
            bindir = Path(d)
            for name, body in {
                "podman": f'echo "{pid}"\n',
                "docker": "exit 1\n",
                # Drop `-t PID -n /bin/sh` (the sandbox has no /bin/sh).
                "nsenter": 'shift 4\nexec sh "$@"\n',
            }.items():
                (bindir / name).write_text(f"#!{sh}\n{body}")
                (bindir / name).chmod(0o755)
            probe = units.probe_command(
                "app", "web", {**self.HC, "command": "exit 0"},
            )
            env = {"PATH": f"{bindir}:{Path(sh).parent}"}
            return subprocess.run([sh, "-c", probe], env=env).returncode

    def test_probe_passes_when_the_container_runs(self):
        self.assertEqual(self._run_probe("4242"), 0)

    def test_probe_fails_when_the_container_is_stopped(self):
        # A stopped container reports PID 0; the command must not run.
        self.assertNotEqual(self._run_probe("0"), 0)


if __name__ == "__main__":
    unittest.main()
