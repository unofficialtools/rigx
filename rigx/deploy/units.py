"""Render the systemd units for a testbed service from a compiled manifest.

Each service produces one `.service` unit, plus — when it declares a
`healthcheck` — a `-health.service`/`-health.timer` pair that turns
that check into a *runtime* liveness probe (see "Health checks"
below).

Reuses the *existing* capsule runner env-var contract (`RIGX_NAME`,
`RIGX_PUBLISH`, `RIGX_VOLUMES`, `RIGX_FOREGROUND` — see
`rigx.capsule.start`) rather than systemd-native mechanisms like
`BindPaths=`: a "lite" capsule's runner shells out to `podman run -v
…`, which has its own mount namespace, so systemd bind-mounting the
*unit's* namespace wouldn't reach inside the container. `ExecStart`
invokes the capsule's `bin/run-<capsule>` directly with those env vars
set, exactly as `rigx.capsule.start()` does when driving a capsule
interactively.

`RIGX_FOREGROUND=1` is what makes the unit supervisable at all. The
runner's default mode is `-it`, which is a hard error under systemd
(`ExecStart` stdin is `/dev/null`, and docker refuses `-t` against a
non-terminal); `RIGX_DETACH=1` is equally wrong here, because the
runner would exit as soon as the container spawned and `Type=simple`
would read that as the service having finished. Foreground mode is
attached-but-TTY-less: the engine stays in the foreground, exits with
the container's exit code, and systemd's `Restart=` sees it.

Secrets need systemd itself involved: `LoadCredentialEncrypted=`
decrypts each secret to `$CREDENTIALS_DIRECTORY/<name>`
(systemd-managed tmpfs, readable only by this unit's process — design
decision #6), and that path is then folded into `RIGX_VOLUMES` as a
read-only mount at the service's declared exposed path. Because
`$CREDENTIALS_DIRECTORY` is only known at process-start time, this
requires `ExecStart` to run through a shell rather than exec'ing the
runner directly.

**Content vs. state.** A service mounts two kinds of thing, and the
distinction is the whole deployment model. `artifacts` are rigx-built
targets, mounted from the Nix store `:ro` — immutable at runtime (not
even root in the container can write to them), and replaced wholesale
by a new store path on every deploy, so deleting a file locally makes
it absent from the next generation, atomically, with rollback bringing
it back. `volumes` are the deliberate exception: read-write state that
`activate` creates once and never touches again, surviving redeploys,
rollbacks, and the removal of the service itself.

They compose by nesting — a read-only artifact tree with read-write
volumes mounted at paths underneath it — which is why the two are
rendered into one list ordered by mount depth rather than kept apart.

**Networking.** Every service joins one implicit per-testbed network
(`rigx-<testbed>`, created by `bin/activate`) and takes a
`--network-alias` equal to its service name, so peers reach each other
at `<service>:<port>` — the port being whatever the capsule's own
`ports` declares. Nothing about this is configurable, because nothing
needs to be: membership is "every service in the testbed", and the
address is built from names that already exist.

The consequence worth understanding is what `ports` on a *service* now
means. It is only about the host: peers never need it. A service with
no `ports` is reachable by its peers and invisible from outside — the
equivalent of compose's `expose:`, with nothing to declare. Publishing
is therefore an explicit act, and a bind address (`host =
"127.0.0.1:8000"`) narrows it further: the canonical use is exposing a
health endpoint to `activate`'s host-side probe without exposing it to
the world.

Name resolution needs the engine's DNS backend — netavark/aardvark-dns
on podman, which is standard on current distros but **fails silently
when absent** rather than erroring. `rigx.deploy.bootstrap` checks for
it and surfaces a warning.

**Restart semantics.** `Restart=` alone is not enough to make
`restart = "always"` mean always: systemd's default start limit (5
starts in 10s) would put a crash-looping service into `failed` and
stop retrying, which is exactly the case the operator wanted covered.
Units therefore set `StartLimitIntervalSec=0` (no rate limit) with
`RestartSec=5` as backoff, so the policy reads literally. The tradeoff
is that a permanently broken service retries forever rather than
parking in `failed` — acceptable here because a broken *deployment* is
already caught at deploy time by the health gate in
`rigx.deploy.activate`, so a failure that shows up later is a runtime
fault that should keep trying to recover.

`depends_on` renders as `Requires=` + `After=` (start order, and stop
propagation) *and* `PartOf=`, so restarting a dependency restarts its
dependents. Without `PartOf=`, restarting `db` would leave `web`
running against a connection that went away — systemd propagates
stops through `Requires=` but not restarts.

**Health checks.** A service's `healthcheck` is used in two places: as
a one-shot gate at deploy time (`rigx.deploy.activate`), and — via the
`-health.timer` rendered here — as a recurring liveness probe
afterwards. Without the timer, `healthcheck` would only ever describe
whether a deploy succeeded, and a service that hangs without exiting
would stay `active` forever with nothing to notice; `Restart=` only
reacts to the process *exiting*. The timer is `BindsTo=` its service,
so it stops when the service stops and never probes something that
isn't meant to be running. The probe restarts the service with
`--no-block` to avoid a synchronous `systemctl restart` from inside a
unit, which can deadlock against the job it is waiting on.

**Confinement (design decision #11).** Services run as root. The
previous revision gave `backend = "lite"` services `DynamicUser=yes` +
`SupplementaryGroups=docker`, which was theater: membership in the
`docker` group is root-equivalent by construction (anything that can
reach the daemon socket can `run -v /:/host --privileged`), so the
"unprivileged" dynamic UID held root anyway, while `ProtectSystem=`
constrained a client process whose real work happened in a root
daemon. Podman has no daemon, so that privilege stops being launderable
and is simply named. Rootless-as-`rigx` is the intended endpoint and a
real improvement, but it needs subuid/subgid ranges from bootstrap,
lingering, `XDG_RUNTIME_DIR`, userns-aware ownership for the
persistent volumes under `/var/lib/rigx/<testbed>/volumes/`, and rules
out host ports below 1024 — and it is the same open question as the
sudoers policy noted on `rigx.deploy.bootstrap.ensure_rigx_user`. It
should be designed with that, not bolted on here.
"""

from __future__ import annotations

# Backoff between restarts. Paired with StartLimitIntervalSec=0 below:
# without a rate limit, this is the only thing keeping a crash loop
# from spinning as fast as the engine can load an image.
RESTART_SEC = 5


def unit_name(testbed: str, service: str) -> str:
    return f"rigx-{testbed}-{service}.service"


def health_unit_name(testbed: str, service: str) -> str:
    return f"rigx-{testbed}-{service}-health.service"


def health_timer_name(testbed: str, service: str) -> str:
    return f"rigx-{testbed}-{service}-health.timer"


def _env_line(key: str, value: str) -> str:
    """One `Environment=` assignment, quoted.

    systemd splits an unquoted `Environment=` value on whitespace and
    treats each field as a separate assignment, so
    `Environment=RIGX_ENV=GREETING=hello world` sets RIGX_ENV to
    "GREETING=hello" and silently discards the rest. Any value that can
    contain a space — an env var the user wrote, a volume path — has to
    be quoted, and there is no cost to quoting the rest.
    """
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'Environment="{key}={escaped}"'


def render_unit(
    testbed: str, service_name: str, service: dict, network: str = "",
) -> str:
    """Render the `.service` unit text for one manifest `services[name]`
    entry (the dict shape `plan.compile_manifest` produces).

    `network` is the testbed's shared container network; empty renders a
    unit with no network wiring, which is what pre-network manifests
    (and unit tests that don't care) produce."""
    deps = [unit_name(testbed, d) for d in service["depends_on"]]

    # Artifacts (read-only store paths) and volumes (read-write state)
    # share one ordered list, because they nest in each other: the
    # canonical shape is a read-only artifact tree with read-write
    # volumes mounted at paths underneath it.
    #
    # Ordered parent-before-child by mount path, never by name. Mounting
    # a parent last shadows everything beneath it — for an artifact over
    # a state volume that silently hides the data the volume exists to
    # keep. Engines generally sort destinations themselves, but relying
    # on that leaves the correctness of a deployment resting on an
    # implementation detail of whichever engine the target picked.
    mounts: list[tuple[str, str]] = [
        (path, f"/var/lib/rigx/{testbed}/volumes/{vol}:{path}:rw")
        for vol, path in service["volumes"].items()
    ]
    mounts += [
        (a["mount"], f"{a['store_path']}:{a['mount']}:ro")
        for a in service.get("artifacts") or []
    ]
    volume_mounts = [
        spec for _, spec in sorted(mounts, key=lambda m: (m[0].count("/"), m[0]))
    ]
    secret_names = sorted(service["secrets"])
    # `publish` carries the bind address when one was declared; fall
    # back to host:container for manifests compiled before it existed.
    port_specs = [
        p.get("publish") or f"{p['host']}:{p['container']}"
        for p in service["ports"]
    ]

    lines = ["[Unit]"]
    lines.append(f"Description=rigx testbed {testbed} service {service_name}")
    lines.append("After=network.target" + "".join(f" {d}" for d in deps))
    if deps:
        lines.append("Requires=" + " ".join(deps))
        # Restart propagation: Requires= only propagates stops.
        lines.append("PartOf=" + " ".join(deps))
    if service["healthcheck"] is not None:
        # Pulls the probe timer in when this service starts; the timer
        # is BindsTo= this unit, so it goes away again on stop.
        lines.append(f"Wants={health_timer_name(testbed, service_name)}")
    # `Restart=always` should mean always — see the module docstring.
    lines.append("StartLimitIntervalSec=0")
    lines.append("")
    lines.append("[Service]")
    lines.append("Type=simple")
    lines.append(f"Restart={service['restart']}")
    lines.append(f"RestartSec={RESTART_SEC}")
    lines.append(_env_line("RIGX_NAME", f"rigx-{testbed}-{service_name}"))
    # Attached, no TTY — the only mode systemd can supervise.
    lines.append(_env_line("RIGX_FOREGROUND", "1"))
    # systemd sets no HOME for system services, and the lite runner puts
    # its per-container Nix profile/gcroot dirs under it. Give it a real
    # writable location rather than letting it fall back to /tmp.
    # `bin/activate` creates this directory.
    lines.append(_env_line("HOME", f"/var/lib/rigx/{testbed}/home"))
    if network:
        lines.append(_env_line("RIGX_NETWORK", network))
        # Peers dial `<service>:<port>`; the container's own --name has
        # to be globally unique, so it's too long to be the address.
        lines.append(_env_line("RIGX_NETWORK_ALIAS", service_name))
    if port_specs:
        lines.append(_env_line("RIGX_PUBLISH", ",".join(port_specs)))
    service_env = service.get("env") or {}
    if service_env:
        lines.append(_env_line(
            "RIGX_ENV",
            ",".join(f"{k}={service_env[k]}" for k in sorted(service_env)),
        ))
    for name in secret_names:
        cred_path = f"/var/lib/rigx/{testbed}/secrets/{name}.cred"
        lines.append(f"LoadCredentialEncrypted={name}:{cred_path}")

    runner = f"{service['capsule_path']}/bin/run-{service['capsule']}"
    if secret_names:
        # RIGX_VOLUMES needs $CREDENTIALS_DIRECTORY, which only exists
        # once the process has started — compose it in a shell instead
        # of a plain Environment= line.
        cred_mounts = [
            f"${{CREDENTIALS_DIRECTORY}}/{name}:{service['secrets'][name]}:ro"
            for name in secret_names
        ]
        all_mounts = volume_mounts + cred_mounts
        vols = ",".join(all_mounts)
        lines.append(
            "ExecStart=/bin/sh -c "
            + _sh_quote(f'RIGX_VOLUMES={vols} exec "{runner}"')
        )
    else:
        if volume_mounts:
            lines.append(_env_line("RIGX_VOLUMES", ",".join(volume_mounts)))
        lines.append(f"ExecStart={runner}")

    lines.append("")
    lines.append("[Install]")
    lines.append("WantedBy=multi-user.target")
    lines.append("")
    return "\n".join(lines)


def render_health_unit(testbed: str, service_name: str, service: dict) -> str:
    """Render the oneshot `-health.service` that probes one service and
    restarts it if the probe stays down for `retries` attempts."""
    hc = service["healthcheck"]
    target_unit = unit_name(testbed, service_name)
    probe = (
        f"i=0; "
        f"until {hc['command']}; do "
        f"i=$((i + 1)); "
        f'if [ "$i" -ge {hc["retries"]} ]; then '
        f'echo "rigx: {service_name} failed healthcheck after '
        f'{hc["retries"]} tries, restarting" >&2; '
        f"exec systemctl restart --no-block {target_unit}; "
        f"fi; "
        f"sleep {hc['interval']}; "
        f"done"
    )
    return "\n".join([
        "[Unit]",
        f"Description=rigx testbed {testbed} health probe for {service_name}",
        "",
        "[Service]",
        "Type=oneshot",
        "ExecStart=/bin/sh -c " + _sh_quote(probe),
        "",
    ])


def render_health_timer(testbed: str, service_name: str, service: dict) -> str:
    """Render the `-health.timer` driving the probe above. `BindsTo=` the
    service so it only runs while the service is meant to be up."""
    hc = service["healthcheck"]
    target_unit = unit_name(testbed, service_name)
    return "\n".join([
        "[Unit]",
        f"Description=rigx testbed {testbed} health timer for {service_name}",
        f"BindsTo={target_unit}",
        f"After={target_unit}",
        "",
        "[Timer]",
        f"OnActiveSec={hc['interval']}",
        f"OnUnitActiveSec={hc['interval']}",
        f"Unit={health_unit_name(testbed, service_name)}",
        "",
    ])


def _sh_quote(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


def service_unit_names(testbed: str, service_name: str, service: dict) -> list[str]:
    """Every unit filename belonging to one service — the main unit
    first, then its health pair if it has a `healthcheck`. `activate`
    installs and cleans up units by this list."""
    names = [unit_name(testbed, service_name)]
    if service["healthcheck"] is not None:
        names.append(health_unit_name(testbed, service_name))
        names.append(health_timer_name(testbed, service_name))
    return names


def render_units(manifest: dict) -> dict[str, str]:
    """Render every unit for every service in `manifest` — returns unit
    filename -> unit text, in the manifest's `start_order` (each
    service's own units grouped together, main unit first)."""
    testbed = manifest["testbed"]
    network = manifest.get("network", "")
    out: dict[str, str] = {}
    for sname in manifest["start_order"]:
        svc = manifest["services"][sname]
        out[unit_name(testbed, sname)] = render_unit(testbed, sname, svc, network)
        if svc["healthcheck"] is not None:
            out[health_unit_name(testbed, sname)] = render_health_unit(
                testbed, sname, svc
            )
            out[health_timer_name(testbed, sname)] = render_health_timer(
                testbed, sname, svc
            )
    return out
