"""Shared helpers for all capsule backends (lite, nixos, qemu).

Includes the on-disk manifest schema, the bash runner body, and the
volume/env helpers that any capsule needs at startup.
"""

from __future__ import annotations

from rigx.config import Target
from rigx.nix.render import shell_quote


def capsule_image_tag(target: Target) -> str:
    """OCI tag baked into the lite-capsule image. Stable across rebuilds
    so the runner script can `docker load` and reference by tag without
    looking up the digest."""
    safe = target.qualified_name.replace(".", "_").replace("/", "_")
    return f"rigx/{safe}:latest"


def internal_dep_paths_expr(target: Target, project) -> str:
    """A Nix list expression of every `deps.internal` store path.

    Written into the capsule's `manifest.json`, and it has to be, because
    of how Nix computes references: it scans a derivation's output for
    store-path *strings*. A capsule's entrypoint is baked into the OCI
    image's `Cmd`, and the image ships as `image/image.tar.gz` — gzipped,
    so the path is not findable by scanning. The result is a capsule
    whose recorded references are bash and nothing else: it does not
    claim to depend on the program it exists to run.

    That breaks two things. `nix copy` of the capsule doesn't bring the
    application with it, so a deploy lands a container that exits 127.
    And locally, `nix-collect-garbage` is free to delete the entrypoint
    while keeping the capsule, quietly breaking an artifact that looks
    fine.

    Naming the paths in a plaintext file makes the closure honest.
    """
    from rigx.nix.render import nix_interp_str, rewrite_interp

    items = [
        nix_interp_str(rewrite_interp("${" + dep + "}", project))
        for dep in target.deps.internal
    ]
    return "[ " + " ".join(items) + " ]" if items else "[ ]"


def capsule_manifest(target: Target) -> dict:
    """The on-disk contract orchestrators (rigx.capsule, rigx.testbed,
    future mixed-backend tools) read instead of reaching into Nix attrs.
    Captures everything that varies per capsule and that a downstream
    process needs to know without re-evaluating the flake."""
    hostname = target.hostname or target.name
    manifest: dict = {
        "name": target.qualified_name,
        "backend": target.backend,
        "hostname": hostname,
        "ports": list(target.ports),
    }
    if target.backend == "lite":
        manifest["image"] = {
            "kind": "oci-tarball-host-store",
            "path": "image/image.tar.gz",
            "tag": capsule_image_tag(target),
        }
    elif target.backend == "nixos":
        manifest["image"] = {
            "kind": "oci-tarball-nixos-systemd",
            "path": "image/image.tar.gz",
            "tag": capsule_image_tag(target),
        }
    elif target.backend == "qemu":
        manifest["image"] = {
            "kind": "qemu-nixos-vm",
            "path": "system/vm-script",
        }
    if target.env:
        manifest["env"] = dict(target.env)
    return manifest


# Sentinels in the runner-script source for stuff we want resolved at
# Nix-eval time. After bash-escaping the body, these get substituted
# with bare `${capsule…_<name>}` Nix interpolation tokens that the
# evaluator resolves against the let scope of the calling derivation.
PLACEHOLDER_PATH = "@@RIGX_PATH@@"
PLACEHOLDER_BASH_BIN = "@@RIGX_BASH_BIN@@"


def emit_toml_volumes_array(target: Target) -> str:
    """Bake `target.volumes` into a bash array literal in the runner.

    Each element is a `host:container:mode` triple. Empty array if no
    volumes are declared. Bash quoting is permissive here — the config
    layer rejects `:` and `,` in either path, so the only quoting we
    need to handle is shell metacharacters like spaces."""
    if not target.volumes:
        return "TOML_VOLUMES=()"
    lines = ["TOML_VOLUMES=("]
    for v in target.volumes:
        triple = f"{v.host}:{v.container}:{v.mode}"
        lines.append(f"    {shell_quote(triple)}")
    lines.append(")")
    return "\n".join(lines)


def emit_volume_helpers(label: str) -> str:
    """Bash helpers that resolve a `host:cont[:mode]` spec, prepending
    `RIGX_PROJECT_ROOT` to a relative host path. The helpers are
    embedded in the runner; they're shared verbatim between the
    `run-` and `shell-` runners and the nixos runner."""
    return f"""\
RIGX_PROJECT_ROOT="${{RIGX_PROJECT_ROOT:-}}"
if [ -z "$RIGX_PROJECT_ROOT" ]; then
    _d="$PWD"
    while [ "$_d" != "/" ] && [ -n "$_d" ]; do
        if [ -f "$_d/rigx.toml" ]; then RIGX_PROJECT_ROOT="$_d"; break; fi
        _d="$(dirname "$_d")"
    done
fi

_rigx_resolve_volume() {{
    local spec="$1" h c m colons
    colons=$(awk -F: '{{print NF-1}}' <<< "$spec")
    case "$colons" in
        1) h="${{spec%:*}}"; c="${{spec##*:}}"; m="rw";;
        2) h="${{spec%%:*}}"; rest="${{spec#*:}}"
           c="${{rest%:*}}"; m="${{rest##*:}}";;
        *) echo "{label}: malformed volume spec: $spec" >&2; exit 1;;
    esac
    case "$h" in
        /*|~*) ;;
        *) if [ -z "$RIGX_PROJECT_ROOT" ]; then
               echo "{label}: relative host path '$h' needs RIGX_PROJECT_ROOT or a rigx.toml in \\$PWD or above" >&2
               exit 1
           fi
           h="$RIGX_PROJECT_ROOT/$h" ;;
    esac
    printf '%s:%s:%s' "$h" "$c" "$m"
}}"""


def _emit_runtime_spec_loader(label: str) -> str:
    """Bash that loads `RIGX_RUNTIME_SPEC` (a JSON file) and surfaces its
    fields onto the RIGX_* env vars the rest of the runner consumes.

    Spec shape (all fields optional):
      { "name": str,
        "network": str,
        "detach": bool,
        "publish": ["host:cont", ...],
        "publish_udp": ["host:cont", ...],
        "env": { "KEY": "VALUE", ... },
        "volumes": ["host:cont:mode", ...],
        "user": str,
        "keep_state": bool }

    The spec is authoritative: any field present in it OVERRIDES the
    corresponding inherited RIGX_* env var (orchestrators set both; the
    legacy vars only exist so runners built by an older rigx keep
    working). Env entries are surfaced as the `_RIGX_SPEC_ENV` bash
    array — one `KEY=VALUE` element each — so values containing commas
    survive intact; values must not contain newlines (the orchestrator
    rejects those up front). If `RIGX_RUNTIME_SPEC` is unset, no
    overrides happen and the legacy comma-separated RIGX_PUBLISH /
    RIGX_ENV / RIGX_VOLUMES variables apply as they did before.
    """
    return f"""\
_RIGX_SPEC_ENV=()
if [ -n "${{RIGX_RUNTIME_SPEC:-}}" ]; then
    if ! [ -r "$RIGX_RUNTIME_SPEC" ]; then
        echo "{label}: RIGX_RUNTIME_SPEC ($RIGX_RUNTIME_SPEC) is not readable" >&2
        exit 1
    fi
    if command -v jq >/dev/null 2>&1; then
        _rigx_jq() {{ jq -r "$@" "$RIGX_RUNTIME_SPEC" 2>/dev/null || true; }}
        _v=$(_rigx_jq '.name // empty');           [ -n "$_v" ] && RIGX_NAME="$_v"
        _v=$(_rigx_jq '.network // empty');        [ -n "$_v" ] && RIGX_NETWORK="$_v"
        _v=$(_rigx_jq '.user // empty');           [ -n "$_v" ] && RIGX_USER="$_v"
        _v=$(_rigx_jq 'if .detach then "1" else empty end');     [ -n "$_v" ] && RIGX_DETACH="$_v"
        _v=$(_rigx_jq 'if .keep_state then "1" else empty end'); [ -n "$_v" ] && RIGX_KEEP_STATE="$_v"
        _v=$(_rigx_jq '(.publish // []) | join(",")');     [ -n "$_v" ] && RIGX_PUBLISH="$_v"
        _v=$(_rigx_jq '(.publish_udp // []) | join(",")'); [ -n "$_v" ] && RIGX_PUBLISH_UDP="$_v"
        _v=$(_rigx_jq '(.volumes // []) | join(",")');     [ -n "$_v" ] && RIGX_VOLUMES="$_v"
        _v=$(_rigx_jq '.env // {{}} | to_entries | map("\\(.key)=\\(.value)") | .[]')
        if [ -n "$_v" ]; then
            RIGX_ENV=""
            while IFS= read -r _e; do
                [ -n "$_e" ] && _RIGX_SPEC_ENV+=("$_e")
            done <<< "$_v"
        fi
    else
        echo "{label}: RIGX_RUNTIME_SPEC set but jq is not on PATH; ignoring (legacy RIGX_* vars still apply)" >&2
    fi
fi
"""


def capsule_runner_script(target: Target, *, mode: str) -> str:
    """Body of the bash runner that starts a lite-mode capsule.

    Two flavors share most of the boilerplate (engine detection, host-store
    mount setup, per-container nix-state dirs, port/env/volume plumbing):
    `mode = "run"` runs the user's entrypoint as the container's command;
    `mode = "shell"` overrides the entrypoint with bash so you can poke
    around inside.

    Env knobs the orchestrator (rigx.capsule / rigx.testbed) sets:
      RIGX_RUNTIME_SPEC absolute path to a JSON spec; when set,
                        seeds the other RIGX_* knobs below from JSON
                        (primary path; the inline RIGX_* still work
                        as a fallback for older orchestrators).
      RIGX_NAME         stable container name (default: random uuid)
      RIGX_DETACH       1 = `-d` instead of `-it`; default interactive
      RIGX_FOREGROUND   1 = attached with no TTY (neither `-d` nor
                        `-it`). For running under a process supervisor:
                        the engine stays in the foreground so the
                        supervisor sees the container exit, but no TTY
                        is requested because there isn't one. Ignored
                        when RIGX_DETACH is also set. Used by
                        `rigx.deploy.units`.
      RIGX_NETWORK      join an existing container network
      RIGX_NETWORK_ALIAS
                        extra name this container answers to on
                        RIGX_NETWORK (`--network-alias`), so peers can
                        use a short stable name rather than the unique
                        container name in RIGX_NAME. Set by
                        `rigx.deploy.units`; ignored without
                        RIGX_NETWORK.
      RIGX_PUBLISH      comma-list of `host:container` port pairs
      RIGX_PUBLISH_UDP  comma-list of `host:container` UDP pairs
      RIGX_ENV          comma-list of `KEY=VALUE` extra env vars
      RIGX_VOLUMES      comma-list of `host:container[:mode]` bind-mounts
                        appended to the TOML-declared set; relative `host`
                        paths resolve against `RIGX_PROJECT_ROOT`
      RIGX_PROJECT_ROOT absolute project root (used to resolve relative
                        host paths in TOML and RIGX_VOLUMES). Auto-detected
                        by walking up from `$PWD` if unset.
      RIGX_USER         override the TOML-declared `user`. Empty/unset
                        falls back to the TOML default.
      RIGX_KEEP_STATE   1 = persist per-container profile/gcroot dirs
    """
    image_tag = capsule_image_tag(target)
    hostname = target.hostname or target.name
    label = f"{mode}-{target.name}"
    entrypoint_flag = (
        f'--entrypoint "{PLACEHOLDER_BASH_BIN}"' if mode == "shell" else ""
    )
    toml_volumes_block = emit_toml_volumes_array(target)
    volume_helpers_block = emit_volume_helpers(label)
    runtime_spec_loader = _emit_runtime_spec_loader(label)
    user_default = target.user
    return f"""\
#!/usr/bin/env bash
set -euo pipefail

if command -v podman >/dev/null 2>&1; then ENGINE=podman
elif command -v docker >/dev/null 2>&1; then ENGINE=docker
else echo "{label}: podman or docker is required on PATH" >&2; exit 1
fi

HERE="$(cd "$(dirname "$0")/.." && pwd)"

{runtime_spec_loader}
# Per-container nix-state dirs on host, cleaned up on exit unless the
# orchestrator wants them persisted (RIGX_KEEP_STATE=1).
# HOME has a fallback because this runner is not always started by a
# login: systemd does not set HOME for system services, and the runner
# runs under `set -u`, so a bare $HOME aborts before the container is
# ever created. `rigx.deploy.units` sets HOME explicitly; this keeps any
# other non-interactive caller working too.
RIGX_CACHE="${{XDG_CACHE_HOME:-${{HOME:-/tmp}}/.cache}}/rigx/lite-state"
CID="${{RIGX_NAME:-rigx-{target.name}-$(uuidgen 2>/dev/null || cat /proc/sys/kernel/random/uuid)}}"
PROFILE_DIR="$RIGX_CACHE/profiles/$CID"
GCROOT_DIR="$RIGX_CACHE/gcroots/$CID"
mkdir -p "$PROFILE_DIR" "$GCROOT_DIR"
if [ -z "${{RIGX_KEEP_STATE:-}}" ]; then
    trap 'rm -rf "$PROFILE_DIR" "$GCROOT_DIR"' EXIT
fi

echo "[{label}] loading {image_tag}" >&2
"$ENGINE" load < "$HERE/image/image.tar.gz" >&2

RUN_FLAGS=(--rm)
if [ -n "${{RIGX_DETACH:-}}" ]; then
    RUN_FLAGS+=(-d)
elif [ -n "${{RIGX_FOREGROUND:-}}" ]; then
    # Attached but no TTY: the supervisor (systemd) wants the engine
    # to stay in the foreground so it can see the container exit, but
    # ExecStart's stdin is /dev/null, and `-t` against a non-terminal
    # is a hard error on docker.
    :
else
    RUN_FLAGS+=(-it)
fi
[ -n "${{RIGX_NAME:-}}" ] && RUN_FLAGS+=(--name "$RIGX_NAME")
[ -n "${{RIGX_NETWORK:-}}" ] && RUN_FLAGS+=(--network "$RIGX_NETWORK")
# A short name peers can dial, independent of the container's own
# (necessarily unique, necessarily long) --name.
[ -n "${{RIGX_NETWORK_ALIAS:-}}" ] && RUN_FLAGS+=(--network-alias "$RIGX_NETWORK_ALIAS")

USER_VAL="${{RIGX_USER:-{user_default}}}"
[ -n "$USER_VAL" ] && RUN_FLAGS+=(--user "$USER_VAL")

if [ -n "${{RIGX_PUBLISH:-}}" ]; then
    IFS=',' read -ra PUBS <<< "$RIGX_PUBLISH"
    for p in "${{PUBS[@]}}"; do RUN_FLAGS+=(-p "$p"); done
fi
if [ -n "${{RIGX_PUBLISH_UDP:-}}" ]; then
    IFS=',' read -ra PUBS <<< "$RIGX_PUBLISH_UDP"
    for p in "${{PUBS[@]}}"; do RUN_FLAGS+=(-p "$p/udp"); done
fi

if [ ${{#_RIGX_SPEC_ENV[@]}} -gt 0 ]; then
    for e in "${{_RIGX_SPEC_ENV[@]}}"; do RUN_FLAGS+=(-e "$e"); done
elif [ -n "${{RIGX_ENV:-}}" ]; then
    IFS=',' read -ra EVS <<< "$RIGX_ENV"
    for e in "${{EVS[@]}}"; do RUN_FLAGS+=(-e "$e"); done
fi

{volume_helpers_block}
{toml_volumes_block}
for _v in "${{TOML_VOLUMES[@]}}"; do
    RUN_FLAGS+=(-v "$(_rigx_resolve_volume "$_v")")
done
if [ -n "${{RIGX_VOLUMES:-}}" ]; then
    IFS=',' read -ra _RUNTIME_VOLS <<< "$RIGX_VOLUMES"
    for _v in "${{_RUNTIME_VOLS[@]}}"; do
        RUN_FLAGS+=(-v "$(_rigx_resolve_volume "$_v")")
    done
fi

# A container from a previous run can outlive its runner: killing an
# attached `run` does not reliably stop the container, so `--rm` never
# fires and the name stays taken. The next start then dies with "name
# already in use" — and under a supervisor that is silent, because the
# stale container is still serving. A health check aimed at the port
# passes, so a failed deploy looks like a successful one.
#
# The unit owns this name, so reclaiming it is always the right call.
if [ -n "${{RIGX_NAME:-}}" ]; then
    "$ENGINE" rm -f "$RIGX_NAME" >/dev/null 2>&1 || true
fi

exec "$ENGINE" run "${{RUN_FLAGS[@]}}" \\
    --hostname "{hostname}" \\
    -v /nix/store:/nix/store:ro \\
    -v /nix/var/nix/daemon-socket:/nix/var/nix/daemon-socket \\
    -v /etc/ssl/certs:/etc/ssl/certs:ro \\
    -v "$PROFILE_DIR":/nix/var/nix/profiles \\
    -v "$GCROOT_DIR":/nix/var/nix/gcroots \\
    -e "PATH={PLACEHOLDER_PATH}" \\
    -e "NIX_REMOTE=daemon" \\
    {entrypoint_flag} \\
    {image_tag} \\
    "$@"
"""


def nixos_runner_script(target: Target) -> str:
    """Body of the bash runner that starts a NixOS-mode capsule. Same
    RIGX_* contract as lite mode plus the runtime-spec JSON loader.

    `--privileged` is required for systemd-in-docker — see the README's
    security model section.
    """
    image_tag = capsule_image_tag(target)
    hostname = target.hostname or target.name
    label = f"run-{target.name}"
    nixos_volumes_array = emit_toml_volumes_array(target)
    nixos_volume_helpers = emit_volume_helpers(label)
    runtime_spec_loader = _emit_runtime_spec_loader(label)
    return f"""\
#!/usr/bin/env bash
set -euo pipefail

if command -v podman >/dev/null 2>&1; then ENGINE=podman
elif command -v docker >/dev/null 2>&1; then ENGINE=docker
else echo "{label}: podman or docker is required on PATH" >&2; exit 1
fi

HERE="$(cd "$(dirname "$0")/.." && pwd)"

{runtime_spec_loader}
CID="${{RIGX_NAME:-rigx-{target.name}-$(uuidgen 2>/dev/null || cat /proc/sys/kernel/random/uuid)}}"

echo "[{label}] loading {image_tag}" >&2
"$ENGINE" load < "$HERE/image/image.tar.gz" >&2

RUN_FLAGS=(--rm --privileged)
if [ -n "${{RIGX_DETACH:-}}" ]; then
    RUN_FLAGS+=(-d)
elif [ -n "${{RIGX_FOREGROUND:-}}" ]; then
    # See the lite runner: attached, no TTY, for supervisor use.
    :
else
    RUN_FLAGS+=(-it)
fi
[ -n "${{RIGX_NAME:-}}" ] && RUN_FLAGS+=(--name "$RIGX_NAME")
[ -n "${{RIGX_NETWORK:-}}" ] && RUN_FLAGS+=(--network "$RIGX_NETWORK")
# A short name peers can dial, independent of the container's own
# (necessarily unique, necessarily long) --name.
[ -n "${{RIGX_NETWORK_ALIAS:-}}" ] && RUN_FLAGS+=(--network-alias "$RIGX_NETWORK_ALIAS")

if [ -n "${{RIGX_PUBLISH:-}}" ]; then
    IFS=',' read -ra PUBS <<< "$RIGX_PUBLISH"
    for p in "${{PUBS[@]}}"; do RUN_FLAGS+=(-p "$p"); done
fi
if [ -n "${{RIGX_PUBLISH_UDP:-}}" ]; then
    IFS=',' read -ra PUBS <<< "$RIGX_PUBLISH_UDP"
    for p in "${{PUBS[@]}}"; do RUN_FLAGS+=(-p "$p/udp"); done
fi

if [ ${{#_RIGX_SPEC_ENV[@]}} -gt 0 ]; then
    for e in "${{_RIGX_SPEC_ENV[@]}}"; do RUN_FLAGS+=(-e "$e"); done
elif [ -n "${{RIGX_ENV:-}}" ]; then
    IFS=',' read -ra EVS <<< "$RIGX_ENV"
    for e in "${{EVS[@]}}"; do RUN_FLAGS+=(-e "$e"); done
fi

{nixos_volume_helpers}
{nixos_volumes_array}
for _v in "${{TOML_VOLUMES[@]}}"; do
    RUN_FLAGS+=(-v "$(_rigx_resolve_volume "$_v")")
done
if [ -n "${{RIGX_VOLUMES:-}}" ]; then
    IFS=',' read -ra _RUNTIME_VOLS <<< "$RIGX_VOLUMES"
    for _v in "${{_RUNTIME_VOLS[@]}}"; do
        RUN_FLAGS+=(-v "$(_rigx_resolve_volume "$_v")")
    done
fi

# A container from a previous run can outlive its runner: killing an
# attached `run` does not reliably stop the container, so `--rm` never
# fires and the name stays taken. The next start then dies with "name
# already in use" — and under a supervisor that is silent, because the
# stale container is still serving. A health check aimed at the port
# passes, so a failed deploy looks like a successful one.
#
# The unit owns this name, so reclaiming it is always the right call.
if [ -n "${{RIGX_NAME:-}}" ]; then
    "$ENGINE" rm -f "$RIGX_NAME" >/dev/null 2>&1 || true
fi

exec "$ENGINE" run "${{RUN_FLAGS[@]}}" \\
    --hostname "{hostname}" \\
    --tmpfs /run \\
    --tmpfs /tmp \\
    --tmpfs /var/log \\
    -v /nix/store:/nix/store:ro \\
    {image_tag} \\
    "$@"
"""


def qemu_runner_script(target: Target) -> str:
    """Body of the bash runner that boots a qemu-mode capsule.

    Translates the docker-shaped RIGX_PUBLISH contract to qemu-shaped
    `QEMU_NET_OPTS=hostfwd=...` rules and execs the underlying NixOS vm
    script. Volumes are not supported on qemu (declare them on lite/nixos).
    """
    hostname = target.hostname or target.name
    label = f"run-{target.name}"
    runtime_spec_loader = _emit_runtime_spec_loader(label)
    return f"""\
#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"

{runtime_spec_loader}
if [ -n "${{RIGX_VOLUMES:-}}" ]; then
    echo "{label}: RIGX_VOLUMES is not supported on backend=qemu" >&2
    echo "    (qemu volumes need NixOS-time virtualisation.sharedDirectories;" >&2
    echo "     declare them in rigx.toml on a lite/nixos capsule instead.)" >&2
    exit 1
fi

QEMU_NET_OPTS="${{QEMU_NET_OPTS:-}}"
_rigx_emit_fwd() {{
    local proto="$1" entry="$2"
    case "$entry" in
        */udp) proto="udp"; entry="${{entry%/udp}}";;
        */tcp) proto="tcp"; entry="${{entry%/tcp}}";;
    esac
    local cnt=$(awk -F: '{{print NF-1}}' <<< "$entry")
    case "$cnt" in
        1) host="${{entry%:*}}"; cont="${{entry##*:}}"
           echo "hostfwd=$proto::$host-:$cont";;
        2) addr="${{entry%%:*}}"; rest="${{entry#*:}}"
           host="${{rest%:*}}"; cont="${{rest##*:}}"
           echo "hostfwd=$proto:$addr:$host-:$cont";;
        *) echo "{label}: malformed publish entry: $entry" >&2; exit 1;;
    esac
}}
if [ -n "${{RIGX_PUBLISH:-}}" ]; then
    extra=""
    IFS=',' read -ra PUBS <<< "$RIGX_PUBLISH"
    for p in "${{PUBS[@]}}"; do
        rule=$(_rigx_emit_fwd tcp "$p")
        extra="${{extra:+$extra,}}$rule"
    done
    if [ -n "$extra" ]; then
        QEMU_NET_OPTS="${{QEMU_NET_OPTS:+$QEMU_NET_OPTS,}}$extra"
    fi
fi
if [ -n "${{RIGX_PUBLISH_UDP:-}}" ]; then
    extra=""
    IFS=',' read -ra PUBS <<< "$RIGX_PUBLISH_UDP"
    for p in "${{PUBS[@]}}"; do
        rule=$(_rigx_emit_fwd udp "$p")
        extra="${{extra:+$extra,}}$rule"
    done
    if [ -n "$extra" ]; then
        QEMU_NET_OPTS="${{QEMU_NET_OPTS:+$QEMU_NET_OPTS,}}$extra"
    fi
fi
export QEMU_NET_OPTS

exec "$HERE/system/vm-script/bin/run-{hostname}-vm" "$@"
"""
