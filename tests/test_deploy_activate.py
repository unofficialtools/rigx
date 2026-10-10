"""Behavioral tests for the GENERATED `bin/activate` / `bin/rollback`
scripts (`rigx.deploy.activate`).

Executes the real rendered POSIX-sh scripts under bash (a strict
superset), against a real temp filesystem for
`RIGX_SYSTEMD_DIR`/`RIGX_STATE_ROOT`/`RIGX_PROFILE_DIR` (all
overridable via env — see the module docstring) and fake
`systemctl`/`nix-env` binaries on PATH that log every call and
simulate just enough of the real ones (profile symlink + generation
history for `nix-env`) for the diff/health-gate/rollback logic to be
exercised for real, without root or a real systemd/Nix profile store.
Mirrors the fake-binary approach in test_capsule_runner_exec.py.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from rigx.deploy import activate, units

_BASH = shutil.which("bash") or "/bin/bash"


def _write_fake_bin(bindir: Path, name: str, body: str) -> None:
    p = bindir / name
    p.write_text(f"#!{_BASH}\n" + body)
    p.chmod(0o755)


_FAKE_SYSTEMCTL = """
echo "$*" >> "$SYSTEMCTL_LOG"
exit 0
"""

# Container engine stand-in. `network inspect` succeeds only once the
# network has been "created", so activate's idempotence guard is
# exercised for real rather than assumed.
_FAKE_ENGINE = """
echo "$*" >> "$ENGINE_LOG"
if [ "${1:-}" = network ]; then
  case "${2:-}" in
    inspect) [ -e "$ENGINE_STATE/net-${3:-}" ] ;;
    create)  touch "$ENGINE_STATE/net-${3:-}" ;;
    *) exit 0 ;;
  esac
  exit $?
fi
exit 0
"""

# Minimal Nix profile: `<profile>.history` holds one generation per
# line (generation N = line N), `<profile>.current` the current number.
# `--set` appends a generation and makes it current; `--rollback` steps
# back one; `--switch-generation N` jumps; `--list-generations` prints
# them with `(current)` marked, like the real thing.
_FAKE_NIX_ENV = """
echo "$*" >> "$NIXENV_LOG"
profile=""; mode=""; arg=""
while [ $# -gt 0 ]; do
  case "$1" in
    --profile) profile="$2"; shift 2 ;;
    --set) mode=set; arg="$2"; shift 2 ;;
    --rollback) mode=rollback; shift ;;
    --switch-generation) mode=switch; arg="$2"; shift 2 ;;
    --list-generations) mode=list; shift ;;
    *) shift ;;
  esac
done
hist="$profile.history"; cur="$profile.current"
point() { ln -sfn "$(sed -n "${1}p" "$hist")" "$profile"; echo "$1" > "$cur"; }
case "$mode" in
  set) echo "$arg" >> "$hist"; point "$(wc -l < "$hist")" ;;
  rollback) c=$(cat "$cur"); [ "$c" -gt 1 ] && point $((c - 1)) ;;
  switch) point "$arg" ;;
  list)
    c=$(cat "$cur" 2>/dev/null || echo 0); n=0
    while IFS= read -r _; do
      n=$((n + 1))
      if [ "$n" = "$c" ]; then echo "   $n   2026-10-10 (current)"
      else echo "   $n   2026-10-10"; fi
    done < "$hist" ;;
esac
exit 0
"""


def _plan_dir(root: Path, name: str, manifest: dict) -> Path:
    """Materialize a plan directory (units/ + bin/activate + bin/rollback)
    for `manifest` under `root/name`."""
    plan = root / name
    (plan / "units").mkdir(parents=True)
    (plan / "bin").mkdir()
    for unit_name, text in units.render_units(manifest).items():
        (plan / "units" / unit_name).write_text(text)
    activate_path = plan / "bin" / "activate"
    rollback_path = plan / "bin" / "rollback"
    activate_path.write_text(activate.render_activate(manifest))
    rollback_path.write_text(activate.render_rollback(manifest))
    # `rollback` execs `<generation>/bin/activate` directly (relying on
    # its shebang, same as it would on a real deploy target) — needs
    # the exec bit for that to work.
    activate_path.chmod(0o755)
    rollback_path.chmod(0o755)
    return plan


def _service(capsule_path="/nix/store/x", healthcheck=None, **overrides) -> dict:
    base = {
        "capsule": "c", "capsule_path": capsule_path, "depends_on": [],
        "restart": "always", "ports": [], "volumes": {}, "secrets": {},
        "healthcheck": healthcheck,
    }
    base.update(overrides)
    return base


class ActivateHarness(unittest.TestCase):
    """Sets up a temp filesystem + fake systemctl/nix-env, running
    activate scripts against it."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.systemd_dir = self.root / "systemd"
        self.state_root = self.root / "state"
        self.profile_dir = self.root / "profiles"
        for d in (self.systemd_dir, self.state_root, self.profile_dir):
            d.mkdir()
        self.bindir = self.root / "bin"
        self.bindir.mkdir()
        _write_fake_bin(self.bindir, "systemctl", _FAKE_SYSTEMCTL)
        _write_fake_bin(self.bindir, "nix-env", _FAKE_NIX_ENV)
        _write_fake_bin(self.bindir, "podman", _FAKE_ENGINE)
        self.systemctl_log = self.root / "systemctl.log"
        self.nixenv_log = self.root / "nixenv.log"
        self.engine_log = self.root / "engine.log"
        self.engine_state = self.root / "engine-state"
        self.engine_state.mkdir()

    def addCleanup_(self):
        self._tmp.cleanup()

    def tearDown(self):
        self._tmp.cleanup()

    def run_script(self, script: Path, extra_env: dict | None = None) -> subprocess.CompletedProcess:
        import os
        env = dict(os.environ)
        env["PATH"] = f"{self.bindir}:{env['PATH']}"
        env["RIGX_SYSTEMD_DIR"] = str(self.systemd_dir)
        env["RIGX_STATE_ROOT"] = str(self.state_root)
        env["RIGX_PROFILE_DIR"] = str(self.profile_dir)
        env["RIGX_GCROOTS_DIR"] = str(self.root / "gcroots")
        env["SYSTEMCTL_LOG"] = str(self.systemctl_log)
        env["NIXENV_LOG"] = str(self.nixenv_log)
        env["ENGINE_LOG"] = str(self.engine_log)
        env["ENGINE_STATE"] = str(self.engine_state)
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            [_BASH, str(script)], env=env, capture_output=True, text=True,
        )

    def systemctl_calls(self) -> list[str]:
        if not self.systemctl_log.exists():
            return []
        return self.systemctl_log.read_text().splitlines()

    def engine_calls(self) -> list[str]:
        if not self.engine_log.exists():
            return []
        return self.engine_log.read_text().splitlines()

    def profile_target(self, testbed: str) -> str | None:
        link = self.profile_dir / f"rigx-{testbed}"
        return str(link.resolve()) if link.exists() else None


class FirstDeploySucceeds(ActivateHarness):
    def test_healthcheck_passes_then_profile_advances(self):
        flag = self.root / "healthy"
        flag.touch()
        manifest = {
            "testbed": "app", "start_order": ["web"],
            "services": {
                "web": _service(healthcheck={
                    "command": f'test -e "{flag}"', "interval": 0, "retries": 3,
                }),
            },
            "volumes": [], "secrets": [],
        }
        plan = _plan_dir(self.root, "plan1", manifest)
        result = self.run_script(plan / "bin" / "activate")

        self.assertEqual(result.returncode, 0, result.stderr)
        unit_link = self.systemd_dir / "rigx-app-web.service"
        self.assertTrue(unit_link.is_symlink())
        self.assertEqual(unit_link.resolve(), (plan / "units" / "rigx-app-web.service").resolve())
        calls = self.systemctl_calls()
        self.assertIn("daemon-reload", calls)
        self.assertIn("restart rigx-app-web.service", calls)
        self.assertEqual(self.profile_target("app"), str(plan.resolve()))
        # Enabled for boot, as `systemctl enable` would.
        wants = self.systemd_dir / "multi-user.target.wants" / "rigx-app-web.service"
        self.assertTrue(wants.is_symlink())
        self.assertEqual(Path(os.readlink(wants)), unit_link)

    def test_no_healthcheck_uses_is_active_fallback(self):
        manifest = {
            "testbed": "app", "start_order": ["web"],
            "services": {"web": _service()},
            "volumes": [], "secrets": [],
        }
        plan = _plan_dir(self.root, "plan1", manifest)
        result = self.run_script(plan / "bin" / "activate")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("is-active --quiet rigx-app-web.service", self.systemctl_calls())
        self.assertEqual(self.profile_target("app"), str(plan.resolve()))

    def test_creates_declared_volume_directories(self):
        manifest = {
            "testbed": "app", "start_order": ["db"],
            "services": {"db": _service(volumes={"data": "/var/lib/postgresql/data"})},
            "volumes": ["data"], "secrets": [],
        }
        plan = _plan_dir(self.root, "plan1", manifest)
        result = self.run_script(plan / "bin" / "activate")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.state_root / "app" / "volumes" / "data").is_dir())


class FirstDeployFails(ActivateHarness):
    def test_exhausted_healthcheck_rolls_back_and_exits_nonzero(self):
        manifest = {
            "testbed": "app", "start_order": ["web"],
            "services": {
                "web": _service(healthcheck={
                    "command": "false", "interval": 0, "retries": 2,
                }),
            },
            "volumes": [], "secrets": [],
        }
        plan = _plan_dir(self.root, "plan1", manifest)
        result = self.run_script(plan / "bin" / "activate")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("failed healthcheck", result.stderr)
        # first-ever deploy: nothing to fall back to -> unit removed, stopped
        self.assertFalse((self.systemd_dir / "rigx-app-web.service").exists())
        self.assertIn("stop rigx-app-web.service", self.systemctl_calls())
        self.assertFalse(os.path.lexists(
            self.systemd_dir / "multi-user.target.wants" / "rigx-app-web.service"
        ))
        # profile pointer never advances on failure
        self.assertIsNone(self.profile_target("app"))


class SecondDeployOnlyRestartsChanged(ActivateHarness):
    def test_upgrade_only_touches_changed_service(self):
        m1 = {
            "testbed": "app", "start_order": ["db", "web"],
            "services": {
                "db": _service(capsule_path="/nix/store/db-v1"),
                "web": _service(capsule_path="/nix/store/web-v1", depends_on=["db"]),
            },
            "volumes": [], "secrets": [],
        }
        plan1 = _plan_dir(self.root, "plan1", m1)
        r1 = self.run_script(plan1 / "bin" / "activate")
        self.assertEqual(r1.returncode, 0, r1.stderr)
        self.assertEqual(self.profile_target("app"), str(plan1.resolve()))

        self.systemctl_log.write_text("")  # reset call log for the upgrade

        m2 = {
            "testbed": "app", "start_order": ["db", "web"],
            "services": {
                "db": _service(capsule_path="/nix/store/db-v1"),               # unchanged
                "web": _service(capsule_path="/nix/store/web-v2", depends_on=["db"]),  # changed
            },
            "volumes": [], "secrets": [],
        }
        plan2 = _plan_dir(self.root, "plan2", m2)
        r2 = self.run_script(plan2 / "bin" / "activate")
        self.assertEqual(r2.returncode, 0, r2.stderr)

        calls = self.systemctl_calls()
        self.assertIn("restart rigx-app-web.service", calls)
        self.assertNotIn("restart rigx-app-db.service", calls)
        self.assertEqual(self.profile_target("app"), str(plan2.resolve()))
        # db's unit symlink still points at plan1's copy — never relinked
        db_link = self.systemd_dir / "rigx-app-db.service"
        self.assertEqual(db_link.resolve(), (plan1 / "units" / "rigx-app-db.service").resolve())

    def test_removed_service_is_disabled_and_unlinked(self):
        m1 = {
            "testbed": "app", "start_order": ["db", "web"],
            "services": {"db": _service(), "web": _service(depends_on=["db"])},
            "volumes": [], "secrets": [],
        }
        plan1 = _plan_dir(self.root, "plan1", m1)
        r1 = self.run_script(plan1 / "bin" / "activate")
        self.assertEqual(r1.returncode, 0, r1.stderr)

        m2 = {
            "testbed": "app", "start_order": ["db"],
            "services": {"db": _service()},
            "volumes": [], "secrets": [],
        }
        plan2 = _plan_dir(self.root, "plan2", m2)
        r2 = self.run_script(plan2 / "bin" / "activate")
        self.assertEqual(r2.returncode, 0, r2.stderr)

        self.assertIn("disable --now rigx-app-web.service", self.systemctl_calls())
        self.assertFalse((self.systemd_dir / "rigx-app-web.service").exists())

    def test_removed_service_has_its_container_reaped(self):
        # The unit is gone, so nothing is left to reap the container it
        # started — it would keep running, unsupervised, still holding
        # the ports of a service the config no longer declares.
        m1 = {
            "testbed": "app", "start_order": ["db", "web"],
            "services": {"db": _service(), "web": _service(depends_on=["db"])},
            "volumes": [], "secrets": [],
        }
        plan1 = _plan_dir(self.root, "plan1", m1)
        self.run_script(plan1 / "bin" / "activate")
        self.engine_log.write_text("")

        m2 = {
            "testbed": "app", "start_order": ["db"],
            "services": {"db": _service()},
            "volumes": [], "secrets": [],
        }
        plan2 = _plan_dir(self.root, "plan2", m2)
        r2 = self.run_script(plan2 / "bin" / "activate")
        self.assertEqual(r2.returncode, 0, r2.stderr)

        self.assertIn("rm -f rigx-app-web", self.engine_calls())
        # db is still declared; its container must not be touched.
        self.assertNotIn("rm -f rigx-app-db", self.engine_calls())


class FailedGateReapsContainers(ActivateHarness):
    """A container that outlives a *failed* deploy is the worst version
    of the problem: it holds the ports the next attempt needs while
    still answering the host-side health probe that decides whether that
    attempt worked."""

    def test_first_deploy_health_failure_reaps_the_container(self):
        manifest = {
            "testbed": "app", "start_order": ["web"],
            "services": {
                "web": _service(healthcheck={
                    "command": "false", "interval": 0, "retries": 2,
                }),
            },
            "volumes": [], "secrets": [],
        }
        plan = _plan_dir(self.root, "plan1", manifest)
        result = self.run_script(plan / "bin" / "activate")

        self.assertEqual(result.returncode, 1)
        self.assertIn("failed healthcheck", result.stderr)
        # No previous generation to fall back to, so the unit is stopped
        # outright — and the container has to go with it.
        self.assertIn("stop rigx-app-web.service", self.systemctl_calls())
        self.assertIn("rm -f rigx-app-web", self.engine_calls())


class Rollback(ActivateHarness):
    def test_rollback_reverts_to_previous_generation(self):
        m1 = {
            "testbed": "app", "start_order": ["web"],
            "services": {"web": _service(capsule_path="/nix/store/web-v1")},
            "volumes": [], "secrets": [],
        }
        plan1 = _plan_dir(self.root, "plan1", m1)
        r1 = self.run_script(plan1 / "bin" / "activate")
        self.assertEqual(r1.returncode, 0, r1.stderr)

        m2 = {
            "testbed": "app", "start_order": ["web"],
            "services": {"web": _service(capsule_path="/nix/store/web-v2")},
            "volumes": [], "secrets": [],
        }
        plan2 = _plan_dir(self.root, "plan2", m2)
        r2 = self.run_script(plan2 / "bin" / "activate")
        self.assertEqual(r2.returncode, 0, r2.stderr)
        self.assertEqual(self.profile_target("app"), str(plan2.resolve()))

        r3 = self.run_script(plan2 / "bin" / "rollback")
        self.assertEqual(r3.returncode, 0, r3.stderr)
        self.assertEqual(self.profile_target("app"), str(plan1.resolve()))
        # rollback re-invoked plan1's own activate, which re-linked the
        # live unit back to plan1's copy
        web_link = self.systemd_dir / "rigx-app-web.service"
        self.assertEqual(web_link.resolve(), (plan1 / "units" / "rigx-app-web.service").resolve())


class NetworkCreation(ActivateHarness):
    """The shared network has to exist before anything that joins it
    starts, and re-deploying must not try to recreate it."""

    def _manifest(self):
        return {
            "testbed": "app", "network": "rigx-app",
            "start_order": ["web"],
            "services": {"web": _service()},
            "volumes": [], "secrets": [],
        }

    def test_creates_the_network_before_starting_services(self):
        plan = _plan_dir(self.root, "plan1", self._manifest())
        r = self.run_script(plan / "bin" / "activate")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("network create rigx-app", self.engine_calls())
        # Ordering matters: a unit that joins a missing network fails.
        self.assertTrue(
            (self.engine_state / "net-rigx-app").exists(),
        )
        self.assertTrue(
            any("restart rigx-app-web.service" in c for c in self.systemctl_calls())
        )

    def test_redeploy_does_not_recreate_an_existing_network(self):
        m = self._manifest()
        r1 = self.run_script(_plan_dir(self.root, "plan1", m) / "bin" / "activate")
        self.assertEqual(r1.returncode, 0, r1.stderr)
        self.engine_log.unlink()

        r2 = self.run_script(
            _plan_dir(self.root, "plan2", m) / "bin" / "activate"
        )
        self.assertEqual(r2.returncode, 0, r2.stderr)
        calls = self.engine_calls()
        self.assertIn("network inspect rigx-app", calls)
        self.assertNotIn("network create rigx-app", calls)

    def test_manifest_without_a_network_touches_no_engine(self):
        m = self._manifest()
        del m["network"]
        r = self.run_script(_plan_dir(self.root, "plan1", m) / "bin" / "activate")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.engine_calls(), [])

    def test_fails_clearly_when_no_engine_is_installed(self):
        (self.bindir / "podman").unlink()
        plan = _plan_dir(self.root, "plan1", self._manifest())
        # PATH is scrubbed to the fake bin dir so no real engine on the
        # test host is found. activate still runs `mkdir` before it gets
        # to the engine check (state dirs, HOME), so that has to remain
        # reachable or the script dies at 127 instead of the exit 1 this
        # is checking for.
        for tool in ("mkdir", "readlink"):
            real = shutil.which(tool)
            if real:
                (self.bindir / tool).symlink_to(real)
        r = self.run_script(
            plan / "bin" / "activate", extra_env={"PATH": str(self.bindir)},
        )
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("podman or docker", r.stderr)


class HealthUnitLifecycle(ActivateHarness):
    """A healthchecked service owns three units, and activate has to
    install and retire all of them — a leftover probe would keep
    restarting a service nobody asked it to watch."""

    HC = {"command": "true", "interval": 1, "retries": 2}

    def _manifest(self, *, healthcheck):
        return {
            "testbed": "app", "start_order": ["web"],
            "services": {"web": _service(healthcheck=healthcheck)},
            "volumes": [], "secrets": [],
        }

    def test_first_deploy_installs_probe_and_timer(self):
        plan = _plan_dir(self.root, "plan1", self._manifest(healthcheck=self.HC))
        result = self.run_script(plan / "bin" / "activate")

        self.assertEqual(result.returncode, 0, result.stderr)
        for unit in (
            "rigx-app-web.service",
            "rigx-app-web-health.service",
            "rigx-app-web-health.timer",
        ):
            link = self.systemd_dir / unit
            self.assertTrue(link.is_symlink(), f"{unit} not installed")
            self.assertEqual(link.resolve(), (plan / "units" / unit).resolve())

    def test_dropping_a_healthcheck_retires_the_probe_units(self):
        m1 = self._manifest(healthcheck=self.HC)
        plan1 = _plan_dir(self.root, "plan1", m1)
        r1 = self.run_script(plan1 / "bin" / "activate")
        self.assertEqual(r1.returncode, 0, r1.stderr)
        self.assertTrue((self.systemd_dir / "rigx-app-web-health.timer").exists())

        m2 = self._manifest(healthcheck=None)
        plan2 = _plan_dir(self.root, "plan2", m2)
        r2 = self.run_script(plan2 / "bin" / "activate")
        self.assertEqual(r2.returncode, 0, r2.stderr)

        for unit in ("rigx-app-web-health.service", "rigx-app-web-health.timer"):
            self.assertFalse(
                (self.systemd_dir / unit).exists(),
                f"{unit} left behind after the healthcheck was removed",
            )
        # The service itself stays, and the change was noticed.
        self.assertTrue((self.systemd_dir / "rigx-app-web.service").is_symlink())
        self.assertTrue(
            any("restart rigx-app-web.service" in c for c in self.systemctl_calls())
        )

    def test_removed_service_takes_its_probe_units_with_it(self):
        m1 = {
            "testbed": "app", "start_order": ["web", "old"],
            "services": {
                "web": _service(healthcheck=None),
                "old": _service(healthcheck=self.HC),
            },
            "volumes": [], "secrets": [],
        }
        plan1 = _plan_dir(self.root, "plan1", m1)
        r1 = self.run_script(plan1 / "bin" / "activate")
        self.assertEqual(r1.returncode, 0, r1.stderr)
        self.assertTrue((self.systemd_dir / "rigx-app-old-health.timer").exists())

        m2 = self._manifest(healthcheck=None)
        plan2 = _plan_dir(self.root, "plan2", m2)
        r2 = self.run_script(plan2 / "bin" / "activate")
        self.assertEqual(r2.returncode, 0, r2.stderr)

        for unit in (
            "rigx-app-old.service",
            "rigx-app-old-health.service",
            "rigx-app-old-health.timer",
        ):
            self.assertFalse(
                (self.systemd_dir / unit).exists(), f"{unit} left behind",
            )

    def test_unchanged_healthchecked_service_is_not_restarted(self):
        m = self._manifest(healthcheck=self.HC)
        plan1 = _plan_dir(self.root, "plan1", m)
        r1 = self.run_script(plan1 / "bin" / "activate")
        self.assertEqual(r1.returncode, 0, r1.stderr)

        # Same manifest, same capsule path: nothing about the units
        # differs, so the extra health units must not make activate
        # think the service changed.
        self.systemctl_log.unlink()
        plan2 = _plan_dir(self.root, "plan2", m)
        r2 = self.run_script(plan2 / "bin" / "activate")
        self.assertEqual(r2.returncode, 0, r2.stderr)
        self.assertFalse(
            any("restart" in c for c in self.systemctl_calls()),
            f"unexpected restart: {self.systemctl_calls()}",
        )


if __name__ == "__main__":
    unittest.main()


def _m(services: dict) -> dict:
    return {
        "testbed": "app", "start_order": list(services),
        "services": services, "volumes": [], "secrets": [],
    }


class RemovalsAfterTheGates(ActivateHarness):
    """Dropped services go only once every gate has passed, and are
    worked out against the plan that is actually live."""

    def test_failed_deploy_keeps_a_dropped_service(self):
        plan1 = _plan_dir(self.root, "plan1", _m({
            "db": _service(capsule_path="/nix/store/db-v1"), "web": _service(),
        }))
        self.assertEqual(self.run_script(plan1 / "bin" / "activate").returncode, 0)
        plan2 = _plan_dir(self.root, "plan2", _m({
            "db": _service(capsule_path="/nix/store/db-v2", healthcheck={
                "command": "false", "interval": 0, "retries": 1,
            }),
        }))
        r = self.run_script(plan2 / "bin" / "activate")
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn("disable --now rigx-app-web.service", self.systemctl_calls())
        self.assertTrue((self.systemd_dir / "rigx-app-web.service").exists())
        self.assertEqual(self.profile_target("app"), str(plan1.resolve()))

    def test_rollback_removes_services_the_newer_plan_added(self):
        plan1 = _plan_dir(self.root, "plan1", _m({"web": _service()}))
        plan2 = _plan_dir(self.root, "plan2", _m({
            "web": _service(), "worker": _service(capsule_path="/nix/store/w"),
        }))
        for plan in (plan1, plan2):
            self.assertEqual(self.run_script(plan / "bin" / "activate").returncode, 0)
        self.nixenv_log.write_text("")

        r = self.run_script(plan2 / "bin" / "rollback")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("disable --now rigx-app-worker.service", self.systemctl_calls())
        self.assertFalse((self.systemd_dir / "rigx-app-worker.service").exists())
        self.assertIn("rm -f rigx-app-worker", self.engine_calls())
        self.assertEqual(self.profile_target("app"), str(plan1.resolve()))

    def test_rollback_creates_no_generation_so_a_second_one_goes_further(self):
        plans = [
            _plan_dir(self.root, f"plan{i}", _m({
                "web": _service(capsule_path=f"/nix/store/web-v{i}"),
            }))
            for i in (1, 2, 3)
        ]
        for plan in plans:
            self.assertEqual(self.run_script(plan / "bin" / "activate").returncode, 0)
        self.assertEqual(self.run_script(plans[2] / "bin" / "rollback").returncode, 0)
        self.assertEqual(self.profile_target("app"), str(plans[1].resolve()))
        self.assertNotIn("--set", self.nixenv_log.read_text().splitlines()[-1])
        self.assertEqual(self.run_script(plans[1] / "bin" / "rollback").returncode, 0)
        self.assertEqual(self.profile_target("app"), str(plans[0].resolve()))

    def test_failed_rollback_moves_the_profile_forward_again(self):
        plan1 = _plan_dir(self.root, "plan1", _m({"web": _service(healthcheck={
            "command": f'test -e "{self.root / "v1-ok"}"', "interval": 0, "retries": 1,
        })}))
        (self.root / "v1-ok").touch()
        self.assertEqual(self.run_script(plan1 / "bin" / "activate").returncode, 0)
        plan2 = _plan_dir(self.root, "plan2", _m({
            "web": _service(capsule_path="/nix/store/web-v2"),
        }))
        self.assertEqual(self.run_script(plan2 / "bin" / "activate").returncode, 0)
        (self.root / "v1-ok").unlink()  # the old version no longer passes

        r = self.run_script(plan2 / "bin" / "rollback")
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self.profile_target("app"), str(plan2.resolve()))


class GcRoots(ActivateHarness):
    """Capsules are rooted per plan, so garbage collection can't remove
    what a live or rollback-able generation runs."""

    def roots(self) -> Path:
        return self.root / "gcroots" / "rigx-app"

    def test_capsules_and_artifacts_are_rooted(self):
        plan = _plan_dir(self.root, "plan1", _m({"web": _service(
            capsule_path="/nix/store/web",
            artifacts=[{"store_path": "/nix/store/site", "mount": "/srv"}],
        )}))
        self.assertEqual(self.run_script(plan / "bin" / "activate").returncode, 0)
        mine = self.roots() / "plan1"
        targets = {os.readlink(p) for p in mine.iterdir()}
        self.assertEqual(
            targets, {"/nix/store/site", "/nix/store/web", str(plan.resolve())},
        )

    def test_roots_of_unused_plans_are_pruned(self):
        plans = [
            _plan_dir(self.root, f"plan{i}", _m({
                "web": _service(capsule_path=f"/nix/store/web-v{i}"),
            }))
            for i in (1, 2, 3)
        ]
        # plan1 stays a profile generation; nothing refers to plan2 once
        # plan3 is live. A generation of `app-staging` must not count.
        (self.profile_dir / "rigx-app-1-link").symlink_to(plans[0])
        (self.profile_dir / "rigx-app-staging-2-link").symlink_to(plans[1])
        for plan in plans:
            self.assertEqual(self.run_script(plan / "bin" / "activate").returncode, 0)
        # While plan3 activated, plan2 was still live (plan3's gates
        # could have failed), so it is pruned on the next activation.
        self.assertIn("plan2", [p.name for p in self.roots().iterdir()])
        self.assertEqual(self.run_script(plans[2] / "bin" / "activate").returncode, 0)
        self.assertEqual(
            sorted(p.name for p in self.roots().iterdir()), ["plan1", "plan3"],
        )
