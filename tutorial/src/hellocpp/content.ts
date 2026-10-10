// Content for the "hello cpp" tutorial: a C++ library and program that
// use fmt, then the same code shipped as a zip, tested, cross-compiled
// for ARM, packaged as a capsule, and the capsule checked by a test.
//
// Every file, command, and output below was run for real against
// rigx on nixos-24.11 (gcc 13.3.0, fmt 10.2.1); `../hello-cpp/` is this
// content replayed into a project. Store paths in build output are
// shortened to `./output/<name>`.
//
// Each step is an explainer page (what we're about to do) followed by
// slides. An `edit` slide shows one file in an editor and types `text`
// into it, with a callout per `notes` entry; a `terminal` slide runs
// commands. Scene lengths are derived from this content.

export type Lang = "cpp" | "js" | "toml" | "py" | "txt";

export type Note = {
  // Substring identifying the line the callout points at: the first
  // typed line containing it.
  at: string;
  // `backticks` render as code.
  text: string;
};

export type TermItem =
  // Typed after a `$` prompt. `creates` lists directories (trailing
  // `/`) the command makes, so the sidebar file tree can show them.
  | { kind: "cmd"; text: string; creates?: string[] }
  // Printed instantly, one line every few frames, after the preceding cmd.
  | { kind: "out"; text: string }
  // A typed `# ...` shell comment.
  | { kind: "comment"; text: string }
  // Dead air, e.g. to let a result sit on screen.
  | { kind: "pause"; seconds: number };

export type Slide =
  | {
      kind: "edit";
      path: string;
      lang: Lang;
      // Typed at the end of the file. With `append`, the file's
      // current contents (from earlier slides) are shown first.
      text: string;
      append?: boolean;
      notes: Note[];
    }
  | { kind: "terminal"; items: TermItem[] };

export type Step = {
  title: string;
  // What this step achieves, not how. `backticks` render as code.
  what: string;
  bullets: string[];
  slides: Slide[];
};

export const STEPS: Step[] = [
  {
    title: "Start a project",
    what: "This tutorial assumes nothing on your machine except a shell, a text editor, and rigx. We start with an empty folder.",
    bullets: [
      "No compiler, no libraries, no cross-toolchain to install: rigx brings pinned versions of everything",
      "Folders for the sources and the public header",
    ],
    slides: [
      {
        kind: "terminal",
        items: [
          { kind: "cmd", text: "rigx --version" },
          { kind: "out", text: "rigx 0.13.0" },
          { kind: "cmd", text: "mkdir hello-cpp && cd hello-cpp" },
          { kind: "cmd", text: "mkdir src include", creates: ["src/", "include/"] },
        ],
      },
    ],
  },
  {
    title: "Write a library and a program",
    what: "Write a tiny greeting library and a program that uses it. The library formats text with fmt, a popular C++ formatting library.",
    bullets: [
      "A header that says what the library offers",
      "The library itself",
      "A program that greets whoever you name on the command line",
    ],
    slides: [
      {
        kind: "edit",
        path: "include/greet.h",
        lang: "cpp",
        text: `#pragma once
#include <string>

std::string greet(const std::string& name);`,
        notes: [
          { at: "#pragma once", text: "Include this header at most once per file." },
          { at: "std::string greet", text: "The library's whole interface: one function that builds a greeting." },
        ],
      },
      {
        kind: "edit",
        path: "src/greet.cpp",
        lang: "cpp",
        text: `#include "greet.h"
#include <fmt/core.h>

std::string greet(const std::string& name) {
    return fmt::format("Hello, {}!", name);
}`,
        notes: [
          { at: "<fmt/core.h>", text: "fmt, a library we don't have installed. rigx will provide it." },
          { at: "fmt::format", text: "Fill `{}` with the name." },
        ],
      },
      {
        kind: "edit",
        path: "src/main.cpp",
        lang: "cpp",
        text: `#include "greet.h"
#include <iostream>

int main(int argc, char** argv) {
    std::cout << greet(argc > 1 ? argv[1] : "world") << "\\n";
}`,
        notes: [
          { at: '#include "greet.h"', text: "The program only knows the library through its header." },
          { at: "argc > 1", text: "Greet the first argument, or `world` if there isn't one." },
        ],
      },
    ],
  },
  {
    title: "Build it with fmt",
    what: "Tell rigx about the library and the program, then build and run them. fmt comes from rigx, not from your machine.",
    bullets: [
      "The library is built first, then linked into the program",
      "The exact same fmt and compiler on every machine",
      "Run the program with and without a name",
    ],
    slides: [
      {
        kind: "edit",
        path: "rigx.toml",
        lang: "toml",
        text: `[project]
name    = "hello-cpp"
version = "0.1.0"

[nixpkgs]
ref = "nixos-24.11"

[targets.greet]
kind           = "static_library"
sources        = ["src/greet.cpp"]
includes       = ["include"]
public_headers = ["include"]
cxxflags       = ["-std=c++17"]
deps.nixpkgs   = ["fmt"]

[targets.hello]
kind          = "executable"
sources       = ["src/main.cpp"]
cxxflags      = ["-std=c++17"]
ldflags       = ["-lfmt"]
deps.internal = ["greet"]
deps.nixpkgs  = ["fmt"]`,
        notes: [
          { at: "[project]", text: "The project's name and version." },
          { at: "ref =", text: "Every compiler and library comes from nixpkgs (nixpkgs.org). This pins it to one release." },
          { at: "static_library", text: "`greet` is a library: an archive other targets link in." },
          { at: "includes ", text: "Where its own sources find `greet.h`." },
          { at: "public_headers", text: "…and the headers it hands to whoever depends on it." },
          { at: 'deps.nixpkgs   = ["fmt"]', text: "fmt from the pinned package set. Its headers land on the include path." },
          { at: '"executable"', text: "`hello` is the program." },
          { at: "ldflags", text: "Link against fmt's library." },
          { at: "deps.internal", text: "Build `greet` first and link it in. Its public header comes along." },
        ],
      },
      {
        kind: "terminal",
        items: [
          { kind: "cmd", text: "rigx build hello" },
          { kind: "out", text: "[rigx] building hello" },
          { kind: "out", text: "  hello -> ./output/hello" },
          { kind: "out", text: "      run:   output/hello/bin/hello" },
          { kind: "cmd", text: "./output/hello/bin/hello" },
          { kind: "out", text: "Hello, world!" },
          { kind: "cmd", text: "./output/hello/bin/hello rigx" },
          { kind: "out", text: "Hello, rigx!" },
        ],
      },
    ],
  },
  {
    title: "Ship it as a zip",
    what: "Produce a zip file containing the program, ready to hand to someone. The zip tool comes from rigx too.",
    bullets: [
      "Runs a tool from nixpkgs on what we already built",
      "The zip is a build output like any other, rebuilt only when the program changes",
    ],
    slides: [
      {
        kind: "edit",
        path: "rigx.toml",
        lang: "toml",
        append: true,
        text: `
[targets.hello_zip]
kind          = "run"
run           = "zip"
deps.nixpkgs  = ["zip"]
deps.internal = ["hello"]
args          = ["-j", "hello.zip", "\${hello}/bin/hello"]
outputs       = ["hello.zip"]`,
        notes: [
          { at: '"run"', text: "Run a tool and keep the files it writes." },
          { at: 'run           = "zip"', text: "The tool: `zip`…" },
          { at: '["zip"]', text: "…from the pinned package set." },
          { at: "deps.internal", text: "Build `hello` first." },
          { at: "args", text: "`${hello}` is the built program. `-j` stores it without its folder path." },
          { at: "outputs", text: "The file to keep." },
        ],
      },
      {
        kind: "terminal",
        items: [
          { kind: "cmd", text: "rigx build hello_zip" },
          { kind: "out", text: "[rigx] building hello_zip" },
          { kind: "out", text: "  hello_zip -> ./output/hello_zip" },
          { kind: "cmd", text: "ls output/hello_zip/" },
          { kind: "out", text: "hello.zip" },
        ],
      },
    ],
  },
  {
    title: "Lock it in with a test",
    what: "Add an automated check that the program greets people correctly, so a later change can't break it unnoticed.",
    bullets: [
      "Runs the program and expects the exact greeting",
      "Runs isolated from your machine",
      "Re-runs only when the program or the test changes",
    ],
    slides: [
      {
        kind: "edit",
        path: "rigx.toml",
        lang: "toml",
        append: true,
        text: `
[targets.test_hello]
kind          = "test"
deps.internal = ["hello"]
deps.nixpkgs  = ["gnugrep"]
script = """
\${hello}/bin/hello rigx | grep -qx 'Hello, rigx!'
"""`,
        notes: [
          { at: '"test"', text: "A check that `rigx test` runs inside a sandbox." },
          { at: "deps.internal", text: "Build `hello` first." },
          { at: "deps.nixpkgs", text: "Tools the script may use. Nothing else is on its PATH." },
          { at: "grep -qx", text: "Pass only if the output is exactly `Hello, rigx!`." },
        ],
      },
      {
        kind: "terminal",
        items: [
          { kind: "cmd", text: "rigx test test_hello" },
          { kind: "out", text: "[rigx test] test_hello (sandboxed)" },
          { kind: "out", text: "" },
          { kind: "out", text: "  PASS  test_hello" },
          { kind: "out", text: "" },
          { kind: "out", text: "1 passed, 0 failed" },
        ],
      },
    ],
  },
  {
    title: "Cross-compile for ARM",
    what: "Build the same program for 64-bit ARM machines, like a Raspberry Pi or an ARM server, from this computer.",
    bullets: [
      "No cross-compiler to install or configure: one line picks the target",
      "fmt is built for ARM too",
      "A test confirms the result really is an ARM program",
    ],
    slides: [
      {
        kind: "edit",
        path: "rigx.toml",
        lang: "toml",
        append: true,
        text: `
[targets.hello_arm64]
kind         = "executable"
sources      = ["src/main.cpp", "src/greet.cpp"]
includes     = ["include"]
cxxflags     = ["-std=c++17"]
ldflags      = ["-lfmt"]
deps.nixpkgs = ["fmt"]
target       = "aarch64-linux"`,
        notes: [
          { at: "[targets.hello_arm64]", text: "The same program again, as a separate target." },
          { at: "sources", text: "Library and program compiled together in one step." },
          { at: "deps.nixpkgs", text: "fmt, built for ARM to match." },
          { at: "target", text: "Compile for 64-bit ARM Linux with a cross-compiler from nixpkgs." },
        ],
      },
      {
        kind: "edit",
        path: "rigx.toml",
        lang: "toml",
        append: true,
        text: `
[targets.test_arm64]
kind          = "test"
deps.internal = ["hello_arm64"]
deps.nixpkgs  = ["file", "gnugrep"]
script = """
file -L \${hello_arm64}/bin/hello_arm64 | grep -q 'ARM aarch64'
"""`,
        notes: [
          { at: "deps.internal", text: "Build the ARM program first." },
          { at: "deps.nixpkgs", text: "`file` identifies what kind of binary it is." },
          { at: "file -L", text: "Pass only if it reports an ARM aarch64 executable." },
        ],
      },
      {
        kind: "terminal",
        items: [
          { kind: "cmd", text: "rigx build hello_arm64" },
          { kind: "out", text: "[rigx] building hello_arm64" },
          { kind: "out", text: "  hello_arm64 -> ./output/hello_arm64" },
          { kind: "out", text: "      run:   output/hello_arm64/bin/hello_arm64" },
          { kind: "cmd", text: "rigx test test_arm64" },
          { kind: "out", text: "[rigx test] test_arm64 (sandboxed)" },
          { kind: "out", text: "" },
          { kind: "out", text: "  PASS  test_arm64" },
          { kind: "out", text: "" },
          { kind: "out", text: "1 passed, 0 failed" },
        ],
      },
    ],
  },
  {
    title: "Package it as a capsule",
    what: "Turn the program into a small container that greets from inside.",
    bullets: [
      "Holds just the program and the libraries it needs, with no base OS",
      "Built from the program we already have",
      "Running containers is the one thing rigx can't provide: this step needs docker or podman on the machine",
    ],
    slides: [
      {
        kind: "edit",
        path: "rigx.toml",
        lang: "toml",
        append: true,
        text: `
[targets.hello_capsule]
kind          = "capsule"
backend       = "lite"
deps.internal = ["hello"]
entrypoint    = "\${hello}/bin/hello capsule"`,
        notes: [
          { at: '"capsule"', text: "Package a built program as a runnable container." },
          { at: "backend", text: "`lite`: a minimal image with just the program and the libraries it needs. No base OS." },
          { at: "deps.internal", text: "The program goes inside." },
          { at: "entrypoint", text: "What the container runs: greet `capsule`." },
        ],
      },
      {
        kind: "terminal",
        items: [
          { kind: "cmd", text: "rigx build hello_capsule" },
          { kind: "out", text: "[rigx] building hello_capsule" },
          { kind: "out", text: "  hello_capsule -> ./output/hello_capsule" },
          { kind: "out", text: "      start: output/hello_capsule/bin/run-hello_capsule" },
          { kind: "out", text: "      shell: output/hello_capsule/bin/shell-hello_capsule" },
          { kind: "cmd", text: "./output/hello_capsule/bin/run-hello_capsule" },
          { kind: "out", text: "[run-hello_capsule] loading rigx/hello_capsule:latest" },
          { kind: "out", text: "Loaded image: rigx/hello_capsule:latest" },
          { kind: "out", text: "Hello, capsule!" },
        ],
      },
    ],
  },
  {
    title: "Check the capsule with a test",
    what: "Add a test that starts the container and checks its greeting, then run every test in the project with one command.",
    bullets: [
      "This test runs on your machine instead of the sandbox, because it starts a container",
      "The capsule is built before the test runs",
      "One command runs all three tests",
    ],
    slides: [
      {
        kind: "edit",
        path: "rigx.toml",
        lang: "toml",
        append: true,
        text: `
[targets.test_capsule]
kind          = "test"
sandbox       = false
deps.internal = ["hello_capsule"]
deps.nixpkgs  = ["gnugrep"]
script = """
RIGX_FOREGROUND=1 \${hello_capsule}/bin/run-hello_capsule \\
  | grep -qx 'Hello, capsule!'
"""`,
        notes: [
          { at: "sandbox", text: "Run on the host: the sandbox can't start containers." },
          { at: "deps.internal", text: "Build the capsule first." },
          { at: "RIGX_FOREGROUND", text: "Start the container attached, without a terminal, so its output can be piped." },
          { at: "grep -qx", text: "Pass only if it greets exactly `Hello, capsule!`." },
        ],
      },
      {
        kind: "terminal",
        items: [
          { kind: "cmd", text: "rigx test" },
          { kind: "out", text: "[rigx test] test_hello (sandboxed)" },
          { kind: "out", text: "[rigx test] test_arm64 (sandboxed)" },
          { kind: "out", text: "[rigx test] building host-test deps: hello_capsule" },
          { kind: "out", text: "[rigx] building hello_capsule" },
          { kind: "out", text: "[rigx test] test_capsule" },
          { kind: "out", text: "[run-hello_capsule] loading rigx/hello_capsule:latest" },
          { kind: "out", text: "Loaded image: rigx/hello_capsule:latest" },
          { kind: "out", text: "" },
          { kind: "out", text: "  PASS  test_hello" },
          { kind: "out", text: "  PASS  test_arm64" },
          { kind: "out", text: "  PASS  test_capsule" },
          { kind: "out", text: "" },
          { kind: "out", text: "3 passed, 0 failed" },
        ],
      },
    ],
  },
];
