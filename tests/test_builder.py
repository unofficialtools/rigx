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
