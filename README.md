# procton

A Nix-backed declarative build system. Targets are described in `procton.toml`;
procton generates a Nix flake, which drives sandboxed builds entirely through
the Nix store. Build artifacts materialize only as symlinks under `output/`.

## Features

- **TOML target declarations**: inputs, outputs, internal and external deps.
- **External deps** via pinned `nixpkgs` or `git` flake inputs.
- **Lock file** (`flake.lock`) pins every input revision.
- **Sandboxed builds**: compilation never touches the local filesystem; each
  derivation runs against the Nix store's layered filesystem.
- **Outputs** only appear under `output/` as symlinks into the Nix store.
- **Parameterized targets** via variants (e.g. `debug` / `release`).

## Requirements

- Python 3.11+
- [Nix](https://nixos.org/download) 2.4+ with flakes enabled (procton passes
  `--extra-experimental-features "nix-command flakes"` automatically).

## Usage

From a project directory containing `procton.toml`:

```
procton list                 # list targets
procton lock                 # generate flake.nix and update flake.lock
procton build                # build every target (and variant)
procton build hello          # build one target
procton build hello@release  # build a specific variant
procton flake                # print generated flake.nix (for debugging)
procton clean                # remove output/
```

If procton isn't installed, invoke it as a module:
`PYTHONPATH=/path/to/procton python3 -m procton -C /path/to/project build`.

## procton.toml reference

```toml
[project]
name = "myproject"
version = "0.1.0"

[nixpkgs]
ref = "nixos-24.11"              # any nixpkgs branch, tag, or commit

# Optional external git flake deps
[dependencies.git.mylib]
url = "https://github.com/someone/mylib"
rev = "v1.0.0"                   # branch, tag, or 40-char commit SHA
flake = true                     # must be a flake in this version
attr = "default"                 # attribute inside packages.${system}

[targets.greet]
kind = "static_library"          # executable | static_library
sources = ["src/greet.cpp"]
includes = ["include"]           # -I flags for compilation
public_headers = ["include"]     # copied into $out/include
cxxflags = ["-std=c++17"]
deps.nixpkgs = ["fmt"]

[targets.hello]
kind = "executable"
sources = ["src/main.cpp"]
includes = ["include"]
cxxflags = ["-std=c++17"]
ldflags = ["-lfmt"]
deps.internal = ["greet"]        # refs other targets
deps.nixpkgs = ["fmt"]
deps.git = ["mylib"]             # refs dependencies.git.*

[targets.hello.variants.debug]
cxxflags = ["-O0", "-g"]
defines = { DEBUG = "1" }

[targets.hello.variants.release]
cxxflags = ["-O2"]
defines = { NDEBUG = "1" }
```

Variants append to the base target's flags. When a target has variants,
`procton build <target>` builds all of them; the unqualified name is an
alias for the first variant alphabetically.

## Example project

See `project/` for a working example: a static library + executable that
depend on `fmt` from nixpkgs, with `debug` and `release` variants.

```
cd project
procton build hello@release
./output/hello-release/bin/hello "friend"
```
