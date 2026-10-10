"""`rigx remote` — manage a declarative testbed deployment.

`rigx remote <target> deploy [ssh_url]` runs the full pipeline:
bootstrap the target (Nix + systemd check + service user), build the
referenced capsules and compile the deployment plan, push it via `nix
copy`, push any declared secrets, then run the plan's `bin/activate`
on the target. `start`/`stop`/`restart` and `rollback` operate on an
already-deployed testbed (design decision #10 — lifecycle scoped to
exactly these, nothing fancier) by reading back the manifest the
currently-live generation already has and driving
`systemctl`/`bin/rollback` directly; nothing gets rebuilt or
re-copied. See the project design memory for the full design and
what's still a known gap (flake.nix integration for the plan itself;
the non-bootstrap privilege model).

Every action takes its destination from the target's `ssh_url` in
`rigx.toml`, so the host a testbed deploys to is versioned with the
testbed rather than retyped per invocation. The positional `ssh_url`
is an optional override (staging host, one-off), and the only source
when the target declares none — see `_resolve_ssh_url`.
"""

from __future__ import annotations

import argparse
import json
import sys

from rigx import builder, config
from rigx.config import Project, Target
from rigx.deploy import bootstrap, hostinv, pack, plan, secrets, units
from rigx.deploy.bootstrap import DeployError, SSHTarget


def _sh_quote(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


def _resolve_target(project: Project, target_name: str) -> Target:
    """Look up the named `kind = "testbed"` target and check it has a
    declarative `services` section — i.e. is actually deployable."""
    t = project.targets.get(target_name)
    if t is None:
        raise DeployError(f"no such target: {target_name!r}")
    if t.kind != "testbed":
        raise DeployError(f"target {target_name!r} is kind={t.kind!r}, not 'testbed'")
    if not t.services:
        raise DeployError(
            f"testbed {target_name!r} has no declarative 'services' "
            f"section — script-only testbeds aren't deployable"
        )
    return t


def _resolve_ssh_url(testbed: Target, args: argparse.Namespace) -> str:
    """Pick the deploy destination: the command-line `ssh_url` if given,
    otherwise the target's `ssh_url` from `rigx.toml`.

    The destination lives in `rigx.toml` so it is versioned alongside
    the thing being deployed instead of retyped (and mistyped) per
    invocation. The command-line form stays available as an override —
    for a staging host, or a one-off — and is the only source when the
    target declares none.
    """
    override = getattr(args, "ssh_url", None)
    if override:
        return override
    if testbed.ssh_url:
        return testbed.ssh_url
    raise DeployError(
        f"testbed {testbed.name!r} declares no 'ssh_url' in rigx.toml — "
        f"either add one to [targets.{testbed.name}] or pass a destination "
        f"on the command line (rigx remote {testbed.name} "
        f"{getattr(args, 'remote_command', '<action>')} ssh://user@host)"
    )


def _fetch_previous_manifest(ssh_target: SSHTarget, testbed_name: str) -> dict | None:
    """Read `manifest.json` out of whatever generation is currently
    live on the target (if any) — used to diff removed services at
    plan-compile time (design decision #3). Returns None on a
    first-ever deploy (no profile yet) or any read/parse failure."""
    profile = f"/nix/var/nix/profiles/rigx-{testbed_name}"
    proc = bootstrap.run_remote(
        ssh_target, f"cat {profile}/manifest.json 2>/dev/null", check=False,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None


def _evict_other_testbeds(ssh_target: SSHTarget, testbed: Target) -> None:
    """`host_exclusive = true`: take every *other* rigx testbed on the
    host down before deploying this one.

    Reads the host's own list of testbeds rather than anything local, so
    it evicts deployments this project has never heard of — which is the
    only interesting case, since a testbed you still have the config for
    you could already stop by name. Persistent volumes are left behind
    (see `hostinv.teardown`), so an eviction is reversible by
    redeploying whatever was evicted.
    """
    others = [n for n in hostinv.list_testbeds(ssh_target) if n != testbed.name]
    if not others:
        return
    print(
        f"[rigx remote deploy] host_exclusive: evicting {len(others)} other "
        f"testbed(s) from {ssh_target.destination}: {', '.join(others)}"
    )
    for name in others:
        hostinv.teardown(ssh_target, name)
        print(f"[rigx remote deploy] evicted {name!r} (volumes kept)")


def _check_port_conflicts(ssh_target: SSHTarget, testbed: Target) -> None:
    """Fail before building anything if a container that isn't ours
    already holds a host port this testbed publishes.

    This is a preflight rather than something `activate` discovers,
    because of how badly the engine reports it: a port collision only
    surfaces when the container tries to bind, which is after `activate`
    has restarted the unit, so the operator sees `Restart=always` cycle
    and the health gate exhaust its retries against a service that never
    got far enough to be unhealthy. The port is the cause; the health
    check is what happens to notice. Saying so up front is the entire
    improvement.
    """
    wanted = hostinv.declared_host_ports(testbed)
    if not wanted:
        return
    conflicts = hostinv.find_port_conflicts(
        hostinv.list_containers(ssh_target), testbed.name, wanted,
    )
    if conflicts:
        raise DeployError(hostinv.format_conflicts(testbed.name, conflicts))


def cmd_remote_deploy(args: argparse.Namespace) -> int:
    from rigx import cli
    try:
        project = cli._load(args)
        testbed = _resolve_target(project, args.target)
        ssh_url = _resolve_ssh_url(testbed, args)
    except (config.ConfigError, DeployError) as e:
        print(f"rigx remote deploy: {e}", file=sys.stderr)
        return 1

    try:
        info = bootstrap.bootstrap(ssh_url)
        print(
            f"[rigx remote deploy] bootstrap ok: {info.os_name}/{info.arch}, "
            f"nix={'present' if info.has_nix else 'missing'}"
        )
        if info.dns_warning:
            # Not fatal: the deployment works, only peer name lookups
            # break — and they break silently, which is why this is
            # said out loud rather than left to be discovered.
            print(f"rigx remote deploy: warning: {info.dns_warning}", file=sys.stderr)

        ssh_target = bootstrap.parse_ssh_url(ssh_url)

        # Both of these run before anything is built or copied: a host
        # that can't accept this testbed should say so in seconds, not
        # after a full capsule build and a `nix copy`.
        if testbed.host_exclusive:
            _evict_other_testbeds(ssh_target, testbed)
        _check_port_conflicts(ssh_target, testbed)

        previous_manifest = _fetch_previous_manifest(ssh_target, testbed.name)

        print(f"[rigx remote deploy] building capsules + compiling plan for {testbed.name!r}")
        store_path = pack.build_and_push_plan(
            project, testbed, ssh_target, previous_manifest=previous_manifest,
        )
        print(f"[rigx remote deploy] plan at {store_path}")

        if testbed.testbed_secrets:
            print(f"[rigx remote deploy] pushing {len(testbed.testbed_secrets)} secret(s)")
            secrets.push_secrets(
                ssh_target, testbed.name, testbed.testbed_secrets,
                project_root=project.root,
            )

        print("[rigx remote deploy] activating")
        bootstrap.run_remote(ssh_target, f"sudo {store_path}/bin/activate")
    except (DeployError, plan.PlanError, builder.BuildError) as e:
        print(f"rigx remote deploy: {e}", file=sys.stderr)
        return 1

    print(f"[rigx remote deploy] {testbed.name!r} deployed to {ssh_target.destination}")
    return 0


def _resolve_and_connect(args: argparse.Namespace) -> tuple[Target, SSHTarget]:
    from rigx import cli
    project = cli._load(args)
    testbed = _resolve_target(project, args.target)
    return testbed, bootstrap.parse_ssh_url(_resolve_ssh_url(testbed, args))


def _lifecycle(args: argparse.Namespace, action: str) -> int:
    assert action in ("start", "stop", "restart")
    try:
        testbed, ssh_target = _resolve_and_connect(args)
    except (config.ConfigError, DeployError) as e:
        print(f"rigx remote {action}: {e}", file=sys.stderr)
        return 1

    try:
        manifest = _fetch_previous_manifest(ssh_target, testbed.name)
        if manifest is None:
            raise DeployError(
                f"testbed {testbed.name!r} has no active deployment on "
                f"{ssh_target.destination} (run `rigx remote {testbed.name} deploy` first)"
            )
        # Stop in reverse dependency order (dependents before what they
        # depend on); start/restart follow the manifest's own order.
        order = manifest["start_order"]
        sequence = list(reversed(order)) if action == "stop" else order
        for sname in sequence:
            unit = f"rigx-{testbed.name}-{sname}.service"
            print(f"[rigx remote {action}] {unit}")
            bootstrap.run_remote(ssh_target, f"sudo systemctl {action} {unit}")
            if action == "stop":
                # `stop` has to mean the container is gone, not just its
                # supervisor. Units rendered from this version reap
                # themselves via ExecStopPost=, but a host deployed by an
                # older rigx is running units that don't — and reporting
                # "done" while the container still holds port 80 is how a
                # later deploy of a *different* testbed ends up failing a
                # health check it was never going to pass.
                bootstrap.run_remote(
                    ssh_target,
                    "sudo sh -c "
                    + _sh_quote(
                        units.reap_command(
                            units.container_name(testbed.name, sname)
                        )
                    ),
                    check=False,
                )
    except DeployError as e:
        print(f"rigx remote {action}: {e}", file=sys.stderr)
        return 1

    print(f"[rigx remote {action}] {testbed.name!r} on {ssh_target.destination}: done")
    return 0


def cmd_remote_start(args: argparse.Namespace) -> int:
    return _lifecycle(args, "start")


def cmd_remote_stop(args: argparse.Namespace) -> int:
    return _lifecycle(args, "stop")


def cmd_remote_restart(args: argparse.Namespace) -> int:
    return _lifecycle(args, "restart")


def cmd_remote_rollback(args: argparse.Namespace) -> int:
    """Explicit operator rollback: point the target's Nix profile back
    at the previous generation via that generation's own
    `bin/rollback` (see design decision #3) — reached through the
    profile symlink itself so this doesn't need to already know the
    current generation's exact store path."""
    try:
        testbed, ssh_target = _resolve_and_connect(args)
    except (config.ConfigError, DeployError) as e:
        print(f"rigx remote rollback: {e}", file=sys.stderr)
        return 1

    try:
        profile = f"/nix/var/nix/profiles/rigx-{testbed.name}"
        bootstrap.run_remote(ssh_target, f"sudo {profile}/bin/rollback")
    except DeployError as e:
        print(f"rigx remote rollback: {e}", file=sys.stderr)
        return 1

    print(f"[rigx remote rollback] {testbed.name!r} on {ssh_target.destination}: rolled back")
    return 0
