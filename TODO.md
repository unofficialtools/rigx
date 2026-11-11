# TODO — capsule + testbed follow-on work

Deferred from the v1 capsule landing, plus follow-ons from `rigx
remote`. Each entry has enough context to pick up cold; no blockers
from the current schema.

## QEMU backend (cross-arch user-mode emulation)

The motivating use case: testing flight software cross-compiled to ARM
on an x86_64 host. rigx already cross-compiles via
`target = "aarch64-linux"` etc.; the capsule layer needs a backend
that runs the resulting binary under QEMU.

Two flavors:

- **`mode = "user"`** (`qemu-aarch64 ${binary}`): translates syscalls
  to the host kernel. Fast, simple. Right for Linux-userland flight
  code. v1.5 target.
- **`mode = "system"`** (`qemu-system-aarch64 -machine virt -kernel … -nographic …`):
  full machine emulation. Right for bare-metal / FreeRTOS flight code
  running on emulated hardware. More flags (machine, memory, serial,
  network), more configuration. Later.

Schema sketch:

```toml
[targets.flight_computer]
kind          = "capsule"
backend       = "qemu"
arch          = "aarch64"           # uses rigx's existing cross-target aliases
mode          = "user"              # or "system"
deps.internal = ["fc_firmware"]      # already cross-compiled to aarch64
entrypoint    = "${fc_firmware}/bin/fc_firmware"
ports         = [5001]              # qemu hostfwd

[targets.flight_computer.qemu]      # backend-specific escape hatch
machine     = "virt"                # system mode only
memory      = "256M"
serial      = "stdio"
extra_args  = ["-cpu", "cortex-a72"]
```

Manifest gets `image.kind = "qemu-binary"` for user mode,
`"qemu-machine"` for system mode. Runner script invokes qemu directly
(no docker), but exposes the same env-var contract (`RIGX_PUBLISH`,
`RIGX_NAME`, `RIGX_DETACH`) so `rigx.capsule.start()` works
unchanged. `rigx.testbed` doesn't need to know which backend is on
the other end — it talks to host:port either way.

Estimate: user mode ~250 lines (mostly Nix gen for the runner script
and figuring out the right qemu invocation). System mode another
~300, more if we want to bake disk images.

## s6 multi-service capsules

Some capsules need multiple supervised processes (the simulator
running plus a metrics exporter; a service plus its log shipper).
nix-docker recommends `s6` for this since there's no systemd inside.

```toml
[targets.svc]
kind          = "capsule"
backend       = "lite"
deps.internal = ["main_bin", "log_shipper"]

[targets.svc.services.main]
run = "exec ${main_bin}/bin/main_bin --port 5000"

[targets.svc.services.log_shipper]
run = "exec ${log_shipper}/bin/log_shipper --tail /var/log/svc.log"
log = true                          # auto-create log/run with s6-log
```

When `services.*` is set, the capsule's image includes `s6` from
nixpkgs and the entrypoint becomes `s6-svscan /etc/services`. Each
service entry produces an `/etc/services/<name>/run` script (and
optionally `log/run`).

Mutually exclusive with the single-`entrypoint` form: a capsule has
*either* `entrypoint = "…"` or `services = { … }`, never both.

Estimate: ~150 lines of nix gen + schema updates. Mainly fiddly
because of the s6 directory layout.

## Distinct virtual addresses per capsule

**Status (option 1):** ✅ shipped. `Network(subnet=...)` auto-allocates
loopback aliases per capsule; `declare(address=...)` pins one
explicitly. The proxy upstream socket binds on the source's address
before connect, so destinations see real source IPs (verified by
`tests.test_testbed.DistinctAddresses.test_destination_sees_source_address_through_proxy`).
Capsule publish maps now use `(host_addr, host_port)` tuples that
`rigx.capsule` renders as `addr:host:cont` for `docker -p`. The
default `Network()` (no subnet) keeps shared-loopback behavior, so
existing tests are unchanged.

Remaining gaps in option 1:

- **macOS loopback aliases** beyond `127.0.0.1` need
  `sudo ifconfig lo0 alias 127.0.X.Y` per address. Ship a one-time
  helper script under `bin/` that idempotently aliases everything in
  the configured subnet on entry, removes on exit.
- **`/etc/hosts` injection inside the container** so capsule code can
  refer to peers by name (`fc`, `sim`) instead of IP literals.
  `--add-host fc=127.0.10.2` per outbound link, set automatically
  from `bindings(name)`.

**Option (2) — Docker network with static IPs.** Still deferred. The
shape: create a docker bridge (`docker network create --subnet
10.42.0.0/24 rigx-net`), assign each capsule a static IP on it, let
capsules talk directly. Testbed proxy sits as another container on the
same network between every linked pair so fault rules still apply.
~200–300 lines plus lifecycle design (network ownership, teardown,
how the qemu backend would join). Real IP-level isolation, real
subnets visible inside containers. Right path if mixed-backend
(lite + qemu) ever needs shared network semantics.

**Option (3) — Linux netns + veth pairs.** Production-grade network
simulator. Requires `CAP_NET_ADMIN`, Linux-only, real plumbing.
ns-3 / Mininet exist if anyone needs this. Skip unless (1) and (2)
both fall short for a concrete use case.

## `rigx.testbed` extensions

Things the testbed still punts on:

- **UDP src-IP on reply path.** Forward path preserves the real
  source IP via the upstream-bind trick; replies come back through
  the proxy's listener address. Capsule code using `connect()` with
  strict source checking sees the proxy, not the destination. Fix:
  send replies from a third socket bound on `(dst_addr, ephemeral)`.
  Adds another fd per session — measure before adopting.
- **UDP multicast.** The forwarder is point-to-point. Multicast
  needs join semantics on the listener side and replication on the
  upstream side. Defer until a concrete protocol needs it.
- **L2/L3 effects** (MTU truncation, fragmentation, ARP). Outside
  L4 proxy semantics — would need a tap interface and netem on the
  host. Probably not worth doing in-process; users who need it can
  set up a separate netns / network sidecar (option 3 in the
  distinct-addresses section above).
- **Bandwidth limiting.** Token-bucket on the proxy's per-link send
  side. Easy in principle (~30 lines), just hasn't been needed yet.
- **Mid-test topology changes.** `Network.declare()` and `link()`
  must be called before the `with` block. Hot-add / hot-remove of
  links during a test is conceptually clean but adds locking and
  race surface.
- **Time control.** "Freeze sim's clock for 1s while fc's runs."
  Requires clock virtualization (`-icount` for QEMU, libfaketime for
  containers). Genuinely hard; defer until a concrete need shows up.

## Per-system gating

Capsule attrs are emitted on every system in `forAll` (linux + darwin).
On darwin, `pkgs.dockerTools.buildImage` works but the runner won't —
darwin doesn't have a Linux nix daemon to mount the socket from. Could
restrict capsule attrs to `*-linux` so `nix flake show` on darwin
doesn't list broken targets.

## Mixed-backend network simulation

Once qemu user-mode lands, capsules of different backends (lite +
qemu) should talk through the same testbed naturally — both sides
just expose host ports, and the testbed proxies between them.
Verification needed once both backends are real.

<<<<<<< Updated upstream
## Offline operation — what's left

Done in 0.13.0: `rigx prefetch`, `rigx --offline` / `RIGX_OFFLINE=1`
(`--offline` on every nix call), a network-unreachable hint on failed
build/lock/test/run/pkg, `[nixpkgs] rev` + `rigx lock --pin`, and README
notes (including that a `pypi.org/simple/rigx` error is the launcher,
e.g. `uvx`, not rigx).
Verified end to end: after `rigx prefetch`, a source edit rebuilds and
tests offline (`unshare -rn`), cross target included; with a pinned
`rev`, it builds offline even after deleting flake.lock.

Still open:

- **Python venvs re-download on every dependency change.** The
  clear error and the stale-venv fix are done (0.13.0). Removing the
  re-download needs per-package derivations from `uv.lock` (fetchurl
  per wheel, then an offline `uv sync`), i.e. uv2nix territory: sdists,
  platform wheel selection, path and git deps.
- **`rigx remote` to a fresh host** curls `install.determinate.systems`
  on the target. An offline installer (pre-staged tarball copied over
  SSH) would close it.
- **`rigx bundle export` / `import`** — `nix copy` a project's closure
  to a file for air-gapped machines; the machinery exists in the SSH
  deploy backend. ~150 lines.
- **Doctor check** — "can this project build offline right now?": lock
  present, locked rev in store, per-target closures, venv FOD hashes.
  ~120 lines.
=======
## Offline operation — `rigx` should work without a network

Reported symptom:

```
error: Request failed after 3 retries
  Caused by: Failed to fetch: `https://pypi.org/simple/rigx/`
  Caused by: dns error: failed to lookup address information
```

**That specific error is not rigx's code.** rigx never contacts PyPI at
runtime — no update check, no `pip`, no index lookup anywhere in
`rigx/`. The only PyPI traffic in the repo comes from targets you invoke
deliberately (`rigx run publish`, `rigx run dist` → `uv build`/`uv
publish`), and those fetch *setuptools*, not `rigx`. A fetch of
`pypi.org/simple/**rigx**/` means something is resolving the rigx
*package* from the index — i.e. the launcher, not the tool. The wording
is verbatim `uv`, so the likely cause is `uvx rigx` / `uv tool run rigx`,
which re-resolves the tool against the index on every invocation.

Fixes at that layer are documentation, not code:

- `uv tool install rigx` once (resolves at install time) instead of
  `uvx rigx` on every run
- `UV_OFFLINE=1` / `uvx --offline`
- `nix profile install github:unofficialtools/rigx/stable` — no index at all
- README should state plainly: rigx has no PyPI dependency at runtime;
  a pypi.org error means your *launcher* is re-resolving it.

The rest of this entry is the real work: what an offline `rigx build`
actually needs. There are four independent chokepoints, and today every
one of them surfaces as raw `nix` stderr.

### 1. Flake input resolution

`rigx.nix.flake` emits `nixpkgs.url = "github:NixOS/nixpkgs/<ref>"` — a
*branch*, not a rev. Offline works only if `flake.lock` exists **and**
the locked nixpkgs source is already in the store. It breaks when the
project is fresh (no lock), the lock is gitignored, or `write_flake`
changed the input set so nix re-locks.

Worse: when `builder._locked_nixpkgs_flakeref()` returns `None` (no
lock, unreadable lock, non-github input), the host-side paths fall back
to the indirect ref `nixpkgs/<branch>`, which needs the flake registry —
a second network dependency, and the same one that motivated the
locked-rev preference in the first place (see that function's docstring
on GitHub API rate limits).

Directions: let `[nixpkgs]` accept a `rev` (or a full `url`) so the
branch never needs resolving; have `build()` warn or refuse when no
usable lock exists rather than letting nix discover it three layers
down; treat "no lock" as a first-class diagnosable state — `rigx lock`
is already the fix, it just isn't suggested at the right moment.

### 2. Substituters

Even with a perfect lock, any derivation not in the local store is
fetched from cache.nixos.org. Offline that is a hang, then a failure
with no useful text. Passing `--offline` to nix (which also disables
substituters) turns it into an immediate, honest "path not available".

### 3. Python venv FODs

`rigx.nix.python.mk_python_derivation` runs `uv sync --frozen` inside a
fixed-output derivation — network is permitted *by design*, and pinned
by `outputHash`. Offline this works only if a store path with that exact
hash already exists (then nix skips the build entirely — the happy
path). It breaks the moment deps change, or while the hash is still
`FAKE_SHA256`.

Options, roughly in increasing order of ambition: pre-seed a uv cache
into the FOD source; vendor wheels and use `--find-links`; or move to a
nix-native resolver (uv2nix / pyproject.nix) so Python deps arrive from
substituters like every other dependency. Regardless of which, this case
deserves a targeted error: "this target's venv is not in the store and
building it requires network".

### 4. `rigx remote`

`rigx.deploy.bootstrap` curls `install.determinate.systems`. LAN deploys
to an already-bootstrapped host are fine; first touch of a fresh host
needs internet *on the target*. An offline-installer path (pre-staged
tarball, copied over SSH) would close it.

### Candidate features, in value order

- **`rigx --offline` / `RIGX_OFFLINE=1`** — add `--offline` to every
  `nix build` / `nix shell` / `nix run` invocation. All the call sites
  funnel through `builder.py` (`build_one`, `_script_cmd`,
  `run_nixpkgs_tool`, `update_lock`, `_run_sandboxed_tests`), so this is
  small. Turns DNS-timeout soup into a fast, legible failure.
  ~40 lines. Highest payoff per line in this list.
- **Offline-aware error triage** — pattern-match nix stderr for
  DNS/connect failures the way `_cross_arch_hint` already does for
  binfmt, and print: "network unreachable — run `rigx lock` while online
  and commit flake.lock, or pass `--offline` if everything is already in
  your store." ~40 lines. This alone makes most reports self-answering.
- **`rigx prefetch` (a.k.a. `warm`)** — while online, realize the full
  build-time closure of every target, including the venv FODs, so a
  later offline run is guaranteed to hit. This is the piece that makes
  offline actually *work* rather than merely fail politely. ~100 lines.
- **Pin nixpkgs by rev in `rigx.toml`**, with `rigx lock` writing the
  resolved rev back. Removes both the branch-resolution and the registry
  dependency. ~60 lines plus schema and docs.
- **`rigx bundle export` / `import`** — `nix copy` a project's closure to
  a file for genuinely air-gapped machines. The `nix copy` machinery
  already exists in the SSH deploy backend; this is mostly plumbing.
  ~150 lines.
- **A doctor check** — "can this project build offline right now?":
  lock present, locked rev in store, per-target closure availability,
  venv FOD hash present. Reports which of the four chokepoints would
  bite. ~120 lines.

The minimal coherent slice is the first three: `prefetch` makes offline
possible, `--offline` makes it deterministic, the error hint makes the
failure teachable.
>>>>>>> Stashed changes
