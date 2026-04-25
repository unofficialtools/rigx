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


if __name__ == "__main__":
    unittest.main()
