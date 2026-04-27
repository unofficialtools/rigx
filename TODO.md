# TODO — capsule + testbed follow-on work

Deferred from the v1 capsule landing. Each entry has enough context to
pick up cold; no blockers from the current schema.

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
- **Container reach to non-default loopback aliases.** On Linux the
  default docker bridge can reach `127.0.X.Y` from the host network
  side via `--network host`; the test bed would need to set this on
  capsules that act as clients of host-bound peers. Not yet wired in
  the runner. Probably surfaces as a `RIGX_HOST_NETWORK=1` env knob.
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
- **UDP `delay_ms` is head-of-line.** A delayed datagram blocks the
  listener thread until the sleep finishes; subsequent datagrams
  from any source wait. Switch to a heap-based scheduler when this
  bites.
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
