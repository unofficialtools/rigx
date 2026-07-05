"""Tests for argv-splitting in the rigx CLI."""

import io
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from textwrap import dedent
from unittest import mock

from rigx import cli, sources


class SplitRunPassthrough(unittest.TestCase):
    def test_no_run_subcommand(self):
        self.assertIsNone(cli._split_run_passthrough(["build", "hello"]))

    def test_run_without_double_dash(self):
        self.assertIsNone(cli._split_run_passthrough(["run", "publish"]))

    def test_run_with_double_dash_and_args(self):
        pre, args = cli._split_run_passthrough(
            ["run", "deploy", "--", "--dry-run", "prod"]
        )
        self.assertEqual(pre, ["run", "deploy"])
        self.assertEqual(args, ["--dry-run", "prod"])

    def test_run_with_empty_passthrough(self):
        pre, args = cli._split_run_passthrough(["run", "deploy", "--"])
        self.assertEqual(pre, ["run", "deploy"])
        self.assertEqual(args, [])

    def test_run_with_leading_global_flag(self):
        pre, args = cli._split_run_passthrough(
            ["-C", "./proj", "run", "deploy", "--", "x"]
        )
        self.assertEqual(pre, ["-C", "./proj", "run", "deploy"])
        self.assertEqual(args, ["x"])

    def test_run_as_value_of_project_flag_is_ignored(self):
        # `-C run` means project dir is "run", not the run subcommand.
        self.assertIsNone(
            cli._split_run_passthrough(["-C", "run", "build", "--", "x"])
        )

    def test_run_as_positional_of_other_subcommand_is_ignored(self):
        # `rigx build run -- x` — "run" is a target name, not the subcommand.
        self.assertIsNone(
            cli._split_run_passthrough(["build", "run", "--", "x"])
        )


class SplitPkgPassthrough(unittest.TestCase):
    def test_no_pkg_subcommand(self):
        self.assertIsNone(cli._split_pkg_passthrough(["build", "hello"]))

    def test_pkg_with_no_attr_returns_none(self):
        # `rigx pkg` alone — let argparse complain about the missing attr.
        self.assertIsNone(cli._split_pkg_passthrough(["pkg"]))

    def test_pkg_with_attr_only(self):
        pre, args = cli._split_pkg_passthrough(["pkg", "uv"])
        self.assertEqual(pre, ["pkg", "uv"])
        self.assertEqual(args, [])

    def test_pkg_with_args_no_dash(self):
        pre, args = cli._split_pkg_passthrough(["pkg", "uv", "lock"])
        self.assertEqual(pre, ["pkg", "uv"])
        self.assertEqual(args, ["lock"])

    def test_pkg_with_dash_separator(self):
        # `--` is consumed; args after it forwarded.
        pre, args = cli._split_pkg_passthrough(["pkg", "uv", "--", "lock", "--frozen"])
        self.assertEqual(pre, ["pkg", "uv"])
        self.assertEqual(args, ["lock", "--frozen"])

    def test_pkg_with_leading_global_flag(self):
        pre, args = cli._split_pkg_passthrough(
            ["-C", "./proj", "pkg", "jq", "--", ".foo"]
        )
        self.assertEqual(pre, ["-C", "./proj", "pkg", "jq"])
        self.assertEqual(args, [".foo"])

    def test_pkg_as_value_of_project_flag_is_ignored(self):
        # `-C pkg` means project dir is "pkg", not the pkg subcommand.
        self.assertIsNone(
            cli._split_pkg_passthrough(["-C", "pkg", "build"])
        )

    def test_pkg_as_positional_of_other_subcommand_is_ignored(self):
        # `rigx build pkg app lib` — "pkg" is a target name; splitting here
        # would silently drop "lib" from the build.
        self.assertIsNone(
            cli._split_pkg_passthrough(["build", "pkg", "app", "lib"])
        )

    def test_pkg_with_inline_project_flag(self):
        pre, args = cli._split_pkg_passthrough(
            ["--project=./proj", "pkg", "jq", "--", ".foo"]
        )
        self.assertEqual(pre, ["--project=./proj", "pkg", "jq"])
        self.assertEqual(args, [".foo"])


class MainKeyboardInterrupt(unittest.TestCase):
    def test_keyboard_interrupt_returns_130(self):
        # `rigx version` is dispatch-only with no I/O; raise KeyboardInterrupt
        # from cmd_version to simulate a SIGINT bubbling up from any command.
        stderr = io.StringIO()
        with mock.patch.object(cli, "cmd_version", side_effect=KeyboardInterrupt), \
             mock.patch("sys.stderr", stderr):
            rc = cli.main(["version"])
        self.assertEqual(rc, 130)
        self.assertIn("interrupted", stderr.getvalue())


class BuildHints(unittest.TestCase):
    """`rigx build` prints a per-kind hint after each successful target —
    `run:` for executable / python_script, `start:` + `shell:` for
    capsule, nothing for static_library / custom / run."""

    def _project(self):
        from pathlib import Path
        from rigx.config import Project, Target, Variant, TargetDeps
        targets = {
            "hello": Target(name="hello", kind="executable"),
            "lib": Target(name="lib", kind="static_library"),
            "greeter": Target(
                name="greeter", kind="capsule",
                backend="lite", entrypoint="x",
            ),
            "tool": Target(name="tool", kind="python_script"),
            "build_thing": Target(name="build_thing", kind="custom"),
            "hello_var": Target(
                name="hello_var", kind="executable",
                variants={"debug": Variant(name="debug")},
            ),
        }
        return Project(
            name="p", version="0.0.0", nixpkgs_ref="x",
            git_deps={}, targets=targets, root=Path("/tmp/p"),
        )

    def test_executable_emits_run_hint(self):
        from pathlib import Path
        proj = self._project()
        hints = cli._build_hint_lines(proj, "hello", Path("/tmp/p/output/hello"))
        self.assertEqual(len(hints), 1)
        self.assertTrue(hints[0].startswith("run:"))
        self.assertIn("/bin/hello", hints[0])

    def test_capsule_emits_start_and_shell_hints(self):
        from pathlib import Path
        proj = self._project()
        hints = cli._build_hint_lines(
            proj, "greeter", Path("/tmp/p/output/greeter")
        )
        self.assertEqual(len(hints), 2)
        self.assertTrue(hints[0].startswith("start:"))
        self.assertIn("/bin/run-greeter", hints[0])
        self.assertTrue(hints[1].startswith("shell:"))
        self.assertIn("/bin/shell-greeter", hints[1])

    def test_python_script_emits_run_hint(self):
        from pathlib import Path
        proj = self._project()
        hints = cli._build_hint_lines(proj, "tool", Path("/tmp/p/output/tool"))
        self.assertEqual(len(hints), 1)
        self.assertIn("/bin/tool", hints[0])

    def test_static_library_emits_no_hint(self):
        # Static libraries are consumed by linking, not invocation —
        # there's no "run this" guidance to give.
        from pathlib import Path
        proj = self._project()
        hints = cli._build_hint_lines(proj, "lib", Path("/tmp/p/output/lib"))
        self.assertEqual(hints, [])

    def test_custom_emits_no_hint(self):
        from pathlib import Path
        proj = self._project()
        hints = cli._build_hint_lines(
            proj, "build_thing", Path("/tmp/p/output/build_thing")
        )
        self.assertEqual(hints, [])

    def test_variant_attr_resolves_to_base_target(self):
        # Variant attrs (`hello_var-debug`) reverse-map to the base
        # target so we still know to print the executable hint.
        # The binary inside is named after the base target, not the
        # variant.
        from pathlib import Path
        proj = self._project()
        hints = cli._build_hint_lines(
            proj, "hello_var-debug", Path("/tmp/p/output/hello_var-debug")
        )
        self.assertEqual(len(hints), 1)
        self.assertIn("/bin/hello_var", hints[0])

    def test_unknown_attr_returns_no_hint(self):
        from pathlib import Path
        proj = self._project()
        self.assertEqual(
            cli._build_hint_lines(
                proj, "ghost", Path("/tmp/p/output/ghost")
            ),
            [],
        )


class _LsSourceFixture:
    """Spin up a temp project with a populated tree for `rigx ls-source`
    integration tests. Mirrors the fixture in test_sources but goes through
    the CLI entry point so we cover argument parsing and stdout layout."""

    def __init__(self, toml_body: str, files: dict[str, str]):
        self.toml_body = dedent(toml_body).lstrip()
        self.files = files

    def __enter__(self) -> Path:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        for rel, body in self.files.items():
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(body)
        (root / "rigx.toml").write_text(self.toml_body)
        sources._project_files_cached.cache_clear()
        return root

    def __exit__(self, *exc) -> None:
        sources._project_files_cached.cache_clear()
        self._tmp.cleanup()


class LsSourceCLI(unittest.TestCase):
    def _run(self, root: Path, target: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = cli.main(["-C", str(root), "ls-source", target])
        return rc, out.getvalue(), err.getvalue()

    def test_prints_resolved_files_and_summary(self):
        body = """
            [project]
            name = "p"
            sources = ["**/*.cpp", "**/*.h"]
            respect_gitignore = false

            [targets.hello]
            kind = "executable"
            sources = ["src/main.cpp"]
            includes = ["include"]
            language = "cxx"
        """
        files = {
            "src/main.cpp": "int main(){}",
            "include/foo.h": "#define FOO\n",
            "data/unused.txt": "x",
        }
        with _LsSourceFixture(body, files) as root:
            rc, out, err = self._run(root, "hello")
        self.assertEqual(rc, 0)
        self.assertIn("src/main.cpp", out)
        self.assertIn("include/foo.h", out)
        self.assertNotIn("data/unused.txt", out)
        self.assertRegex(err, r"\b2 files,")

    def test_unset_project_sources_returns_error(self):
        body = """
            [project]
            name = "p"

            [targets.t]
            kind = "custom"
            install_script = "true"
        """
        with _LsSourceFixture(body, {}) as root:
            rc, _out, err = self._run(root, "t")
        self.assertEqual(rc, 1)
        self.assertIn("[project].sources", err)

    def test_unknown_target_returns_error(self):
        body = """
            [project]
            name = "p"
            sources = ["**/*"]
            respect_gitignore = false
        """
        with _LsSourceFixture(body, {"a.txt": ""}) as root:
            rc, _out, err = self._run(root, "ghost")
        self.assertEqual(rc, 1)
        self.assertIn("'ghost' not found", err)


class CmdNew(unittest.TestCase):
    def _run(self, root: Path, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = cli.main(["-C", str(root), "new", *argv])
        return rc, out.getvalue(), err.getvalue()

    def test_broken_toml_reports_clean_error(self):
        # A syntax error in rigx.toml must produce a clean error, not a
        # raw tomllib traceback.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "rigx.toml").write_text("[project\nname = broken")
            rc, _out, err = self._run(root, "script", "task")
        self.assertEqual(rc, 1)
        self.assertIn("invalid TOML", err)

    def test_rejects_name_with_space(self):
        # `rigx new script "a b"` would append `[targets.a b]` — invalid
        # TOML that bricks every subsequent command.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "rigx.toml").write_text('[project]\nname = "p"\n')
            rc, _out, err = self._run(root, "script", "a b")
            self.assertEqual(rc, 1)
            self.assertIn("invalid target name", err)
            self.assertNotIn("a b", (root / "rigx.toml").read_text())

    def test_rejects_name_with_path_traversal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "rigx.toml").write_text('[project]\nname = "p"\n')
            for bad in ("../evil", "a/b", "a.b", '"quoted"'):
                rc, _out, err = self._run(root, "executable", bad)
                self.assertEqual(rc, 1, bad)
                self.assertIn("invalid target name", err)

    def test_overwrite_clash_leaves_no_partial_scaffold(self):
        # static_library writes src/lib.cpp then include/lib.h; if only
        # the second clashes, nothing must be written (a mid-loop abort
        # used to leave the first stub orphaned and rigx.toml untouched).
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            toml_body = '[project]\nname = "p"\n'
            (root / "rigx.toml").write_text(toml_body)
            (root / "include").mkdir()
            (root / "include" / "lib.h").write_text("// mine\n")
            rc, _out, err = self._run(root, "static_library", "lib")
            self.assertEqual(rc, 1)
            self.assertIn("refusing to overwrite", err)
            self.assertFalse((root / "src" / "lib.cpp").exists())
            self.assertEqual((root / "rigx.toml").read_text(), toml_body)

    def test_accepts_hyphenated_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "rigx.toml").write_text('[project]\nname = "p"\n')
            rc, _out, err = self._run(root, "script", "my-task")
            self.assertEqual(rc, 0, err)
            self.assertIn("[targets.my-task]", (root / "rigx.toml").read_text())

    def test_finds_project_root_from_subdirectory(self):
        # Without -C, cmd_new must search upward for rigx.toml like every
        # other command, not assume cwd is the project root.
        import os
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / "rigx.toml").write_text('[project]\nname = "p"\n')
            sub = root / "src" / "deep"
            sub.mkdir(parents=True)
            cwd = os.getcwd()
            os.chdir(sub)
            try:
                out, err = io.StringIO(), io.StringIO()
                with redirect_stdout(out), redirect_stderr(err):
                    rc = cli.main(["new", "script", "task"])
            finally:
                os.chdir(cwd)
            self.assertEqual(rc, 0, err.getvalue())
            self.assertIn("[targets.task]", (root / "rigx.toml").read_text())


class CmdFmt(unittest.TestCase):
    def test_finds_project_root_from_subdirectory(self):
        import os
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / "rigx.toml").write_text('[project]\nname = "p"\n')
            sub = root / "src"
            sub.mkdir()
            cwd = os.getcwd()
            os.chdir(sub)
            try:
                out, err = io.StringIO(), io.StringIO()
                with redirect_stdout(out), redirect_stderr(err):
                    rc = cli.main(["fmt"])
            finally:
                os.chdir(cwd)
            self.assertEqual(rc, 0, err.getvalue())
            self.assertIn('name = "p"', out.getvalue())


class CmdRunExitCode(unittest.TestCase):
    def _proj(self):
        from rigx.config import Project, Target
        return Project(
            name="p",
            version="0.1.0",
            nixpkgs_ref="nixos-24.11",
            git_deps={},
            targets={
                "deploy": Target(name="deploy", kind="script", script="exit 7"),
            },
            root=Path("/tmp/fake"),
        )

    def test_forwards_script_exit_code(self):
        # The script exited 7 — `rigx run` must exit 7, not a generic 1.
        err = io.StringIO()
        with mock.patch("rigx.cli._load", return_value=self._proj()), \
             mock.patch("rigx.builder.run_script_target", return_value=7), \
             redirect_stdout(io.StringIO()), redirect_stderr(err):
            rc = cli.main(["run", "deploy"])
        self.assertEqual(rc, 7)
        self.assertIn("exit 7", err.getvalue())

    def test_other_build_errors_still_exit_1(self):
        err = io.StringIO()
        with mock.patch("rigx.cli._load", return_value=self._proj()), \
             redirect_stdout(io.StringIO()), redirect_stderr(err):
            rc = cli.main(["run", "missing"])
        self.assertEqual(rc, 1)


class FormatBytes(unittest.TestCase):
    def test_b(self):
        self.assertEqual(cli._format_bytes(0), "0 B")
        self.assertEqual(cli._format_bytes(500), "500 B")

    def test_kb(self):
        self.assertEqual(cli._format_bytes(2048), "2.0 KB")

    def test_mb(self):
        self.assertEqual(cli._format_bytes(2_500_000), "2.4 MB")


class MainModuleExitCode(unittest.TestCase):
    """`python -m rigx` (via rigx/__main__.py) must propagate cli.main's
    return value as the process exit code. The pyproject console-script
    entry gets this for free; the module entry needs an explicit
    sys.exit() — without it, scripts that shell out to `python -m rigx
    ... build ...` (like the repo's own example_project_build test)
    silently report PASS on a failed build."""

    def _run_main_returning(self, exit_code: int) -> int:
        import subprocess
        import sys
        return subprocess.run(
            [
                sys.executable, "-c",
                "import rigx.cli, runpy\n"
                f"rigx.cli.main = lambda *a, **k: {exit_code}\n"
                "runpy.run_module('rigx', run_name='__main__')\n",
            ],
            capture_output=True,
        ).returncode

    def test_propagates_nonzero(self):
        self.assertEqual(self._run_main_returning(7), 7)

    def test_zero_is_clean_exit(self):
        self.assertEqual(self._run_main_returning(0), 0)


if __name__ == "__main__":
    unittest.main()
