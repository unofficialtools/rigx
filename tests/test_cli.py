"""Tests for argv-splitting in the rigx CLI."""

import io
import unittest
from unittest import mock

from rigx import cli


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
