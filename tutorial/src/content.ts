// Tutorial content as plain data so the renderer stays focused on
// timing/animation. Each scene picks one of three "panel" types:
//
//   - terminal:   typed shell prompts + simulated output
//   - file:       a single source file revealed top-to-bottom
//   - split:      a small file on the left + a terminal on the right
//
// Durations are in seconds; the Video component converts to frames.

export type Line =
  | { kind: "cmd"; text: string }
  | { kind: "out"; text: string }
  | { kind: "comment"; text: string };

export type Scene =
  | {
      kind: "terminal";
      title: string;
      caption: string;
      seconds: number;
      lines: Line[];
    }
  | {
      kind: "file";
      title: string;
      caption: string;
      seconds: number;
      path: string;
      content: string;
    }
  | {
      kind: "split";
      title: string;
      caption: string;
      seconds: number;
      file: { path: string; content: string };
      terminal: Line[];
    };

const RIGX_TOML_BASE = `[project]
name = "hello-cpp"
version = "0.1.0"

[nixpkgs]
ref = "nixos-24.11"

[targets.greet]
kind           = "static_library"
sources        = ["src/greet.cpp"]
includes       = ["include"]
public_headers = ["include"]
cxxflags       = ["-std=c++17", "-Wall"]
deps.nixpkgs   = ["fmt"]

[targets.hello]
kind          = "executable"
sources       = ["src/main.cpp"]
includes      = ["include"]
cxxflags      = ["-std=c++17", "-Wall"]
ldflags       = ["-lfmt"]
deps.internal = ["greet"]
deps.nixpkgs  = ["fmt"]
`;

export const SCENES: Scene[] = [
  // ------------------------------------------------------------------ 1
  {
    kind: "terminal",
    title: "1 / 10  ·  Create the project folder",
    caption: "A fresh dir with the conventional src/ + include/ layout.",
    seconds: 5,
    lines: [
      { kind: "cmd", text: "mkdir hello-cpp && cd hello-cpp" },
      { kind: "cmd", text: "mkdir -p src include tests" },
    ],
  },

  // ------------------------------------------------------------------ 2
  {
    kind: "file",
    title: "2 / 10  ·  Write the C++ code",
    caption: "Header + tiny library + an executable that calls it.",
    seconds: 9,
    path: "include/greet.h  ·  src/greet.cpp  ·  src/main.cpp",
    content: `// include/greet.h
#pragma once
#include <string>

namespace greet {
std::string hello(const std::string& name);
}  // namespace greet


// src/greet.cpp
#include "greet.h"
#include <fmt/core.h>

namespace greet {
std::string hello(const std::string& name) {
    return fmt::format("Hello, {}!", name);
}
}  // namespace greet


// src/main.cpp
#include "greet.h"
#include <iostream>
#include <string>

int main(int argc, char** argv) {
    std::string who = argc > 1 ? argv[1] : "world";
    std::cout << greet::hello(who) << std::endl;
    return 0;
}
`,
  },

  // ------------------------------------------------------------------ 3
  {
    kind: "terminal",
    title: "3 / 10  ·  Install rigx",
    caption: "From PyPI. Nix is the only host tool you need separately.",
    seconds: 6,
    lines: [
      { kind: "comment", text: "# pick whichever Python flow you use" },
      { kind: "cmd", text: "pip install rigx" },
      { kind: "cmd", text: "rigx --version" },
      { kind: "out", text: "rigx 0.3.x" },
    ],
  },

  // ------------------------------------------------------------------ 4
  {
    kind: "file",
    title: "4 / 10  ·  Add rigx.toml",
    caption: "Two targets: a static library and an executable that links it.",
    seconds: 11,
    path: "rigx.toml",
    content: RIGX_TOML_BASE,
  },

  // ------------------------------------------------------------------ 5
  {
    kind: "terminal",
    title: "5 / 10  ·  Build & run",
    caption: "Sandboxed Nix build. Output is a symlink under output/.",
    seconds: 7,
    lines: [
      { kind: "cmd", text: "rigx build hello" },
      { kind: "out", text: "[rigx] building hello" },
      { kind: "out", text: "  hello -> /nix/store/…/hello" },
      { kind: "cmd", text: "./output/hello/bin/hello" },
      { kind: "out", text: "Hello, world!" },
    ],
  },

  // ------------------------------------------------------------------ 6
  {
    kind: "split",
    title: "6 / 10  ·  Add a zip target",
    caption: "kind = \"run\" calls a nixpkgs tool against built deps.",
    seconds: 9,
    file: {
      path: "rigx.toml  (additions)",
      content: `[targets.hello_zip]
kind          = "run"
run           = "zip"
deps.nixpkgs  = ["zip"]
deps.internal = ["hello"]
args          = ["-j", "hello.zip", "\${hello}/bin/hello"]
outputs       = ["hello.zip"]
`,
    },
    terminal: [
      { kind: "cmd", text: "rigx build hello_zip" },
      { kind: "out", text: "[rigx] building hello_zip" },
      { kind: "cmd", text: "ls output/hello_zip/" },
      { kind: "out", text: "hello.zip" },
    ],
  },

  // ------------------------------------------------------------------ 7
  {
    kind: "split",
    title: "7 / 10  ·  Add a test target",
    caption: "kind = \"test\" runs sandboxed; cached by input hash.",
    seconds: 9,
    file: {
      path: "rigx.toml  (additions)",
      content: `[targets.test_hello]
kind          = "test"
deps.internal = ["hello"]
deps.nixpkgs  = ["gnugrep"]
script = """
\${hello}/bin/hello | grep -q 'Hello, world!'
"""
`,
    },
    terminal: [
      { kind: "cmd", text: "rigx test test_hello" },
      { kind: "out", text: "[rigx test] test_hello (sandboxed)" },
      { kind: "out", text: "PASS  test_hello" },
    ],
  },

  // ------------------------------------------------------------------ 8
  {
    kind: "split",
    title: "8 / 10  ·  Cross-compile for ARM",
    caption: "target = \"aarch64-linux\" routes c/cxx through pkgsCross.",
    seconds: 8,
    file: {
      path: "rigx.toml  (additions)",
      content: `[targets.hello_arm64]
kind          = "executable"
sources       = ["src/main.cpp", "src/greet.cpp"]
includes      = ["include"]
cxxflags      = ["-std=c++17", "-Wall"]
ldflags       = ["-lfmt"]
deps.nixpkgs  = ["fmt"]
target        = "aarch64-linux"
`,
    },
    terminal: [
      { kind: "cmd", text: "rigx build hello_arm64" },
      { kind: "cmd", text: "file output/hello_arm64/bin/hello_arm64" },
      { kind: "out", text: "ELF 64-bit LSB executable, ARM aarch64, …" },
    ],
  },

  // ------------------------------------------------------------------ 9
  {
    kind: "split",
    title: "9 / 10  ·  Package as a capsule",
    caption: "FROM-scratch container that mounts the host /nix/store.",
    seconds: 9,
    file: {
      path: "rigx.toml  (additions)",
      content: `[targets.hello_capsule]
kind          = "capsule"
backend       = "lite"
deps.internal = ["hello"]
deps.nixpkgs  = ["coreutils"]
entrypoint    = "\${hello}/bin/hello capsule"
ports         = []
hostname      = "hellocap"
`,
    },
    terminal: [
      { kind: "cmd", text: "rigx build hello_capsule" },
      { kind: "cmd", text: "./output/hello_capsule/bin/run-hello_capsule" },
      { kind: "out", text: "Hello, capsule!" },
    ],
  },

  // ------------------------------------------------------------------ 10
  {
    kind: "split",
    title: "10 / 10  ·  Drive the capsule from a testbed",
    caption: "rigx.testbed.Network + rigx.capsule.start — one Python file.",
    seconds: 12,
    file: {
      path: "tests/test_capsule.py  +  rigx.toml entry",
      content: `# tests/test_capsule.py
from rigx.capsule import start
from rigx.testbed import Network

with Network() as net:
    net.declare("hello", listens_on=[])
    with start("hello_capsule",
               **net.bindings("hello")) as cap:
        out = cap.logs()
        assert "Hello," in out, out
        print("[testbed] OK:", out.strip())


# rigx.toml
[targets.test_hello_capsule]
kind          = "test"
sandbox       = false        # launches docker; can't sandbox
deps.internal = ["hello_capsule"]
deps.nixpkgs  = ["python3"]
script = "python3 tests/test_capsule.py"
`,
    },
    terminal: [
      { kind: "cmd", text: "rigx test test_hello_capsule" },
      { kind: "out", text: "[rigx test] building host-test deps: hello_capsule" },
      { kind: "out", text: "[rigx test] test_hello_capsule" },
      { kind: "out", text: "[testbed] OK: Hello, capsule!" },
      { kind: "out", text: "PASS  test_hello_capsule" },
    ],
  },
];
