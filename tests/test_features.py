"""Tests for: rigx fmt, rigx new, rigx test, rigx build --json,
[vars].extends. Lighter-weight than full integration; exercises the pieces
that don't require a Nix runtime."""

import json
import tempfile
import unittest
from pathlib import Path
from textwrap import dedent
from unittest import mock

from rigx import builder, cli, fmt, scaffold
from rigx.config import ConfigError, Project, Target


class Fmt(unittest.TestCase):
    def test_idempotent(self):
        # Two passes produce the same bytes.
        src = dedent("""
            [project]
            name = "p"

            [targets.b]
            kind = "executable"
            sources = ["b.cpp"]

            [targets.a]
            kind     = "executable"
            sources  = ["a.cpp"]
            cxxflags = ["-O2"]
        """).lstrip()
        once = fmt.format_toml(src)
        twice = fmt.format_toml(once)
        self.assertEqual(once, twice)

    def test_canonical_section_order(self):
        # Sections come out in TOP_LEVEL_ORDER regardless of source order.
        src = dedent("""
            [targets.a]
            kind = "executable"
            sources = ["a.cpp"]

            [project]
            name = "p"
        """).lstrip()
        out = fmt.format_toml(src)
        # [project] precedes [targets.a].
        self.assertLess(out.index("[project]"), out.index("[targets.a]"))


class Scaffold(unittest.TestCase):
    def test_executable_cxx_stub(self):
        s = scaffold.scaffold("executable", "myapp", "cxx", None)
        self.assertIn("[targets.myapp]", s.toml_block)
        self.assertIn("kind     = \"executable\"", s.toml_block)
        self.assertIn("src/myapp.cpp", s.files)

    def test_executable_go_stub(self):
        s = scaffold.scaffold("executable", "tool", "go", None)
        self.assertIn("src/tool.go", s.files)
        self.assertIn("package main", s.files["src/tool.go"])

    def test_static_library_cxx_creates_header(self):
        s = scaffold.scaffold("static_library", "lib", "cxx", None)
        self.assertIn("src/lib.cpp", s.files)
        self.assertIn("include/lib.h", s.files)

    def test_static_library_rejects_zig(self):
        with self.assertRaises(ValueError):
            scaffold.scaffold("static_library", "lib", "zig", None)

    def test_run_uses_provided_run_target(self):
        s = scaffold.scaffold("run", "gen", None, "my_tool")
        self.assertIn('run     = "my_tool"', s.toml_block)


class VarsExtends(unittest.TestCase):
    def _write(self, root: Path, files: dict[str, str]) -> None:
        for rel, body in files.items():
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(dedent(body).lstrip())

    def test_extends_pulls_vars_from_other_file(self):
        from rigx import config
        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp))
        root = Path(tmp)
        self._write(root, {
            "shared.toml": """
                [vars]
                cxx_libs = ["fmt", "spdlog"]
            """,
            "rigx.toml": """
                [project]
                name = "p"

                [vars]
                extends = ["shared.toml"]
                local = ["x"]

                [targets.a]
                kind = "executable"
                sources = ["m.cpp"]
                deps.nixpkgs = ["$vars.cxx_libs"]
            """,
            "m.cpp": "",
        })
        proj = config.load(root)
        self.assertEqual(proj.targets["a"].deps.nixpkgs, ["fmt", "spdlog"])

    def test_extends_collision_errors(self):
        from rigx import config
        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp))
        root = Path(tmp)
        self._write(root, {
            "shared.toml": """
                [vars]
                shared = ["a"]
            """,
            "rigx.toml": """
                [project]
                name = "p"

                [vars]
                extends = ["shared.toml"]
                shared = ["b"]
            """,
        })
        with self.assertRaisesRegex(ConfigError, "collides with vars inherited"):
            config.load(root)

    def test_extends_cycle_detected(self):
        from rigx import config
        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp))
        root = Path(tmp)
        self._write(root, {
            "a.toml": """
                [vars]
                extends = ["b.toml"]
            """,
            "b.toml": """
                [vars]
                extends = ["a.toml"]
            """,
            "rigx.toml": """
                [project]
                name = "p"

                [vars]
                extends = ["a.toml"]
            """,
        })
        with self.assertRaisesRegex(ConfigError, "cycle"):
            config.load(root)


class TestKind(unittest.TestCase):
    def test_kind_test_requires_script(self):
        from rigx import config
        from rigx.config import ConfigError
        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp))
        root = Path(tmp)
        (root / "rigx.toml").write_text(dedent("""
            [project]
            name = "p"

            [targets.t]
            kind = "test"
        """).lstrip())
        with self.assertRaisesRegex(ConfigError, "kind='test' requires 'script'"):
            config.load(root)

    def test_run_tests_calls_script_runner_per_test(self):
        # `run_tests` should invoke `run_script_target` once per test target.
        from rigx.config import Variant
        proj = Project(
            name="p", version="0.1.0", nixpkgs_ref="nixos-24.11",
            git_deps={}, root=Path("/tmp"),
            targets={
                "exe": Target(name="exe", kind="executable", sources=["m.cpp"]),
                "t1":  Target(name="t1", kind="test", script="exit 0"),
                "t2":  Target(name="t2", kind="test", script="exit 1"),
            },
        )
        with mock.patch("rigx.builder.run_script_target") as run:
            run.side_effect = [0, 1]
            results = builder.run_tests(proj)
        self.assertEqual(results, [("t1", 0), ("t2", 1)])
        self.assertEqual(run.call_count, 2)

    def test_run_tests_filter_narrows_selection(self):
        proj = Project(
            name="p", version="0.1.0", nixpkgs_ref="nixos-24.11",
            git_deps={}, root=Path("/tmp"),
            targets={
                "t1": Target(name="t1", kind="test", script="exit 0"),
                "t2": Target(name="t2", kind="test", script="exit 0"),
            },
        )
        with mock.patch("rigx.builder.run_script_target", return_value=0):
            results = builder.run_tests(proj, filters=["t2"])
        self.assertEqual(results, [("t2", 0)])

    def test_build_rejects_test_with_pointer_to_rigx_test(self):
        proj = Project(
            name="p", version="0.1.0", nixpkgs_ref="nixos-24.11",
            git_deps={}, root=Path("/tmp"),
            targets={
                "t1": Target(name="t1", kind="test", script="echo"),
            },
        )
        with self.assertRaisesRegex(builder.BuildError, "use `rigx test t1` instead"):
            builder.build(proj, ["t1"])


class BuildJson(unittest.TestCase):
    def test_json_flag_emits_array(self):
        proj = Project(
            name="p", version="0.1.0", nixpkgs_ref="nixos-24.11",
            git_deps={}, root=Path("/tmp"),
            targets={"a": Target(name="a", kind="executable", sources=["m.cpp"])},
        )
        with mock.patch("rigx.cli._load", return_value=proj), \
             mock.patch(
                 "rigx.builder.build",
                 return_value=[("a", Path("/tmp/output/a"))],
             ), \
             mock.patch("rigx.cli.sys.stdout.write") as out:
            args = mock.MagicMock(targets=[], json=True)
            cli.cmd_build(args)
        # Captured printed JSON via the print() call — easier to inspect via builtins.
        # (We patched stdout.write above just to silence; the real assertion comes
        # via parsing print's output. Re-run the helper inline.)
        import io, contextlib
        buf = io.StringIO()
        with mock.patch("rigx.cli._load", return_value=proj), \
             mock.patch(
                 "rigx.builder.build",
                 return_value=[("a", Path("/tmp/output/a"))],
             ), contextlib.redirect_stdout(buf):
            cli.cmd_build(mock.MagicMock(targets=[], json=True))
        parsed = json.loads(buf.getvalue())
        self.assertEqual(parsed, [{"attr": "a", "output": "/tmp/output/a"}])


if __name__ == "__main__":
    unittest.main()
