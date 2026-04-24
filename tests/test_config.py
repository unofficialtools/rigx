"""Tests for rigx.toml parsing and validation."""

import tempfile
import unittest
from pathlib import Path
from textwrap import dedent

from rigx import config
from rigx.config import ConfigError


class TempProject:
    """Context manager: a tempdir with a rigx.toml containing the given body."""

    def __init__(self, toml_body: str):
        self.toml_body = dedent(toml_body).lstrip()

    def __enter__(self) -> Path:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        (root / "rigx.toml").write_text(self.toml_body)
        return root

    def __exit__(self, *exc) -> None:
        self._tmp.cleanup()


class LoadMinimal(unittest.TestCase):
    def test_name_and_default_version(self):
        body = """
            [project]
            name = "hi"
        """
        with TempProject(body) as root:
            proj = config.load(root)
        self.assertEqual(proj.name, "hi")
        self.assertEqual(proj.version, "0.0.0")
        self.assertEqual(proj.nixpkgs_ref, "nixos-24.11")
        self.assertEqual(proj.targets, {})
        self.assertEqual(proj.git_deps, {})

    def test_version_and_nixpkgs_ref(self):
        body = """
            [project]
            name = "p"
            version = "1.2.3"

            [nixpkgs]
            ref = "nixos-unstable"
        """
        with TempProject(body) as root:
            proj = config.load(root)
        self.assertEqual(proj.version, "1.2.3")
        self.assertEqual(proj.nixpkgs_ref, "nixos-unstable")


class LoadGitDeps(unittest.TestCase):
    def test_defaults_and_overrides(self):
        body = """
            [project]
            name = "p"

            [dependencies.git.lib1]
            url = "https://github.com/a/b"

            [dependencies.git.lib2]
            url = "https://github.com/c/d"
            rev = "v1.2.3"
            flake = false
            attr = "mylib"
        """
        with TempProject(body) as root:
            proj = config.load(root)
        self.assertEqual(proj.git_deps["lib1"].url, "https://github.com/a/b")
        self.assertEqual(proj.git_deps["lib1"].rev, "HEAD")
        self.assertTrue(proj.git_deps["lib1"].flake)
        self.assertIsNone(proj.git_deps["lib1"].attr)
        self.assertEqual(proj.git_deps["lib2"].rev, "v1.2.3")
        self.assertFalse(proj.git_deps["lib2"].flake)
        self.assertEqual(proj.git_deps["lib2"].attr, "mylib")


class LoadTargetKinds(unittest.TestCase):
    def test_executable_and_static_library(self):
        body = """
            [project]
            name = "p"

            [targets.lib]
            kind = "static_library"
            sources = ["src/a.cpp"]
            public_headers = ["include"]
            cxxflags = ["-std=c++17"]

            [targets.app]
            kind = "executable"
            sources = ["src/main.cpp"]
            cxxflags = ["-O2"]
            ldflags = ["-lfmt"]
            defines = { X = "1" }
            deps.internal = ["lib"]
            deps.nixpkgs = ["fmt"]
        """
        with TempProject(body) as root:
            proj = config.load(root)
        self.assertEqual(proj.targets["lib"].kind, "static_library")
        self.assertEqual(proj.targets["lib"].public_headers, ["include"])
        self.assertEqual(proj.targets["app"].kind, "executable")
        self.assertEqual(proj.targets["app"].deps.internal, ["lib"])
        self.assertEqual(proj.targets["app"].deps.nixpkgs, ["fmt"])
        self.assertEqual(proj.targets["app"].defines, {"X": "1"})

    def test_nim_executable(self):
        body = """
            [project]
            name = "p"

            [targets.t]
            kind = "nim_executable"
            sources = ["main.nim"]
            nim_flags = ["-d:release"]
            deps.nixpkgs = ["nim"]
        """
        with TempProject(body) as root:
            proj = config.load(root)
        self.assertEqual(proj.targets["t"].kind, "nim_executable")
        self.assertEqual(proj.targets["t"].nim_flags, ["-d:release"])

    def test_python_script_defaults(self):
        body = """
            [project]
            name = "p"

            [targets.py]
            kind = "python_script"
            sources = ["s.py"]
        """
        with TempProject(body) as root:
            proj = config.load(root)
        t = proj.targets["py"]
        self.assertEqual(t.python_version, "3.12")
        self.assertEqual(t.python_project, ".")
        self.assertIsNone(t.python_venv_hash)

    def test_run_target(self):
        body = """
            [project]
            name = "p"

            [targets.tool]
            kind = "executable"
            sources = ["t.cpp"]

            [targets.r]
            kind = "run"
            run = "tool"
            args = ["--a", "1"]
            outputs = ["out.txt"]
        """
        with TempProject(body) as root:
            proj = config.load(root)
        r = proj.targets["r"]
        self.assertEqual(r.kind, "run")
        self.assertEqual(r.run, "tool")
        self.assertEqual(r.args, ["--a", "1"])
        self.assertEqual(r.outputs, ["out.txt"])

    def test_custom_target(self):
        body = """
            [project]
            name = "p"

            [targets.c]
            kind = "custom"
            build_script = "echo building"
            install_script = "mkdir -p $out"
            native_build_inputs = ["makeWrapper"]
        """
        with TempProject(body) as root:
            proj = config.load(root)
        c = proj.targets["c"]
        self.assertEqual(c.kind, "custom")
        self.assertIn("building", c.build_script)
        self.assertIn("$out", c.install_script)
        self.assertEqual(c.native_build_inputs, ["makeWrapper"])


class LoadVariants(unittest.TestCase):
    def test_parsing(self):
        body = """
            [project]
            name = "p"

            [targets.app]
            kind = "executable"
            sources = ["m.cpp"]
            cxxflags = ["-std=c++17"]

            [targets.app.variants.debug]
            cxxflags = ["-O0", "-g"]
            defines = { DEBUG = "1" }

            [targets.app.variants.release]
            cxxflags = ["-O2"]
            ldflags = ["-Wl,-s"]
        """
        with TempProject(body) as root:
            proj = config.load(root)
        app = proj.targets["app"]
        self.assertEqual(app.variant_names(), ["debug", "release"])
        self.assertEqual(app.variants["debug"].cxxflags, ["-O0", "-g"])
        self.assertEqual(app.variants["debug"].defines, {"DEBUG": "1"})
        self.assertEqual(app.variants["release"].ldflags, ["-Wl,-s"])


class ValidationErrors(unittest.TestCase):
    def test_missing_file(self):
        with tempfile.TemporaryDirectory() as t:
            with self.assertRaises(ConfigError):
                config.load(Path(t))

    def test_missing_project_name(self):
        body = """[nixpkgs]\nref = "x"\n"""
        with TempProject(body) as root:
            with self.assertRaises(ConfigError):
                config.load(root)

    def test_invalid_kind(self):
        body = """
            [project]
            name = "p"
            [targets.x]
            kind = "banana"
        """
        with TempProject(body) as root:
            with self.assertRaises(ConfigError):
                config.load(root)

    def test_unknown_deps_key(self):
        body = """
            [project]
            name = "p"
            [targets.x]
            kind = "executable"
            deps.unknown = ["a"]
        """
        with TempProject(body) as root:
            with self.assertRaisesRegex(ConfigError, "unknown deps key"):
                config.load(root)

    def test_internal_dep_not_found(self):
        body = """
            [project]
            name = "p"
            [targets.x]
            kind = "executable"
            sources = ["a.cpp"]
            deps.internal = ["missing"]
        """
        with TempProject(body) as root:
            with self.assertRaisesRegex(ConfigError, "is not a defined target"):
                config.load(root)

    def test_git_dep_not_found_in_target(self):
        body = """
            [project]
            name = "p"
            [targets.x]
            kind = "executable"
            deps.git = ["missing"]
        """
        with TempProject(body) as root:
            with self.assertRaisesRegex(ConfigError, "undefined dependency"):
                config.load(root)

    def test_run_missing_run_field(self):
        body = """
            [project]
            name = "p"
            [targets.r]
            kind = "run"
            outputs = ["a"]
        """
        with TempProject(body) as root:
            with self.assertRaisesRegex(ConfigError, "requires 'run"):
                config.load(root)

    def test_run_missing_outputs(self):
        body = """
            [project]
            name = "p"
            [targets.tool]
            kind = "executable"
            sources = ["t.cpp"]
            [targets.r]
            kind = "run"
            run = "tool"
        """
        with TempProject(body) as root:
            with self.assertRaisesRegex(ConfigError, "requires 'outputs'"):
                config.load(root)

    def test_run_wrong_kind(self):
        body = """
            [project]
            name = "p"
            [targets.lib]
            kind = "static_library"
            sources = ["a.cpp"]
            [targets.r]
            kind = "run"
            run = "lib"
            outputs = ["a"]
        """
        with TempProject(body) as root:
            with self.assertRaisesRegex(ConfigError, "must be one of"):
                config.load(root)

    def test_custom_missing_install_script(self):
        body = """
            [project]
            name = "p"
            [targets.c]
            kind = "custom"
            build_script = "true"
        """
        with TempProject(body) as root:
            with self.assertRaisesRegex(ConfigError, "install_script"):
                config.load(root)


if __name__ == "__main__":
    unittest.main()
