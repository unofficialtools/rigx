"""Render `bin/activate` / `bin/rollback` for a testbed deployment plan.

Generated as POSIX `sh`, not bash — bootstrap only guarantees Nix +
systemd on the target (design decision #4), nothing about `/bin/sh`
beyond it existing. Every service gets its own unrolled block in the
generated script rather than parsing `manifest.json` at runtime: the
renderer already has the full manifest at generation time, so there's
no need for a JSON parser (jq/python3) to be present on the target —
that dependency would otherwise be a gap given this plan isn't wired
through rigx's own Nix build yet (see the packaging TODO in
`rigx.deploy.pack`). The one thing it can't know is what it is
replacing, so dropped services are found at runtime from the units
directory of the live plan (`/var/lib/rigx/<testbed>/live`).

Algorithm (design decision #3): for each service, compare the new
rendered unit file against whatever's actually symlinked at
`/etc/systemd/system/<unit>` right now (ground truth, not the profile
pointer) — daemon-reload once if anything changed, then restart only
the changed services in dependency order, health-gating each one
(design decision #7). Any gate failure re-links just the services this
run touched back to the previous generation's units (or removes them,
on a first-ever deploy with nothing to fall back to) and exits
non-zero. Services this generation drops are removed only after every
gate has passed, so a failed deploy leaves them running. The live link
and the Nix profile pointer (`/nix/var/nix/profiles/rigx-<testbed>`)
only advance on a successful run, which is what makes the profile a
meaningful "atomic activation" marker rather than just a label.

Each plan's capsules and artifacts are registered as GC roots under
`/nix/var/nix/gcroots/rigx-<testbed>/<plan>/`: the plan itself records
no references (see `rigx.deploy.pack`), so without them a
`nix-collect-garbage` on the host would delete what the units run.

`bin/rollback` is for explicit operator-triggered rollback to the
*previous* generation (`nix-env --profile ... --rollback`), then
re-invokes that generation's own `bin/activate` — which converges the
running units to match it (including removing services the newer
generation added), reusing the same diff/health-gate logic rather than
duplicating it. Since the profile already points at that generation,
activate doesn't `--set` it again, so no new generation is created and
a second rollback goes one further back. If the older generation fails
its gates, the profile is switched forward again.
"""

from __future__ import annotations

from rigx.deploy import units

_SH_HEADER = "#!/bin/sh\nset -eu\n"

# `sudo` resets PATH to sudoers' `secure_path`, which does not include
# /nix/var/nix/profiles/default/bin — where a multi-user Nix install puts
# nix-env. So `sudo <plan>/bin/activate` runs with nix-env unreachable,
# and the script dies on its very last line, after the services are
# already up: the deployment works but never records itself.
# `rigx.deploy.bootstrap._NIX_CHECK` makes the same allowance for `nix`.
_RESOLVE_NIX_ENV = [
    ': "${RIGX_NIX_ENV:=}"',
    'if [ -z "$RIGX_NIX_ENV" ]; then',
    "  if command -v nix-env >/dev/null 2>&1; then",
    "    RIGX_NIX_ENV=nix-env",
    "  elif [ -x /nix/var/nix/profiles/default/bin/nix-env ]; then",
    "    RIGX_NIX_ENV=/nix/var/nix/profiles/default/bin/nix-env",
    "  else",
    '    echo "rigx: nix-env not found (looked on PATH and in'
    ' /nix/var/nix/profiles/default/bin)" >&2',
    "    exit 1",
    "  fi",
    "fi",
]


def _quote(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


def render_activate(manifest: dict) -> str:
    """Render `bin/activate` for one plan.

    Which services this generation drops is worked out when the script
    runs, against the plan that is actually live on the host
    (`$RIGX_STATE_ROOT/<testbed>/live`), not when the plan is built:
    under `bin/rollback`, an older plan's activate runs against a newer
    live plan its builder never saw. The drops happen only after every
    health gate has passed, so a failed deploy leaves them running."""
    testbed = manifest["testbed"]

    lines: list[str] = [_SH_HEADER.rstrip("\n"), ""]
    lines.append(f"TESTBED={_quote(testbed)}")
    # Overridable for testing (real behavioral execution against a temp
    # filesystem + faked systemctl/nix-env, no root/real systemd/Nix
    # profile store needed); defaults are exactly the design's paths.
    lines.append(': "${RIGX_SYSTEMD_DIR:=/etc/systemd/system}"')
    lines.append(': "${RIGX_STATE_ROOT:=/var/lib/rigx}"')
    lines.append(': "${RIGX_PROFILE_DIR:=/nix/var/nix/profiles}"')
    lines.append(': "${RIGX_GCROOTS_DIR:=/nix/var/nix/gcroots}"')
    # `RIGX_ENGINE` overrides container-engine detection (below).
    lines.append(f'PROFILE="$RIGX_PROFILE_DIR/rigx-{testbed}"')
    lines.append('SELF="$(cd "$(dirname "$0")/.." && pwd -P)"')
    # The plan whose units are installed right now. Recorded by the last
    # successful activation; before that (hosts deployed by an older
    # rigx), the profile is the best record there is. Not simply the
    # profile: `bin/rollback` moves the profile back *before* running
    # the older plan's activate, which must still see the newer plan as
    # what it is replacing.
    lines.append(f'LIVE="$RIGX_STATE_ROOT/{testbed}/live"')
    lines.append('PREV=""')
    lines.append('if [ -e "$LIVE" ]; then PREV="$(readlink -f "$LIVE")"')
    lines.append('elif [ -e "$PROFILE" ]; then PREV="$(readlink -f "$PROFILE")"; fi')
    # Where `systemctl enable` would put the boot-time link. Done by hand
    # because the units are themselves symlinks into the store, which
    # `systemctl enable` treats as linked units with their own rules.
    lines.append('WANTS="$RIGX_SYSTEMD_DIR/multi-user.target.wants"')
    lines.extend(_RESOLVE_NIX_ENV)
    lines.append("")
    # Reverts just the services this run has already restarted, back
    # to whatever the previous generation had (or removes them, if
    # this is a first-ever deploy with no previous generation).
    lines.append("rigx_rollback_partial() {")
    lines.append('  for s in "$@"; do')
    # Every unit a service can own. Naming all three unconditionally is
    # deliberate: for a service with no healthcheck the health units
    # won't exist in $PREV either, so they get rm -f'd — which is also
    # exactly the right cleanup when the previous generation *did* have
    # a healthcheck and this one dropped it.
    lines.append(
        f'    for u in "rigx-{testbed}-$s.service" '
        f'"rigx-{testbed}-$s-health.service" '
        f'"rigx-{testbed}-$s-health.timer"; do'
    )
    lines.append('      if [ -n "$PREV" ] && [ -e "$PREV/units/$u" ]; then')
    lines.append('        ln -sf "$PREV/units/$u" "$RIGX_SYSTEMD_DIR/$u"')
    lines.append("      else")
    lines.append('        rm -f "$RIGX_SYSTEMD_DIR/$u" "$WANTS/$u"')
    lines.append("      fi")
    lines.append("    done")
    lines.append("  done")
    lines.append("  systemctl daemon-reload")
    lines.append('  for s in "$@"; do')
    lines.append(f'    u="rigx-{testbed}-$s.service"')
    lines.append('    if [ -n "$PREV" ] && [ -e "$PREV/units/$u" ]; then')
    lines.append('      systemctl restart "$u" || true')
    lines.append("    else")
    lines.append('      systemctl stop "$u" || true')
    # Nothing is left to supervise this service, so nothing is left to
    # notice if its container outlived the stop — and a container that
    # outlives a *failed* deploy is the worst version of the problem:
    # it holds the ports the next attempt needs while answering the
    # health probe that decides whether that attempt worked.
    #
    # Units this generation installs reap themselves via ExecStopPost=,
    # but a rollback stops whatever is on disk, which may be an older
    # generation's unit that predates it. Reaping here covers that.
    lines.append(
        "      " + units.reap_command(f'"rigx-{testbed}-$s"')
    )
    lines.append("    fi")
    lines.append("  done")
    lines.append("}")
    lines.append("")

    # Persistent volume / secrets-cred directories — created once,
    # idempotent, never touched again by later generations.
    for vol in manifest["volumes"]:
        lines.append(f'mkdir -p "$RIGX_STATE_ROOT/{testbed}/volumes/{vol}"')
    if manifest["secrets"]:
        lines.append(f'mkdir -p "$RIGX_STATE_ROOT/{testbed}/secrets"')
    # HOME for the service units — systemd provides none for system
    # services, and the lite capsule runner needs somewhere writable for
    # its per-container Nix profile/gcroot dirs.
    lines.append(f'mkdir -p "$RIGX_STATE_ROOT/{testbed}/home"')
    lines.append("")

    # The shared per-testbed network, created before any unit that
    # joins it starts. Idempotent via `network inspect`, which both
    # engines have (podman also has `network exists`; docker doesn't).
    #
    # The engine is detected here rather than taken from the plan: the
    # runner picks it at run time on the target, so the plan can't know
    # it at build time. Same two-line probe the runners use — see
    # `rigx.nix.capsule_common`.
    network = manifest.get("network")
    if network:
        lines.append(': "${RIGX_ENGINE:=}"')
        lines.append('if [ -z "$RIGX_ENGINE" ]; then')
        lines.append("  if command -v podman >/dev/null 2>&1; then RIGX_ENGINE=podman")
        lines.append("  elif command -v docker >/dev/null 2>&1; then RIGX_ENGINE=docker")
        lines.append("  else")
        lines.append(
            '    echo "rigx: podman or docker is required on PATH" >&2; exit 1'
        )
        lines.append("  fi")
        lines.append("fi")
        lines.append(
            f'"$RIGX_ENGINE" network inspect {_quote(network)} >/dev/null 2>&1 || '
            f'"$RIGX_ENGINE" network create {_quote(network)}'
        )
        lines.append("")

    # Before any unit (re)starts, so a GC can't take a capsule away
    # between linking a unit and running it.
    lines.extend(_render_gc_roots(testbed, manifest))
    lines.append("")

    # Install/diff each unit; CHANGED tracks which ones actually differ
    # from what's currently on disk.
    lines.append('CHANGED=""')
    for sname in manifest["start_order"]:
        svc = manifest["services"][sname]
        lines.append(f"# service: {sname}")
        # A service owns more than one unit once it has a healthcheck.
        # Any of them differing counts as the service having changed —
        # deliberately slightly over-eager (editing only the healthcheck
        # command restarts the service too), in exchange for not needing
        # to reason about which unit changes require which action.
        lines.append("_changed=")
        for unit in units.service_unit_names(testbed, sname, svc):
            new_path = f'"$SELF/units/{unit}"'
            live_path = f'"$RIGX_SYSTEMD_DIR/{unit}"'
            lines.append(f"if ! cmp -s {new_path} {live_path} 2>/dev/null; then")
            lines.append(f"  ln -sf {new_path} {live_path}")
            lines.append("  _changed=1")
            lines.append("fi")
        if svc["healthcheck"] is None:
            # Previous generation may have had a healthcheck for this
            # service; leave no orphaned probe behind.
            for unit in (
                units.health_timer_name(testbed, sname),
                units.health_unit_name(testbed, sname),
            ):
                lines.append(f'if [ -e "$RIGX_SYSTEMD_DIR/{unit}" ]; then')
                lines.append(
                    f"  systemctl disable --now {_quote(unit)} 2>/dev/null || true"
                )
                lines.append(f'  rm -f "$RIGX_SYSTEMD_DIR/{unit}"')
                lines.append("  _changed=1")
                lines.append("fi")
        # Started at boot, as `WantedBy=multi-user.target` asks; without
        # this link a reboot leaves every service (and its health timer,
        # pulled in by the service's Wants=) down.
        main = units.unit_name(testbed, sname)
        lines.append('mkdir -p "$WANTS"')
        lines.append(f'ln -sf "$RIGX_SYSTEMD_DIR/{main}" "$WANTS/{main}"')
        lines.append('if [ -n "$_changed" ]; then')
        lines.append(f'  CHANGED="$CHANGED {sname}"')
        lines.append("fi")
    lines.append("")
    lines.append('if [ -n "$CHANGED" ]; then')
    lines.append("  systemctl daemon-reload")
    lines.append("fi")
    lines.append("")

    # Restart + health-gate only the services that changed, in
    # dependency order. A gate failure rolls back everything CHANGED
    # so far (including this service) and exits.
    for sname in manifest["start_order"]:
        svc = manifest["services"][sname]
        unit = units.unit_name(testbed, sname)
        lines.append(f"# {sname}: restart + health-gate if changed")
        lines.append('case " $CHANGED " in')
        lines.append(f'  *" {sname} "*)')
        lines.append(
            f"    systemctl restart {_quote(unit)} || "
            f"{{ rigx_rollback_partial $CHANGED; exit 1; }}"
        )
        hc = svc["healthcheck"]
        if hc is not None:
            lines.append("    i=0")
            lines.append(
                f"    until {units.probe_command(testbed, sname, hc)}; do"
            )
            lines.append("      i=$((i + 1))")
            lines.append(f'      if [ "$i" -ge {hc["retries"]} ]; then')
            where = (
                " (probed inside its container)"
                if hc.get("scope", "host") == "container" else ""
            )
            lines.append(
                f'        echo "rigx: {sname} failed healthcheck after '
                f'{hc["retries"]} tries{where}" >&2'
            )
            lines.append("        rigx_rollback_partial $CHANGED")
            lines.append("        exit 1")
            lines.append("      fi")
            lines.append(f'      sleep {hc["interval"]}')
            lines.append("    done")
        else:
            lines.append(f"    if ! systemctl is-active --quiet {_quote(unit)}; then")
            lines.append(
                f'      echo "rigx: {sname} did not become active" >&2'
            )
            lines.append("      rigx_rollback_partial $CHANGED")
            lines.append("      exit 1")
            lines.append("    fi")
        lines.append("    ;;")
        lines.append("esac")
    lines.append("")

    lines.extend(_render_removals(testbed, manifest))
    lines.append("")

    # Only reached if every gate above passed. `--set` only when the
    # profile isn't already here: under `bin/rollback` it already points
    # at this (older) generation, and setting it again would create a
    # new generation, so a second rollback would land on the bad one.
    lines.append('ln -sfn "$SELF" "$LIVE"')
    lines.append(
        'if [ "$(readlink -f "$PROFILE" 2>/dev/null || true)" != "$SELF" ]; then'
    )
    lines.append('  "$RIGX_NIX_ENV" --profile "$PROFILE" --set "$SELF"')
    lines.append("fi")
    lines.append("")
    return "\n".join(lines)


def _render_gc_roots(testbed: str, manifest: dict) -> list[str]:
    """Register this plan's capsules and artifacts as GC roots.

    The plan reaches the store through `nix store add`, which records no
    references, so the profile keeps only the plan alive and
    `nix-collect-garbage` would delete the capsules the units run. Each
    plan gets a root directory; directories for plans that are neither
    live nor a profile generation are pruned, so deleting generations
    frees their capsules at the next activation."""
    paths = sorted({
        p for svc in manifest["services"].values()
        for p in [svc["capsule_path"]]
        + [a["store_path"] for a in svc.get("artifacts") or []]
    })
    lines = [
        f'ROOTS="$RIGX_GCROOTS_DIR/rigx-{testbed}"',
        'MYROOTS="$ROOTS/${SELF##*/}"',
        'mkdir -p "$MYROOTS"',
        'ln -sfn "$SELF" "$MYROOTS/plan"',
    ]
    for i, p in enumerate(paths):
        lines.append(f"ln -sfn {_quote(p)} \"$MYROOTS/{i}\"")
    lines += [
        # Plans still in use: this one, the live one, every generation.
        'KEEP=" ${SELF##*/} ${PREV##*/} "',
        'for g in "$RIGX_PROFILE_DIR/rigx-$TESTBED-"*-link; do',
        "  " + _GENERATION_CHECK,
        '  _t=$(readlink -f "$g"); KEEP="$KEEP ${_t##*/} "',
        "done",
        'for d in "$ROOTS"/*; do',
        '  [ -d "$d" ] || continue',
        '  case "$KEEP" in *" ${d##*/} "*) ;; *) rm -rf "$d" ;; esac',
        "done",
    ]
    return lines


# Inside a loop over `rigx-$TESTBED-*-link`: skip entries whose middle
# isn't all digits. The glob alone would also match generations of a
# testbed whose name merely starts with ours (`app` vs `app-staging`).
_GENERATION_CHECK = (
    '_n=${g##*/}; _n=${_n#rigx-$TESTBED-}; _n=${_n%-link}; '
    'case "$_n" in ""|*[!0-9]*) continue ;; esac'
)


def _render_removals(testbed: str, manifest: dict) -> list[str]:
    """Remove services the live plan has and this one doesn't. Only the
    live plan's own units directory is globbed, which holds this
    testbed's units and nothing else."""
    mine = " ".join(units.unit_name(testbed, s) for s in manifest["services"])
    return [
        "# Services this generation drops: stop, disable, unlink, reap.",
        f"MINE=' {mine} '",
        "_removed=",
        'if [ -n "$PREV" ] && [ "$PREV" != "$SELF" ]; then',
        f'  for f in "$PREV/units/rigx-{testbed}-"*.service; do',
        '    [ -e "$f" ] || continue',
        '    u=${f##*/}',
        '    case "$u" in *-health.service) continue ;; esac',
        '    case "$MINE" in *" $u "*) continue ;; esac',
        '    c=${u%.service}',
        '    for x in "$c-health.timer" "$c-health.service" "$u"; do',
        '      systemctl disable --now "$x" 2>/dev/null || true',
        '      rm -f "$RIGX_SYSTEMD_DIR/$x" "$WANTS/$x"',
        "    done",
        "    " + units.reap_command('"$c"'),
        "    _removed=1",
        "  done",
        "fi",
        'if [ -n "$_removed" ]; then systemctl daemon-reload; fi',
    ]


def render_rollback(manifest: dict) -> str:
    """Explicit operator rollback: point the Nix profile back at the
    previous generation, then let *that* generation's own `activate`
    reconcile the running units — reuses the diff/health-gate logic
    above instead of duplicating it."""
    testbed = manifest["testbed"]
    return (
        _SH_HEADER
        + "\n"
        + ': "${RIGX_PROFILE_DIR:=/nix/var/nix/profiles}"\n'
        + f'PROFILE="$RIGX_PROFILE_DIR/rigx-{testbed}"\n'
        + "\n".join(_RESOLVE_NIX_ENV) + "\n"
        # Remember where we were, so a failed rollback can come back.
        + 'GEN="$("$RIGX_NIX_ENV" --profile "$PROFILE" --list-generations '
        + "2>/dev/null | awk '/\\(current\\)/ {print $1}')\"\n"
        + '"$RIGX_NIX_ENV" --profile "$PROFILE" --rollback\n'
        + 'NEW_SELF="$(readlink -f "$PROFILE")"\n'
        # Not exec: if the older plan's gates fail, its activate has put
        # the live plan's units back, and the profile must follow.
        + 'if ! "$NEW_SELF/bin/activate"; then\n'
        + '  if [ -n "$GEN" ]; then\n'
        + '    "$RIGX_NIX_ENV" --profile "$PROFILE" --switch-generation "$GEN"\n'
        + "  fi\n"
        + "  exit 1\n"
        + "fi\n"
    )
