# Changelog

## 0.12.0 — 2026-08-13

### New features

- **`rigx remote` — SSH deployment for declarative testbeds.** A
  `kind = "testbed"` target can now carry a `services` table describing a
  supervised multi-capsule deployment (dependencies, restart policy, ports,
  volumes, secrets, health checks) plus an `ssh_url` destination.
  `rigx remote <target> deploy|start|stop|restart|rollback [ssh_url]`
  bootstraps a bare Linux host, builds the referenced capsules, compiles a
  deployment plan, pushes it and every capsule closure with `nix copy`, and
  drives it with systemd. Nothing needs to be preinstalled on the target but
  SSH, sudo, and systemd; no container registry is involved. Still a
  prototype — see the README for the remaining gaps (the plan is assembled
  by rigx rather than by its own `flake.nix`; no sudoers scoping; services
  run rootful).

- **`RIGX_FOREGROUND=1` capsule runner mode.** Attached with no TTY —
  neither `-d` nor `-it`. For running a capsule under a process supervisor,
  which needs the engine to stay in the foreground so it can see the
  container exit, but has no TTY to offer. `RIGX_DETACH` still wins if both
  are set; unset behaves exactly as before.

### Behavior changes

- **Podman is now preferred over Docker when both are installed.**
  `rigx.capsule` and the generated capsule runners try `podman` first and
  fall back to `docker`; previously the order was reversed. Docker-only and
  podman-only hosts are unaffected. On a host with *both*, a container
  started under Docker by an older rigx will not be found by
  `Capsule.stop()`, which shells out to whichever engine is selected now —
  such containers need stopping by hand once, after upgrading.

  The motivation is `rigx remote`: Podman is daemonless, so a deployment's
  services are independent units rather than sharing a single daemon whose
  restart takes all of them down at once. Preferring the same engine locally
  keeps "works locally" a meaningful signal for "works deployed".

## 0.11.0 — 2026-07-25

### Behavior changes

- **`python_script` venvs are now reproducible for path / local-wheel deps,
  which changes their `python_venv_hash`.** For such deps uv writes two
  per-build files into `.dist-info/`: `uv_cache.json`, containing a
  nanosecond wall-clock timestamp (so it differed on *every* build), and
  `direct_url.json`, containing `$NIX_BUILD_TOP` and the target name (stable
  only under a sandboxed build, and never shared between two targets with
  identical `pyproject.toml` + `uv.lock`). Both are hashed into `RECORD`.
  The venv build phase now deletes both files and strips their `RECORD` rows.

  Projects whose deps are all registry wheels are unaffected — uv never wrote
  these files for them, and their pinned hashes remain valid. Projects with a
  path or vendored-wheel dep must re-pin `python_venv_hash` once, using the
  usual workflow (build, copy the `got:` hash). They should not have to do it
  again.

## 0.10.0 — 2026-07-05

Bug-fix release: a full-codebase review followed by 49 fix commits, each with a
regression test. Test suite: 444 → 556 passing.

### Behavior changes

- `capsule.start()` gained a `timeout=` parameter; the runner-launch default
  changed from 60 s to 300 s (large image `docker load` legitimately exceeds
  60 s).
- `PublishSpec.host_endpoint()` now truthfully reports `("0.0.0.0", port)` for
  the bare-int publish form (it binds all interfaces); `host_address()` and
  `wait_for_port()` map the wildcard to `127.0.0.1` for connecting.
- `rigx clean` on a symlinked `output/` now unlinks the symlink instead of
  deleting through it into the link target.
- `rigx run` forwards the script's real exit code instead of collapsing every
  failure to 1.
- `rigx new` and `rigx fmt` now search upward for `rigx.toml` like every other
  command, and `rigx new` validates the target name
  (`[A-Za-z_][A-Za-z0-9_-]*`).
- Negated glob character classes (`[!x]`) in `sources`/`excludes` no longer
  match `/` (path-aware, matching bash rather than `fnmatch`).
- Script/test targets with no `deps.nixpkgs` now run directly via bash instead
  of `nix shell` with zero installables (which failed, or silently built the
  install package); such scripts no longer require nix on PATH.

### Fixed — `rigx fmt`

- Multi-line strings are escaped per TOML rules; values containing
  backslashes (e.g. sed `\1`) or `"""` no longer produce an unparseable file.
- Top-level scalars are no longer rewritten as empty `[key]` table headers.
- Non-table values under `[dependencies]` are preserved instead of dropped.
- Keys outside the TOML bare-key charset (dots, spaces) are quoted instead of
  being silently restructured into nested tables.
- Safety net: `format_toml` reparses its own output and raises `FmtError`
  instead of ever writing a file that is not semantically identical to the
  input; TOML syntax errors print cleanly instead of tracebacks.

### Fixed — config loading (`rigx.toml`)

- Validation errors for `custom`/`script`/`test`/`testbed`/`run` targets now
  name the offending target instead of the last-parsed one.
- `[modules]` namespace resolution: interpolation-derived deps
  (`sources = ["${gen}/…"]`) and `run = "<sibling>"` refs inside a module are
  qualified correctly instead of silently dropped/unvalidated;
  `Project.find_target` resolves module-merged dotted names (also fixing
  host-test dep filtering and `localdep.module.target` refs).
- Mutually-including modules raise a clean `ConfigError` instead of
  `RecursionError`.
- A scalar where a list is expected (`args = "hello"`) is rejected instead of
  iterating per character.
- Malformed table shapes (`deps = [...]`, non-table entries under `[targets]`,
  `[external_inputs]`, git/local deps, `nixpkgs`), absolute glob patterns, and
  TOML syntax errors all raise clean `ConfigError`s instead of raw tracebacks.
- Module `python_project` paths are no longer mangled by `lstrip("./")`;
  absolute paths no longer get the module path prefix prepended.
- `ports` entries reject booleans.

### Fixed — CLI and `rigx watch`

- `rigx build pkg app lib` (a target literally named `pkg`, or `run`) no
  longer silently swallows the remaining arguments — passthrough splitting
  only triggers on the actual subcommand token.
- `rigx watch --watch-all` no longer rebuilds in an infinite loop (builds
  rewrote `flake.nix`, which was itself watched; `write_flake` now writes only
  on content change and the scan skips `flake.nix`).
- After a `rigx.toml` edit, watch rebuilds with the *reloaded* config instead
  of the stale one (both modes); a broken edit prints a reload error instead
  of being silently swallowed.
- Watch detects file additions, deletions, and renames, and no longer crashes
  when a temp file vanishes mid-scan.
- `rigx new` pre-checks all scaffold paths before writing anything (no more
  half-written scaffolds) and sanitizes hyphenated names in generated C/Rust
  identifiers.

### Fixed — builder

- Sandboxed tests from `[modules]` includes build the sanitized flake attr
  (`mod_test`) instead of a dotted attrpath that always failed.
- `flake.lock` follows-path edges are resolved by walking the input chain
  instead of misreading the last element as a node key (host-side tools could
  pin to the wrong nixpkgs).
- The cross-arch qemu hint resolves Nix attrs back to targets, so it fires for
  module/variant capsules too.
- Build failures using the `.` flake ref now hint that only git-tracked files
  are visible to the build.

### Fixed — capsules

- Env values containing commas are no longer corrupted into extra `-e` flags:
  the JSON runtime spec is now authoritative in the generated runner; legacy
  env vars remain for runners built by older rigx. Newlines in env values are
  rejected up front.
- UDP publish mappings are applied exactly once (they were emitted through
  both the legacy var and the JSON spec, killing qemu capsules at boot with a
  duplicate hostfwd rule).
- `start()` cleans up its spec/log temp files on every pre-launch failure,
  and module-namespaced capsules (`frontend.web`) can be started by either
  their qualified name or the sanitized attr.
- A stale inherited `RIGX_PROJECT_ROOT` no longer overrides the resolved
  project root.

### Fixed — testbed

- Partial startup failure (one link failing to bind) now tears down the
  already-started listeners, threads, and volume tempdirs; `Network` is
  re-enterable after `_stop` (the stop flag latched forever before).
- `_stop()` closes in-flight TCP connections instead of leaking sockets and
  pump threads across tests.
- Idle UDP sessions are garbage-collected on a timer even under continuous
  traffic from other peers.
- `fault()` validates rule names against the actual `RuleSet` fields,
  type/range-checks values, and validates `proto` — errors surface at the
  call site instead of killing proxy threads later.
- `bindings()` env var names are sanitized to valid POSIX names
  (`my-svc` → `MY_SVC_…`) with a clear error on collisions.

### Fixed — misc

- Mermaid graphs keep distinct targets distinct when their sanitized node ids
  collide (`web.app` vs `web-app`).

## 0.9.2 — 2026-06-26

- README overhaul; earlier history not tracked in this changelog.
