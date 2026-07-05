# rigx

rigx is a Nix-backed build tool; this repo builds and tests itself with the
in-tree rigx via `rigx.toml`.

## Running tests

**Always run tests through rigx, never bare `python3 -m unittest`:**

```sh
rigx test              # everything: unit tests + example-project smoke test
rigx test unittests    # just the unit tests
```

The `unittests` target runs the suite inside a hermetic Nix build sandbox.
That sandbox is the environment that matters: it has no `/usr/bin/env`, no
host `/bin`, and only the declared deps (`python3`) plus stdenv on PATH.
Tests that pass on the host can still fail there (e.g. scripts relying on
`#!/usr/bin/env` shebangs), so a bare host run is not evidence the suite
passes. When writing tests that execute scripts, resolve interpreters with
`shutil.which()` instead of assuming `/usr/bin/env` exists.

Note: `rigx test` builds from the git tree, so untracked new files must be
`git add`ed before the sandbox can see them.

The `example_project_build` target is the end-to-end smoke test: it builds
every target in `example-project/` with the in-tree rigx. It runs
unsandboxed (needs the host nix daemon) and exclusive (writes
`example-project/flake.nix` and `example-project/output/`).

## Sandbox failure logs

`rigx test` truncates the build log. For the full log of a failed
sandboxed test, use the command it prints:

```sh
nix-store -l /nix/store/<hash>-unittests-<version>.drv
```
