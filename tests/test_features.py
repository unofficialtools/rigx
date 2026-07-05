"""Tests for: rigx fmt, rigx new, rigx test, rigx build --json,
[vars].extends. Lighter-weight than full integration; exercises the pieces
that don't require a Nix runtime."""

import json
import tempfile
import tomllib
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

    def test_nasty_string_values_round_trip(self):
        # Multi-line strings used to be emitted verbatim inside """…"""
        # with no escaping: backslashes (sed \1), embedded triple
        # quotes, and trailing backslash/quote produced non-reparseable
        # or corrupted TOML.
        nasty = [
            'sed -e "s/\\(a\\)/\\1/"\nsecond line',
            'a """ b\nc',
            "line1\nline2\\",
            'line1\nline2"',
            '"""\n"""',
            "\nleading newline",
            "tab\there\nand more",
            'single line with \\1 and "quotes"',
        ]
        for value in nasty:
            emitted = fmt._emit_scalar(value)
            parsed = tomllib.loads(f"x = {emitted}")["x"]
            self.assertEqual(parsed, value)

    def test_multiline_script_round_trip(self):
        # End-to-end: a script target containing sed backreferences
        # survives fmt (the known `rigx fmt` corruption bug).
        src = (
            '[targets.t]\n'
            'kind = "script"\n'
            'script = "echo hi\\nsed -e \'s/\\\\(a\\\\)/\\\\1/\'"\n'
        )
        out = fmt.format_toml(src)
        self.assertEqual(tomllib.loads(out), tomllib.loads(src))
        self.assertEqual(fmt.format_toml(out), out)

    def test_top_level_scalars_preserved(self):
        # A top-level non-table value used to be rewritten as a bare
        # [version] header, silently deleting the value.
        src = dedent("""
            version = "1.0"
            count = 3

            [project]
            name = "p"
        """).lstrip()
        out = fmt.format_toml(src)
        self.assertEqual(tomllib.loads(out), tomllib.loads(src))
        # Scalars come before any table section.
        self.assertLess(out.index('version = "1.0"'), out.index("[project]"))
        self.assertNotIn("[version]", out)
        self.assertEqual(fmt.format_toml(out), out)

    def test_dependencies_scalars_preserved(self):
        # Non-table values directly under [dependencies] were silently
        # dropped, and a scalar under a sub-table emitted a bare header
        # with no body (value deleted, type changed).
        src = dedent("""
            [dependencies]
            flat = "x"

            [dependencies.git]
            note = "y"

            [dependencies.git.mylib]
            url = "https://example.com/repo.git"
            rev = "abc123"

            [dependencies.local]
        """).lstrip()
        out = fmt.format_toml(src)
        self.assertEqual(tomllib.loads(out), tomllib.loads(src))
        self.assertEqual(fmt.format_toml(out), out)

    def test_non_bare_keys_quoted(self):
        # Keys with dots were silently restructured into nested tables
        # (`"a.b"` under env became env.a.b) and keys with spaces
        # emitted invalid TOML.
        src = dedent("""
            [project]
            name = "p"
            "spaced key" = "v"

            [targets.app]
            kind = "script"
            script = "true"
            env = { "a.b" = "1", "has space" = "2", PLAIN = "3" }

            [targets."dot.ted"]
            kind = "script"
            script = "true"

            [targets."dot.ted".variants."v.1"]
            script = "false"

            [modules]
            "mod.one" = "path/one"
        """).lstrip()
        out = fmt.format_toml(src)
        self.assertEqual(tomllib.loads(out), tomllib.loads(src))
        self.assertEqual(fmt.format_toml(out), out)

    def test_round_trip_safety_net_raises(self):
        # An emitter bug that changes the document's meaning must raise
        # FmtError instead of silently emitting corrupted output.
        src = '[project]\nname = "p"\n'
        with mock.patch.object(fmt, "_emit_scalar", return_value='"wrong"'):
            with self.assertRaises(fmt.FmtError):
                fmt.format_toml(src)

    def test_write_refused_on_formatter_bug(self):
        # format_file(write=True) must not touch the file when the
        # round-trip check fails.
        src = '[project]\nname = "p"\n'
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "rigx.toml"
            path.write_text(src)
            with mock.patch.object(
                fmt, "_emit_scalar", return_value='"wrong"'
            ):
                with self.assertRaises(fmt.FmtError):
                    fmt.format_file(path, write=True)
            self.assertEqual(path.read_text(), src)

    def test_invalid_toml_raises_fmt_error(self):
        with self.assertRaises(fmt.FmtError):
            fmt.format_toml("[project\nname =")

    def test_empty_targets_table_preserved(self):
        src = '[targets]\n'
        out = fmt.format_toml(src)
        self.assertEqual(tomllib.loads(out), tomllib.loads(src))

    def test_kitchen_sink_round_trip(self):
        # Broad property check: a document combining every nasty case
        # (escapes, triple quotes, dotted/spaced keys, scalars at odd
        # levels, empty tables) parses back identical.
        src = dedent('''
            toplevel = "scalar"

            [project]
            name = "p"
            desc = "back\\\\slash \\"quoted\\" and \\u0001 control"

            [dependencies]
            flat = 1

            [dependencies.git.lib]
            url = "https://x/y.git"

            [modules]
            "a.b" = "m/one"

            [targets.app]
            kind = "script"
            script = """
            sed -e 's/\\\\(a\\\\)/\\\\1/'
            echo "quo\\"""ted"
            trailing backslash \\\\"""
            env = { "K.1" = "v,1", PATH2 = "/x" }

            [targets.app.variants.debug]
            script = "true"
        ''').lstrip()
        parsed = tomllib.loads(src)
        out = fmt.format_toml(src)
        self.assertEqual(tomllib.loads(out), parsed)
        self.assertEqual(fmt.format_toml(out), out)

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

    def test_hyphenated_name_yields_valid_c_identifiers(self):
        # Hyphens are legal in rigx target names but not in C identifiers;
        # the stub must sanitize them (paths and TOML keep the real name).
        s = scaffold.scaffold("static_library", "my-lib", "c", None)
        self.assertIn("void my_lib_hello", s.files["src/my-lib.c"])
        self.assertIn("void my_lib_hello();", s.files["include/my-lib.h"])
        self.assertNotIn("my-lib_hello", s.files["src/my-lib.c"])
        self.assertIn("[targets.my-lib]", s.toml_block)

    def test_hyphenated_name_yields_valid_rust_identifier(self):
        s = scaffold.scaffold("static_library", "my-lib", "rust", None)
        self.assertIn("fn my_lib_hello", s.files["src/my-lib.rs"])


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
                "t1":  Target(name="t1", kind="test", script="exit 0", sandbox=False),
                "t2":  Target(name="t2", kind="test", script="exit 1", sandbox=False),
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
                "t1": Target(name="t1", kind="test", script="exit 0", sandbox=False),
                "t2": Target(name="t2", kind="test", script="exit 0", sandbox=False),
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


class PythonVenvExtra(unittest.TestCase):
    """`python_venv_extra` lets python_script targets pull vendored wheels
    or other sibling files into the venv FOD source. Paths are typed
    relative to `python_project`; we glob+expand them and re-root the
    destinations under the FOD root."""

    def _write(self, root: Path, files: dict[str, str]) -> None:
        for rel, body in files.items():
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(body)

    def test_flat_layout_paths_unchanged(self):
        from rigx import config
        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp))
        root = Path(tmp)
        self._write(root, {
            "rigx.toml": dedent("""
                [project]
                name = "p"
                [targets.app]
                kind              = "python_script"
                sources           = ["app.py"]
                python_project    = "."
                python_venv_extra = ["vendor"]
            """).lstrip(),
            "app.py": "",
            "vendor/.keep": "",
        })
        proj = config.load(root)
        self.assertEqual(proj.targets["app"].python_venv_extra, ["vendor"])

    def test_glob_expands_in_python_project_dir(self):
        from rigx import config
        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp))
        root = Path(tmp)
        self._write(root, {
            "rigx.toml": dedent("""
                [project]
                name = "p"
                [targets.app]
                kind              = "python_script"
                sources           = ["app.py"]
                python_project    = "."
                python_venv_extra = ["wheels/*.whl"]
            """).lstrip(),
            "app.py": "",
            "wheels/a.whl": "",
            "wheels/b.whl": "",
        })
        proj = config.load(root)
        # Globs against python_project dir; results sorted; output paths
        # preserve the relative location.
        self.assertEqual(
            proj.targets["app"].python_venv_extra,
            ["wheels/a.whl", "wheels/b.whl"],
        )

    def test_nested_python_project_paths_get_prefixed(self):
        from rigx import config
        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp))
        root = Path(tmp)
        self._write(root, {
            "rigx.toml": dedent("""
                [project]
                name = "p"
                [targets.app]
                kind              = "python_script"
                sources           = ["py/app.py"]
                python_project    = "py"
                python_venv_extra = ["vendor"]
            """).lstrip(),
            "py/app.py": "",
            "py/vendor/.keep": "",
        })
        proj = config.load(root)
        # Stored project-root-relative.
        self.assertEqual(proj.targets["app"].python_venv_extra, ["py/vendor"])

    def test_nix_gen_emits_copy_lines_with_correct_dests(self):
        # Unit-level: the venv-extra-pairs helper re-roots paths under
        # python_project so the FOD source stays pyproject-relative.
        from rigx import nix_gen
        from rigx.config import Target
        t = Target(
            name="app", kind="python_script",
            sources=["py/app.py"],
            python_project="py",
            python_venv_extra=["py/vendor", "py/wheels/foo.whl"],
        )
        pairs = nix_gen._venv_extra_pairs(t)
        self.assertEqual(pairs, [
            ("py/vendor", "vendor"),
            ("py/wheels/foo.whl", "wheels/foo.whl"),
        ])

    def test_nix_gen_flat_layout_dest_equals_src(self):
        from rigx import nix_gen
        from rigx.config import Target
        t = Target(
            name="app", kind="python_script",
            sources=["app.py"],
            python_project=".",
            python_venv_extra=["vendor", "wheels/foo.whl"],
        )
        pairs = nix_gen._venv_extra_pairs(t)
        self.assertEqual(pairs, [
            ("vendor", "vendor"),
            ("wheels/foo.whl", "wheels/foo.whl"),
        ])


if __name__ == "__main__":
    unittest.main()
