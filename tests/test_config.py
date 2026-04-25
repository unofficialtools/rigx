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
        # Nim is now just a `language` for `kind = "executable"` (inferred
        # from the `.nim` extension); nim_executable is no longer a kind.
        body = """
            [project]
            name = "p"

            [targets.t]
            kind = "executable"
            sources = ["main.nim"]
            nim_flags = ["-d:release"]
        """
        with TempProject(body) as root:
            (root / "main.nim").write_text("")
            proj = config.load(root)
        self.assertEqual(proj.targets["t"].kind, "executable")
        self.assertEqual(proj.targets["t"].language, "nim")
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


class LocalDeps(unittest.TestCase):
    @staticmethod
    def _write(root: Path, rel: str, body: str) -> None:
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(dedent(body).lstrip())

    def _setup(self, parent_body: str, sub_body: str | None = None) -> Path:
        tmp = tempfile.mkdtemp()
        root = Path(tmp)
        self._write(root, "rigx.toml", parent_body)
        if sub_body is not None:
            self._write(root, "sub/rigx.toml", sub_body)
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp))
        return root

    def test_parses_path_and_loads_subproject(self):
        parent = """
            [project]
            name = "parent"

            [dependencies.local.sub]
            path = "./sub"
        """
        sub = """
            [project]
            name = "sub"

            [targets.app]
            kind = "executable"
            sources = ["m.cpp"]
        """
        root = self._setup(parent, sub)
        proj = config.load(root)
        self.assertIn("sub", proj.local_deps)
        self.assertIsNotNone(proj.local_deps["sub"].sub_project)
        self.assertIn("app", proj.local_deps["sub"].sub_project.targets)

    def test_missing_path_field(self):
        parent = """
            [project]
            name = "parent"

            [dependencies.local.sub]
        """
        root = self._setup(parent)
        with self.assertRaisesRegex(ConfigError, "missing 'path'"):
            config.load(root)

    def test_path_does_not_contain_rigx_toml(self):
        parent = """
            [project]
            name = "parent"

            [dependencies.local.sub]
            path = "./sub"
        """
        root = self._setup(parent)
        (root / "sub").mkdir()
        with self.assertRaisesRegex(ConfigError, "no rigx.toml at"):
            config.load(root)

    def test_collision_with_git_dep(self):
        parent = """
            [project]
            name = "parent"

            [dependencies.git.sub]
            url = "https://example.com/x"

            [dependencies.local.sub]
            path = "./sub"
        """
        sub = """
            [project]
            name = "sub"
        """
        root = self._setup(parent, sub)
        with self.assertRaisesRegex(ConfigError, "collides with dependencies.git"):
            config.load(root)

    def test_deps_internal_dotted_validation(self):
        parent = """
            [project]
            name = "parent"

            [dependencies.local.sub]
            path = "./sub"

            [targets.bundle]
            kind = "custom"
            install_script = "true"
            deps.internal = ["sub.app"]
        """
        sub = """
            [project]
            name = "sub"

            [targets.app]
            kind = "executable"
            sources = ["m.cpp"]
        """
        root = self._setup(parent, sub)
        proj = config.load(root)
        self.assertIn("sub.app", proj.targets["bundle"].deps.internal)

    def test_deps_internal_unknown_local_dep(self):
        parent = """
            [project]
            name = "parent"

            [targets.bundle]
            kind = "custom"
            install_script = "true"
            deps.internal = ["sub.app"]
        """
        root = self._setup(parent)
        with self.assertRaisesRegex(ConfigError, "is not a defined target"):
            config.load(root)

    def test_deps_internal_unknown_target_in_local_dep(self):
        parent = """
            [project]
            name = "parent"

            [dependencies.local.sub]
            path = "./sub"

            [targets.bundle]
            kind = "custom"
            install_script = "true"
            deps.internal = ["sub.missing"]
        """
        sub = """
            [project]
            name = "sub"

            [targets.app]
            kind = "executable"
            sources = ["m.cpp"]
        """
        root = self._setup(parent, sub)
        with self.assertRaisesRegex(ConfigError, "target 'missing' not found"):
            config.load(root)

    def test_cycle_detected(self):
        # parent depends on sub; sub depends back on parent → cycle.
        parent = """
            [project]
            name = "parent"

            [dependencies.local.sub]
            path = "./sub"
        """
        sub = """
            [project]
            name = "sub"

            [dependencies.local.up]
            path = ".."
        """
        root = self._setup(parent, sub)
        with self.assertRaisesRegex(ConfigError, "cycle detected"):
            config.load(root)


class Modules(unittest.TestCase):
    @staticmethod
    def _write(root: Path, rel: str, body: str) -> None:
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(dedent(body).lstrip())

    def _setup(self, files: dict[str, str]) -> Path:
        tmp = tempfile.mkdtemp()
        root = Path(tmp)
        for rel, body in files.items():
            self._write(root, rel, body)
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp))
        return root

    def test_module_targets_get_namespaced(self):
        root = self._setup({
            "rigx.toml": """
                [project]
                name = "p"

                [modules]
                include = ["frontend"]
            """,
            "frontend/rigx.toml": """
                [targets.app]
                kind = "executable"
                sources = ["src/main.cpp"]
            """,
            "frontend/src/main.cpp": "",
        })
        proj = config.load(root)
        self.assertIn("frontend.app", proj.targets)
        self.assertNotIn("app", proj.targets)
        # Source paths are rewritten to be relative to the parent root.
        self.assertEqual(
            proj.targets["frontend.app"].sources, ["frontend/src/main.cpp"]
        )
        # Namespace is recorded on the dataclass.
        self.assertEqual(proj.targets["frontend.app"].namespace, "frontend")
        self.assertEqual(proj.targets["frontend.app"].name, "app")
        self.assertEqual(proj.targets["frontend.app"].qualified_name, "frontend.app")

    def test_module_globs_resolve_against_module_root(self):
        root = self._setup({
            "rigx.toml": """
                [project]
                name = "p"

                [modules]
                include = ["frontend"]
            """,
            "frontend/rigx.toml": """
                [targets.app]
                kind = "executable"
                sources = ["src/**/*.cpp"]
            """,
            "frontend/src/a.cpp": "",
            "frontend/src/sub/b.cpp": "",
        })
        proj = config.load(root)
        self.assertEqual(
            proj.targets["frontend.app"].sources,
            ["frontend/src/a.cpp", "frontend/src/sub/b.cpp"],
        )

    def test_intra_module_dep_internal_auto_qualifies(self):
        # Inside `frontend`, `deps.internal = ["lib"]` should bind to the
        # same module's `lib` target (i.e., `frontend.lib`).
        root = self._setup({
            "rigx.toml": """
                [project]
                name = "p"

                [modules]
                include = ["frontend"]
            """,
            "frontend/rigx.toml": """
                [targets.lib]
                kind = "static_library"
                sources = ["src/lib.cpp"]

                [targets.app]
                kind = "executable"
                sources = ["src/main.cpp"]
                deps.internal = ["lib"]
            """,
            "frontend/src/lib.cpp": "",
            "frontend/src/main.cpp": "",
        })
        proj = config.load(root)
        self.assertEqual(
            proj.targets["frontend.app"].deps.internal, ["frontend.lib"]
        )

    def test_module_rejects_project_section(self):
        root = self._setup({
            "rigx.toml": """
                [project]
                name = "p"

                [modules]
                include = ["frontend"]
            """,
            "frontend/rigx.toml": """
                [project]
                name = "frontend"

                [targets.app]
                kind = "executable"
                sources = ["m.cpp"]
            """,
        })
        with self.assertRaisesRegex(ConfigError, "must not contain \\[project\\]"):
            config.load(root)

    def test_module_rejects_nixpkgs_section(self):
        root = self._setup({
            "rigx.toml": """
                [project]
                name = "p"

                [modules]
                include = ["frontend"]
            """,
            "frontend/rigx.toml": """
                [nixpkgs]
                ref = "nixos-23.05"

                [targets.app]
                kind = "executable"
                sources = ["m.cpp"]
            """,
        })
        with self.assertRaisesRegex(ConfigError, "must not contain \\[nixpkgs\\]"):
            config.load(root)

    def test_vars_collision_across_modules(self):
        root = self._setup({
            "rigx.toml": """
                [project]
                name = "p"

                [vars]
                shared = ["a"]

                [modules]
                include = ["frontend"]
            """,
            "frontend/rigx.toml": """
                [vars]
                shared = ["b"]

                [targets.app]
                kind = "executable"
                sources = ["m.cpp"]
            """,
        })
        with self.assertRaisesRegex(ConfigError, "vars.shared collides"):
            config.load(root)

    def test_nested_modules_chain_namespaces(self):
        root = self._setup({
            "rigx.toml": """
                [project]
                name = "p"

                [modules]
                include = ["outer"]
            """,
            "outer/rigx.toml": """
                [modules]
                include = ["inner"]
            """,
            "outer/inner/rigx.toml": """
                [targets.deep]
                kind = "executable"
                sources = ["m.cpp"]
            """,
            "outer/inner/m.cpp": "",
        })
        proj = config.load(root)
        self.assertIn("outer.inner.deep", proj.targets)
        self.assertEqual(
            proj.targets["outer.inner.deep"].sources, ["outer/inner/m.cpp"]
        )

    def test_cross_module_dep_internal(self):
        # Module `app` references `lib.foo` (cross-module ref).
        root = self._setup({
            "rigx.toml": """
                [project]
                name = "p"

                [modules]
                include = ["lib", "app"]
            """,
            "lib/rigx.toml": """
                [targets.foo]
                kind = "static_library"
                sources = ["foo.cpp"]
            """,
            "lib/foo.cpp": "",
            "app/rigx.toml": """
                [targets.main]
                kind = "executable"
                sources = ["main.cpp"]
                deps.internal = ["lib.foo"]
            """,
            "app/main.cpp": "",
        })
        proj = config.load(root)
        self.assertEqual(
            proj.targets["app.main"].deps.internal, ["lib.foo"]
        )

    def test_target_name_collision_across_modules_errors(self):
        root = self._setup({
            "rigx.toml": """
                [project]
                name = "p"

                [modules]
                include = ["a", "b"]

                [targets.shared]
                kind = "executable"
                sources = ["x.cpp"]
            """,
            "x.cpp": "",
            "a/rigx.toml": """
                [targets.tool]
                kind = "executable"
                sources = ["t.cpp"]
            """,
            "a/t.cpp": "",
            "b/rigx.toml": """
                [targets.tool]
                kind = "executable"
                sources = ["t.cpp"]
            """,
            "b/t.cpp": "",
        })
        # `a.tool` and `b.tool` are distinct (different namespaces). No collision.
        proj = config.load(root)
        self.assertIn("a.tool", proj.targets)
        self.assertIn("b.tool", proj.targets)
        self.assertIn("shared", proj.targets)


class LanguageInference(unittest.TestCase):
    def test_cxx_inferred_from_extensions(self):
        body = """
            [project]
            name = "p"
            [targets.app]
            kind    = "executable"
            sources = ["a.cpp", "b.cc"]
        """
        with TempProject(body) as root:
            (root / "a.cpp").write_text("")
            (root / "b.cc").write_text("")
            proj = config.load(root)
        self.assertEqual(proj.targets["app"].language, "cxx")

    def test_go_inferred(self):
        body = """
            [project]
            name = "p"
            [targets.app]
            kind    = "executable"
            sources = ["m.go"]
        """
        with TempProject(body) as root:
            (root / "m.go").write_text("")
            proj = config.load(root)
        self.assertEqual(proj.targets["app"].language, "go")

    def test_mixed_extensions_rejected(self):
        body = """
            [project]
            name = "p"
            [targets.app]
            kind    = "executable"
            sources = ["a.cpp", "b.go"]
        """
        with TempProject(body) as root:
            (root / "a.cpp").write_text("")
            (root / "b.go").write_text("")
            with self.assertRaisesRegex(ConfigError, "mixed source languages"):
                config.load(root)

    def test_explicit_language_overrides_extension(self):
        body = """
            [project]
            name = "p"
            [targets.app]
            kind     = "executable"
            sources  = ["a.cpp", "b.go"]
            language = "cxx"
        """
        with TempProject(body) as root:
            (root / "a.cpp").write_text("")
            (root / "b.go").write_text("")
            proj = config.load(root)
        self.assertEqual(proj.targets["app"].language, "cxx")

    def test_static_library_rejects_zig(self):
        body = """
            [project]
            name = "p"
            [targets.lib]
            kind    = "static_library"
            sources = ["m.zig"]
        """
        with TempProject(body) as root:
            (root / "m.zig").write_text("")
            with self.assertRaisesRegex(ConfigError, "does not support language 'zig'"):
                config.load(root)

    def test_unknown_language_rejected(self):
        body = """
            [project]
            name = "p"
            [targets.app]
            kind     = "executable"
            sources  = ["m.cpp"]
            language = "fortran"
        """
        with TempProject(body) as root:
            (root / "m.cpp").write_text("")
            with self.assertRaisesRegex(ConfigError, "language must be one of"):
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
