"""Template scaffolds for `rigx new <kind> <name>`.

Each template returns (toml_block, files_to_create). The CLI appends the
TOML block to rigx.toml and writes any stub source files. Stubs are
deliberately tiny — the goal is to remove the "what do I write again?"
friction for first-time users, not to ship a full project skeleton."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Scaffold:
    toml_block: str
    files: dict[str, str]   # relative path -> file body


def _executable(name: str, language: str) -> Scaffold:
    ext = {"cxx": ".cpp", "c": ".c", "go": ".go", "rust": ".rs",
           "zig": ".zig", "nim": ".nim"}[language]
    src_path = f"src/{name}{ext}"
    bodies = {
        "cxx": (
            "#include <iostream>\n"
            "int main(int argc, char** argv) {\n"
            "    std::cout << \"hello from " + name + "\\n\";\n"
            "    return 0;\n"
            "}\n"
        ),
        "c": (
            "#include <stdio.h>\n"
            "int main(int argc, char **argv) {\n"
            "    printf(\"hello from " + name + "\\n\");\n"
            "    return 0;\n"
            "}\n"
        ),
        "go": (
            'package main\n\nimport "fmt"\n\n'
            'func main() { fmt.Println("hello from ' + name + '") }\n'
        ),
        "rust": (
            'fn main() { println!("hello from ' + name + '"); }\n'
        ),
        "zig": (
            'const std = @import("std");\n\n'
            'pub fn main() !void {\n'
            '    try std.io.getStdOut().writer().print("hello from ' + name + '\\n", .{});\n'
            '}\n'
        ),
        "nim": (
            'echo "hello from ' + name + '"\n'
        ),
    }
    flag_field = {"cxx": "cxxflags", "c": "cflags", "go": "goflags",
                  "rust": "rustflags", "zig": "zigflags", "nim": "nim_flags"}[language]
    flag_default = {
        "cxx": '["-std=c++17", "-Wall"]',
        "c":   '["-Wall"]',
        "go":  '[]',
        "rust": '[]',
        "zig": '[]',
        "nim": '["-d:release"]',
    }[language]
    block = (
        f'\n[targets.{name}]\n'
        f'kind     = "executable"\n'
        f'sources  = ["{src_path}"]\n'
        f'{flag_field} = {flag_default}\n'
    )
    return Scaffold(toml_block=block, files={src_path: bodies[language]})


def _static_library(name: str, language: str) -> Scaffold:
    if language not in {"c", "cxx", "rust"}:
        raise ValueError(f"static_library does not support language {language!r}")
    if language == "rust":
        src = f"src/{name}.rs"
        body = (
            "#[no_mangle]\n"
            f"pub extern \"C\" fn {name}_hello() {{ println!(\"hi\"); }}\n"
        )
        block = (
            f'\n[targets.{name}]\n'
            f'kind     = "static_library"\n'
            f'sources  = ["{src}"]\n'
        )
        return Scaffold(toml_block=block, files={src: body})
    ext = ".cpp" if language == "cxx" else ".c"
    src = f"src/{name}{ext}"
    hdr = f"include/{name}.h"
    body = (
        f'#include "{name}.h"\n\n'
        f"void {name}_hello() {{}}\n"
    )
    header = (
        f"#pragma once\nvoid {name}_hello();\n"
    )
    flag_field = "cxxflags" if language == "cxx" else "cflags"
    flag_default = '["-std=c++17", "-Wall"]' if language == "cxx" else '["-Wall"]'
    block = (
        f'\n[targets.{name}]\n'
        f'kind           = "static_library"\n'
        f'sources        = ["{src}"]\n'
        f'includes       = ["include"]\n'
        f'public_headers = ["include"]\n'
        f'{flag_field}       = {flag_default}\n'
    )
    return Scaffold(toml_block=block, files={src: body, hdr: header})


def _python_script(name: str) -> Scaffold:
    src = f"src/{name}.py"
    body = (
        '"""' + name + ' entry point."""\n\n'
        'if __name__ == "__main__":\n'
        f'    print("hello from {name}")\n'
    )
    block = (
        f'\n[targets.{name}]\n'
        f'kind           = "python_script"\n'
        f'sources        = ["{src}"]\n'
        f'python_version = "3.12"\n'
        f'python_project = "."\n'
    )
    return Scaffold(toml_block=block, files={src: body})


def _custom(name: str) -> Scaffold:
    block = (
        f'\n[targets.{name}]\n'
        f'kind           = "custom"\n'
        f'deps.nixpkgs   = []\n'
        f'build_script   = """\n'
        f'# build steps run inside the Nix sandbox; cwd is the source root.\n'
        f'# $out, $TMPDIR, $HOME (writable) are available.\n'
        f'echo TODO: replace with real build commands\n'
        f'"""\n'
        f'install_script = """\n'
        f'mkdir -p $out\n'
        f'# cp <built artifacts> $out/\n'
        f'"""\n'
    )
    return Scaffold(toml_block=block, files={})


def _script(name: str) -> Scaffold:
    block = (
        f'\n[targets.{name}]\n'
        f'kind         = "script"\n'
        f'deps.nixpkgs = []\n'
        f'script       = """\n'
        f'# host-side task — runs in your shell, not a sandbox.\n'
        f'# invoke with `rigx run {name}`.\n'
        f'echo TODO: implement\n'
        f'"""\n'
    )
    return Scaffold(toml_block=block, files={})


def _run(name: str, run_target: str | None) -> Scaffold:
    run_field = run_target or "tool_or_command_name"
    block = (
        f'\n[targets.{name}]\n'
        f'kind    = "run"\n'
        f'run     = "{run_field}"\n'
        f'args    = []\n'
        f'outputs = ["output_file"]\n'
    )
    return Scaffold(toml_block=block, files={})


def _test(name: str) -> Scaffold:
    block = (
        f'\n[targets.{name}]\n'
        f'kind          = "test"\n'
        f'deps.nixpkgs  = []\n'
        f'script        = """\n'
        f'# exits 0 = pass, non-0 = fail. Discovered by `rigx test`.\n'
        f'echo TODO: run actual checks\n'
        f'"""\n'
    )
    return Scaffold(toml_block=block, files={})


def scaffold(kind: str, name: str, language: str, run_target: str | None) -> Scaffold:
    if kind == "executable":
        return _executable(name, language)
    if kind == "static_library":
        return _static_library(name, language)
    if kind == "python_script":
        return _python_script(name)
    if kind == "custom":
        return _custom(name)
    if kind == "script":
        return _script(name)
    if kind == "run":
        return _run(name, run_target)
    if kind == "test":
        return _test(name)
    raise ValueError(
        f"unknown kind {kind!r}; supported: executable, static_library, "
        f"python_script, custom, script, run, test"
    )
