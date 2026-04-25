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

    def test_script_target(self):
        body = """
            [project]
            name = "p"

            [targets.publish]
            kind = "script"
            deps.nixpkgs = ["uv"]
            script = "uv build && uv publish"
        """
        with TempProject(body) as root:
            proj = config.load(root)
        s = proj.targets["publish"]
        self.assertEqual(s.kind, "script")
        self.assertEqual(s.deps.nixpkgs, ["uv"])
        self.assertIn("uv publish", s.script)

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

    def test_script_missing_script_field(self):
        body = """
            [project]
            name = "p"
            [targets.x]
            kind = "script"
        """
        with TempProject(body) as root:
            with self.assertRaisesRegex(ConfigError, "requires 'script'"):
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


class VarsExpansion(unittest.TestCase):
    def test_expands_in_sources_and_deps_nixpkgs(self):
        body = """
            [project]
            name = "p"

            [vars]
            common = ["src/util.cpp", "src/log.cpp"]
            cxxlibs = ["fmt", "spdlog"]

            [targets.app]
            kind = "executable"
            sources = ["$vars.common", "src/main.cpp"]
            deps.nixpkgs = ["$vars.cxxlibs"]
        """
        with TempProject(body) as root:
            proj = config.load(root)
        t = proj.targets["app"]
        self.assertEqual(
            t.sources, ["src/util.cpp", "src/log.cpp", "src/main.cpp"]
        )
        self.assertEqual(t.deps.nixpkgs, ["fmt", "spdlog"])

    def test_expands_in_variants(self):
        body = """
            [project]
            name = "p"

            [vars]
            opt = ["-O2", "-flto"]

            [targets.app]
            kind = "executable"
            sources = ["m.cpp"]

            [targets.app.variants.release]
            cxxflags = ["$vars.opt", "-DNDEBUG"]
        """
        with TempProject(body) as root:
            proj = config.load(root)
        v = proj.targets["app"].variants["release"]
        self.assertEqual(v.cxxflags, ["-O2", "-flto", "-DNDEBUG"])

    def test_empty_var_expands_to_nothing(self):
        body = """
            [project]
            name = "p"

            [vars]
            empty = []

            [targets.app]
            kind = "executable"
            sources = ["$vars.empty", "m.cpp"]
        """
        with TempProject(body) as root:
            proj = config.load(root)
        self.assertEqual(proj.targets["app"].sources, ["m.cpp"])

    def test_undefined_var_raises(self):
        body = """
            [project]
            name = "p"

            [targets.app]
            kind = "executable"
            sources = ["$vars.missing"]
        """
        with TempProject(body) as root:
            with self.assertRaisesRegex(ConfigError, r"undefined var '\$vars\.missing'"):
                config.load(root)

    def test_partial_match_is_not_expanded(self):
        # Substring matches are intentional literals — only whole-element
        # `$vars.x` is expanded.
        body = """
            [project]
            name = "p"

            [vars]
            inc = ["include"]

            [targets.app]
            kind = "executable"
            sources = ["m.cpp"]
            includes = ["prefix/$vars.inc"]
        """
        with TempProject(body) as root:
            proj = config.load(root)
        self.assertEqual(proj.targets["app"].includes, ["prefix/$vars.inc"])

    def test_vars_must_be_table(self):
        body = """
            [project]
            name = "p"
            vars = "not a table"
        """
        # TOML-level: `vars` under [project] is fine, but a scalar at the
        # top-level [vars] would actually be a TOML parse error. Test the
        # dataclass-level guard via a non-list value instead.
        body = """
            [project]
            name = "p"

            [vars]
            broken = "not a list"
        """
        with TempProject(body) as root:
            with self.assertRaisesRegex(ConfigError, "must be a list of strings"):
                config.load(root)

    def test_nested_var_reference_is_rejected(self):
        body = """
            [project]
            name = "p"

            [vars]
            a = ["$vars.b"]
            b = ["x"]
        """
        with TempProject(body) as root:
            with self.assertRaisesRegex(ConfigError, "nested \\$vars references"):
                config.load(root)


class SourceGlobs(unittest.TestCase):
    def test_star_glob_expands_to_sorted_files(self):
        body = """
            [project]
            name = "p"

            [targets.app]
            kind = "executable"
            sources = ["src/*.cpp"]
        """
        with TempProject(body) as root:
            (root / "src").mkdir()
            (root / "src" / "b.cpp").write_text("")
            (root / "src" / "a.cpp").write_text("")
            (root / "src" / "z.h").write_text("")  # excluded by extension
            proj = config.load(root)
        self.assertEqual(
            proj.targets["app"].sources, ["src/a.cpp", "src/b.cpp"]
        )

    def test_doublestar_glob_recurses(self):
        body = """
            [project]
            name = "p"

            [targets.app]
            kind = "executable"
            sources = ["src/**/*.cpp"]
        """
        with TempProject(body) as root:
            (root / "src" / "deep" / "nested").mkdir(parents=True)
            (root / "src" / "top.cpp").write_text("")
            (root / "src" / "deep" / "mid.cpp").write_text("")
            (root / "src" / "deep" / "nested" / "leaf.cpp").write_text("")
            proj = config.load(root)
        self.assertEqual(
            proj.targets["app"].sources,
            ["src/deep/mid.cpp", "src/deep/nested/leaf.cpp", "src/top.cpp"],
        )

    def test_literal_path_passes_through_unchanged(self):
        body = """
            [project]
            name = "p"

            [targets.app]
            kind = "executable"
            sources = ["src/main.cpp"]
        """
        with TempProject(body) as root:
            # File not required to exist — literals are not validated here.
            proj = config.load(root)
        self.assertEqual(proj.targets["app"].sources, ["src/main.cpp"])

    def test_literal_entry_point_kept_before_glob_results(self):
        body = """
            [project]
            name = "p"

            [targets.app]
            kind = "executable"
            sources = ["src/main.cpp", "src/lib/*.cpp"]
        """
        with TempProject(body) as root:
            (root / "src" / "lib").mkdir(parents=True)
            (root / "src" / "main.cpp").write_text("")
            (root / "src" / "lib" / "a.cpp").write_text("")
            (root / "src" / "lib" / "b.cpp").write_text("")
            proj = config.load(root)
        self.assertEqual(
            proj.targets["app"].sources,
            ["src/main.cpp", "src/lib/a.cpp", "src/lib/b.cpp"],
        )

    def test_zero_match_glob_raises(self):
        body = """
            [project]
            name = "p"

            [targets.app]
            kind = "executable"
            sources = ["src/*.cpp"]
        """
        with TempProject(body) as root:
            (root / "src").mkdir()
            with self.assertRaisesRegex(ConfigError, "matched no files"):
                config.load(root)

    def test_glob_inside_var(self):
        body = """
            [project]
            name = "p"

            [vars]
            cxx_srcs = ["src/**/*.cpp"]

            [targets.app]
            kind = "executable"
            sources = ["$vars.cxx_srcs"]
        """
        with TempProject(body) as root:
            (root / "src" / "sub").mkdir(parents=True)
            (root / "src" / "a.cpp").write_text("")
            (root / "src" / "sub" / "b.cpp").write_text("")
            proj = config.load(root)
        self.assertEqual(
            proj.targets["app"].sources, ["src/a.cpp", "src/sub/b.cpp"]
        )


if __name__ == "__main__":
    unittest.main()
