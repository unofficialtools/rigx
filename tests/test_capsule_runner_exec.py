"""Behavioral tests for the GENERATED capsule runner scripts.

`rigx/nix/capsule_common.py` renders the bash runner at build time;
`rigx.capsule.start()` drives it at run time — possibly a runner built
by a different rigx version. These tests execute the real rendered
runner under bash with a fake `docker` (records the argv it would have
run) and a fake `jq` (implements exactly the filters the runtime-spec
loader uses) so the spec-vs-legacy precedence contract is pinned
without needing a container engine on the test host.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from rigx.config import Target
from rigx.nix import capsule_common


# Implements only the jq filters `_emit_runtime_spec_loader` uses; any
# other filter aborts loudly so a loader change can't silently bypass
# these tests.
_FAKE_JQ_PY = r'''
import json, sys

args = sys.argv[1:]
if not args or args[0] != "-r" or len(args) != 3:
    sys.exit(f"fake jq: unexpected argv {args!r}")
filt, path = args[1], args[2]
with open(path) as f:
    spec = json.load(f)

def emit(val):
    if val:
        print(val)

if filt == ".name // empty":
    emit(spec.get("name") or "")
elif filt == ".network // empty":
    emit(spec.get("network") or "")
elif filt == ".user // empty":
    emit(spec.get("user") or "")
elif filt == 'if .detach then "1" else empty end':
    emit("1" if spec.get("detach") else "")
elif filt == 'if .keep_state then "1" else empty end':
    emit("1" if spec.get("keep_state") else "")
elif filt == '(.publish // []) | join(",")':
    print(",".join(spec.get("publish") or []))
elif filt == '(.publish_udp // []) | join(",")':
    print(",".join(spec.get("publish_udp") or []))
elif filt == '(.volumes // []) | join(",")':
    print(",".join(spec.get("volumes") or []))
elif filt == '.env // {} | to_entries | map("\\(.key)=\\(.value)") | .[]':
    for k, v in (spec.get("env") or {}).items():
        print(f"{k}={v}")
else:
    sys.exit(f"fake jq: unsupported filter {filt!r} — update the test")
'''

_FAKE_DOCKER = """\
#!/usr/bin/env bash
cmd="${1:-}"; shift || true
case "$cmd" in
    load) cat > /dev/null ;;
    run)  printf '%s\\n' "$@" > "$DOCKER_DUMP" ;;
esac
exit 0
"""


def _capsule_target(**kwargs) -> Target:
    defaults = dict(
        name="svc", kind="capsule", backend="lite", entrypoint="/bin/true",
    )
    defaults.update(kwargs)
    return Target(**defaults)


class RunnerExecBase(unittest.TestCase):
    """Shared harness: lay out a fake built capsule, prepend fake
    docker/jq to PATH, run the rendered runner, return the `docker run`
    argv (one arg per line in $DOCKER_DUMP)."""

    def run_runner(
        self,
        script: str,
        *,
        spec: dict | None = None,
        extra_env: dict[str, str] | None = None,
        expect_dump: str = "docker",
    ) -> list[str]:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        cap_dir = root / "cap"
        (cap_dir / "bin").mkdir(parents=True)
        (cap_dir / "image").mkdir()
        (cap_dir / "image" / "image.tar.gz").write_bytes(b"")
        runner = cap_dir / "bin" / "run-svc"
        runner.write_text(script)
        runner.chmod(0o755)

        bin_dir = root / "fakebin"
        bin_dir.mkdir()
        (bin_dir / "fake_jq.py").write_text(_FAKE_JQ_PY)
        jq = bin_dir / "jq"
        jq.write_text(
            f'#!/usr/bin/env bash\n'
            f'exec "{sys.executable}" "{bin_dir}/fake_jq.py" "$@"\n'
        )
        jq.chmod(0o755)
        docker = bin_dir / "docker"
        docker.write_text(_FAKE_DOCKER)
        docker.chmod(0o755)

        dump = root / "docker-args"
        env = {
            k: v for k, v in os.environ.items() if not k.startswith("RIGX_")
        }
        env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
        env["DOCKER_DUMP"] = str(dump)
        env["XDG_CACHE_HOME"] = str(root / "cache")
        if spec is not None:
            spec_path = root / "spec.json"
            spec_path.write_text(json.dumps(spec))
            env["RIGX_RUNTIME_SPEC"] = str(spec_path)
        env.update(extra_env or {})
        r = subprocess.run(
            [str(runner)], env=env, capture_output=True, text=True,
        )
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        return dump.read_text().splitlines()

    @staticmethod
    def flag_values(args: list[str], flag: str) -> list[str]:
        """Values following each occurrence of `flag` (e.g. `-e`, `-p`)."""
        return [args[i + 1] for i, a in enumerate(args) if a == flag]


class SpecEnvPrecedence(RunnerExecBase):
    """The JSON spec is authoritative in new runners: env values with
    commas survive intact and legacy RIGX_ENV never shadows the spec."""

    def test_spec_env_survives_commas_and_beats_legacy(self):
        script = capsule_common.capsule_runner_script(
            _capsule_target(), mode="run"
        )
        args = self.run_runner(
            script,
            spec={
                "name": "c1", "detach": True,
                "env": {"OPTS": "a,b", "FOO": "bar"},
            },
            # What an orchestrator sets alongside the spec for runners
            # built by an older rigx (comma entries omitted there).
            extra_env={"RIGX_ENV": "FOO=bar", "RIGX_DETACH": "1"},
        )
        envs = self.flag_values(args, "-e")
        self.assertIn("OPTS=a,b", envs)
        self.assertEqual(envs.count("FOO=bar"), 1)
        # The legacy comma-split failure mode: `-e OPTS=a -e b`.
        self.assertNotIn("OPTS=a", envs)
        self.assertNotIn("b", envs)

    def test_legacy_env_still_applies_without_spec(self):
        script = capsule_common.capsule_runner_script(
            _capsule_target(), mode="run"
        )
        args = self.run_runner(
            script,
            extra_env={"RIGX_ENV": "FOO=bar,BAZ=qux", "RIGX_DETACH": "1"},
        )
        envs = self.flag_values(args, "-e")
        self.assertIn("FOO=bar", envs)
        self.assertIn("BAZ=qux", envs)

    def test_nixos_runner_spec_env_beats_legacy(self):
        script = capsule_common.nixos_runner_script(
            _capsule_target(backend="nixos")
        )
        args = self.run_runner(
            script,
            spec={"name": "c1", "detach": True, "env": {"OPTS": "a,b"}},
            extra_env={"RIGX_DETACH": "1"},
        )
        envs = self.flag_values(args, "-e")
        self.assertIn("OPTS=a,b", envs)
        self.assertNotIn("b", envs)


class PublishAppliedOnce(RunnerExecBase):
    """Each publish mapping must yield exactly one `-p` flag (docker)
    or one hostfwd rule (qemu). The old host contract folded UDP
    entries into RIGX_PUBLISH with a `/udp` suffix ON TOP OF the spec's
    publish_udp — applying every UDP mapping twice; on qemu the
    duplicate hostfwd rule fails to bind and the VM dies at boot."""

    # What the fixed orchestrator sets: spec + equivalent legacy vars.
    _SPEC = {
        "name": "c1", "detach": True,
        "publish": ["5040:5000"],
        "publish_udp": ["6040:6000"],
    }
    _LEGACY = {
        "RIGX_PUBLISH": "5040:5000",
        "RIGX_PUBLISH_UDP": "6040:6000",
        "RIGX_DETACH": "1",
    }

    def test_lite_runner_emits_one_p_flag_per_mapping(self):
        script = capsule_common.capsule_runner_script(
            _capsule_target(), mode="run"
        )
        args = self.run_runner(script, spec=self._SPEC,
                               extra_env=dict(self._LEGACY))
        pubs = self.flag_values(args, "-p")
        self.assertEqual(sorted(pubs), ["5040:5000", "6040:6000/udp"])

    def test_qemu_runner_emits_one_hostfwd_rule_per_mapping(self):
        script = capsule_common.qemu_runner_script(
            _capsule_target(backend="qemu")
        )
        opts = self.run_qemu_runner(script, spec=self._SPEC,
                                    extra_env=dict(self._LEGACY))
        rules = sorted(opts.split(",")) if opts else []
        self.assertEqual(
            rules,
            ["hostfwd=tcp::5040-:5000", "hostfwd=udp::6040-:6000"],
        )

    def run_qemu_runner(
        self,
        script: str,
        *,
        spec: dict | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> str:
        """Like run_runner, but for the qemu flavor: stub out the
        underlying NixOS vm script and return the QEMU_NET_OPTS the
        runner exec'd it with."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        cap_dir = root / "cap"
        (cap_dir / "bin").mkdir(parents=True)
        vm_bin = cap_dir / "system" / "vm-script" / "bin"
        vm_bin.mkdir(parents=True)
        vm = vm_bin / "run-svc-vm"
        vm.write_text(
            '#!/usr/bin/env bash\n'
            'printf \'%s\' "${QEMU_NET_OPTS:-}" > "$QEMU_DUMP"\n'
            'exit 0\n'
        )
        vm.chmod(0o755)
        runner = cap_dir / "bin" / "run-svc"
        runner.write_text(script)
        runner.chmod(0o755)

        bin_dir = root / "fakebin"
        bin_dir.mkdir()
        (bin_dir / "fake_jq.py").write_text(_FAKE_JQ_PY)
        jq = bin_dir / "jq"
        jq.write_text(
            f'#!/usr/bin/env bash\n'
            f'exec "{sys.executable}" "{bin_dir}/fake_jq.py" "$@"\n'
        )
        jq.chmod(0o755)

        dump = root / "qemu-opts"
        env = {
            k: v for k, v in os.environ.items() if not k.startswith("RIGX_")
        }
        env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
        env["QEMU_DUMP"] = str(dump)
        if spec is not None:
            spec_path = root / "spec.json"
            spec_path.write_text(json.dumps(spec))
            env["RIGX_RUNTIME_SPEC"] = str(spec_path)
        env.update(extra_env or {})
        r = subprocess.run(
            [str(runner)], env=env, capture_output=True, text=True,
        )
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        return dump.read_text()


class SpecLoaderText(unittest.TestCase):
    """Text-level pin: the loader assigns spec fields directly (spec
    overrides legacy) — the old `${VAR:-$_v}` form gave legacy vars
    precedence and, since orchestrators always set them, the spec
    never won."""

    def test_loader_gives_spec_precedence_in_all_runners(self):
        scripts = [
            capsule_common.capsule_runner_script(
                _capsule_target(), mode="run"
            ),
            capsule_common.capsule_runner_script(
                _capsule_target(), mode="shell"
            ),
            capsule_common.nixos_runner_script(
                _capsule_target(backend="nixos")
            ),
            capsule_common.qemu_runner_script(
                _capsule_target(backend="qemu")
            ),
        ]
        for script in scripts:
            self.assertNotIn(":-$_v", script)
            self.assertIn('RIGX_PUBLISH="$_v"', script)
            self.assertIn("_RIGX_SPEC_ENV", script)


if __name__ == "__main__":
    unittest.main()
