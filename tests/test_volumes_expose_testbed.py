"""Tests for the v0.4 additions:

- `volumes` on `kind = "capsule"` (TOML schema, runner emission)
- `volumes=` on `rigx.capsule.start` (RIGX_VOLUMES env var)
- `Network.shared_volume` + `declare(volumes=…)` + `bindings()` glue
- `Network.declare(expose=…)` for external port publication
- `kind = "testbed"` discovery + dispatch
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from textwrap import dedent
from unittest import mock

from rigx import capsule, config, nix_gen, scaffold, testbed
from rigx.config import ConfigError


# ---------------------------------------------------------------------------
# Shared helpers (mirrors test_capsule_config / test_capsule_helper).
# ---------------------------------------------------------------------------


class TempProject:
    def __init__(self, toml_body: str, files: dict[str, str] | None = None):
        self.toml_body = dedent(toml_body).lstrip()
        self.files = files or {}

    def __enter__(self) -> Path:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        (root / "rigx.toml").write_text(self.toml_body)
        for rel, body in self.files.items():
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(body)
        return root

    def __exit__(self, *exc) -> None:
        self._tmp.cleanup()


@contextmanager
def fake_capsule(
    name: str = "svc",
    runner_body: str = "#!/bin/sh\nexit 0\n",
    manifest: dict | None = None,
):
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


# ---------------------------------------------------------------------------
# 1. TOML `volumes` parsing.
# ---------------------------------------------------------------------------


class VolumesParse(unittest.TestCase):
    def test_loads_volumes_with_default_mode(self):
        body = """
            [project]
            name = "p"

            [targets.svc]
            kind       = "capsule"
            backend    = "lite"
            entrypoint = "/bin/true"
            volumes    = [
                { host = "data", container = "/shared" },
                { host = "/var/log/app", container = "/var/log/app", mode = "ro" },
            ]
        """
        with TempProject(body) as root:
            proj = config.load(root)
        vols = proj.targets["svc"].volumes
        self.assertEqual(len(vols), 2)
        self.assertEqual((vols[0].host, vols[0].container, vols[0].mode),
                         ("data", "/shared", "rw"))
        self.assertEqual((vols[1].host, vols[1].container, vols[1].mode),
                         ("/var/log/app", "/var/log/app", "ro"))

    def test_relative_host_path_in_module_gets_prefix(self):
        """Module-declared relative host paths are rewritten with the
        module's path_prefix so they stay valid relative to the parent
        root — same trick `nixos_modules` and `sources` use."""
        body = """
            [project]
            name = "p"

            [modules]
            include = ["mod"]
        """
        files = {
            "mod/rigx.toml": dedent("""
                [targets.svc]
                kind       = "capsule"
                backend    = "lite"
                entrypoint = "/bin/true"
                volumes    = [
                    { host = "data", container = "/shared" },
                    { host = "/abs/path", container = "/abs" },
                ]
            """).lstrip(),
        }
        with TempProject(body, files) as root:
            proj = config.load(root)
        vols = proj.targets["mod.svc"].volumes
        self.assertEqual(vols[0].host, "mod/data")  # rewritten
        self.assertEqual(vols[1].host, "/abs/path")  # absolute pass-through

    def test_volumes_rejected_on_qemu_backend(self):
        body = """
            [project]
            name = "p"

            [targets.svc]
            kind       = "capsule"
            backend    = "qemu"
            entrypoint = "/bin/true"
            volumes    = [{ host = "data", container = "/shared" }]
        """
        with TempProject(body) as root:
            with self.assertRaisesRegex(ConfigError, "volumes is only valid"):
                config.load(root)

    def test_volume_mode_must_be_rw_or_ro(self):
        body = """
            [project]
            name = "p"

            [targets.svc]
            kind       = "capsule"
            backend    = "lite"
            entrypoint = "/bin/true"
            volumes    = [{ host = "x", container = "/shared", mode = "rwx" }]
        """
        with TempProject(body) as root:
            with self.assertRaisesRegex(ConfigError, "'rw' or 'ro'"):
                config.load(root)

    def test_container_path_must_be_absolute(self):
        body = """
            [project]
            name = "p"

            [targets.svc]
            kind       = "capsule"
            backend    = "lite"
            entrypoint = "/bin/true"
            volumes    = [{ host = "x", container = "shared" }]
        """
        with TempProject(body) as root:
            with self.assertRaisesRegex(ConfigError, "must be an absolute"):
                config.load(root)

    def test_overmount_of_nix_store_rejected(self):
        body = """
            [project]
            name = "p"

            [targets.svc]
            kind       = "capsule"
            backend    = "lite"
            entrypoint = "/bin/true"
            volumes    = [{ host = "x", container = "/nix/store" }]
        """
        with TempProject(body) as root:
            with self.assertRaisesRegex(ConfigError, "overmounts"):
                config.load(root)

    def test_duplicate_container_path_rejected(self):
        body = """
            [project]
            name = "p"

            [targets.svc]
            kind       = "capsule"
            backend    = "lite"
            entrypoint = "/bin/true"
            volumes    = [
                { host = "a", container = "/shared" },
                { host = "b", container = "/shared" },
            ]
        """
        with TempProject(body) as root:
            with self.assertRaisesRegex(ConfigError, "declared twice"):
                config.load(root)

    def test_colon_in_host_path_rejected(self):
        body = """
            [project]
            name = "p"

            [targets.svc]
            kind       = "capsule"
            backend    = "lite"
            entrypoint = "/bin/true"
            volumes    = [{ host = "weird:path", container = "/shared" }]
        """
        with TempProject(body) as root:
            with self.assertRaisesRegex(ConfigError, "may not contain"):
                config.load(root)


# ---------------------------------------------------------------------------
# 2. Runner emits TOML volumes + parses RIGX_VOLUMES.
# ---------------------------------------------------------------------------


class VolumesRunnerEmit(unittest.TestCase):
    """Smoke checks on the generated runner script body — the runtime
    behavior is exercised via shell execution in `VolumesRunnerExec`."""

    _BODY = """
        [project]
        name = "p"

        [targets.svc]
        kind       = "capsule"
        backend    = "lite"
        entrypoint = "/bin/true"
        volumes    = [
            { host = "data", container = "/shared" },
            { host = "/abs/log", container = "/var/log", mode = "ro" },
        ]
    """

    def test_lite_runner_bakes_toml_volumes_array(self):
        with TempProject(self._BODY) as root:
            proj = config.load(root)
            flake = nix_gen.generate(proj)
        # The bash array literal carries the host:container:mode triple
        # for each declared volume.
        self.assertIn("TOML_VOLUMES=(", flake)
        self.assertIn("data:/shared:rw", flake)
        self.assertIn("/abs/log:/var/log:ro", flake)

    def test_lite_runner_parses_runtime_rigx_volumes(self):
        with TempProject(self._BODY) as root:
            proj = config.load(root)
            flake = nix_gen.generate(proj)
        # Runtime additions land on RUN_FLAGS via the same resolver.
        self.assertIn("_RUNTIME_VOLS", flake)
        self.assertIn("$RIGX_VOLUMES", flake)
        # The resolver helper detects relative host paths and prepends
        # RIGX_PROJECT_ROOT.
        self.assertIn("RIGX_PROJECT_ROOT", flake)
        self.assertIn("_rigx_resolve_volume", flake)

    def test_qemu_runner_rejects_rigx_volumes(self):
        body = """
            [project]
            name = "p"

            [targets.svc]
            kind       = "capsule"
            backend    = "qemu"
            entrypoint = "/bin/true"
        """
        with TempProject(body) as root:
            proj = config.load(root)
            flake = nix_gen.generate(proj)
        self.assertIn(
            "RIGX_VOLUMES is not supported on backend=qemu", flake,
        )


class VolumesRunnerExec(unittest.TestCase):
    """End-to-end: render the runner script via Python, execute it with
    bash, and inspect what would have been passed to docker."""

    def _render_runner(self, target_volumes: list[config.Volume]) -> str:
        """Build a bash script that exercises the volume helpers in
        isolation. The full lite runner pulls in `docker load` and
        Nix-baked tokens; we emit just the volume-handling block by
        calling the same helpers used to generate it."""
        from rigx.config import Target
        target = Target(
            name="svc", kind="capsule", backend="lite",
            entrypoint="/bin/true", volumes=target_volumes,
        )
        helpers = nix_gen._emit_volume_helpers("run-svc")
        toml_arr = nix_gen._emit_toml_volumes_array(target)
        # Wrap in a tiny script that prints out the resolved -v flags
        # we'd have appended to RUN_FLAGS.
        return (
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "RUN_FLAGS=()\n"
            f"{helpers}\n"
            f"{toml_arr}\n"
            'for _v in "${TOML_VOLUMES[@]}"; do\n'
            '    RUN_FLAGS+=(-v "$(_rigx_resolve_volume "$_v")")\n'
            "done\n"
            'if [ -n "${RIGX_VOLUMES:-}" ]; then\n'
            '    IFS="," read -ra _RUNTIME_VOLS <<< "$RIGX_VOLUMES"\n'
            '    for _v in "${_RUNTIME_VOLS[@]}"; do\n'
            '        RUN_FLAGS+=(-v "$(_rigx_resolve_volume "$_v")")\n'
            "    done\n"
            "fi\n"
            'printf "%s\\n" "${RUN_FLAGS[@]}"\n'
        )

    def test_relative_host_resolves_against_project_root(self):
        import subprocess
        with tempfile.TemporaryDirectory() as proj:
            (Path(proj) / "rigx.toml").touch()
            script = self._render_runner([
                config.Volume(host="data", container="/shared", mode="rw"),
            ])
            r = subprocess.run(
                ["bash", "-c", script],
                env={**os.environ, "RIGX_PROJECT_ROOT": proj},
                capture_output=True, text=True, check=True,
            )
        # `-v <projroot>/data:/shared:rw`
        lines = [l for l in r.stdout.splitlines() if l]
        self.assertEqual(lines[0], "-v")
        self.assertEqual(lines[1], f"{proj}/data:/shared:rw")

    def test_absolute_host_passes_through(self):
        import subprocess
        script = self._render_runner([
            config.Volume(host="/abs/data", container="/shared", mode="ro"),
        ])
        r = subprocess.run(
            ["bash", "-c", script],
            env={**os.environ, "RIGX_PROJECT_ROOT": "/unused"},
            capture_output=True, text=True, check=True,
        )
        lines = [l for l in r.stdout.splitlines() if l]
        self.assertEqual(lines, ["-v", "/abs/data:/shared:ro"])

    def test_runtime_rigx_volumes_resolved(self):
        import subprocess
        with tempfile.TemporaryDirectory() as proj:
            (Path(proj) / "rigx.toml").touch()
            script = self._render_runner([])
            r = subprocess.run(
                ["bash", "-c", script],
                env={
                    **os.environ,
                    "RIGX_PROJECT_ROOT": proj,
                    "RIGX_VOLUMES": f"runtime-data:/r,/abs:/a:ro",
                },
                capture_output=True, text=True, check=True,
            )
        lines = [l for l in r.stdout.splitlines() if l]
        # 2 mounts × 2 lines each (-v <spec>)
        self.assertEqual(lines[0:2], ["-v", f"{proj}/runtime-data:/r:rw"])
        self.assertEqual(lines[2:4], ["-v", "/abs:/a:ro"])


# ---------------------------------------------------------------------------
# 3. `start(volumes=…)` serializes RIGX_VOLUMES.
# ---------------------------------------------------------------------------


class StartVolumesEnv(unittest.TestCase):
    def test_volumes_kwarg_serializes_to_rigx_volumes(self):
        runner_body = (
            '#!/bin/sh\n'
            'env | grep "^RIGX_VOLUMES=" > "$DUMP" || true\n'
            'env | grep "^RIGX_PROJECT_ROOT=" >> "$DUMP" || true\n'
            'exit 0\n'
        )
        with fake_capsule("svc", runner_body=runner_body) as root:
            dump = root / "vol-dump"
            with mock.patch.dict(os.environ, {"DUMP": str(dump)}):
                with capsule.start(
                    "svc",
                    volumes={
                        Path("/host/data"): "/shared",
                        "/host/log": ("/var/log", "ro"),
                    },
                    output_dir=root / "output",
                ):
                    pass
            text = dump.read_text()
        vol_line = next(
            l for l in text.splitlines() if l.startswith("RIGX_VOLUMES=")
        )
        parts = sorted(vol_line.split("=", 1)[1].split(","))
        self.assertEqual(
            parts,
            sorted(["/host/data:/shared:rw", "/host/log:/var/log:ro"]),
        )
        # `start()` defaults RIGX_PROJECT_ROOT to the project root if
        # the orchestrator didn't pin one.
        proot = next(
            l for l in text.splitlines() if l.startswith("RIGX_PROJECT_ROOT=")
        )
        self.assertEqual(proot.split("=", 1)[1], str(root.resolve()))

    def test_volumes_rejects_qemu_backend(self):
        with fake_capsule(
            "svc",
            manifest={
                "name": "svc", "backend": "qemu", "hostname": "svc",
                "ports": [], "image": {"kind": "qemu-nixos-vm"},
            },
        ) as root:
            with self.assertRaisesRegex(RuntimeError, "not supported on backend=qemu"):
                with capsule.start(
                    "svc",
                    volumes={"/host": "/cont"},
                    output_dir=root / "output",
                ):
                    pass

    def test_volumes_rejects_colon_in_paths(self):
        with fake_capsule("svc") as root:
            with self.assertRaisesRegex(ValueError, "may not contain"):
                with capsule.start(
                    "svc",
                    volumes={"/host:weird": "/cont"},
                    output_dir=root / "output",
                ):
                    pass


# ---------------------------------------------------------------------------
# 4. `Network.shared_volume` + `declare(volumes=…)` + `bindings()`.
# ---------------------------------------------------------------------------


class TestbedSharedVolume(unittest.TestCase):
    def test_shared_volume_allocates_tempdir_on_enter(self):
        net = testbed.Network()
        vol = net.shared_volume("data")
        self.assertIsNone(vol.host_path)
        with net:
            self.assertIsNotNone(vol.host_path)
            self.assertTrue(vol.host_path.is_dir())
            saved = vol.host_path
        # Cleared on exit; tempdir removed.
        self.assertIsNone(vol.host_path)
        self.assertFalse(saved.exists())

    def test_bindings_volumes_dict_points_at_host_path(self):
        net = testbed.Network()
        data = net.shared_volume("data")
        net.declare(
            "telemetry", listens_on=[8001],
            volumes={data: "/shared"},
        )
        with net:
            b = net.bindings("telemetry")
            # Default mode "rw" emits a bare container path; "ro" promotes
            # to a `(cont, mode)` tuple — matches `start()`'s API.
            self.assertEqual(len(b["volumes"]), 1)
            host_path, spec = next(iter(b["volumes"].items()))
            self.assertEqual(spec, "/shared")
            # The host_path exists while the testbed is up.
            self.assertTrue(host_path.is_dir())

    def test_bindings_volumes_ro_mode_renders_as_tuple(self):
        net = testbed.Network()
        data = net.shared_volume("data")
        net.declare(
            "vis", listens_on=[8000],
            volumes={data: ("/shared", "ro")},
        )
        with net:
            b = net.bindings("vis")
        spec = next(iter(b["volumes"].values()))
        self.assertEqual(spec, ("/shared", "ro"))

    def test_two_capsules_see_same_host_path(self):
        net = testbed.Network()
        data = net.shared_volume("data")
        net.declare("a", listens_on=[1001], volumes={data: "/shared"})
        net.declare("b", listens_on=[1002], volumes={data: "/shared"})
        with net:
            ba = net.bindings("a")
            bb = net.bindings("b")
        self.assertEqual(list(ba["volumes"]), list(bb["volumes"]))

    def test_shared_volume_handle_must_belong_to_this_network(self):
        net1 = testbed.Network()
        net2 = testbed.Network()
        v = net1.shared_volume("data")
        with self.assertRaisesRegex(ValueError, "not produced by this testbed"):
            net2.declare("x", listens_on=[1000], volumes={v: "/shared"})

    def test_shared_volume_after_enter_rejected(self):
        net = testbed.Network()
        with net:
            with self.assertRaisesRegex(RuntimeError, "before entering"):
                net.shared_volume("late")


# ---------------------------------------------------------------------------
# 5. `Network.declare(expose=…)`.
# ---------------------------------------------------------------------------


class TestbedExpose(unittest.TestCase):
    def test_expose_adds_external_publish_alongside_loopback(self):
        net = testbed.Network()
        net.declare(
            "vis", listens_on=[8000],
            expose=[("0.0.0.0", 8000)],
        )
        with net:
            b = net.bindings("vis")
        # publish[8000] is a list now: [(loopback alias, host port),
        # (0.0.0.0, 8000)] — `start()` will emit one `-p` flag per entry.
        mapping = b["publish"][8000]
        self.assertIsInstance(mapping, list)
        self.assertEqual(len(mapping), 2)
        self.assertEqual(mapping[1], ("0.0.0.0", 8000))

    def test_expose_unlisted_port_rejected(self):
        net = testbed.Network()
        with self.assertRaisesRegex(ValueError, "not in listens_on"):
            net.declare(
                "vis", listens_on=[8000],
                expose=[("0.0.0.0", 9999)],
            )

    def test_udp_expose_separate_from_tcp(self):
        net = testbed.Network()
        net.declare(
            "vis",
            listens_on=[8000], udp_listens_on=[8000],
            udp_expose=[("0.0.0.0", 8000)],
        )
        with net:
            b = net.bindings("vis")
        # TCP map keeps the bare loopback tuple; UDP gets the list form.
        self.assertIsInstance(b["publish"][8000], tuple)
        self.assertIsInstance(b["publish_udp"][8000], list)


class StartListPublish(unittest.TestCase):
    """A list-valued publish entry expands to one `-p` flag per host
    endpoint — the wire format the testbed-expose pathway depends on."""

    def test_list_publish_expands_per_entry(self):
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
                    publish={
                        8000: [("127.0.10.2", 8050), ("0.0.0.0", 8000)],
                    },
                    output_dir=root / "output",
                ):
                    pass
            line = dump.read_text().strip()
        parts = sorted(line.split("=", 1)[1].split(","))
        self.assertEqual(
            parts, sorted(["127.0.10.2:8050:8000", "0.0.0.0:8000:8000"]),
        )

    def test_host_endpoint_returns_first_mapping(self):
        cap = capsule.Capsule(
            name="vis", container_name="x",
            capsule_dir=Path("/nowhere"),
            manifest={},
            publish={8000: [("127.0.10.2", 8050), ("0.0.0.0", 8000)]},
        )
        # First entry is the canonical "intra-testbed" endpoint —
        # that's what host_endpoint returns.
        self.assertEqual(cap.host_endpoint(8000), ("127.0.10.2", 8050))


# ---------------------------------------------------------------------------
# 6. `kind = "testbed"`.
# ---------------------------------------------------------------------------


class TestbedKind(unittest.TestCase):
    def test_loads_testbed_kind(self):
        body = """
            [project]
            name = "p"

            [targets.start_demo]
            kind   = "testbed"
            script = "echo go"
        """
        with TempProject(body) as root:
            proj = config.load(root)
        t = proj.targets["start_demo"]
        self.assertEqual(t.kind, "testbed")
        self.assertEqual(t.script, "echo go")

    def test_testbed_requires_script(self):
        body = """
            [project]
            name = "p"

            [targets.demo]
            kind = "testbed"
        """
        with TempProject(body) as root:
            with self.assertRaisesRegex(ConfigError, "kind='testbed' requires"):
                config.load(root)

    def test_testbed_not_buildable(self):
        from rigx import builder
        body = """
            [project]
            name = "p"

            [targets.demo]
            kind   = "testbed"
            script = "echo go"
        """
        with TempProject(body) as root:
            proj = config.load(root)
        with self.assertRaisesRegex(builder.BuildError, "use `rigx run"):
            builder._expand_build_spec(proj, "demo")

    def test_testbed_skipped_in_all_attrs(self):
        from rigx import builder
        body = """
            [project]
            name = "p"

            [targets.bin]
            kind     = "executable"
            sources  = ["m.cpp"]

            [targets.demo]
            kind   = "testbed"
            script = "echo go"
        """
        with TempProject(body, {"m.cpp": "int main(){return 0;}\n"}) as root:
            proj = config.load(root)
        attrs = builder._all_attrs(proj)
        self.assertIn("bin", attrs)
        self.assertNotIn("demo", attrs)

    def test_testbed_skipped_in_flake_generation(self):
        body = """
            [project]
            name = "p"

            [targets.demo]
            kind   = "testbed"
            script = "echo go"
        """
        with TempProject(body) as root:
            proj = config.load(root)
            flake = nix_gen.generate(proj)
        # No `demo = …;` Nix attr — testbed is host-side only.
        self.assertNotIn("demo = ", flake)

    def test_run_named_script_accepts_testbed(self):
        from rigx import builder
        body = """
            [project]
            name = "p"

            [targets.demo]
            kind         = "testbed"
            deps.nixpkgs = []
            script       = "exit 0"
        """
        with TempProject(body) as root:
            proj = config.load(root)
            # Mock the actual nix shell call — we just want to verify
            # the kind dispatch.
            with mock.patch("rigx.builder.run_script_target", return_value=0):
                builder.run_named_script(proj, "demo")

    def test_scaffold_emits_testbed_block(self):
        s = scaffold.scaffold("testbed", "demo", language="cxx", run_target=None)
        self.assertIn("[targets.demo]", s.toml_block)
        self.assertIn('kind         = "testbed"', s.toml_block)
        self.assertIn("press Enter", s.toml_block)


if __name__ == "__main__":
    unittest.main()
