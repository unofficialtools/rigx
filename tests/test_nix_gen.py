"""Tests for the Nix flake generator."""

import unittest
from pathlib import Path

from rigx import nix_gen
from rigx.config import (
    GitDep,
    Project,
    Target,
    TargetDeps,
    Variant,
)


class Helpers(unittest.TestCase):
    def test_nix_str_basic(self):
        self.assertEqual(nix_gen._nix_str("hello"), '"hello"')

    def test_nix_str_escapes(self):
        s = nix_gen._nix_str('a"b\\c$d')
        self.assertEqual(s, '"a\\"b\\\\c\\$d"')

    def test_nix_list_empty(self):
        self.assertEqual(nix_gen._nix_list([]), "[ ]")

    def test_nix_list_items(self):
        self.assertEqual(nix_gen._nix_list(["pkgs.a", "pkgs.b"]), "[ pkgs.a pkgs.b ]")

    def test_shell_quote_plain(self):
        self.assertEqual(nix_gen._shell_quote("plain"), "plain")

    def test_shell_quote_with_space(self):
        self.assertEqual(nix_gen._shell_quote("a b"), "'a b'")

    def test_shell_quote_with_dollar(self):
        # $ triggers quoting so Nix interpolation survives and bash doesn't
        # expand the text.
        self.assertEqual(nix_gen._shell_quote("${x}/y"), "'${x}/y'")

    def test_shell_quote_embedded_single_quote(self):
        self.assertEqual(nix_gen._shell_quote("it's"), "'it'\\''s'")

    def test_python_pkg_attr(self):
        self.assertEqual(nix_gen._python_pkg_attr("3.12"), "python312")
        self.assertEqual(nix_gen._python_pkg_attr("3.13"), "python313")
        self.assertEqual(nix_gen._python_pkg_attr("3.10"), "python310")

    def test_obj_name(self):
        self.assertEqual(nix_gen._obj_name("src/a.cpp"), "src_a.o")
        self.assertEqual(nix_gen._obj_name("a.cc"), "a.o")
        self.assertEqual(nix_gen._obj_name("deep/path/file.cxx"), "deep_path_file.o")

    def test_git_input_url_with_sha(self):
        sha = "a" * 40
        dep = GitDep(name="x", url="https://example/repo.git", rev=sha)
        url = nix_gen._git_input_url(dep)
        self.assertIn(f"rev={sha}", url)
        self.assertNotIn("ref=", url)

    def test_git_input_url_with_branch(self):
        dep = GitDep(name="x", url="https://example/repo.git", rev="main")
        url = nix_gen._git_input_url(dep)
        self.assertIn("ref=main", url)


class EffectiveFlags(unittest.TestCase):
    def test_base_only(self):
        t = Target(name="t", kind="executable", cxxflags=["-O2"])
        self.assertEqual(nix_gen._effective_cxxflags(t, None), ["-O2"])

    def test_variant_appends(self):
        t = Target(name="t", kind="executable", cxxflags=["-O2"])
        v = Variant(name="d", cxxflags=["-g"])
        self.assertEqual(nix_gen._effective_cxxflags(t, v), ["-O2", "-g"])

    def test_defines_become_D_flags(self):
        t = Target(
            name="t",
            kind="executable",
            cxxflags=["-O2"],
            defines={"A": "1", "B": ""},
        )
        flags = nix_gen._effective_cxxflags(t, None)
        # Empty-value define becomes a bare -DB; valued one becomes -DA=1
        self.assertIn("-DA=1", flags)
        self.assertIn("-DB", flags)

    def test_variant_overrides_defines(self):
        t = Target(name="t", kind="executable", defines={"A": "1"})
        v = Variant(name="v", defines={"A": "2"})
        flags = nix_gen._effective_cxxflags(t, v)
        self.assertIn("-DA=2", flags)
        self.assertNotIn("-DA=1", flags)

    def test_effective_nim_flags(self):
        t = Target(name="t", kind="nim_executable", nim_flags=["-d:release"],
                   defines={"X": "1"})
        v = Variant(name="debug", nim_flags=["-d:debug"])
        out = nix_gen._effective_nim_flags(t, v)
        self.assertIn("-d:release", out)
        self.assertIn("-d:debug", out)
        self.assertIn("-d:X=1", out)


def _project(name="p", targets=None, git_deps=None) -> Project:
    return Project(
        name=name,
        version="0.1.0",
        nixpkgs_ref="nixos-24.11",
        git_deps=git_deps or {},
        targets=targets or {},
        root=Path("/tmp"),
    )


class GenerateExecutable(unittest.TestCase):
    def test_output_contains_key_fragments(self):
        t = Target(
            name="app",
            kind="executable",
            sources=["src/main.cpp"],
            cxxflags=["-std=c++17"],
            ldflags=["-lfmt"],
            deps=TargetDeps(nixpkgs=["fmt"]),
        )
        out = nix_gen.generate(_project(targets={"app": t}))
        self.assertIn('description = "rigx build for p"', out)
        self.assertIn("github:NixOS/nixpkgs/nixos-24.11", out)
        self.assertIn("pkgs.fmt", out)
        self.assertIn("src/main.cpp", out)
        self.assertIn("-lfmt", out)
        self.assertIn("mkdir -p $out/bin", out)


class GenerateStaticLibrary(unittest.TestCase):
    def test_archive_and_public_headers(self):
        t = Target(
            name="greet",
            kind="static_library",
            sources=["src/greet.cpp"],
            public_headers=["include"],
            cxxflags=["-std=c++17"],
        )
        out = nix_gen.generate(_project(targets={"greet": t}))
        self.assertIn("$AR rcs libgreet.a", out)
        self.assertIn("mkdir -p $out/lib $out/include", out)
        self.assertIn("cp -r include/. $out/include/", out)


class GenerateVariants(unittest.TestCase):
    def test_variants_produce_aliased_attrs(self):
        t = Target(
            name="hello",
            kind="executable",
            sources=["m.cpp"],
            variants={
                "debug": Variant(name="debug", cxxflags=["-O0"]),
                "release": Variant(name="release", cxxflags=["-O2"]),
            },
        )
        out = nix_gen.generate(_project(targets={"hello": t}))
        self.assertIn("hello-debug =", out)
        self.assertIn("hello-release =", out)
        # Unqualified alias points at the alphabetically-first variant.
        self.assertIn("hello = hello-debug", out)


class GenerateRun(unittest.TestCase):
    def test_internal_run_uses_store_path(self):
        tool = Target(name="tool", kind="executable", sources=["t.cpp"])
        runner = Target(
            name="r",
            kind="run",
            run="tool",
            args=["--x", "1"],
            outputs=["out.txt"],
        )
        out = nix_gen.generate(
            _project(targets={"tool": tool, "r": runner})
        )
        # Internal target reference is ${tool}/bin/tool
        self.assertIn("${tool}/bin/tool", out)
        self.assertIn("cp -r out.txt $out/out.txt", out)

    def test_external_run_uses_bare_name(self):
        runner = Target(
            name="z",
            kind="run",
            run="zip",
            args=["-r", "a.zip", "dir"],
            outputs=["a.zip"],
            deps=TargetDeps(nixpkgs=["zip"]),
        )
        out = nix_gen.generate(_project(targets={"z": runner}))
        self.assertIn("pkgs.zip", out)
        # Not wrapped with ${...}; resolved on PATH
        self.assertNotIn("${zip}/bin/zip", out)
        # Bare "zip " command appears in build phase
        self.assertRegex(out, r"\bzip -r a\.zip dir\b")

    def test_interpolation_in_args_preserved(self):
        tool = Target(name="produce", kind="executable", sources=["p.cpp"])
        consumer = Target(
            name="consume",
            kind="run",
            run="unzip",
            deps=TargetDeps(nixpkgs=["unzip"], internal=["produce"]),
            args=["-d", "out", "${produce}/data.zip"],
            outputs=["out"],
        )
        out = nix_gen.generate(
            _project(targets={"produce": tool, "consume": consumer})
        )
        # The ${produce} stays as Nix interpolation (inside single-quoted
        # shell argument).
        self.assertIn("'${produce}/data.zip'", out)


class GeneratePythonScript(unittest.TestCase):
    def test_fod_and_wrapper_structure(self):
        t = Target(
            name="greet_py",
            kind="python_script",
            sources=["src/greet.py"],
            python_version="3.12",
            python_project=".",
            python_venv_hash="sha256-deadbeef",
        )
        out = nix_gen.generate(_project(targets={"greet_py": t}))
        self.assertIn("pythonPkg = pkgs.python312", out)
        self.assertIn("uv sync --frozen --no-install-project", out)
        self.assertIn('outputHash = "sha256-deadbeef"', out)
        self.assertIn("makeWrapper", out)
        self.assertIn("lib/python3.12/site-packages", out)

    def test_fakehash_when_missing(self):
        t = Target(
            name="p",
            kind="python_script",
            sources=["a.py"],
        )
        out = nix_gen.generate(_project(targets={"p": t}))
        self.assertIn(nix_gen.FAKE_SHA256, out)

    def test_subdir_python_project(self):
        t = Target(
            name="p",
            kind="python_script",
            sources=["python/a.py"],
            python_project="python",
        )
        out = nix_gen.generate(_project(targets={"p": t}))
        self.assertIn("./python/pyproject.toml", out)
        self.assertIn("./python/uv.lock", out)


class GenerateCustom(unittest.TestCase):
    def test_scripts_inlined(self):
        t = Target(
            name="c",
            kind="custom",
            deps=TargetDeps(nixpkgs=["go"]),
            build_script="go build -o c main.go",
            install_script="mkdir -p $out/bin\ncp c $out/bin/",
        )
        out = nix_gen.generate(_project(targets={"c": t}))
        self.assertIn("pkgs.go", out)
        self.assertIn("go build -o c main.go", out)
        self.assertIn("cp c $out/bin/", out)

    def test_build_script_optional(self):
        t = Target(
            name="c",
            kind="custom",
            install_script="mkdir -p $out",
        )
        out = nix_gen.generate(_project(targets={"c": t}))
        self.assertIn("dontBuild = true", out)

    def test_native_build_inputs(self):
        t = Target(
            name="c",
            kind="custom",
            install_script="true",
            native_build_inputs=["makeWrapper"],
        )
        out = nix_gen.generate(_project(targets={"c": t}))
        self.assertIn("nativeBuildInputs = [ pkgs.makeWrapper ]", out)


class GenerateGitDeps(unittest.TestCase):
    def test_git_input_becomes_flake_input(self):
        dep = GitDep(name="mylib", url="https://example/mylib", rev="main")
        proj = _project(
            targets={
                "t": Target(
                    name="t",
                    kind="executable",
                    sources=["m.cpp"],
                    deps=TargetDeps(git=["mylib"]),
                )
            },
            git_deps={"mylib": dep},
        )
        out = nix_gen.generate(proj)
        self.assertIn("mylib.url = ", out)
        self.assertIn("mylib.flake = true", out)
        self.assertIn("inputs.mylib.packages.${system}.default", out)


if __name__ == "__main__":
    unittest.main()
