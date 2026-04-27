# rigx

<p align="center"><img src="rigx.png" alt="rigx logo" width="320"></p>

[VIDEO TUTORIAL - CPP DEMO](https://vimeo.com/1187129074)

> **Status: experimental.** This is an early version
> APIs, the `rigx.toml` schema, and CLI behavior may change without
> notice. If you use it, please report issues, but don't rely on it
> for production builds yet.

`rigx` is a build system (think Make or Bazel) for C, C++, Go, Rust, Zig,
Nim, and Python — plus anything else you can script. It is designed to be
very easy to use while quietly enforcing good policies on your behalf.
Your build and test targets are defined in a single `rigx.toml` file with
a very simple syntax.

Your builds and tests do not depend on what you have (or don't have)
installed on your system. All the packages your targets depend on are
pulled in on demand, kept cached, and never pollute the host. Every build
and test runs in a sandbox; outputs are cached for speed and land in an
`output/` folder so the rest of your tree stays clean.

This works through the magic of a system called Nix, which is referenced
multiple times in this document — but you do not have to know Nix to use
rigx. Install Nix and `rigx` once and forget about them.

`rigx` also offers some advanced features: running tests concurrently,
running integration tests outside the sandbox, packaging artifacts as
capsules (a kind of lightweight container), and orchestrating tests on
testbeds that comprise multiple capsules and support fault injection.

While rigx itself is written in Python, the actual build system is not
Python — it's Nix. rigx works by parsing `rigx.toml` and generating a
file called `flake.nix` (you will not need to read or edit this file).
A flake tells Nix how to derive each target from its sources inside its
own sandbox.

**Why do I care about reproducibility?** Because if you ship anything
that runs on your machine, you expect it to run the same way everywhere
else — your CI, your colleague's laptop, your customer's server. Most
"works on my machine" bugs are really "I depended on something I didn't
declare" bugs; rigx makes that class of bug structurally impossible.

**Do I need to run NixOS to use rigx?** No. Rigx runs everywhere Nix runs.
This means any Linux distribution (and macOS);
Nix a tool you install alongside your existing OS, not a replacement for it.

**Why use rigx instead of Make?** Make is a recipe runner: you write
the shell commands and manage every dependency — including the
toolchain — yourself. That makes "works on my machine" the default
failure mode, and Make has no idea whether your `gcc` matches your
colleague's. rigx is declarative (you describe *what* to build, not the
recipe), the toolchain comes from a pinned `nixpkgs`, builds run in a
sandbox that can't see whatever you happen to have installed, and
outputs are content-addressed and cached across machines.

**Why use rigx instead of Bazel?** Bazel is more powerful — fine-grained
action caching, Starlark for custom rules, true remote build execution,
designed for 10k-target monorepos at a large company. The cost is a
steep learning curve, `BUILD` / `WORKSPACE` files written in Starlark,
and a heavyweight setup. rigx gives you most of the same wins
(reproducibility, sandboxing, content-addressed caching, remote
builders, multi-language) through a single declarative TOML file with
no scripting layer. For most projects that's enough; if you're running
a monorepo with thousands of fine-grained build actions and a team to
maintain it, reach for Bazel. Unlike Bazel, with Rigx you do not need
to maintain your own toolchains. Moreover, Rigx supports the unique
concepts of capsules and testbeds which allow you to orchestrate
and test distributed applications.

**How well is it tested?** rigx has more than 300 unit tests covering
config parsing, Nix generation, capsule construction, the testbed
proxy, and the CLI workflow.

## A taste, if you've used Make or Bazel

```make
# Makefile
CXX = g++
CXXFLAGS = -std=c++17 -O2

hello: src/main.cpp src/greet.cpp
	$(CXX) $(CXXFLAGS) -o hello $^
```

```python
# BUILD.bazel
cc_binary(
    name = "hello",
    srcs = ["src/main.cpp", "src/greet.cpp"],
    copts = ["-std=c++17", "-O2"],
)
```

```toml
# rigx.toml
[project]
name = "myproject"

[targets.hello]
kind     = "executable"
sources  = ["src/main.cpp", "src/greet.cpp"]
cxxflags = ["-std=c++17", "-O2"]
```

```
rigx build hello              # builds in a sandbox
./output/hello/bin/hello      # run it
```

What's different:

- You describe **what to build**, not the recipe — closer to Bazel's
  `cc_binary` than Make's `$(CXX) ... $^` block. `kind = "executable"` tells
  rigx how to compile a C++ program.
- The compiler and any `deps.nixpkgs = ["fmt"]` libraries come from a pinned
  `nixpkgs` — not your `$PATH` (Make) and not Bazel's host toolchain. First
  build pulls them; later builds use the Nix store cache.
- Outputs live in `/nix/store/...` and `output/hello` is a symlink to the
  current build. `rigx clean` removes the symlink; the store entry persists
  and gets reused next time.
- No `.PHONY`, no `genrule` — for side-effecting tasks (publish, deploy, run
  a script) use `kind = "script"` and `rigx run <name>`.
- Need a different language? Just drop `.go`, `.rs`, `.zig`, or `.nim`
  files into `sources` — language is inferred from the extension, the
  toolchain comes from nixpkgs, and you get the same `kind = "executable"`
  shape. Use `kind = "python_script"` for Python; `kind = "custom"` for
  project-managed builds (Cargo workspaces, `cmake`, …).
- `rigx.toml` is pure data — no Starlark, no Make macros. Sharing values
  across targets is `[vars]`; sharing across folders is `[modules]` or
  `[dependencies.local.*]` (see below).
- **Remote and distributed builds**: Nix supports remote builders
  (`nix.conf`'s `builders = ssh://…` dispatches whole derivations to other
  machines over SSH — handy for cross-platform builds and crude
  parallelism) and binary caches (cache.nixos.org, Cachix, attic, S3 —
  the equivalent of Bazel's remote cache). There's no per-action RBE
  scheduler like Bazel's; granularity is whole derivations, not
  individual actions. With a shared cache pointed at by your team and
  CI, that's usually plenty.

## Features

- **TOML target declarations**: inputs, outputs, internal and external deps.
- **External deps** via pinned `nixpkgs` or `git` flake inputs.
- **Lock file** (`flake.lock`) pins every input revision.
- **Sandboxed builds**: compilation never touches the local filesystem; each
  derivation runs against the Nix store's layered filesystem.
- **Parallel builds**: `rigx build -j N` runs up to N targets concurrently
  (each via its own `nix build`; the Nix daemon dedupes shared deps).
  Default is sequential — failures are reported per-target rather than
  cancelling the whole batch.
- **Outputs** only appear under `output/` as symlinks into the Nix store.
- **Parameterized targets** via variants (e.g. `debug` / `release`).
- **Multi-language, first-class**: C, C++, Go, Rust, Zig, Nim, Python — pick
  by extension or set `language = "..."` explicitly. Anything else (Cargo
  workspaces, `cmake`, custom build pipelines) goes through `kind = "custom"`.
- **Cross-compilation** built in: `target = "aarch64-linux"` (or
  `armv7-linux`, `x86_64-windows`, …) routes c/cxx through `pkgsCross.<x>`,
  sets `GOOS`/`GOARCH` for Go, passes `-target` to Zig, auto-emits a `zigcc`
  shim for Nim. Combine with variants for one-source / multi-platform builds.
- **Multi-folder projects**: split a project into subfolders via
  `[modules]` (merged into one flake) or `[dependencies.local.*]` (each
  subfolder is its own flake, parent depends on built artifacts).
- **Sharable vars** (`[vars]` + `extends`) keep flag/source/dep lists
  DRY across targets and across files.
- **Built-in workflow tools**: `rigx watch` (rebuild on change),
  `rigx test` (discovers every `kind = "test"` target — sandboxed +
  cached by default, opt-out with `sandbox = false`), `rigx new`
  (scaffold a target + stub source), `rigx fmt` (canonical TOML),
  `rigx graph` (Mermaid dep graph), `rigx build --json` (CI-friendly
  output).

## Requirements

- [Nix](https://nixos.org/download) 2.4+ with flakes enabled (rigx passes
  `--extra-experimental-features "nix-command flakes"` automatically). This is
  the **only** tool you must install on the host — everything else
  (toolchains, `uv`, language-specific interpreters, …) comes from nixpkgs on
  demand and is pinned in `flake.lock`.

Python is provided by whatever channel you use to install rigx (PyPI install
methods manage it for you; nixpkgs brings it along automatically). For
generating Python `uv.lock` files you can run `rigx pkg uv -- lock` — rigx
pulls `uv` (or any other binary) from the project's pinned nixpkgs, no host
install needed.

## Installation

### From PyPI

Pick whichever matches your toolchain. Nix is **not** bundled — if it isn't
already on your `PATH`, rigx prints install instructions the first time you run
`rigx build`.

**`uv tool install` (recommended — isolated, on your PATH):**
```
uv tool install rigx
```
Upgrade with `uv tool upgrade rigx`; remove with `uv tool uninstall rigx`.

**`pipx` (same idea, pipx-managed venv):**
```
pipx install rigx
```

**`pip` in a virtualenv:**
```
python3 -m venv .venv
. .venv/bin/activate
pip install rigx
```

**Ephemeral (run once without installing):**
```
uv tool run rigx -C ./example-project build    # uv
pipx run rigx -C ./example-project build       # pipx
```

On nixpkgs-Python systems you'll see an `externally-managed-environment` error
from a bare `pip install` — use one of the isolated methods above instead.

After installation, install Nix:
- macOS / Linux (official): `sh <(curl -L https://nixos.org/nix/install) --daemon`
- macOS / Linux (Determinate Systems): `curl --proto '=https' --tlsv1.2 -sSf -L https://install.determinate.systems/nix | sh -s -- install`

Restart your shell (or source `/nix/var/nix/profiles/default/etc/profile.d/nix-daemon.sh`), then confirm: `nix --version && rigx --help`.


## Usage

From a project directory containing `rigx.toml`:

```
rigx version              # print the rigx version (also: `--version` / `-V`)
rigx list                 # list targets
rigx list --kind test     # filter by kind (executable, test, run, …)
rigx lock                 # generate flake.nix and update flake.lock
rigx build                # build every target (and variant)
rigx build hello          # build one target
rigx build hello@release  # build a specific variant
rigx build 'hello*'       # glob over target names (variants expanded)
rigx build -j 8           # up to 8 targets concurrently (one nix-build each)
rigx build --json         # machine-readable output for CI / scripts
rigx watch [target]       # rebuild on source change (Ctrl-C to stop)
rigx test                 # discover & run all kind=test targets, sequentially
rigx test smoke perf      # run only the named tests (literal names)
rigx test 'unit_*'        # filters are fnmatch patterns — globs work too
rigx test -j 4            # up to 4 tests concurrently (exclusives still serial)
rigx graph hello          # print a Mermaid dep graph for one target
rigx flake                # print generated flake.nix (for debugging)
rigx fmt [--write]        # canonical-format rigx.toml (comments not preserved)
rigx new executable foo   # scaffold a new target + stub source files
rigx clean                # remove output/
rigx run publish          # execute a script-kind target (publish/deploy/etc.)
rigx run deploy -- --dry-run prod   # forward args after `--` as $1, $2, …
rigx pkg uv -- lock       # run any nixpkgs binary (uv, jq, ripgrep, …) from pinned nixpkgs
```

If rigx isn't installed, invoke it as a module:
`PYTHONPATH=/path/to/rigx python3 -m rigx -C /path/to/project build`.

---

# `rigx.toml` reference

Every `rigx.toml` has a `[project]` section, an optional `[nixpkgs]` section,
an optional `[vars]` table, zero or more `[dependencies.git.*]` entries,
zero or more `[dependencies.local.*]` entries, an optional `[modules]` block,
and one or more `[targets.*]`.

## Top-level sections

### `include = [...]`

An optional list of other TOML files to splice into this one before parsing. Each
entry is a literal path or a glob (`*`, `**`, `?`, `[…]`) resolved
relative to the file declaring the `include`. Use this to keep large
projects manageable by splitting `[targets.*]`, `[vars.*]`, and
`[dependencies.*]` across files.

```toml
# rigx.toml — must come BEFORE any [section] header
include = [
    "lib/extra.toml",
    "targets/*.toml",       # glob — sorted, empty match is OK
]

[project]
name = "myproject"
```

Semantics:

- **Inlined textually.** Included files are merged into the parent's data
  before any other parsing. Paths inside them (target sources,
  `[vars].extends`, etc.) resolve against the *root* `rigx.toml`'s
  directory, not the include file's directory. If you want subtree-
  relative paths and a namespace, use `[modules]` instead.
- **No identity sections.** Included files must not declare `[project]`
  or `[nixpkgs]` — only the root `rigx.toml` owns project identity and
  the nixpkgs ref.
- **Duplicate names error.** A target, var, or dependency name defined
  in two files (root + include, or two includes) is a config error.
- **Recursion.** An included file may itself declare `include = [...]`;
  paths in that nested array resolve relative to that file's directory.
  Cycles are detected.
- **Position matters.** `include = [...]` must appear before any
  `[section]` header. After a header, TOML scopes the assignment into
  that section — rigx will print an explicit error if it finds
  `project.include`.

### `[project]`

```toml
[project]
name        = "myproject"        # required. Used as the generated flake's identity.
version     = "0.1.0"            # optional; default "0.0.0". Used as Nix derivation version.
description = "A short summary"  # optional; defaults to "rigx build for <name>". Shown in `nix flake metadata`.
```

### `[nixpkgs]`

```toml
[nixpkgs]
ref = "nixos-24.11"          # optional; default "nixos-24.11". Any nixpkgs branch, tag, or commit.
```

The revision is resolved into `flake.lock` on `rigx lock`.
The default value for `ref` may change in future versions of rigx so we recommend
that you specify a value for it.

### `[vars]`

An optiona list of reusable values shared between targets. Each entry must be a list of
strings. Reference one inside any list field with `"$vars.<name>"` — it
expands inline (the entry is replaced by the var's contents).

```toml
[vars]
common_sources = ["src/util.cpp", "src/log.cpp"]
cxx_deps       = ["fmt", "spdlog"]
opt_release    = ["-O2", "-flto"]

[targets.app]
kind          = "executable"
sources       = ["$vars.common_sources", "src/main.cpp"]
deps.nixpkgs  = ["$vars.cxx_deps"]

[targets.app.variants.release]
cxxflags = ["$vars.opt_release", "-DNDEBUG"]
```

- Expansion is **whole-element only**: `"prefix/$vars.x"` stays literal.
- Vars cannot reference other vars (one-pass resolution, no cycles).
- An undefined `$vars.<name>` is a config error.
- Works in every list field of a target or variant: `sources`, `includes`,
  `public_headers`, `cxxflags`, `ldflags`, `nim_flags`, `args`, `outputs`,
  `native_build_inputs`, and all three `deps.*` lists.

**Sharing vars across files** — the reserved key `extends` pulls in `[vars]`
from another TOML file, useful when independent rigx projects (e.g. siblings
declared via `[dependencies.local.*]`) want a common toolchain config:

```toml
# shared.toml
[vars]
cxx_libs = ["fmt", "spdlog"]
opt      = ["-O2", "-flto"]

# rigx.toml
[vars]
extends = ["../shared.toml"]    # paths relative to this file
local   = ["x"]
```

Extended files are loaded recursively (with cycle detection); collisions
across `extends` chains and the local table are config errors.

### `[dependencies.git.<name>]`

Declare external flake inputs. Referenced from targets via `deps.git = ["<name>"]`.

```toml
[dependencies.git.mylib]
url   = "https://github.com/someone/mylib"
rev   = "v1.0.0"             # branch / tag / 40-char commit SHA
flake = true                 # must be a flake in this version (default true)
attr  = "default"            # attribute inside packages.${system} (default "default")
```

### `[dependencies.local.<name>]`

Pull in a sibling rigx project as a **path flake input**. The sub-project
stays standalone (its own flake, its own `output/`, builds independently from
its directory) and the parent depends on its **built outputs** — never raw
sources.

```toml
[dependencies.local.frontend]
path  = "./frontend"          # required; relative to this rigx.toml
flake = true                  # default true; mirrors [dependencies.git.*]
```

Reference targets across the boundary with the `<localdep>.<target>` form in
`deps.internal`, `run`, `args`, and shell scripts:

```toml
[targets.bundle]
kind          = "custom"
deps.internal = ["frontend.app"]                        # adds the dep to buildInputs
install_script = "cp ${frontend.app}/bin/app $out/bin/" # ${X.Y} resolves cross-flake
```

Cross-flake refs are *opaque* to the parent — it has no metadata about the
sub-project's targets, so the linker/include helpers used for same-project
deps don't fire. To consume a sibling `static_library`, write a `custom`
target that copies headers/archives explicitly, or use `[modules]` (below).

`rigx build frontend.app` from the parent re-exports and builds the
sub-project's output. `rigx list` shows everything reachable as
`<localdep>.<target>` forms. The parent's `flake.lock` pins each local-dep
as a path input.

### `[modules]`

Merge sibling rigx-style configs into the **same flake**. Use this when the
project really is a monorepo and you want cross-folder targets to share
sources, vars, and the parent's pinned `[nixpkgs]`.

```toml
[modules]
include = ["frontend", "service"]   # paths to sub-folders containing rigx.toml
```

Each module's `rigx.toml`:

- **must not** define `[project]` or `[nixpkgs]` (the parent owns identity).
- **may** define `[targets.*]`, `[vars]`, `[dependencies.git.*]`,
  `[dependencies.local.*]`, and its own `[modules]` (recursive).
- has its `[targets.*]` automatically prefixed with the module's directory
  name: `frontend/rigx.toml`'s `[targets.app]` becomes `frontend.app` in the
  merged set.
- has its source paths interpreted relative to the module's directory and
  rewritten to be parent-root-relative — so a module looks like a normal
  rigx project to its author.

Inside a module, `deps.internal = ["greet"]` (unqualified) auto-binds to the
same module's `greet`. To reference a different module, qualify it:
`deps.internal = ["other.foo"]`.

`[vars]`, `[dependencies.git.*]`, and `[dependencies.local.*]` from each
module are flat-merged into the parent. Name collisions across modules (or
between a module and the parent) are config errors — keeps things explicit.

Picking between (A) `[dependencies.local.*]` and (B) `[modules]`:

| You want…                                      | Use |
|-----------------------------------------------|-----|
| Subfolders that build independently (`cd` and go) | (A) |
| Subfolders with their own `flake.lock` / nixpkgs ref | (A) |
| Cross-folder `static_library` linking         | (B) |
| Shared `[vars]` across folders                | (B) |
| One-flake monorepo with namespaced targets    | (B) |

You can use both in the same parent — they share the dotted CLI surface
(`frontend.app`) but resolve through different mechanisms.

## Targets

Every target lives under `[targets.<name>]` and has a `kind`. Target and
variant names are taken verbatim — `[targets.foo-bar]` and
`[targets.foo_bar]` are different targets, just like `[targets.Foo]` and
`[targets.foo]` are.

**Source globs.** Entries in `sources` may use `*`, `**`, `?`, and `[…]`
patterns (Python `Path.glob` semantics). Globs are resolved against the
project root at config-load time, results are sorted for deterministic Nix
hashes, and a glob that matches no files is a config error. Literal entries
pass through unchanged, so you can mix them — useful when a kind treats
`sources[0]` as the entry point:

```toml
sources = ["src/main.cpp", "src/lib/**/*.cpp"]   # main.cpp stays first
```

Fields common to several kinds:

| Field                  | Type            | Purpose                                          |
|------------------------|-----------------|--------------------------------------------------|
| `kind`                 | string          | One of the kinds listed below. **Required.**     |
| `sources`              | list[string]    | Source files (paths or globs, relative to root). |
| `includes`             | list[string]    | Header / include search paths.                   |
| `language`             | string          | Override extension-based inference: `c`, `cxx`, `go`, `rust`, `zig`, `nim`. |
| `compiler`             | string          | Toolchain selector: stdenv variant (c/cxx) or nixpkgs attr (go/rust/zig/nim). |
| `target`               | string          | Cross-compilation triple (e.g. `aarch64-linux`). See Cross-compilation below. |
| `cflags`               | list[string]    | Compiler flags (C).                              |
| `cxxflags`             | list[string]    | Compiler flags (C++).                            |
| `goflags`              | list[string]    | Flags forwarded to `go build` (Go).              |
| `rustflags`            | list[string]    | Flags forwarded to `rustc` (Rust).               |
| `zigflags`             | list[string]    | Flags forwarded to `zig build-exe` (Zig).        |
| `ldflags`              | list[string]    | Linker flags (C/C++ only).                       |
| `defines`              | table           | Preprocessor defines: `{ DEBUG = "1" }`.         |
| `deps.internal`        | list[string]    | Other targets in this `rigx.toml`.               |
| `deps.nixpkgs`         | list[string]    | Nixpkgs attrs (e.g. `fmt`, `uv`, `go`).          |
| `deps.git`             | list[string]    | Names from `[dependencies.git.*]`.               |

### Variants — parameterized targets

Variants override/extend fields per configuration. Selected at the CLI as
`target@variant`.

```toml
[targets.hello.variants.debug]
cxxflags = ["-O0", "-g"]
defines  = { DEBUG = "1" }

[targets.hello.variants.release]
cxxflags = ["-O2"]
defines  = { NDEBUG = "1" }

# Toolchain-swap variants: `rigx build hello@gcc` and `rigx build hello@clang`
# produce two binaries from the same sources but different compilers.
[targets.hello.variants.clang]
compiler = "clang"
[targets.hello.variants.gcc13]
compiler = "gcc13"
```

- Variant fields **append** to the target's base flag fields (`cxxflags`,
  `cflags`, `ldflags`, `nim_flags`, `goflags`, `rustflags`, `zigflags`) and
  **merge over** `defines`.
- A variant's `compiler` overrides the target's `compiler` (and so picks a
  different stdenv variant for c/cxx, or a different toolchain attr for
  go/rust/zig).
- Variants produce independent Nix derivations (`hello-debug`, `hello-release`).
- `rigx build hello` builds all variants; the unqualified attribute
  aliases the alphabetically-first variant.

---

## Kinds

### `executable` — C, C++, Go, Rust, Zig, or Nim program

```toml
[targets.hello]
kind     = "executable"
sources  = ["src/main.cpp"]          # extension picks the language
includes = ["include"]
cxxflags = ["-std=c++17", "-Wall"]
ldflags  = ["-lfmt"]                 # linker flags (e.g. -lNAME for nixpkgs libs)
deps.internal = ["greet"]            # static_library deps are linked in automatically
deps.nixpkgs  = ["fmt"]
```

- **Language is inferred from source extensions**: `.c` → C, `.cpp`/`.cxx`/
  `.cc`/`.C` → C++, `.go` → Go, `.rs` → Rust, `.zig` → Zig, `.nim` → Nim.
  Mixed sources require an explicit `language = "cxx"` (etc.) to
  disambiguate.
- **Compiler choice** with `compiler = "..."`:
  - C/C++: names a stdenv variant — `"clang"` → `clangStdenv`, `"gcc13"` →
    `gcc13Stdenv`, etc. Default is `pkgs.stdenv` (gcc on Linux, clang on macOS).
  - Go/Rust/Zig: names a nixpkgs attr providing the toolchain — `"go_1_21"`,
    a specific `"rustc_1_75"`, or whatever is available. Default is `go`,
    `rustc`, `zig`.
  - Per-variant override (`hello@gcc` vs `hello@clang`) lets one target
    produce two binaries with different toolchains.
- **Per-language flag fields**: `cflags` (C), `cxxflags` (C++), `goflags`
  (Go), `rustflags` (Rust), `zigflags` (Zig). Only the field matching the
  resolved language is used.
- Output: `$out/bin/<name>` in the Nix store, symlinked to `output/<name>`.
- Linking (C/C++ only): `static_library` internal deps are added to the link
  line as `${dep}/lib/lib<dep>.a`. Nixpkgs deps go on `buildInputs` (so
  `NIX_CFLAGS_COMPILE` / `NIX_LDFLAGS` pick them up); add `-l<name>` in
  `ldflags` to link a shared lib by soname.

```toml
# Go (toolchain auto-pulled; no need to list it in deps.nixpkgs)
[targets.hello_go]
kind    = "executable"
sources = ["src/hello.go"]
goflags = ["-trimpath"]

# Rust (single source compiled with rustc; for Cargo workspaces use `custom`)
[targets.hello_rust]
kind      = "executable"
sources   = ["src/hello.rs"]
rustflags = ["-Copt-level=2"]

# Zig (single source via `zig build-exe`; for `build.zig` projects use `custom`)
[targets.hello_zig]
kind     = "executable"
sources  = ["src/hello.zig"]
zigflags = ["-O", "ReleaseFast"]

# Pick a different C++ compiler per variant.
[targets.hello.variants.clang]
compiler = "clang"
[targets.hello.variants.gcc13]
compiler = "gcc13"
```

### `static_library` — C, C++, or Rust archive

```toml
[targets.greet]
kind           = "static_library"
sources        = ["src/greet.cpp"]
includes       = ["include"]
public_headers = ["include"]         # dirs whose contents are copied to $out/include
cxxflags       = ["-std=c++17", "-Wall"]
deps.nixpkgs   = ["fmt"]
```

- Same language inference as `executable`, but limited to `c`, `cxx`, and
  `rust` (Go/Zig static libraries are out of scope for v1 — use `custom` if
  you need them).
- Rust archives are built with `rustc --crate-type=staticlib`; the result is
  `lib<name>.a` (so it links naturally into a downstream C/C++ executable
  via `deps.internal`).
- Output: `$out/lib/lib<name>.a` and `$out/include/<public_headers…>`.
- Downstream targets that list this in `deps.internal` automatically get the
  include path and the archive on the link line.

> Nim is now just another language for `kind = "executable"` — drop a `.nim`
> file in `sources` and the `nim` toolchain is auto-pulled from nixpkgs.
> See `executable` above for the Nim example. The earlier `nim_executable`
> kind has been retired.

### `shared_library` — C, C++, or Rust shared object

```toml
[targets.mylib]
kind           = "shared_library"
sources        = ["src/mylib.cpp"]
public_headers = ["include"]
cxxflags       = ["-std=c++17", "-Wall"]
```

- Build line: `$CXX -shared -fPIC … -o lib<name>.so` (analogous for C and
  Rust's `--crate-type=cdylib`).
- Output: `$out/lib/lib<name>.so` and `$out/include/<public_headers…>`.
- Same language constraints as `static_library` (`c`, `cxx`, `rust`); same
  per-language flag fields.
- macOS produces `.so` for cross-platform parity. If you need `.dylib`
  conventions specifically, `kind = "custom"` is the right escape hatch.

### `test` — sandboxed (default) or host-side test, discovered by `rigx test`

```toml
# Default: sandbox = true. Runs as its own Nix derivation, fully
# hermetic, and the result is cached on input hash — unchanged inputs
# means an instant pass without re-running the script.
[targets.fmt_check]
kind          = "test"
deps.internal = ["my_app"]
script        = """
${my_app}/bin/my_app --self-test
diff -u expected.txt <(${my_app}/bin/my_app --print)
"""

# Opt out of the sandbox when a test needs the host: invoke an
# `output/`-symlinked binary, talk to a real database, fight for a
# port, etc. No caching; you own concurrency safety.
[targets.integ_db]
kind         = "test"
sandbox      = false
exclusive    = true                       # see below
deps.nixpkgs = ["postgresql"]
script       = """
pg_ctl -D $TMPDIR/db start
trap 'pg_ctl -D $TMPDIR/db stop' EXIT
./output/myapp/bin/myapp --integration
"""
```

- **`sandbox = true` (default)**: the test becomes a Nix derivation.
  Same isolation guarantees as every other build kind — clean rootfs,
  fresh `$HOME`/`$TMPDIR`, no host filesystem, no network. Success means
  the build succeeds; rigx synthesizes a minimal `$out` for Nix.
  **Automatic caching**: rigx never re-runs an unchanged sandboxed test.
- **`sandbox = false`**: the test runs host-side via
  `nix shell …#deps --command bash -c <script>` with `cwd = project root`.
  No caching; whatever's in your shell environment is in scope.
  `exclusive = true` blocks parallelism for tests that touch shared host
  state (a fixed port, a temp dir, a daemon) — under `rigx test -j N`,
  exclusive tests always run alone in a serial phase before the pool
  spins up. Sandboxed tests don't need `exclusive`; the sandbox provides
  isolation.
- **Both flavors:** `rigx test` discovers them all; reach into dep
  `$out`s via `${dep_name}` interpolation; same `deps.internal` /
  `deps.nixpkgs` / `deps.git` semantics.
- Excluded from `rigx build` default. `rigx build <test>` errors with
  a pointer to `rigx test`.

**Quick guide.** Default to `sandbox = true` — it's safer (hermetic),
faster (cached), and parallel-safe out of the box. Reach for
`sandbox = false` when the test must reach into `./output/`, hit a real
external service, or otherwise step outside Nix's reproducibility
guarantees.

### `python_script` — Python entry-point + uv-managed venv

```toml
[targets.greet_py]
kind            = "python_script"
sources         = ["src/greet.py"]   # sources[0] is the entry; all sources are bundled
python_version  = "3.12"             # → pkgs.python312 from nixpkgs
python_project  = "."                # dir with pyproject.toml + uv.lock (relative to root)
python_venv_hash = "sha256-..."      # optional; see workflow below
python_venv_extra = ["vendor", "wheels/*.whl"]  # optional; vendored wheels / path-deps
```

- Output: `$out/bin/<name>` — a launcher that invokes the pinned Python
  interpreter with the venv's `site-packages` prepended to `PYTHONPATH`,
  plus the entry script's directory.
- Dependencies come from `pyproject.toml` + `uv.lock`, **not** from
  `deps.nixpkgs`. `uv sync --frozen` runs inside a fixed-output derivation
  (FOD) that has network access for PyPI.

**Vendored wheels / path-deps via `python_venv_extra`.** By default the venv
FOD only sees `pyproject.toml` + `uv.lock`. That keeps the FOD hash a
function of the lockfile alone — fast, stable. To make additional files
visible to `uv sync` (typical case: an offline `vendor/` of pinned wheels,
a `find-links` directory, or a `path = "../local-pkg"` dep), list them in
`python_venv_extra`:

```toml
python_venv_extra = ["vendor", "wheels/*.whl"]
```

- Paths are **relative to `python_project`** (the directory holding
  `pyproject.toml`). They land in the FOD source at the same relative
  position, so `pyproject.toml`'s `tool.uv.find-links = ["vendor"]`
  resolves naturally.
- Both `$vars.X` substitution and globs (`*`, `**`, `?`, `[…]`) are
  supported, the same as in `sources`.
- Tradeoff: anything you list here re-runs `uv sync` (and shifts the
  venv hash) when it changes. That's exactly what you want for vendored
  wheels (deterministic), and not what you want for stray sibling files —
  list only what `uv sync` actually needs.

**`python_venv_hash` workflow** (optional but recommended):

1. Write `pyproject.toml` with your deps; run `rigx pkg uv -- lock` (or `uv lock`
   if you have uv installed locally) to produce `uv.lock`.
2. First `rigx build <target>` fails with a hash-mismatch error:
   ```
   error: hash mismatch in fixed-output derivation ...
            specified: sha256-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=
               got:    sha256-<real-hash>
   ```
3. Paste the `got:` hash into `python_venv_hash` and rebuild.
4. When `uv.lock` changes, the hash changes — repeat.

If omitted, every build fails deterministically on hash mismatch.

### `run` — execute an artifact, capture its output files

```toml
# Invoke another target you built
[targets.greeting]
kind    = "run"
run     = "gen_greeting"             # internal target name
args    = ["--name", "Massimo", "--out", "greeting.txt"]
outputs = ["greeting.txt"]           # files (or directories) captured to $out/

# Invoke a nixpkgs tool from PATH
[targets.headers_zip]
kind         = "run"
run          = "zip"                 # not an internal target → looked up on PATH
deps.nixpkgs = ["zip"]               # supplies zip on PATH in the sandbox
args         = ["-r", "headers.zip", "include"]
outputs      = ["headers.zip"]

# Consume another run target's artifact via Nix interpolation
[targets.unpack_headers]
kind           = "run"
run            = "unzip"
deps.nixpkgs   = ["unzip"]
deps.internal  = ["headers_zip"]     # declare the build-order dep
args           = ["-d", "extracted", "${headers_zip}/headers.zip"]
outputs        = ["extracted"]       # directory; cp -r handles it
```

- `run` resolves as an internal target first (`${name}/bin/<name>`), otherwise
  as a bare command looked up on PATH. Use `deps.nixpkgs` to supply PATH tools.
- `args` are shell-quoted by rigx. `${other_target}` inside an arg is a
  Nix interpolation that expands to the dependency's store path at flake
  evaluation time.
- `outputs` are captured with `cp -r`, so directories work.

### `custom` — user-supplied build/install scripts (escape hatch)

Use `custom` when the first-class kinds aren't enough — e.g. a Cargo
workspace, a `cmake` project, a `make`-driven external build, generated
sources, or a *post-build orchestration* like packaging multiple targets
into a single artifact.

```toml
# Stitch already-built targets into a release tarball, using ${dep} to
# reach into each dep's $out and gnutar/gzip from nixpkgs.
[targets.release_bundle]
kind          = "custom"
deps.internal = ["hello", "hello_go", "hello_rust"]
deps.nixpkgs  = ["gnutar", "gzip"]
install_script = """
mkdir -p $out
staging=$TMPDIR/release
mkdir -p $staging
cp ${hello}/bin/hello           $staging/
cp ${hello_go}/bin/hello_go     $staging/
cp ${hello_rust}/bin/hello_rust $staging/
tar -C $TMPDIR -czf $out/release.tar.gz release
"""
# native_build_inputs = ["makeWrapper"]   # optional; mapped to nativeBuildInputs
```

- `install_script` is required; `build_script` is optional.
- Scripts run in a standard Nix stdenv sandbox with `src` already unpacked
  and the cwd set to the source root. `$out`, `$TMPDIR`, `$HOME` are available
  (stdenv's default `HOME=/homeless-shelter` is read-only — redirect it for
  tools like Go, Cargo, Nim that want a writable home).
- All `deps.*` entries end up on `buildInputs`, so their binaries are on
  PATH and their libraries/headers are on the usual compile/link paths.
- Literal `${` inside a script must be written as `''${` (Nix indented-string
  escape) because `${var}` is interpreted by Nix.

### `script` — host-side task (publish, deploy, release)

```toml
[targets.publish]
kind         = "script"
deps.nixpkgs = ["uv"]
script = """
rm -rf dist
uv build
uv publish
"""
```

Unlike every other kind, a `script` target **runs on the host**, not inside
a Nix build sandbox. It executes via `nix shell nixpkgs/<pinned-ref>#<deps> --
command bash -eo pipefail -c "<script>"` in the project root.

**Invoke with `rigx run`, not `rigx build`:**
```
rigx run publish
rigx run publish -- --dry-run prod    # extra args become $1, $2, … in the script
```
Script targets produce no artifact and therefore aren't buildable. If you name
one in `rigx build`, you'll get an error pointing at `rigx run`.

Anything after `--` is forwarded to the script as positional arguments — use
`"$@"` (or `$1`, `$2`, …) inside the `script` body to consume them. The target
name is `$0`. Without `--`, the script runs with no extra arguments.

- Intended for side-effecting tasks: publishing, deploying, pushing images,
  running end-to-end tests against real systems.
- `deps.nixpkgs` tools come from the project's pinned nixpkgs, so the
  environment is still reproducible even though the script is not sandboxed.
- Excluded from `rigx build` entirely — they're listed by `rigx list` for
  discoverability but only runnable via `rigx run`.
- Produces no `output/<target>` symlink — side effects happen in place.
- Variants, `$out`, and the Nix store are not available — the script runs as
  a plain bash `-eo pipefail` block in your current shell environment (with
  tools on PATH, `$HOME`, etc.).

Credentials needed by the script (`UV_PUBLISH_TOKEN`, cloud CLI creds, SSH
keys, …) are read from your shell environment — set them before invoking
`rigx run <target>`.

### `capsule` — runnable container artifact (experimental)

```toml
[targets.greeter]
kind          = "capsule"
backend       = "lite"
deps.internal = ["hello_go"]
deps.nixpkgs  = ["coreutils"]
entrypoint    = "${hello_go}/bin/hello_go"
ports         = [5000]
```

A capsule packages a rigx-built artifact as a container that mounts
the host's `/nix/store` at runtime — `FROM scratch` image, kilobytes
in size, no NixOS, no systemd. After `rigx build greeter`, run with
`./output/greeter/bin/run-greeter` (needs `docker` or `podman`).

The full schema, the `rigx.capsule` Python orchestrator, and the
`rigx.testbed` network simulator are documented under
[Advanced features: capsules and testbeds](#advanced-features-capsules-and-testbeds).

---

## Cross-compilation

Set `target = "<triple>"` on an `executable` or `static_library` and rigx
routes the build through the right cross toolchain. No `kind = "custom"`,
no zigcc shim to maintain by hand. Works for c, cxx, go, zig, and nim.

```toml
[targets.hello_c_arm64]
kind    = "executable"
sources = ["src/hello.c"]
target  = "aarch64-linux"      # → pkgs.pkgsCross.aarch64-multiplatform.stdenv

[targets.hello_nim_arm64]
kind      = "executable"
sources   = ["src/hello.nim"]
target    = "aarch64-linux"    # → auto-emit zigcc shim, set --cpu/--os
nim_flags = ["-d:release"]
```

What each backend does with `target`:

| Language | Behavior |
|---|---|
| `c`, `cxx`            | Routes through `pkgs.pkgsCross.<x>.stdenv` (or `<compiler>Stdenv`). $CC/$CXX point at the cross-gcc/cross-clang. |
| `go`                  | Sets `GOOS=…`, `GOARCH=…`, `CGO_ENABLED=0` before `go build`. |
| `zig`                 | Adds `-target <triple>` to `zig build-exe` (Zig is a cross-compiler natively). |
| `nim`                 | Auto-emits a `zigcc` shim wrapping `zig cc -target …`, points Nim at it via `--cc:clang --clang.exe:zigcc`, sets `--cpu` / `--os`. Pulls `pkgs.zig` automatically. Recipe per the [nim_zigcc guide](https://codeberg.org/janAkali/nim_zigcc_guide). |
| `rust`                | (not yet wired through `target` — fall back to `kind = "custom"` for cross-Rust until then). |

Built-in target aliases (resolve to the right `pkgsCross.<x>` / Zig triple
/ `GOOS`/`GOARCH`):

| `target = …`         | Meaning                                              |
|----------------------|------------------------------------------------------|
| `aarch64-linux`      | ARM64 Linux (musl on Zig/Nim, glibc on c/cxx)        |
| `armv7-linux`        | ARMv7 hard-float Linux                               |
| `x86_64-linux-musl`  | x86_64 Linux, musl libc                              |
| `x86_64-windows`     | x86_64 Windows (mingw-w64)                           |

Anything else passes through verbatim (you're responsible for the spelling
matching whatever the underlying tool expects).

Use variants to produce both native and cross binaries from the same source:

```toml
[targets.hello]
kind    = "executable"
sources = ["src/hello.c"]

[targets.hello.variants.arm64]
target = "aarch64-linux"

[targets.hello.variants.windows]
target = "x86_64-windows"
```

Then `rigx build hello@arm64`, `rigx build hello@windows`, or just
`rigx build hello` for all of them.

---

## Inspecting the build graph

`rigx graph <target>` prints a [Mermaid](https://mermaid.js.org/) `graph TD`
for the dep tree rooted at the named target. GitHub renders Mermaid code
blocks inline, so the simplest way to use it is:

```
rigx graph release_bundle > graph.md
# or paste into any Mermaid-aware viewer (GitHub PR, Obsidian, mermaid.live, …)
```

Running it on the `release_bundle` target from `example-project/` yields:

```mermaid
graph TD
    release_bundle["release_bundle [custom]"]
    hello["hello [executable]"]
    greet["greet [static_library]"]
    pkg_fmt(["pkgs.fmt"])
    hello_go["hello_go [executable]"]
    hello_rust["hello_rust [executable]"]
    hello_zig["hello_zig [executable]"]
    pkg_gnutar(["pkgs.gnutar"])
    pkg_gzip(["pkgs.gzip"])
    release_bundle --> hello
    hello --> greet
    greet --> pkg_fmt
    hello --> pkg_fmt
    release_bundle --> hello_go
    release_bundle --> hello_rust
    release_bundle --> hello_zig
    release_bundle --> pkg_gnutar
    release_bundle --> pkg_gzip
    classDef internal fill:#e1f5fe,stroke:#01579b
    classDef nixpkgs fill:#f3e5f5,stroke:#4a148c
    classDef git fill:#fff3e0,stroke:#e65100
    classDef cross_flake fill:#e0f7fa,stroke:#006064,stroke-dasharray:4 2
    class release_bundle,hello,greet,hello_go,hello_rust,hello_zig internal
    class pkg_fmt,pkg_gnutar,pkg_gzip nixpkgs
```

Visual key:

- **Rectangles** — internal targets in this flake (your own and any
  `[modules]`-merged ones).
- **Stadium shapes (rounded ends)** — leaf dependencies: `pkgs.<name>`
  (nixpkgs), `git:<name>` (git flake inputs), or `<localdep>.<target>`
  (`[dependencies.local.*]`, dashed cyan border).
- **Edges** — `A --> B` means *A depends on B* (B is built first).

Notes:

- `target@variant` works as input but the variant suffix is stripped — rigx
  variants vary *flags*, not deps, so the graph is identical across
  variants.
- A-style cross-flake refs render as opaque leaves. To see *their* graph,
  `cd` into the sibling project and run `rigx graph` there — that flake
  has the metadata.
- The `run` field on a `kind = "run"` target is treated as an implicit
  dep edge to the named target.

---

## Workflow tools

A handful of small commands round out day-to-day use:

### `rigx new <kind> <name>` — scaffold a target

Appends a `[targets.<name>]` block to `rigx.toml` and writes a stub source
file (when applicable). Refuses to overwrite an existing target name or
existing files.

```
rigx new executable hello                 # cxx default; src/hello.cpp
rigx new executable tool --language go    # src/tool.go
rigx new static_library mylib             # src/mylib.cpp + include/mylib.h
rigx new test smoke                       # kind=test stub; run via `rigx test`
rigx new run gen --run my_tool            # kind=run, invokes my_tool
```

Supported kinds: `executable`, `static_library`, `python_script`,
`custom`, `script`, `run`, `test`. Languages for the first two:
`c`, `cxx`, `go`, `rust`, `zig`, `nim`.

### `rigx watch [target …]` — rebuild on change

Polls the project tree (skipping `output/`, `.git`, `flake.lock`) every
0.5s and rebuilds the named targets whenever a file's mtime bumps. Cheap
implementation deliberately — no `inotify` / `fsevents` dependency, works
identically on Linux/macOS. Ctrl-C exits.

```
rigx watch              # all targets
rigx watch hello        # one target
rigx watch hello@arm64  # specific variant
```

### `rigx test [-j N] [target …]` — discover-and-run tests

Discovers every `kind = "test"` target — both sandboxed (default) and
host-side (`sandbox = false`). Tests are *excluded* from `rigx build`;
this is the canonical entry point. Reports a PASS/FAIL summary; exit
code is the worst test's exit code so CI can gate on it.

Filters accept literal names and **fnmatch globs** (`*`, `?`, `[…]`): a
target runs if it matches *any* filter. No filter = `*` = all.

```
rigx test                # run all test targets, sequentially
rigx test smoke          # literal name
rigx test 'unit_*'       # all tests starting with `unit_`
rigx test smoke 'integ_*' # mix literal + glob
rigx test '*'            # explicit "all" (same as no args)
rigx test -j 4           # up to 4 concurrently (per phase, see below)
```

**Phases under `-j N`:**

1. **Sandboxed tests** (`sandbox = true`, default) run first, in a thread
   pool of size N. Each invokes `nix build` against the test's derivation;
   sandbox isolation makes parallelism always safe. Output captured.
2. **Host tests with `exclusive = true`** run sequentially, streaming.
3. **Other host tests** (`sandbox = false`, not exclusive) run in a
   thread pool of size N. Output captured per-test and printed on
   completion.

Sequential mode (no `-j` or `-j 1`) streams every target's output live
in declaration order — sandboxed first, then host.

Quote globs in your shell so the shell doesn't expand them against
filesystem paths first.

### `rigx fmt [--write]` — canonical TOML

Re-emits `rigx.toml` in a stable shape: top-level sections in fixed
order, schema-aware field ordering within each table, `=` aligned per
section. Useful for code review and to settle nit-pick disagreements.

```
rigx fmt                 # print canonical to stdout
rigx fmt --write         # overwrite rigx.toml in place
```

> Caveat: comments are not preserved. Python's stdlib `tomllib` strips
> them on parse and re-emitting them faithfully needs a
> comment-preserving parser. Pipe through stdout first if you have
> comments you care about.

### `rigx build --json` — machine-readable output

Emits a JSON array of `{attr, output}` instead of the human-readable
list, for CI/scripts that want to consume rigx's output.

```
rigx build --json | jq '.[] | select(.attr == "hello") | .output'
```

### Generated `flake.nix` / `flake.lock` and your git repo

rigx writes `flake.nix` and `flake.lock` next to your `rigx.toml`. When
their content changes inside a git work-tree, rigx prints a one-line
hint to stderr:

```
[rigx] regenerated flake.nix — commit when stable so future runs reuse the same lock.
```

That's all it does — rigx never touches the git index or commits anything
on your behalf. Commit both files once they've stabilized; future
invocations will reuse the same lock and stop printing the reminder
until something changes again.

The hint is suppressed outside a git work-tree (no actionable advice).

---

## Advanced features: capsules and testbeds

> Status: experimental. Linux-only. Capsule runners need `/nix/store`
> and the Nix daemon socket mountable (the standard nix multi-user
> setup), and `docker` or `podman` on PATH.

### Capsule schema

A capsule packages a rigx-built artifact as a container that mounts the
host's `/nix/store` and Nix daemon socket at runtime. The image itself
is `FROM scratch` and ships only `/etc/{nix.conf,passwd,group}` plus
mount-anchor stub directories — every binary in the container,
including bash and the user's entrypoint, is reachable through the
shared host store. This is the [unofficialtools/nix-docker](https://github.com/unofficialtools/nix-docker)
shape: kilobyte-sized images, no NixOS, no systemd, no duplication
between host and container.

```toml
[targets.greeter]
kind          = "capsule"
backend       = "lite"                # `lite` (container) or `qemu` (NixOS VM)
deps.internal = ["hello_go"]          # rigx-built deps reachable as ${name}
deps.nixpkgs  = ["coreutils"]         # PATH inside the container; opt-in
entrypoint    = "${hello_go}/bin/hello_go --port $PORT"
ports         = [5000]
hostname      = "greeter"             # default: target name
env           = { PORT = "5000", LOG_LEVEL = "info" }
```

`deps.nixpkgs` controls the container's `PATH` — strict opt-in. Each
listed nixpkgs attr's `/bin` is colon-joined and exported as `PATH`
inside the container. Empty `deps.nixpkgs` means an empty `PATH`; the
`entrypoint` still works because Nix interpolates the rigx-built deps'
absolute store paths into the image's `Cmd` at flake-eval time.
External tools (`ls`, `grep`, `curl`) need to be listed explicitly —
no auto-coreutils.

Output layout (`output/<capsule>/`):

```
bin/run-<name>     # docker/podman wrapper: mounts host /nix/store + daemon
bin/shell-<name>   # same mounts but `--entrypoint <bash>` for poking
image/image.tar.gz # the loadable scratch OCI tarball (kilobytes)
manifest.json      # contract: name, backend, ports, image locator, env
```

The runner inherits a few env knobs that orchestrators set:

| env var          | purpose                                          |
|------------------|--------------------------------------------------|
| `RIGX_NAME`      | stable container name (default: random uuid)     |
| `RIGX_DETACH`    | `1` = `docker run -d` (default: foreground)      |
| `RIGX_NETWORK`   | join an existing docker network                  |
| `RIGX_PUBLISH`   | `host:cont,host:cont,…` port forwards            |
| `RIGX_ENV`       | `K=V,K=V,…` extra env vars                       |

### `backend = "nixos"` — NixOS userspace under systemd in a container

A nixos capsule runs a real NixOS userspace inside a docker/podman
container with **systemd as PID 1** — so declarative services from
`nixos_modules` actually run, units start in dependency order, journald
collects logs. Same FROM-scratch-ish image and host-store mount as
`lite`, but the user's entrypoint runs as a systemd one-shot service
rather than the container's `Cmd`.

```toml
[targets.web]
kind          = "capsule"
backend       = "nixos"
deps.internal = ["web_bin"]
deps.nixpkgs  = ["coreutils", "curl"]
entrypoint    = "${web_bin}/bin/web --port 8080"
ports         = [8080]
hostname      = "webcap"
nixos_modules = ["vm/openssh.nix"]   # standard NixOS modules
```

When to pick it (vs the other two):

| Backend | PID 1                | NixOS services? | Boot time | Runner needs |
|---------|----------------------|-----------------|-----------|--------------|
| `lite`  | your entrypoint      | no              | seconds   | docker/podman |
| `nixos` | systemd              | yes             | a few seconds (systemd) | docker/podman + `--privileged` (handled by runner) |
| `qemu`  | systemd in NixOS VM  | yes             | tens of seconds (kernel + init) | qemu (+ KVM on Linux) |

Output layout (`output/<capsule>/`):

```
bin/run-<name>     # docker/podman runner (--privileged, mounts host /nix/store)
image/image.tar.gz # OCI tarball whose Cmd is `${toplevel}/init`
manifest.json      # contract: name, backend, ports, image locator
```

The runner takes the same `RIGX_*` env-var contract as `lite`. The
`--privileged` flag is required so systemd can manage cgroups and write
to `/run`, `/tmp`, `/var/log` (the runner mounts those as tmpfs). On
hosts where you can't grant `--privileged`, you'd need to pin specific
caps and mounts manually — that's not in v1.

`nixos_modules` works exactly like the qemu backend (see below) — list
NixOS module files to splice into the system evaluation.

### `backend = "qemu"` — full NixOS VM

A qemu capsule boots a real NixOS VM under qemu. The user's entrypoint
runs as a systemd one-shot service inside the VM, `deps.nixpkgs` land in
`environment.systemPackages`, declared `ports` are forwarded host→guest
via qemu user-mode networking. Use this when you need a real kernel,
real init, or a different CPU architecture from the host.

```toml
[targets.hello_arm_capsule]
kind          = "capsule"
backend       = "qemu"
target        = "aarch64-linux"        # ARM VM on an x86_64 host
deps.internal = ["hello_nim_arm64"]
entrypoint    = "${hello_nim_arm64}/bin/hello_nim Massimo && systemctl poweroff"
hostname      = "armcap"
ports         = [22]                   # forwarded to host via qemu hostfwd
```

`target = "<triple>"` (same alias map used for cross-compilation) shifts
the VM's NixOS evaluation to a different system, so the same capsule
declaration produces an ARM VM regardless of the host arch. Building it
needs aarch64 builders or `boot.binfmt.emulatedSystems = [ "aarch64-linux" ]`
on the host; the *declaration* is portable.

Output layout (`output/<capsule>/`):

```
bin/run-<name>      # bash wrapper that execs the underlying NixOS vm script
system/vm-script    # symlink into the NixOS-VM derivation (kernel, initrd, ...)
manifest.json       # contract: name, backend, ports, image locator
```

v1 uses NixOS's `system.build.vm`, which 9p-mounts the host's
`/nix/store` rather than baking a standalone qcow2 — fast iteration,
host-store-tied, requires KVM on Linux. A self-contained qcow2 layout
is reserved for follow-on work when mixed-backend labs need the VM to
run on a different host. The Python `rigx.capsule.start()` orchestrator
and `rigx.testbed` integration are also deferred — for v1, qemu
capsules work via `rigx build` + `output/<name>/bin/run-<name>`.

#### Customizing the VM with `nixos_modules`

The `qemu` and `nixos` backends share the same configuration model: the
"image" isn't a separate base image — it's a NixOS system evaluated
from your project's pinned `nixpkgs` ref, plus a small rigx-generated
module (your `entrypoint`, `ports`, `deps.nixpkgs`, hostname). To go
beyond that — enable services, declare extra users, mount volumes, swap
kernel packages, anything you'd put in a `configuration.nix` — list
user NixOS module files via `nixos_modules`:

```toml
[targets.web_capsule]
kind          = "capsule"
backend       = "qemu"            # or "nixos" — same nixos_modules contract
deps.internal = ["web_bin"]
entrypoint    = "${web_bin}/bin/web --port 8080"
ports         = [8080]
nixos_modules = [
    "vm/openssh.nix",        # literal paths, project-relative
    "vm/extras/*.nix",       # globs are expanded at config-load time
]
```

Each entry is an ordinary NixOS module file:

```nix
# vm/openssh.nix
{ ... }: {
  services.openssh.enable = true;
  services.openssh.settings.PermitRootLogin = "yes";
  users.users.root.password = "rigx";
}
```

The modules are spliced into `eval-config.nix` alongside the
rigx-generated module. They can reference `pkgs`, `lib`, `config` the
usual way — same as a regular NixOS configuration. Available on
`backend = "qemu"` and `backend = "nixos"`; using `nixos_modules` on a
`lite` capsule is a config error (lite is a FROM-scratch container
with no NixOS to configure).

### Driving capsules from Python — `rigx.capsule`

For tests and orchestration, rigx ships a small Python helper:

```python
from rigx.capsule import start

with start("greeter", publish={5000: 5050}) as cap:
    cap.wait_for_port(5000, timeout=10)
    # ... talk to 127.0.0.1:5050
    print(cap.logs())
```

`start(name)` finds `output/<name>/`, invokes the runner in detached
mode, and returns a `Capsule` handle. `wait_for_port` polls the host-
mapped port until something accepts. `cap.exec([…])` runs `docker exec`
inside the container. Context exit calls `docker stop` on the
container.

Supported backends:

- `lite` and `nixos` — docker/podman-shaped. `start()`, `stop()`,
  `exec()`, `logs()`, `wait_for_port()`, and the testbed integration
  all work transparently.
- `qemu` — supported with caveats. `start()` runs the runner via
  `Popen` (qemu is a child process of the test); `stop()` terminates
  it; `logs()` reads the captured console; `wait_for_port()` works
  through the forwarded host ports. **`exec()` is not available** —
  the VM has no docker-shell-out equivalent. To run a command inside
  a qemu capsule, enable SSH via `nixos_modules` and connect through
  a forwarded port.

For testbed use specifically, `RIGX_PUBLISH` is translated by each
backend's runner into the right port-forward primitive: `docker -p`
flags for lite/nixos, `QEMU_NET_OPTS=hostfwd=…` for qemu. The testbed
sees the same `bindings()` interface across all three.

### Multi-capsule tests with faults — `rigx.testbed`

Real integration tests want multiple capsules talking to each other,
plus the ability to inject faults (drop, delay, corrupt, partition).
`rigx.testbed.Network` is a userspace TCP/UDP proxy with a fault-rule
chain that sits between capsules:

```python
from rigx.capsule import start
from rigx.testbed import Network

with Network() as net:
    net.declare("sim", listens_on=[5000])
    net.declare("fc",  listens_on=[5001])
    net.link("sim", "fc", to_port=5001)   # sim → fc:5001

    with start("simulator",       **net.bindings("sim")) as sim, \
         start("flight_computer", **net.bindings("fc"))  as fc:
        sim.wait_for_port(5000)
        fc.wait_for_port(5001)

        # Happy path
        # ...

        # 50% drop on sim→fc for the duration of the block
        with net.fault("sim", "fc", drop_rate=0.5):
            # ...

        # 100ms latency with ±20ms jitter
        with net.fault("sim", "fc", delay_ms=100, jitter_ms=20):
            # ...

        # Bidirectional partition
        with net.partition(["sim"], ["fc"]):
            # ...
```

`bindings("sim")` returns kwargs ready for `start()`:
- `publish` maps each declared listening port to a testbed-allocated
  host endpoint (`(addr, port)` tuple), so `wait_for_port` works.
- `env` exposes peer endpoints as `<DST>_<PORT>_ADDR` env vars (e.g.
  `FC_5001_ADDR=127.0.0.1:5023`). The capsule's entrypoint reads
  these to know where to connect.

### Backend support and mixing

The testbed is a userspace L4 proxy — it never talks to the capsule
directly, only to host-side ports. So all three capsule backends
(`lite`, `nixos`, `qemu`) plug into the same `Network()` and the
**same testbed can mix them**: e.g. a fast `lite` capsule running a
test driver next to a `nixos` capsule running real systemd services
next to a `qemu` capsule running an aarch64 firmware. The proxy, fault
chain, and distinct-loopback addressing apply uniformly.

What each backend supports through `rigx.capsule.start()`:

| Backend | start / stop / wait_for_port | logs        | exec        | Boot time | Network transport |
|---------|------------------------------|-------------|-------------|-----------|-------------------|
| `lite`  | ✅                           | `docker logs` | `docker exec` | seconds | docker `-p` flags |
| `nixos` | ✅                           | `docker logs` | `docker exec` | a few seconds (systemd) | docker `-p` flags |
| `qemu`  | ✅                           | captured tempfile | ❌ NotImplementedError | tens of seconds (kernel + init) | `QEMU_NET_OPTS=hostfwd=…` |

The testbed renders `bindings()` into the same `RIGX_PUBLISH` env-var
shape regardless of backend — each backend's runner translates that
to the right primitive (docker port-forward flags or qemu hostfwd
rules). The `RIGX_PUBLISH` contract is the seam.

Caveats specific to qemu in a testbed:

- **Slow `wait_for_port`** — bump `timeout=` to 60-120s; the VM has to
  finish booting before its services bind. lite/nixos peers can use
  the default 10s.
- **No `cap.exec()`** — there's no docker-shell-out equivalent. If
  your test needs to poke inside the VM (drop a file, trigger a
  signal, read `/proc`), enable SSH via `nixos_modules`, forward a
  port for it, and connect from the test through the forwarded port.
- **Cross-arch (`target = "aarch64-linux"`) needs host setup** —
  `boot.binfmt.emulatedSystems` on NixOS, or qemu-user-static + nix
  `extra-platforms` elsewhere. rigx prints a distro-agnostic hint
  when the build fails for this reason.
- **`network` parameter is ignored** — qemu uses its own user-mode
  networking. The testbed doesn't pass `network`, so this only
  matters for hand-rolled `start(network="…")` calls.

### Distinct loopback addresses per capsule

By default every capsule shares `127.0.0.1` and is distinguished by
port. To give each capsule a real distinct IP — useful when capsule
code asserts on source IPs, hardcodes addresses, or needs subnet
semantics — pass a `subnet` (must be inside `127.0.0.0/8`) to
`Network`:

```python
with Network(subnet="127.0.10.0/24") as net:
    net.declare("sim", listens_on=[5000])    # auto: 127.0.10.2
    net.declare("fc",  listens_on=[5001])    # auto: 127.0.10.3
    # Or pin explicitly: declare("sim", address="127.0.10.10", …)
    net.link("sim", "fc", to_port=5001)
    # ...
```

The testbed binds each capsule's listener on its own loopback alias
and routes the proxy's upstream socket through the source's address
before `connect()`, so the destination sees the real source IP — not
the proxy's. Linux routes all of `127.0.0.0/8` to `lo` automatically.
On macOS, run `sudo ifconfig lo0 alias 127.0.X.Y` once per address
the testbed will use.

### UDP links

Capsules that talk over UDP — telemetry buses, NTP-style protocols,
DNS — declare UDP listening ports separately and link with
`proto="udp"`:

```python
with Network(subnet="127.0.10.0/24") as net:
    net.declare("sim", listens_on=[5000])           # TCP
    net.declare("fc",  udp_listens_on=[5001])        # UDP
    net.link("sim", "fc", to_port=5001, proto="udp")

    with start("simulator",       **net.bindings("sim")) as sim, \
         start("flight_computer", **net.bindings("fc"))  as fc:
        # Capsules may speak both protocols; declare both lists and
        # link both protos. TCP and UDP port spaces are independent
        # — same port number works for both.
        ...
```

The UDP forwarder maintains a per-`(src_addr, src_port)` session
table so reply datagrams route back to the right sender. Source-IP
visibility on the forward path works exactly like TCP — the
upstream socket binds on the source's address before `sendto`. Reply
datagrams come back from the proxy's listener address (capsule code
that uses `connect()` with strict source-checking sees the proxy,
not the destination — symmetric reply-path src-IP visibility is
deferred).

Fault rules apply to UDP datagrams the same way they do to TCP byte
chunks: `drop_rate` discards datagrams, `delay_ms` holds them up,
`corrupt_rate` flips a byte, `partition()` blocks the link.
`net.fault(src, dst, proto="udp", ...)` scopes a fault to the UDP
link if both protocols exist between the same pair.

Outbound UDP env vars get a `_UDP` suffix so they don't collide
with TCP equivalents: `FC_5001_ADDR` is TCP, `FC_5001_UDP_ADDR` is
UDP.

### Worked example: TCP + UDP fault injection

A single test that drives a control-plane (TCP) link and a telemetry
(UDP) link between the same pair of capsules, exercising fault
injection on both protocols:

```python
# tests/flight_loop.py
from rigx.capsule import start
from rigx.testbed import Network

with Network(subnet="127.0.10.0/24") as net:
    # sim sends control commands over TCP/5001 and pushes telemetry
    # over UDP/6000. fc accepts both.
    net.declare("sim", listens_on=[5000], udp_listens_on=[6000])
    net.declare("fc",  listens_on=[5001], udp_listens_on=[6000])

    net.link("sim", "fc", to_port=5001)                  # TCP control
    net.link("sim", "fc", to_port=6000, proto="udp")     # UDP telemetry

    with start("simulator",       **net.bindings("sim")) as sim, \
         start("flight_computer", **net.bindings("fc"))  as fc:
        sim.wait_for_port(5000)
        fc.wait_for_port(5001)

        # Baseline: both links healthy.
        assert_healthy(sim, fc)

        # 30% packet loss on the UDP telemetry link.
        with net.fault("sim", "fc", proto="udp", drop_rate=0.3):
            assert_telemetry_gaps_are_tolerated(fc)

        # 200ms latency + 50ms jitter on TCP control; UDP stays clean.
        with net.fault("sim", "fc", proto="tcp",
                       delay_ms=200, jitter_ms=50):
            assert_command_acks_still_arrive(fc, deadline_ms=2000)

        # Corrupt 1% of UDP datagrams — app-level CRC must reject them.
        with net.fault("sim", "fc", proto="udp", corrupt_rate=0.01):
            assert_no_corrupt_telemetry_accepted(fc)

        # Stack faults: lossy UDP and slow TCP at the same time.
        with net.fault("sim", "fc", proto="udp", drop_rate=0.1), \
             net.fault("sim", "fc", proto="tcp", delay_ms=100):
            assert_degraded_but_live(fc)

        # Hard partition blocks both protocols in both directions.
        with net.partition(["sim"], ["fc"]):
            assert_fc_enters_safe_mode(fc, within_ms=500)

        # Recovery once the partition lifts.
        assert_fc_recovers(fc, within_ms=500)
```

Wire it into rigx as a sandbox-disabled `test` target so it can drive
docker:

```toml
[targets.flight_loop]
kind          = "test"
sandbox       = false
deps.internal = ["simulator", "flight_computer"]
deps.nixpkgs  = ["python3"]
script = """
python3 tests/flight_loop.py
"""
```

Then `rigx test flight_loop` runs the suite.

### Caveats

- Linux only. Capsule runners depend on `/nix/store` and (for
  lite/nixos) the Nix daemon socket being mountable — the standard
  nix-multi-user setup on Linux. qemu additionally needs KVM for
  acceptable performance.
- `docker` or `podman` must be on PATH for `lite`/`nixos` capsules at
  runtime. `qemu` capsules need qemu in the closure (rigx pulls it in
  automatically) but no docker.
- L2 simulation deferred. The testbed operates at L4 (TCP byte
  streams / UDP datagrams) — no Ethernet frames, no ARP, no MTU
  effects, no link-layer drops. Bus protocols (CAN, MIL-STD-1553, …)
  belong in their own simulator process running as another capsule,
  not in `rigx.testbed`. Real L2/L3 simulation needs Linux netns +
  veth pairs (option 3 in TODO).
- UDP `delay_ms` is head-of-line on the listener thread — a delayed
  datagram blocks subsequent ones. Acceptable for moderate delays;
  a heap-based scheduler would eliminate this.
- Mixed-backend topologies are supported in principle (the testbed
  is L4-only and doesn't care what's behind a port) but mixing has
  not been heavily exercised in practice — file an issue if you hit
  edge cases with `lite` + `qemu` or `nixos` + `qemu` topologies.
- See [TODO.md](TODO.md) for further deferred items — s6
  multi-service capsules, time control, qcow2-baked qemu disks for
  cross-host portability.

---

## Complete example (matches `example-project/rigx.toml`)

```toml
# Bolierplate #####################################################################

[project]
name = "hello-example"
version = "0.1.0"

[nixpkgs]
ref = "nixos-24.11"

# Simple Examples #################################################################

[targets.greet]
kind = "static_library"
sources = ["src/greet.cpp"]
includes = ["include"]
public_headers = ["include"]
cxxflags = ["-std=c++17", "-Wall"]
deps.nixpkgs = ["fmt"]

[targets.hello]
kind = "executable"
sources = ["src/main.cpp"]
includes = ["include"]
cxxflags = ["-std=c++17", "-Wall"]
ldflags = ["-lfmt"]
deps.internal = ["greet"]
deps.nixpkgs = ["fmt"]

# Examples of Variants ############################################################

[targets.hello.variants.debug]
cxxflags = ["-O0", "-g"]
defines = { DEBUG = "1" }

[targets.hello.variants.release]
cxxflags = ["-O2"]
defines = { NDEBUG = "1" }

# Toolchain-swap variant: `rigx build hello@clang` reuses the same sources
# but routes the build through nixpkgs `clangStdenv` instead of the default.
[targets.hello.variants.clang]
compiler = "clang"
cxxflags = ["-O2"]

# Examples in Dependencies and Artifact Output ####################################

[targets.gen_greeting]
kind = "executable"
sources = ["src/gen_greeting.cpp"]
cxxflags = ["-std=c++17"]

[targets.greeting]
kind = "run"
run = "gen_greeting"
args = ["--name", "Massimo", "--out", "greeting.txt"]
outputs = ["greeting.txt"]

# A run target that invokes a nixpkgs tool (`zip`) to bundle project files.
[targets.headers_zip]
kind = "run"
run = "zip"                       # name resolved via PATH (no such internal target)
deps.nixpkgs = ["zip"]             # provides `zip` on PATH in the sandbox
args = ["-r", "headers.zip", "include"]
outputs = ["headers.zip"]

# A run target that consumes the artifact of another run target.
# ${headers_zip} Nix-interpolates to the store path of that derivation.
[targets.unpack_headers]
kind = "run"
run = "unzip"
deps.nixpkgs = ["unzip"]
deps.internal = ["headers_zip"]    # declares the build-order dependency
args = ["-d", "extracted", "${headers_zip}/headers.zip"]
outputs = ["extracted"]            # a directory; cp -r handles it

# Examples in Other Languages #####################################################

# Nim: language inferred from `.nim` extension; nim toolchain auto-pulled.
[targets.hello_nim]
kind = "executable"
sources = ["src/hello.nim"]
nim_flags = ["-d:release", "--opt:speed"]

[targets.hello_nim.variants.debug]
nim_flags = ["-d:debug", "--debugger:native"]

[targets.hello_nim.variants.release]
nim_flags = ["-d:release", "--opt:speed"]

[targets.greet_py]
kind = "python_script"
sources = ["src/greet.py"]
python_version = "3.12"
python_project = "."            # dir containing pyproject.toml + uv.lock
# Pinned uv-venv FOD hash; bump whenever pyproject.toml or uv.lock changes
# (rigx prints the new hash in the build error so you can paste it back).
python_venv_hash = "sha256-a1eSPty02qsWzhZCEJ+XpTdSNIXsNg6vw+LsZD/kaIo="

# Go: language inferred from `.go` extension; toolchain auto-pulled from nixpkgs.
[targets.hello_go]
kind    = "executable"
sources = ["src/hello.go"]

# Rust: same idea — `.rs` → language=rust, rustc auto-pulled.
[targets.hello_rust]
kind    = "executable"
sources = ["src/hello.rs"]
rustflags = ["-Copt-level=2"]

# Zig: `.zig` → language=zig, zig auto-pulled.
[targets.hello_zig]
kind    = "executable"
sources = ["src/hello.zig"]
zigflags = ["-O", "ReleaseFast"]

# Example of Cross Compilation #####################################################

# ─── Cross-compilation, first-class via `target = …` ────────────────────────
# `target = "aarch64-linux"` routes c/cxx through `pkgsCross.aarch64-multi…
# .stdenv`, sets `GOOS/GOARCH` for go, passes `-target` to zig, and emits a
# zigcc shim for nim. Same target shape, different language, different
# platform — no custom build_script needed.

[targets.hello_c_arm64]
kind    = "executable"
sources = ["src/hello.c"]
target  = "aarch64-linux"
cflags  = ["-O2"]

[targets.hello_nim_arm64]
kind      = "executable"
sources   = ["src/hello.nim"]
target    = "aarch64-linux"
nim_flags = ["-d:release"]

# Example of Capsules and Testbeds #################################################

# Capsule: package an existing rigx-built binary as a runnable container
# in the unofficialtools/nix-docker shape — kilobyte-sized FROM-scratch
# image that mounts the host's /nix/store at runtime so the binary is
# reachable. `${hello_go}` interpolates to the rigx-built target's store
# path. After `rigx build greeter`, drop into the image with
# `./output/greeter/bin/shell-greeter` (needs docker/podman on PATH).
#
# Heavier first build than the rest of this project — nix builds the
# scratch image and the daemon pulls dockerTools dependencies. Skip
# from `rigx build` (no args) by listing other targets explicitly if
# this is a problem.
[targets.greeter]
kind          = "capsule"
backend       = "lite"
deps.nixpkgs  = ["coreutils", "helix"]
deps.internal = ["hello_go"]
entrypoint    = "${hello_go}/bin/hello_go"

# Qemu capsule running a native (host-arch) Go binary inside a NixOS VM.
# No `target` field means the VM is built for whatever system you run
# rigx under — works out of the box on any Linux host with KVM. Use
# this to verify your host's qemu setup before tackling the cross-arch
# variant below.
[targets.hello_x86_capsule]
kind          = "capsule"
backend       = "qemu"
deps.internal = ["hello_go"]
entrypoint    = "${hello_go}/bin/hello_go Massimo && systemctl poweroff"
hostname      = "x86cap"

[targets.test_hello_x86]
kind          = "test"
sandbox       = false
deps.internal = ["hello_x86_capsule"]
deps.nixpkgs  = ["coreutils", "gnugrep"]
script = """
set -euo pipefail
echo "[test_hello_x86] booting x86_64 qemu capsule (this can take a minute)"
out=$(timeout 180 ./output/hello_x86_capsule/bin/run-hello_x86_capsule 2>&1 || true)
echo "$out" | tail -20
echo "$out" | grep -q "Hello, Massimo!"
echo "[test_hello_x86] saw expected greeting"
"""

# Qemu capsule running an ARM binary inside an aarch64 NixOS VM.
# `target = "aarch64-linux"` shifts the VM evaluation to aarch64 so the
# same capsule declaration produces an ARM VM on an x86_64 host. The
# rigx-built ARM binary (cross-compiled via zigcc above) is referenced
# through `${hello_nim_arm64}` and runs natively in the VM's aarch64
# kernel. Requires aarch64 builders or `boot.binfmt.emulatedSystems` on
# the host to actually build; the declaration itself is portable.
[targets.hello_arm_capsule]
kind          = "capsule"
backend       = "qemu"
target        = "aarch64-linux"
deps.internal = ["hello_nim_arm64"]
entrypoint    = "${hello_nim_arm64}/bin/hello_nim Massimo && systemctl poweroff"
hostname      = "armcap"

# Host-side test that boots the ARM capsule, captures its console output,
# and asserts on the greeting. The entrypoint shuts the VM down once
# hello_nim exits so the run terminates without a timeout. `sandbox =
# false` is required — the test launches qemu, which doesn't run inside
# Nix's build sandbox.
[targets.test_hello_arm]
kind          = "test"
sandbox       = false
deps.internal = ["hello_arm_capsule"]
deps.nixpkgs  = ["coreutils", "gnugrep"]
script = """
set -euo pipefail
echo "[test_hello_arm] booting ARM qemu capsule (this can take a minute)"
out=$(timeout 180 ./output/hello_arm_capsule/bin/run-hello_arm_capsule 2>&1 || true)
echo "$out" | tail -20
echo "$out" | grep -q "Hello, Massimo!"
echo "[test_hello_arm] saw expected greeting"
"""

# `custom` is the escape hatch for non-trivial workflows the first-class
# kinds don't cover. Here it stitches together previously-built targets
# into a release tarball — using ${dep} interpolation to reach into each
# dep's $out, gnutar/gzip from nixpkgs, and producing a single artifact.
[targets.release_bundle]
kind          = "custom"
deps.internal = ["hello", "hello_go", "hello_rust", "hello_zig"]
deps.nixpkgs  = ["gnutar", "gzip"]
install_script = """
mkdir -p $out
staging=$TMPDIR/release
mkdir -p $staging
cp ${hello}/bin/hello              $staging/
cp ${hello_go}/bin/hello_go        $staging/
cp ${hello_rust}/bin/hello_rust    $staging/
cp ${hello_zig}/bin/hello_zig      $staging/
tar -C $TMPDIR -czf $out/release.tar.gz release
"""
```

## Running the tests

The repo's own `rigx.toml` declares two test targets, both discovered by
`rigx test` from the repo root:

| Target | Kind | What it covers |
|---|---|---|
| `unittests`            | `kind = "test"` (sandboxed, default)         | Pure-Python unit suite: TOML parser, validator, flake-text generator, builder attribute resolution. No `nix` or network access required at runtime — runs hermetically in a Nix derivation, cached on input hash. |
| `example_project_build`| `kind = "test"`, `sandbox = false`, `exclusive = true` | End-to-end integration: shells out to `nix build` against every target in `example-project/` to catch regressions in flake generation, parallel dispatch, and per-target failure isolation that unit tests can't see. |

```
rigx test                    # both, sequential
rigx test -j 4 unittests     # just the Python suite, parallel-ready
rigx test 'example_*'        # just the end-to-end smoke test
```

You can also run the unit suite directly (no rigx, no nix):

```
python3 -m unittest discover tests -v
```

## Example project

See `example-project/` for a working version of the above.

```
cd example-project
rigx build hello@release
./output/hello-release/bin/hello "friend"
```

## License

BSD 2-Clause License. See [`LICENSE.md`](LICENSE.md) for the full text.

## Credits

Created by Massimo Di Pierro &lt;massimo.dipierro@gmail.com&gt; in collaboration
with Claude (author's own account), in his own free time, with his own resources, for the greater good.
