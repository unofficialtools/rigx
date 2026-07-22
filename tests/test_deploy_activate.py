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

# Minimal `nix-env --profile P --set T` / `--profile P --rollback`
# simulation: a `<profile>.history` file tracks every `--set` target in
# order; `--set` appends + symlinks; `--rollback` pops the last entry
# and symlinks to the new last one (a simplified two-generation model —
# enough to exercise rigx's own rollback logic, not a real Nix profile).
_FAKE_NIX_ENV = """
echo "$*" >> "$NIXENV_LOG"
profile=""
mode=""
target=""
while [ $# -gt 0 ]; do
  case "$1" in
    --profile) profile="$2"; shift 2 ;;
    --set) mode="set"; target="$2"; shift 2 ;;
    --rollback) mode="rollback"; shift ;;
    *) shift ;;
  esac
done
hist="$profile.history"
case "$mode" in
  set)
    echo "$target" >> "$hist"
    ln -sfn "$target" "$profile"
    ;;
  rollback)
    n=$(wc -l < "$hist" 2>/dev/null || echo 0)
    if [ "$n" -ge 2 ]; then
      sed -i '$d' "$hist"
      new=$(tail -n1 "$hist")
      ln -sfn "$new" "$profile"
    fi
    ;;
esac
exit 0
"""


def _plan_dir(root: Path, name: str, manifest: dict, previous: dict | None = None) -> Path:
    """Materialize a plan directory (units/ + bin/activate + bin/rollback)
    for `manifest` under `root/name`."""
    plan = root / name
    (plan / "units").mkdir(parents=True)
    (plan / "bin").mkdir()
    for unit_name, text in units.render_units(manifest).items():
        (plan / "units" / unit_name).write_text(text)
    activate_path = plan / "bin" / "activate"
    rollback_path = plan / "bin" / "rollback"
    activate_path.write_text(activate.render_activate(manifest, previous))
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
        self.systemctl_log = self.root / "systemctl.log"
        self.nixenv_log = self.root / "nixenv.log"

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
        env["SYSTEMCTL_LOG"] = str(self.systemctl_log)
        env["NIXENV_LOG"] = str(self.nixenv_log)
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            [_BASH, str(script)], env=env, capture_output=True, text=True,
        )

    def systemctl_calls(self) -> list[str]:
        if not self.systemctl_log.exists():
            return []
        return self.systemctl_log.read_text().splitlines()

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
        plan2 = _plan_dir(self.root, "plan2", m2, previous=m1)
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
        plan2 = _plan_dir(self.root, "plan2", m2, previous=m1)
        r2 = self.run_script(plan2 / "bin" / "activate")
        self.assertEqual(r2.returncode, 0, r2.stderr)

        self.assertIn("disable --now rigx-app-web.service", self.systemctl_calls())
        self.assertFalse((self.systemd_dir / "rigx-app-web.service").exists())


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
        plan2 = _plan_dir(self.root, "plan2", m2, previous=m1)
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
        plan2 = _plan_dir(self.root, "plan2", m2, previous=m1)
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
        plan2 = _plan_dir(self.root, "plan2", m2, previous=m1)
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
        plan2 = _plan_dir(self.root, "plan2", m, previous=m)
        r2 = self.run_script(plan2 / "bin" / "activate")
        self.assertEqual(r2.returncode, 0, r2.stderr)
        self.assertFalse(
            any("restart" in c for c in self.systemctl_calls()),
            f"unexpected restart: {self.systemctl_calls()}",
        )


if __name__ == "__main__":
    unittest.main()
