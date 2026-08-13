"""Render `bin/activate` / `bin/rollback` for a testbed deployment plan.

Generated as POSIX `sh`, not bash — bootstrap only guarantees Nix +
systemd on the target (design decision #4), nothing about `/bin/sh`
beyond it existing. Every service gets its own unrolled block in the
generated script rather than parsing `manifest.json` at runtime: the
renderer already has the full manifest (and, for diffing removed
services, the previous generation's manifest) at generation time, so
there's no need for a JSON parser (jq/python3) to be present on the
target — that dependency would otherwise be a gap given this plan
isn't wired through rigx's own Nix build yet (see the packaging TODO
in `rigx.deploy.pack`).

Algorithm (design decision #3): for each service, compare the new
rendered unit file against whatever's actually symlinked at
`/etc/systemd/system/<unit>` right now (ground truth, not the profile
pointer) — daemon-reload once if anything changed, then restart only
the changed services in dependency order, health-gating each one
(design decision #7). Any gate failure re-links just the services this
run touched back to the previous generation's units (or removes them,
on a first-ever deploy with nothing to fall back to) and exits
non-zero. The Nix profile pointer
(`/nix/var/nix/profiles/rigx-<testbed>`) only advances to this
generation as the *last* line of a successful run, which is what makes
it a meaningful "atomic activation" marker rather than just a label.

`bin/rollback` is for explicit operator-triggered rollback to the
*previous* generation (`nix-env --profile ... --rollback`), then
re-invokes that generation's own `bin/activate` — which naturally
converges the running units to match it, reusing the same diff/health-
gate logic rather than duplicating it.
"""

from __future__ import annotations

from rigx.deploy import units

_SH_HEADER = "#!/bin/sh\nset -eu\n"


def _quote(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


def render_activate(manifest: dict, previous_manifest: dict | None = None) -> str:
    testbed = manifest["testbed"]

    lines: list[str] = [_SH_HEADER.rstrip("\n"), ""]
    lines.append(f"TESTBED={_quote(testbed)}")
    # Overridable for testing (real behavioral execution against a temp
    # filesystem + faked systemctl/nix-env, no root/real systemd/Nix
    # profile store needed); defaults are exactly the design's paths.
    lines.append(': "${RIGX_SYSTEMD_DIR:=/etc/systemd/system}"')
    lines.append(': "${RIGX_STATE_ROOT:=/var/lib/rigx}"')
    lines.append(': "${RIGX_PROFILE_DIR:=/nix/var/nix/profiles}"')
    lines.append(f'PROFILE="$RIGX_PROFILE_DIR/rigx-{testbed}"')
    lines.append('SELF="$(cd "$(dirname "$0")/.." && pwd)"')
    lines.append('PREV="$(readlink -f "$PROFILE" 2>/dev/null || true)"')
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
    lines.append('        rm -f "$RIGX_SYSTEMD_DIR/$u"')
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
    lines.append("")

    # Services present in the previous generation but gone from this
    # one: stop, disable, and remove their unit — computed here at
    # render time (diffing two manifests) rather than at runtime.
    if previous_manifest is not None:
        removed = sorted(set(previous_manifest["services"]) - set(manifest["services"]))
        for sname in removed:
            lines.append(f"# removed service: {sname}")
            # Timer first: it BindsTo= the service, so stopping the
            # service on its own would already take it down, but a
            # removed service should not leave a probe behind either way.
            for unit in (
                units.health_timer_name(testbed, sname),
                units.health_unit_name(testbed, sname),
                units.unit_name(testbed, sname),
            ):
                lines.append(
                    f"systemctl disable --now {_quote(unit)} 2>/dev/null || true"
                )
                lines.append(f'rm -f "$RIGX_SYSTEMD_DIR/{unit}"')
        if removed:
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
            lines.append(f'    until {hc["command"]}; do')
            lines.append("      i=$((i + 1))")
            lines.append(f'      if [ "$i" -ge {hc["retries"]} ]; then')
            lines.append(
                f'        echo "rigx: {sname} failed healthcheck after '
                f'{hc["retries"]} tries" >&2'
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

    # Last line, only reached if every gate above passed: advance the
    # profile pointer to this generation.
    lines.append('nix-env --profile "$PROFILE" --set "$SELF"')
    lines.append("")
    return "\n".join(lines)


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
        + 'nix-env --profile "$PROFILE" --rollback\n'
        + 'NEW_SELF="$(readlink -f "$PROFILE")"\n'
        + 'exec "$NEW_SELF/bin/activate"\n'
    )
