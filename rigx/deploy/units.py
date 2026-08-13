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


def render_unit(testbed: str, service_name: str, service: dict) -> str:
    """Render the `.service` unit text for one manifest `services[name]`
    entry (the dict shape `plan.compile_manifest` produces)."""
    deps = [unit_name(testbed, d) for d in service["depends_on"]]

    volume_mounts = [
        f"/var/lib/rigx/{testbed}/volumes/{vol}:{path}:rw"
        for vol, path in sorted(service["volumes"].items())
    ]
    secret_names = sorted(service["secrets"])
    port_specs = [
        f"{p['host']}:{p['container']}" for p in service["ports"]
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
    lines.append(f"Environment=RIGX_NAME=rigx-{testbed}-{service_name}")
    # Attached, no TTY — the only mode systemd can supervise.
    lines.append("Environment=RIGX_FOREGROUND=1")
    if port_specs:
        lines.append(f"Environment=RIGX_PUBLISH={','.join(port_specs)}")
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
            lines.append(f"Environment=RIGX_VOLUMES={','.join(volume_mounts)}")
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
    out: dict[str, str] = {}
    for sname in manifest["start_order"]:
        svc = manifest["services"][sname]
        out[unit_name(testbed, sname)] = render_unit(testbed, sname, svc)
        if svc["healthcheck"] is not None:
            out[health_unit_name(testbed, sname)] = render_health_unit(
                testbed, sname, svc
            )
            out[health_timer_name(testbed, sname)] = render_health_timer(
                testbed, sname, svc
            )
    return out
