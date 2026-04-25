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
        t = Target(name="t", kind="executable", language="nim",
                   nim_flags=["-d:release"], defines={"X": "1"})
        v = Variant(name="debug", nim_flags=["-d:debug"])
        out = nix_gen._effective_nim_flags(t, v)
        self.assertIn("-d:release", out)
        self.assertIn("-d:debug", out)
        self.assertIn("-d:X=1", out)


def _project(name="p", targets=None, git_deps=None, local_deps=None, root=None) -> Project:
    return Project(
        name=name,
        version="0.1.0",
        nixpkgs_ref="nixos-24.11",
        git_deps=git_deps or {},
        targets=targets or {},
        root=root or Path("/tmp"),
        local_deps=local_deps or {},
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


class GenerateScript(unittest.TestCase):
    def test_script_kind_is_omitted_from_flake(self):
        script_target = Target(name="publish", kind="script", script="uv publish")
        exe = Target(name="app", kind="executable", sources=["m.cpp"])
        out = nix_gen.generate(_project(targets={"publish": script_target, "app": exe}))
        self.assertIn("app =", out)
        # The script target has no Nix derivation emitted.
        self.assertNotIn("publish =", out)
        self.assertNotIn('pname = "publish"', out)


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


class GenerateLocalDeps(unittest.TestCase):
    @staticmethod
    def _sub(name="sub") -> Project:
        return Project(
            name=name,
            version="0.1.0",
            nixpkgs_ref="nixos-24.11",
            git_deps={},
            targets={
                "app": Target(name="app", kind="executable", sources=["m.cpp"]),
                "lib": Target(
                    name="lib",
                    kind="executable",
                    sources=["m.cpp"],
                    variants={
                        "debug": Variant(name="debug"),
                        "release": Variant(name="release"),
                    },
                ),
            },
            root=Path("/tmp/sub"),
        )

    def test_local_dep_becomes_path_flake_input(self):
        from rigx.config import LocalDep
        sub = self._sub()
        ldep = LocalDep(name="sub", path=Path("/tmp/parent/sub"), sub_project=sub)
        parent = _project(
            name="parent",
            root=Path("/tmp/parent"),
            local_deps={"sub": ldep},
            targets={
                "bundle": Target(
                    name="bundle",
                    kind="custom",
                    install_script="true",
                    deps=TargetDeps(internal=["sub.app"]),
                ),
            },
        )
        out = nix_gen.generate(parent)
        self.assertIn('sub.url = "path:./sub"', out)
        self.assertIn("sub.flake = true", out)

    def test_re_exports_all_subproject_attrs(self):
        from rigx.config import LocalDep
        sub = self._sub()
        ldep = LocalDep(name="sub", path=Path("/tmp/parent/sub"), sub_project=sub)
        parent = _project(
            name="parent", root=Path("/tmp/parent"), local_deps={"sub": ldep},
        )
        out = nix_gen.generate(parent)
        # Plain target → re-exported under sanitized name.
        self.assertIn("sub_app = inputs.sub.packages.${system}.app;", out)
        # Variant → re-exported with hyphenated sub-attr access.
        self.assertIn(
            "sub_lib_debug = inputs.sub.packages.${system}.lib-debug;", out
        )
        self.assertIn(
            "sub_lib_release = inputs.sub.packages.${system}.lib-release;", out
        )
        # Unqualified alias also re-exported.
        self.assertIn("sub_lib = inputs.sub.packages.${system}.lib;", out)

    def test_dotted_dep_internal_renders_sanitized_id(self):
        from rigx.config import LocalDep
        sub = self._sub()
        ldep = LocalDep(name="sub", path=Path("/tmp/parent/sub"), sub_project=sub)
        parent = _project(
            name="parent",
            root=Path("/tmp/parent"),
            local_deps={"sub": ldep},
            targets={
                "bundle": Target(
                    name="bundle",
                    kind="custom",
                    install_script="cp ${sub.app}/bin/app $out/bin/app",
                    deps=TargetDeps(internal=["sub.app"]),
                ),
            },
        )
        out = nix_gen.generate(parent)
        # buildInputs uses the sanitized identifier.
        self.assertIn("sub_app", out)
        # The user's `${sub.app}` interpolation got rewritten to `${sub_app}`.
        self.assertIn("${sub_app}/bin/app", out)
        self.assertNotIn("${sub.app}", out)

    def test_cross_flake_run_resolves_binary_path(self):
        from rigx.config import LocalDep
        sub = self._sub()
        ldep = LocalDep(name="sub", path=Path("/tmp/parent/sub"), sub_project=sub)
        parent = _project(
            name="parent",
            root=Path("/tmp/parent"),
            local_deps={"sub": ldep},
            targets={
                "out_txt": Target(
                    name="out_txt",
                    kind="run",
                    run="sub.app",
                    args=["--out", "x.txt"],
                    outputs=["x.txt"],
                ),
            },
        )
        out = nix_gen.generate(parent)
        # Binary path uses sanitized id for the store-path interpolation,
        # but the binary's basename is the *last segment* of the dotted ref.
        self.assertIn("${sub_app}/bin/app", out)


class InterpRewrite(unittest.TestCase):
    def test_rewrites_known_local_dep(self):
        from rigx.config import LocalDep
        proj = _project(
            local_deps={
                "sub": LocalDep(name="sub", path=Path("/tmp/sub")),
            },
        )
        self.assertEqual(
            nix_gen._rewrite_interp("hi ${sub.app}/bin/x", proj),
            "hi ${sub_app}/bin/x",
        )

    def test_leaves_unknown_dotted_alone(self):
        # `pkgs.foo` is a Nix attr path the user wrote intentionally; we must
        # not mangle it.
        proj = _project()
        self.assertEqual(
            nix_gen._rewrite_interp("hi ${pkgs.foo}", proj),
            "hi ${pkgs.foo}",
        )

    def test_handles_multiple_segments(self):
        from rigx.config import LocalDep
        proj = _project(
            local_deps={
                "sub": LocalDep(name="sub", path=Path("/tmp/sub")),
            },
        )
        self.assertEqual(
            nix_gen._rewrite_interp("${sub.deep.thing}", proj),
            "${sub_deep_thing}",
        )


class GenerateModuleTargets(unittest.TestCase):
    def test_namespaced_target_emits_sanitized_rec_attr(self):
        t = Target(
            name="greet",
            namespace="frontend",
            kind="static_library",
            sources=["frontend/src/greet.cpp"],
            public_headers=["frontend/include"],
        )
        out = nix_gen.generate(_project(targets={"frontend.greet": t}))
        # Rec attr uses sanitized form (`_`).
        self.assertIn("frontend_greet =", out)
        # Library file is named after the *raw* target name, not the qualified one.
        self.assertIn("$AR rcs libgreet.a", out)
        # pname carries the qualified form (visible in build logs).
        self.assertIn('pname = "frontend.greet"', out)

    def test_intra_flake_b_dep_renders_sanitized_id(self):
        # Two modules: `frontend.greet` (lib) consumed by `app.main` (exe).
        # The exe's link line must reference the sanitized rec attr, but the
        # `lib<name>.a` file uses the dep target's raw name.
        lib = Target(
            name="greet",
            namespace="frontend",
            kind="static_library",
            sources=["frontend/g.cpp"],
            public_headers=["frontend/include"],
        )
        exe = Target(
            name="main",
            namespace="app",
            kind="executable",
            sources=["app/m.cpp"],
            deps=TargetDeps(internal=["frontend.greet"]),
        )
        out = nix_gen.generate(
            _project(targets={"frontend.greet": lib, "app.main": exe}),
        )
        # buildInputs uses the rec attr.
        self.assertIn("frontend_greet", out)
        # Link line references the rec attr but the raw lib filename.
        self.assertIn("${frontend_greet}/lib/libgreet.a", out)
        # Include line uses the rec attr.
        self.assertIn("-I${frontend_greet}/include", out)

    def test_b_dep_interpolation_rewritten(self):
        # Custom target uses ${frontend.greet}/include in its install_script.
        lib = Target(
            name="greet",
            namespace="frontend",
            kind="static_library",
            sources=["frontend/g.cpp"],
        )
        custom = Target(
            name="bundle",
            kind="custom",
            install_script="cp ${frontend.greet}/lib/libgreet.a $out/",
            deps=TargetDeps(internal=["frontend.greet"]),
        )
        out = nix_gen.generate(
            _project(targets={"frontend.greet": lib, "bundle": custom}),
        )
        # Dotted interpolation → underscore interpolation.
        self.assertIn("${frontend_greet}/lib/libgreet.a", out)
        self.assertNotIn("${frontend.greet}/lib", out)


class LanguageDispatch(unittest.TestCase):
    def test_go_executable_uses_go_build_and_pulls_toolchain(self):
        t = Target(
            name="hello_go",
            kind="executable",
            language="go",
            sources=["src/hello.go"],
        )
        out = nix_gen.generate(_project(targets={"hello_go": t}))
        self.assertIn("go build", out)
        self.assertIn("-o hello_go", out)
        # Auto-included toolchain in nativeBuildInputs.
        self.assertIn("nativeBuildInputs = [ pkgs.go ]", out)
        # GOCACHE / GOPATH redirected to TMPDIR (stdenv's HOME is read-only).
        self.assertIn("export GOCACHE=$TMPDIR/go-cache", out)

    def test_rust_executable_uses_rustc(self):
        t = Target(
            name="hello_rust",
            kind="executable",
            language="rust",
            sources=["src/hello.rs"],
        )
        out = nix_gen.generate(_project(targets={"hello_rust": t}))
        self.assertIn("rustc", out)
        self.assertIn("-o hello_rust src/hello.rs", out)
        self.assertIn("nativeBuildInputs = [ pkgs.rustc ]", out)

    def test_zig_executable_uses_zig_build_exe(self):
        t = Target(
            name="hello_zig",
            kind="executable",
            language="zig",
            sources=["src/hello.zig"],
        )
        out = nix_gen.generate(_project(targets={"hello_zig": t}))
        self.assertIn("zig build-exe", out)
        self.assertIn("-femit-bin=hello_zig", out)
        self.assertIn("nativeBuildInputs = [ pkgs.zig ]", out)

    def test_c_executable_uses_cc_and_cflags(self):
        t = Target(
            name="hello_c",
            kind="executable",
            language="c",
            sources=["src/main.c"],
            cflags=["-Wall", "-O2"],
        )
        out = nix_gen.generate(_project(targets={"hello_c": t}))
        self.assertIn("$CC -Wall -O2", out)
        self.assertIn("src/main.c", out)
        self.assertIn("-o hello_c", out)

    def test_clang_compiler_picks_clang_stdenv(self):
        t = Target(
            name="hello",
            kind="executable",
            language="cxx",
            sources=["m.cpp"],
            compiler="clang",
        )
        out = nix_gen.generate(_project(targets={"hello": t}))
        self.assertIn("pkgs.clangStdenv.mkDerivation", out)

    def test_default_compiler_uses_default_stdenv(self):
        t = Target(
            name="hello",
            kind="executable",
            language="cxx",
            sources=["m.cpp"],
        )
        out = nix_gen.generate(_project(targets={"hello": t}))
        self.assertIn("pkgs.stdenv.mkDerivation", out)
        self.assertNotIn("clangStdenv", out)

    def test_compiler_variant_overrides_target_compiler(self):
        t = Target(
            name="hello",
            kind="executable",
            language="cxx",
            sources=["m.cpp"],
            variants={
                "gcc":   Variant(name="gcc", compiler="gcc"),
                "clang": Variant(name="clang", compiler="clang"),
            },
        )
        out = nix_gen.generate(_project(targets={"hello": t}))
        # Both stdenv variants present.
        self.assertIn("pkgs.clangStdenv.mkDerivation", out)
        # Default gcc lands on plain stdenv.
        self.assertIn("pkgs.stdenv.mkDerivation", out)

    def test_versioned_gcc_attr(self):
        t = Target(
            name="hello",
            kind="executable",
            language="cxx",
            sources=["m.cpp"],
            compiler="gcc13",
        )
        out = nix_gen.generate(_project(targets={"hello": t}))
        self.assertIn("pkgs.gcc13Stdenv.mkDerivation", out)

    def test_rust_static_library(self):
        t = Target(
            name="mylib",
            kind="static_library",
            language="rust",
            sources=["src/lib.rs"],
        )
        out = nix_gen.generate(_project(targets={"mylib": t}))
        self.assertIn("rustc --crate-type=staticlib --crate-name=mylib", out)
        self.assertIn("-o libmylib.a src/lib.rs", out)


class SharedLibrary(unittest.TestCase):
    def test_cxx_shared_library_uses_shared_fpic(self):
        t = Target(
            name="mylib", kind="shared_library", language="cxx",
            sources=["src/lib.cpp"], cxxflags=["-std=c++17"],
            public_headers=["include"],
        )
        out = nix_gen.generate(_project(targets={"mylib": t}))
        self.assertIn("$CXX -shared -fPIC", out)
        self.assertIn("-o libmylib.so", out)
        self.assertIn("cp libmylib.so $out/lib/", out)
        self.assertIn("cp -r include/. $out/include/", out)

    def test_rust_shared_library_uses_cdylib(self):
        t = Target(
            name="mylib", kind="shared_library", language="rust",
            sources=["src/lib.rs"],
        )
        out = nix_gen.generate(_project(targets={"mylib": t}))
        self.assertIn("rustc --crate-type=cdylib --crate-name=mylib", out)
        self.assertIn("-o libmylib.so", out)


class CrossCompilation(unittest.TestCase):
    def test_c_target_routes_through_pkgs_cross(self):
        t = Target(
            name="hello", kind="executable", language="c",
            sources=["m.c"], target="aarch64-linux",
        )
        out = nix_gen.generate(_project(targets={"hello": t}))
        self.assertIn("pkgs.pkgsCross.aarch64-multiplatform.stdenv.mkDerivation", out)

    def test_cxx_target_with_clang_uses_cross_clangstdenv(self):
        t = Target(
            name="hello", kind="executable", language="cxx",
            sources=["m.cpp"], target="aarch64-linux", compiler="clang",
        )
        out = nix_gen.generate(_project(targets={"hello": t}))
        self.assertIn("pkgs.pkgsCross.aarch64-multiplatform.clangStdenv", out)

    def test_go_target_sets_goos_goarch(self):
        t = Target(
            name="hello", kind="executable", language="go",
            sources=["m.go"], target="aarch64-linux",
        )
        out = nix_gen.generate(_project(targets={"hello": t}))
        self.assertIn("export GOOS=linux", out)
        self.assertIn("export GOARCH=arm64", out)
        self.assertIn("export CGO_ENABLED=0", out)

    def test_zig_target_passes_minus_target_flag(self):
        t = Target(
            name="hello", kind="executable", language="zig",
            sources=["m.zig"], target="aarch64-linux",
        )
        out = nix_gen.generate(_project(targets={"hello": t}))
        self.assertIn("zig build-exe -target aarch64-linux-musl", out)

    def test_nim_target_emits_zigcc_shim_and_pulls_zig(self):
        t = Target(
            name="hello", kind="executable", language="nim",
            sources=["m.nim"], target="aarch64-linux",
        )
        out = nix_gen.generate(_project(targets={"hello": t}))
        # Toolchain auto-pulls both nim and zig.
        self.assertIn("nativeBuildInputs = [ pkgs.nim pkgs.zig ]", out)
        # Shim is emitted (echo-based to survive nix_gen's indenter).
        self.assertIn("echo '#!/usr/bin/env bash' > $TMPDIR/bin/zigcc", out)
        # Nim is told to use the shim as its C compiler.
        self.assertIn("--clang.exe:$TMPDIR/bin/zigcc", out)
        self.assertIn("--cpu:arm64", out)
        self.assertIn("--os:linux", out)

    def test_variant_target_overrides_target(self):
        t = Target(
            name="hello", kind="executable", language="c",
            sources=["m.c"],
            variants={"arm": Variant(name="arm", target="aarch64-linux")},
        )
        out = nix_gen.generate(_project(targets={"hello": t}))
        # Variant gets the cross stdenv; default does not.
        self.assertIn("pkgs.pkgsCross.aarch64-multiplatform.stdenv", out)


class NixIdHelper(unittest.TestCase):
    def test_replaces_dot_and_hyphen(self):
        self.assertEqual(nix_gen._nix_id("frontend.app"), "frontend_app")
        self.assertEqual(nix_gen._nix_id("hello-debug"), "hello_debug")
        self.assertEqual(nix_gen._nix_id("a.b-c"), "a_b_c")

    def test_no_op_on_clean_id(self):
        self.assertEqual(nix_gen._nix_id("hello"), "hello")


if __name__ == "__main__":
    unittest.main()
