"""Tests for `rigx.capsule.start()` and the `Capsule` handle.

These tests don't run real docker — they substitute a fake runner
script that records its env (so we can assert what the helper passes
through) and either succeeds or fails on demand. Tests that need to
poke at a "running container" stub it via local TCP listeners.
"""

import json
import os
import socket
import stat
import subprocess
import tempfile
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from rigx import capsule


@contextmanager
def fake_capsule(
    name: str = "svc",
    runner_body: str = "#!/bin/sh\nexit 0\n",
    manifest: dict | None = None,
):
    """Create a tempdir laid out like a built capsule. Yields the
    project root (with rigx.toml) so `_find_output_dir()` discovers
    the output dir naturally."""
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name)
    (root / "rigx.toml").write_text("[project]\nname = 'p'\n")
    out_dir = root / "output" / name
    (out_dir / "bin").mkdir(parents=True)
    runner = out_dir / "bin" / f"run-{name}"
    runner.write_text(runner_body)
    runner.chmod(runner.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    m = manifest if manifest is not None else {
        "name": name, "backend": "lite", "hostname": name,
        "ports": [], "image": {"kind": "oci-tarball-host-store"},
    }
    (out_dir / "manifest.json").write_text(json.dumps(m))
    try:
        yield root
    finally:
        tmp.cleanup()


class ContainerEnginePreference(unittest.TestCase):
    """Podman first, docker as fallback — matching what `rigx remote
    deploy` installs and supervises on a target, so a capsule that runs
    locally is running under the same engine it will be deployed under.
    """

    def _which(self, available: set[str]):
        return lambda name: f"/usr/bin/{name}" if name in available else None

    def test_prefers_podman_when_both_present(self):
        with mock.patch.object(
            capsule.shutil, "which", side_effect=self._which({"podman", "docker"}),
        ):
            self.assertEqual(capsule._container_engine(), "podman")

    def test_falls_back_to_docker(self):
        with mock.patch.object(
            capsule.shutil, "which", side_effect=self._which({"docker"}),
        ):
            self.assertEqual(capsule._container_engine(), "docker")

    def test_raises_when_neither_present(self):
        with mock.patch.object(
            capsule.shutil, "which", side_effect=self._which(set()),
        ):
            with self.assertRaises(RuntimeError):
                capsule._container_engine()


class StartContract(unittest.TestCase):
    def test_runner_receives_rigx_env_vars(self):
        """`start()` invokes the runner with RIGX_NAME, RIGX_DETACH,
        RIGX_PUBLISH, RIGX_NETWORK, RIGX_ENV — that's the whole runner
        contract. Pin it via a runner that dumps its env to a file."""
        with fake_capsule(
            "svc",
            runner_body=(
                '#!/bin/sh\n'
                'env | grep -E "^RIGX_" | sort > "$DUMP"\n'
                'exit 0\n'
            ),
        ) as root:
            dump = root / "env-dump"
            with mock.patch.dict(os.environ, {"DUMP": str(dump)}):
                with capsule.start(
                    "svc",
                    publish={5000: 5040, 5001: 5041},
                    network="rigx-net",
                    container_name="my-svc",
                    env={"FOO": "bar", "BAZ": "qux"},
                    output_dir=root / "output",
                ):
                    pass
            text = dump.read_text()
        self.assertIn("RIGX_NAME=my-svc", text)
        self.assertIn("RIGX_DETACH=1", text)
        self.assertIn("RIGX_NETWORK=rigx-net", text)
        # Order-independent check on the comma-list values
        publish_line = next(
            l for l in text.splitlines() if l.startswith("RIGX_PUBLISH=")
        )
        self.assertEqual(
            sorted(publish_line.split("=", 1)[1].split(",")),
            sorted(["5040:5000", "5041:5001"]),
        )
        env_line = next(
            l for l in text.splitlines() if l.startswith("RIGX_ENV=")
        )
        self.assertEqual(
            sorted(env_line.split("=", 1)[1].split(",")),
            sorted(["BAZ=qux", "FOO=bar"]),
        )

    def test_missing_capsule_raises_with_clear_message(self):
        with fake_capsule("svc") as root:
            with self.assertRaises(RuntimeError) as ctx:
                with capsule.start(
                    "missing", output_dir=root / "output"
                ):
                    pass
            self.assertIn("not built", str(ctx.exception))

    def test_runner_failure_surfaces_stderr(self):
        with fake_capsule(
            "svc",
            runner_body='#!/bin/sh\necho boom >&2\nexit 7\n',
        ) as root:
            with self.assertRaises(RuntimeError) as ctx:
                with capsule.start("svc", output_dir=root / "output"):
                    pass
            msg = str(ctx.exception)
            self.assertIn("exit 7", msg)
            self.assertIn("boom", msg)

    def test_unsupported_backend_in_manifest(self):
        # `docker` is reserved for follow-on work; the error lists every
        # supported backend so the user knows their options.
        with fake_capsule(
            "svc",
            manifest={
                "name": "svc", "backend": "docker",
                "image": {"kind": "oci-tarball"},
            },
        ) as root:
            with self.assertRaises(RuntimeError) as ctx:
                with capsule.start("svc", output_dir=root / "output"):
                    pass
            msg = str(ctx.exception)
            for be in ("lite", "nixos", "qemu"):
                self.assertIn(be, msg)

    def test_nixos_backend_accepted(self):
        # `nixos` is docker/podman-shaped — same runner contract as
        # `lite` — so start() accepts it. Pin via a stub runner that
        # exits cleanly.
        with fake_capsule(
            "svc",
            runner_body="#!/bin/sh\nexit 0\n",
            manifest={
                "name": "svc", "backend": "nixos", "hostname": "svc",
                "ports": [],
                "image": {"kind": "oci-tarball-nixos-systemd"},
            },
        ) as root:
            with capsule.start("svc", output_dir=root / "output") as cap:
                self.assertEqual(cap.name, "svc")

    def test_default_container_name_is_unique_and_prefixed(self):
        """Two starts with no explicit name get distinct generated names —
        avoids collision when the same capsule is started twice in one
        test (e.g. happy-path then re-run after fault)."""
        names: list[str] = []
        runner_body = (
            '#!/bin/sh\n'
            'echo "$RIGX_NAME" >> "$DUMP"\n'
            'exit 0\n'
        )
        with fake_capsule("svc", runner_body=runner_body) as root:
            dump = root / "names"
            with mock.patch.dict(os.environ, {"DUMP": str(dump)}):
                with capsule.start("svc", output_dir=root / "output"):
                    pass
                with capsule.start("svc", output_dir=root / "output"):
                    pass
            names = dump.read_text().splitlines()
        self.assertEqual(len(names), 2)
        self.assertNotEqual(names[0], names[1])
        self.assertTrue(all(n.startswith("rigx-svc-") for n in names))


class ProjectRootResolution(unittest.TestCase):
    def test_derived_root_beats_stale_inherited_env_var(self):
        """A RIGX_PROJECT_ROOT inherited from the calling environment
        (e.g. an outer rigx invocation on a different project) must not
        beat the root start() derives from the output dir — a stale
        value would silently repoint relative volume mounts."""
        runner_body = (
            '#!/bin/sh\n'
            'env | grep "^RIGX_PROJECT_ROOT=" > "$DUMP"\n'
            'exit 0\n'
        )
        with fake_capsule("svc", runner_body=runner_body) as root:
            dump = root / "root-dump"
            with mock.patch.dict(os.environ, {
                "DUMP": str(dump),
                "RIGX_PROJECT_ROOT": "/stale/other-project",
            }):
                with capsule.start("svc", output_dir=root / "output"):
                    pass
            line = dump.read_text().strip()
        self.assertEqual(line.split("=", 1)[1], str(root.resolve()))

    def test_explicit_project_root_kwarg_still_wins(self):
        runner_body = (
            '#!/bin/sh\n'
            'env | grep "^RIGX_PROJECT_ROOT=" > "$DUMP"\n'
            'exit 0\n'
        )
        with fake_capsule("svc", runner_body=runner_body) as root:
            dump = root / "root-dump"
            pinned = root / "pinned"
            pinned.mkdir()
            with mock.patch.dict(os.environ, {
                "DUMP": str(dump),
                "RIGX_PROJECT_ROOT": "/stale/other-project",
            }):
                with capsule.start(
                    "svc", project_root=pinned, output_dir=root / "output"
                ):
                    pass
            line = dump.read_text().strip()
        self.assertEqual(line.split("=", 1)[1], str(pinned.resolve()))


class ModuleNamespacedStart(unittest.TestCase):
    """Module-merged capsules build to an out-link named after the
    sanitized Nix attr (`frontend.web` → `output/frontend_web`) while
    the runner inside is named after the SHORT target name
    (`bin/run-web`). start() must accept both the qualified rigx name
    and the sanitized attr, and locate the runner via the manifest."""

    @contextmanager
    def _module_capsule(self):
        """Fake built module capsule laid out the way the builder
        leaves it: dir = sanitized attr, runner = short name,
        manifest name = qualified rigx name."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "rigx.toml").write_text("[project]\nname = 'p'\n")
            out_dir = root / "output" / "frontend_web"
            (out_dir / "bin").mkdir(parents=True)
            runner = out_dir / "bin" / "run-web"
            runner.write_text("#!/bin/sh\nexit 0\n")
            runner.chmod(
                runner.stat().st_mode
                | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH
            )
            (out_dir / "manifest.json").write_text(json.dumps({
                "name": "frontend.web", "backend": "lite",
                "hostname": "web", "ports": [],
                "image": {"kind": "oci-tarball-host-store"},
            }))
            yield root

    def test_start_by_qualified_rigx_name(self):
        with self._module_capsule() as root:
            with capsule.start(
                "frontend.web", output_dir=root / "output"
            ) as cap:
                self.assertEqual(cap.name, "frontend.web")
                self.assertEqual(
                    cap.capsule_dir,
                    (root / "output" / "frontend_web").resolve(),
                )

    def test_start_by_sanitized_attr(self):
        with self._module_capsule() as root:
            with capsule.start(
                "frontend_web", output_dir=root / "output"
            ) as cap:
                self.assertEqual(cap.manifest["name"], "frontend.web")


class StartEnvEncoding(unittest.TestCase):
    """Env vars with commas can't ride the legacy comma-split RIGX_ENV
    var — they travel via the JSON runtime spec only (new runners give
    the spec precedence). Newlines are rejected outright."""

    _RUNNER = (
        '#!/bin/sh\n'
        'env | grep "^RIGX_ENV=" > "$DUMP" || true\n'
        'cat "$RIGX_RUNTIME_SPEC" > "$SPEC_DUMP"\n'
        'exit 0\n'
    )

    def test_comma_value_rides_spec_only(self):
        with fake_capsule("svc", runner_body=self._RUNNER) as root:
            dump = root / "env-dump"
            spec_dump = root / "spec-dump"
            with mock.patch.dict(
                os.environ, {"DUMP": str(dump), "SPEC_DUMP": str(spec_dump)}
            ):
                with capsule.start(
                    "svc",
                    env={"OPTS": "a,b", "FOO": "bar"},
                    output_dir=root / "output",
                ):
                    pass
            env_line = dump.read_text().strip()
            spec = json.loads(spec_dump.read_text())
        # Legacy var carries only the comma-free entry (an old runner
        # would split "OPTS=a,b" into `-e OPTS=a -e b`)...
        self.assertEqual(env_line, "RIGX_ENV=FOO=bar")
        # ...while the spec carries every entry, intact.
        self.assertEqual(spec["env"], {"OPTS": "a,b", "FOO": "bar"})

    def test_all_comma_values_leave_legacy_var_unset(self):
        with fake_capsule("svc", runner_body=self._RUNNER) as root:
            dump = root / "env-dump"
            spec_dump = root / "spec-dump"
            with mock.patch.dict(
                os.environ, {"DUMP": str(dump), "SPEC_DUMP": str(spec_dump)}
            ):
                with capsule.start(
                    "svc",
                    env={"OPTS": "a,b"},
                    output_dir=root / "output",
                ):
                    pass
            self.assertEqual(dump.read_text().strip(), "")
            spec = json.loads(spec_dump.read_text())
        self.assertEqual(spec["env"], {"OPTS": "a,b"})

    def test_newline_in_env_value_rejected(self):
        with fake_capsule("svc") as root:
            with self.assertRaisesRegex(ValueError, "newlines"):
                with capsule.start(
                    "svc",
                    env={"K": "a\nb"},
                    output_dir=root / "output",
                ):
                    pass


class PreYieldCleanup(unittest.TestCase):
    """`start()` writes a runtime-spec tempfile before launching the
    runner. If the launch fails (nonzero exit, timeout) no Capsule is
    yielded and the context-exit cleanup can't run — start() itself
    must remove the tempfile."""

    def test_spec_tempfile_removed_after_successful_exit(self):
        runner_body = (
            '#!/bin/sh\n'
            'echo "$RIGX_RUNTIME_SPEC" > "$DUMP"\n'
            'exit 0\n'
        )
        with fake_capsule("svc", runner_body=runner_body) as root:
            dump = root / "spec-dump"
            with mock.patch.dict(os.environ, {"DUMP": str(dump)}):
                with capsule.start("svc", output_dir=root / "output"):
                    spec_path = Path(dump.read_text().strip())
                    self.assertTrue(spec_path.is_file())
            self.assertFalse(spec_path.exists())

    def test_runner_failure_cleans_up_spec_tempfile(self):
        runner_body = (
            '#!/bin/sh\n'
            'echo "$RIGX_RUNTIME_SPEC" > "$DUMP"\n'
            'exit 7\n'
        )
        with fake_capsule("svc", runner_body=runner_body) as root:
            dump = root / "spec-dump"
            with mock.patch.dict(os.environ, {"DUMP": str(dump)}):
                with self.assertRaises(RuntimeError):
                    with capsule.start("svc", output_dir=root / "output"):
                        pass
            spec_path = Path(dump.read_text().strip())
            self.assertFalse(spec_path.exists())

    def test_timeout_raises_clean_error_and_cleans_up_spec(self):
        """A runner that outlives `timeout=` surfaces as RuntimeError
        (not a raw subprocess.TimeoutExpired) and still cleans up the
        spec tempfile."""
        runner_body = (
            '#!/bin/sh\n'
            'echo "$RIGX_RUNTIME_SPEC" > "$DUMP"\n'
            'sleep 5\n'
        )
        with fake_capsule("svc", runner_body=runner_body) as root:
            dump = root / "spec-dump"
            with mock.patch.dict(os.environ, {"DUMP": str(dump)}):
                with self.assertRaises(RuntimeError) as ctx:
                    with capsule.start(
                        "svc", output_dir=root / "output", timeout=0.3
                    ):
                        pass
            self.assertIn("did not finish", str(ctx.exception))
            spec_path = Path(dump.read_text().strip())
            self.assertFalse(spec_path.exists())


class PublishTupleForm(unittest.TestCase):
    """`publish={cont: (addr, host)}` — the form `rigx.testbed`
    produces when capsules have distinct loopback addresses. Renders
    as `addr:host:cont` for `docker -p` so the host port binds on a
    specific loopback alias instead of `0.0.0.0`."""

    def test_tuple_form_renders_with_address(self):
        runner_body = (
            '#!/bin/sh\n'
            'env | grep "^RIGX_PUBLISH=" > "$DUMP"\n'
            'exit 0\n'
        )
        with fake_capsule("svc", runner_body=runner_body) as root:
            dump = root / "publish-dump"
            with mock.patch.dict(os.environ, {"DUMP": str(dump)}):
                with capsule.start(
                    "svc",
                    publish={5000: ("127.0.10.2", 5040), 5001: 5041},
                    output_dir=root / "output",
                ):
                    pass
            line = dump.read_text().strip()
        # int form stays as `host:cont`; tuple form gets the address
        # prefix. Both can mix in one publish dict.
        parts = sorted(line.split("=", 1)[1].split(","))
        self.assertEqual(parts, sorted(["127.0.10.2:5040:5000", "5041:5001"]))

    def test_udp_publish_rides_its_own_env_var(self):
        """`publish_udp` entries land in RIGX_PUBLISH_UDP (the runner
        appends the `/udp` suffix itself), NOT folded into RIGX_PUBLISH
        — folding them there applied each UDP mapping twice, once from
        each var. TCP and UDP can mix in one start() call."""
        runner_body = (
            '#!/bin/sh\n'
            'env | grep "^RIGX_PUBLISH" | sort > "$DUMP"\n'
            'exit 0\n'
        )
        with fake_capsule("svc", runner_body=runner_body) as root:
            dump = root / "publish-dump"
            with mock.patch.dict(os.environ, {"DUMP": str(dump)}):
                with capsule.start(
                    "svc",
                    publish={5000: 5040},
                    publish_udp={6000: ("127.0.10.2", 6040)},
                    output_dir=root / "output",
                ):
                    pass
            lines = dump.read_text().splitlines()
        self.assertEqual(
            lines,
            [
                "RIGX_PUBLISH=5040:5000",
                "RIGX_PUBLISH_UDP=127.0.10.2:6040:6000",
            ],
        )

    def test_udp_only_publish_leaves_tcp_var_unset(self):
        runner_body = (
            '#!/bin/sh\n'
            'env | grep "^RIGX_PUBLISH" | sort > "$DUMP" || true\n'
            'exit 0\n'
        )
        with fake_capsule("svc", runner_body=runner_body) as root:
            dump = root / "publish-dump"
            with mock.patch.dict(os.environ, {"DUMP": str(dump)}):
                with capsule.start(
                    "svc",
                    publish_udp={6000: 6040},
                    output_dir=root / "output",
                ):
                    pass
            lines = dump.read_text().splitlines()
        self.assertEqual(lines, ["RIGX_PUBLISH_UDP=6040:6000"])

    def test_host_endpoint_proto_kwarg_picks_right_map(self):
        """`host_endpoint(port, proto="udp")` reads from `publish_udp`
        — without the kwarg it'd fall through to the TCP map and
        miss the UDP entry."""
        cap = capsule.Capsule(
            name="svc",
            container_name="x",
            capsule_dir=Path("/nowhere"),
            manifest={},
            publish={5000: 5040},
            publish_udp={6000: ("127.0.10.2", 6040)},
        )
        self.assertEqual(cap.host_endpoint(5000), ("0.0.0.0", 5040))
        self.assertEqual(
            cap.host_endpoint(6000, proto="udp"), ("127.0.10.2", 6040)
        )
        with self.assertRaises(KeyError):
            cap.host_endpoint(6000)            # TCP map doesn't have 6000
        with self.assertRaises(KeyError):
            cap.host_endpoint(5000, proto="udp")  # UDP map doesn't have 5000

    def test_int_publish_form_reports_wildcard_bind(self):
        """The bare-int publish form renders as `host:cont`, which
        docker binds on 0.0.0.0 — host_endpoint reports that truthfully
        while host_address maps the wildcard to 127.0.0.1 (the address
        a host-side client can actually connect to). The explicit
        tuple form passes through untouched."""
        cap = capsule.Capsule(
            name="svc", container_name="x",
            capsule_dir=Path("/nowhere"),
            manifest={},
            publish={5000: 5040, 5001: ("127.0.10.2", 5041)},
        )
        self.assertEqual(cap.host_endpoint(5000), ("0.0.0.0", 5040))
        self.assertEqual(cap.host_address(5000), "127.0.0.1")
        self.assertEqual(cap.host_port(5000), 5040)
        self.assertEqual(cap.host_address(5001), "127.0.10.2")


class CapsuleHandle(unittest.TestCase):
    def test_host_port_lookup(self):
        """The capsule remembers the publish mapping; host_port() reads
        from it. Errors clearly if the port wasn't published."""
        cap = capsule.Capsule(
            name="svc", container_name="rigx-svc-x",
            capsule_dir=Path("/nowhere"),
            manifest={},
            publish={5000: 5040},
        )
        self.assertEqual(cap.host_port(5000), 5040)
        with self.assertRaises(KeyError) as ctx:
            cap.host_port(9999)
        self.assertIn("9999", str(ctx.exception))
        self.assertIn("not published", str(ctx.exception))

    def test_wait_for_port_returns_when_listener_up(self):
        """`wait_for_port` polls the host-mapped port until something
        accepts. We open a tiny local listener to stand in for a
        running container, then confirm the call returns promptly."""
        with socket.socket() as srv:
            srv.bind(("127.0.0.1", 0))
            srv.listen(1)
            host_port = srv.getsockname()[1]
            cap = capsule.Capsule(
                name="svc", container_name="x",
                capsule_dir=Path("/nowhere"),
                manifest={},
                publish={5000: host_port},
            )
            cap.wait_for_port(5000, timeout=2.0)

    def test_wait_for_port_times_out(self):
        """No listener bound — wait_for_port hits the deadline and
        raises. Use a dedicated free port that we don't bind."""
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            free_port = s.getsockname()[1]
        cap = capsule.Capsule(
            name="svc", container_name="x",
            capsule_dir=Path("/nowhere"),
            manifest={},
            publish={5000: free_port},
        )
        with self.assertRaises(TimeoutError):
            cap.wait_for_port(5000, timeout=0.3, interval=0.05)

    def test_qemu_stop_terminates_subprocess(self):
        """For qemu capsules, stop() terminates the runner subprocess
        (the runner exec's qemu in foreground, so signaling the runner
        signals qemu)."""
        from unittest.mock import MagicMock
        proc = MagicMock(spec=subprocess.Popen)
        proc.poll.return_value = None  # still running at first
        cap = capsule.Capsule(
            name="svc", container_name="x",
            capsule_dir=Path("/nowhere"),
            manifest={"backend": "qemu"},
            _proc=proc,
        )
        cap.stop()
        proc.terminate.assert_called_once()
        proc.wait.assert_called()
        self.assertTrue(cap._stopped)

    def test_qemu_stop_escalates_to_kill_on_timeout(self):
        """If SIGTERM doesn't reap qemu within 5s, escalate to SIGKILL."""
        from unittest.mock import MagicMock
        proc = MagicMock(spec=subprocess.Popen)
        proc.poll.return_value = None
        # First wait raises TimeoutExpired (qemu ignored SIGTERM); the
        # second wait succeeds after kill.
        proc.wait.side_effect = [
            subprocess.TimeoutExpired(cmd="qemu", timeout=5),
            None,
        ]
        cap = capsule.Capsule(
            name="svc", container_name="x",
            capsule_dir=Path("/nowhere"),
            manifest={"backend": "qemu"},
            _proc=proc,
        )
        cap.stop()
        proc.terminate.assert_called_once()
        proc.kill.assert_called_once()

    def test_qemu_logs_reads_temp_file(self):
        with tempfile.NamedTemporaryFile(
            mode="w", delete=False, suffix=".log",
        ) as f:
            f.write("kernel boot output\nrigx-entrypoint: hello\n")
            log_path = Path(f.name)
        try:
            cap = capsule.Capsule(
                name="svc", container_name="x",
                capsule_dir=Path("/nowhere"),
                manifest={"backend": "qemu"},
                _log_path=log_path,
            )
            self.assertIn("rigx-entrypoint: hello", cap.logs())
        finally:
            log_path.unlink()

    def test_qemu_exec_raises_not_implemented(self):
        """exec() routes through docker — not available for VMs.
        Pointer at the SSH workaround should be in the message."""
        cap = capsule.Capsule(
            name="svc", container_name="x",
            capsule_dir=Path("/nowhere"),
            manifest={"backend": "qemu"},
        )
        with self.assertRaises(NotImplementedError) as ctx:
            cap.exec(["echo", "hi"])
        self.assertIn("backend=qemu", str(ctx.exception))
        self.assertIn("SSH", str(ctx.exception))

    def test_stop_tolerates_missing_container_engine(self):
        """`stop()` is best-effort: if neither docker nor podman is on
        PATH (sandboxed unit tests, or a runtime where the engine got
        uninstalled), it should silently no-op rather than raise. The
        runner contract handles real container lifecycle; stop() just
        sends the signal if it can."""
        cap = capsule.Capsule(
            name="svc", container_name="x",
            capsule_dir=Path("/nowhere"),
            manifest={},
        )
        with mock.patch("rigx.capsule.shutil.which", return_value=None):
            cap.stop()  # must not raise
        self.assertTrue(cap._stopped)


if __name__ == "__main__":
    unittest.main()
