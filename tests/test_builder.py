"""Tests for builder attribute resolution logic (no Nix invocation)."""

import unittest
from pathlib import Path
from unittest import mock

from rigx import builder
from rigx.builder import BuildError, NixNotFoundError
from rigx.config import Project, Target, Variant


def _project_with(**targets) -> Project:
    return Project(
        name="p",
        version="0.1.0",
        nixpkgs_ref="nixos-24.11",
        git_deps={},
        targets=targets,
        root=Path("/tmp/fake"),
    )


class ResolveAttr(unittest.TestCase):
    def test_plain_target(self):
        t = Target(name="hello", kind="executable", sources=["m.cpp"])
        proj = _project_with(hello=t)
        self.assertEqual(builder._resolve_attr(proj, "hello"), "hello")

    def test_variant(self):
        t = Target(
            name="hello",
            kind="executable",
            sources=["m.cpp"],
            variants={
                "debug": Variant(name="debug"),
                "release": Variant(name="release"),
            },
        )
        proj = _project_with(hello=t)
        self.assertEqual(builder._resolve_attr(proj, "hello@debug"), "hello-debug")

    def test_unknown_target(self):
        proj = _project_with()
        with self.assertRaisesRegex(BuildError, "no such target"):
            builder._resolve_attr(proj, "missing")

    def test_unknown_variant(self):
        t = Target(
            name="h",
            kind="executable",
            sources=["m.cpp"],
            variants={"debug": Variant(name="debug")},
        )
        proj = _project_with(h=t)
        with self.assertRaisesRegex(BuildError, "has no variant"):
            builder._resolve_attr(proj, "h@asan")


class AllAttrs(unittest.TestCase):
    def test_expands_variants(self):
        proj = _project_with(
            plain=Target(name="plain", kind="executable", sources=["m.cpp"]),
            variadic=Target(
                name="variadic",
                kind="executable",
                sources=["m.cpp"],
                variants={
                    "debug": Variant(name="debug"),
                    "release": Variant(name="release"),
                },
            ),
        )
        attrs = builder._all_attrs(proj)
        self.assertIn("plain", attrs)
        self.assertIn("variadic-debug", attrs)
        self.assertIn("variadic-release", attrs)
        # Variadic target itself (without variant suffix) is not listed — its
        # alias is reachable, but _all_attrs emits the concrete variants.
        self.assertNotIn("variadic", attrs)

    def test_script_targets_excluded_from_build_all(self):
        proj = _project_with(
            hello=Target(name="hello", kind="executable", sources=["m.cpp"]),
            publish=Target(name="publish", kind="script", script="uv publish"),
        )
        attrs = builder._all_attrs(proj)
        self.assertIn("hello", attrs)
        self.assertNotIn("publish", attrs)


class NixMissing(unittest.TestCase):
    def test_raises_specific_error_with_instructions(self):
        with mock.patch("rigx.builder.shutil.which", return_value=None):
            with self.assertRaises(NixNotFoundError) as ctx:
                builder._nix_bin()
        msg = str(ctx.exception)
        self.assertIn("Nix is required", msg)
        self.assertIn("nixos.org", msg)
        self.assertIn("install.determinate.systems", msg)

    def test_is_subclass_of_builderror(self):
        self.assertTrue(issubclass(NixNotFoundError, BuildError))


class RunNamedScript(unittest.TestCase):
    def test_rejects_unknown_target(self):
        proj = _project_with()
        with self.assertRaisesRegex(BuildError, "no such target"):
            builder.run_named_script(proj, "missing")

    def test_rejects_non_script_target(self):
        proj = _project_with(
            hello=Target(name="hello", kind="executable", sources=["m.cpp"]),
        )
        with self.assertRaisesRegex(BuildError, "is not a script target"):
            builder.run_named_script(proj, "hello")

    def test_extra_args_forwarded_to_bash_as_positional(self):
        proj = _project_with(
            deploy=Target(name="deploy", kind="script", script='echo "$@"'),
        )
        with mock.patch("rigx.builder._nix_bin", return_value="/usr/bin/nix"), \
             mock.patch("rigx.builder.subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0)
            builder.run_named_script(proj, "deploy", ["--dry-run", "prod"])
        cmd = run.call_args.args[0]
        # `bash -eo pipefail -c <script> $0 $1 $2 ...`
        self.assertIn("bash", cmd)
        i = cmd.index("-c")
        # Script body sits at -c's value; target name is $0; user args follow.
        self.assertEqual(cmd[i + 1 : i + 5], ['echo "$@"', "deploy", "--dry-run", "prod"])

    def test_no_extra_args_means_no_positional_after_target_name(self):
        proj = _project_with(
            deploy=Target(name="deploy", kind="script", script="true"),
        )
        with mock.patch("rigx.builder._nix_bin", return_value="/usr/bin/nix"), \
             mock.patch("rigx.builder.subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0)
            builder.run_named_script(proj, "deploy")
        cmd = run.call_args.args[0]
        i = cmd.index("-c")
        self.assertEqual(cmd[i + 1 : i + 3], ["true", "deploy"])
        self.assertEqual(len(cmd), i + 3)


class BuildRejectsScript(unittest.TestCase):
    def test_build_points_at_rigx_run(self):
        proj = _project_with(
            publish=Target(name="publish", kind="script", script="echo"),
        )
        with self.assertRaisesRegex(BuildError, "use `rigx run publish`"):
            builder.build(proj, ["publish"])


class FlakeRef(unittest.TestCase):
    def test_shape(self):
        proj = _project_with()
        ref = builder._flake_ref(proj, "hello")
        self.assertTrue(ref.startswith("path:"))
        self.assertTrue(ref.endswith("#hello"))

    def test_without_attr(self):
        proj = _project_with()
        ref = builder._flake_ref(proj)
        self.assertTrue(ref.startswith("path:"))
        self.assertNotIn("#", ref)


if __name__ == "__main__":
    unittest.main()
