"""Tests for argv-splitting in the rigx CLI."""

import unittest

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


if __name__ == "__main__":
    unittest.main()
