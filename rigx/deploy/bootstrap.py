"""SSH target bootstrap — the first step of `rigx remote deploy`.

Prepares a bare Linux host (no Nix, no rigx, nothing preinstalled) to
receive rigx deployments: detects OS/arch, requires systemd, installs
Nix if missing (multi-user, pinned Determinate Systems installer),
installs Podman if no container engine is present (`lite`/`nixos`
capsule runners hard-require podman or docker on PATH — see
`rigx.capsule._container_engine` — and bootstrap previously installed
neither, a real gap in the "target needs nothing preinstalled" promise
until now), and creates the unprivileged `rigx` service user that
later deploys connect as. Idempotent — safe to run again against an
already bootstrapped host.

Podman rather than Docker because it is daemonless: a deployed testbed
is a set of independently supervised systemd units, and Docker would
put every one of them behind a single daemon whose restart takes them
all down at once. An existing docker install on the target is still
accepted and used — this only decides what gets installed on a host
that has neither.

Plan compilation, `nix copy` transfer, and generation activation are
separate, not-yet-implemented pieces (see the project design memory).
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, replace
from urllib.parse import urlsplit

# Pinned Determinate Systems Nix installer release (multi-user install,
# non-interactive). Bump deliberately — check
# https://github.com/DeterminateSystems/nix-installer/releases — rather
# than tracking "latest", so deploys stay reproducible across time.
NIX_INSTALLER_VERSION = "v3.21.7"
NIX_INSTALLER_URL = (
    f"https://install.determinate.systems/nix/tag/{NIX_INSTALLER_VERSION}"
)

RIGX_SERVICE_USER = "rigx"


class DeployError(RuntimeError):
    """Raised for any failure preparing or talking to a deploy target."""


@dataclass(frozen=True)
class SSHTarget:
    """A parsed `ssh://[user@]host[:port]` deploy destination."""

    host: str
    user: str | None = None
    port: int | None = None

    @property
    def destination(self) -> str:
        return f"{self.user}@{self.host}" if self.user else self.host

    def ssh_argv(self) -> list[str]:
        argv = ["ssh", "-o", "BatchMode=yes"]
        if self.port:
            argv += ["-p", str(self.port)]
        argv.append(self.destination)
        return argv


def parse_ssh_url(url: str) -> SSHTarget:
    """Parse an `ssh://[user@]host[:port]` deploy target URL."""
    parts = urlsplit(url)
    if parts.scheme != "ssh":
        raise DeployError(f"deploy target must be an ssh:// URL, got {url!r}")
    if not parts.hostname:
        raise DeployError(f"ssh:// URL is missing a host: {url!r}")
    return SSHTarget(host=parts.hostname, user=parts.username, port=parts.port)


def run_remote(
    target: SSHTarget, command: str, *, check: bool = True
) -> subprocess.CompletedProcess:
    """Run `command` on `target` over ssh via the remote login shell,
    return the CompletedProcess. Every caller here passes a fixed
    string with no target-controlled interpolation."""
    proc = subprocess.run(
        target.ssh_argv() + [command],
        capture_output=True,
        text=True,
    )
    if check and proc.returncode != 0:
        raise DeployError(
            f"{target.destination}: remote command failed (exit "
            f"{proc.returncode}): {command}\n{proc.stderr.strip()}"
        )
    return proc


@dataclass(frozen=True)
class HostInfo:
    os_name: str
    arch: str
    has_systemd: bool
    has_nix: bool
    has_container_engine: bool
    # Non-empty when the target's container engine probably can't
    # resolve service names on a user-defined network. A warning rather
    # than an error: the deployment still starts, peer lookups just
    # fail — see `check_dns_backend`.
    dns_warning: str = ""


# `ssh host 'command -v nix'` doesn't reliably see a freshly installed
# Nix: profile.d scripts are sourced by login shells, and a plain `ssh
# host cmd` isn't guaranteed to be one. Fall back to the well-known
# multi-user install path so bootstrap doesn't false-negative right
# after `install_nix` ran.
_NIX_CHECK = "command -v nix || test -x /nix/var/nix/profiles/default/bin/nix"

# Either engine satisfies `rigx.capsule._container_engine` (podman
# tried first, docker as the fallback) — accept whichever is already
# on PATH rather than reinstalling over a working docker host.
_CONTAINER_ENGINE_CHECK = "command -v podman >/dev/null 2>&1 || command -v docker >/dev/null 2>&1"


def detect_host(target: SSHTarget) -> HostInfo:
    proc = run_remote(target, "uname -s && uname -m")
    parts = proc.stdout.split()
    if len(parts) != 2:
        raise DeployError(
            f"{target.destination}: unexpected `uname` output: {proc.stdout!r}"
        )
    os_name, arch = parts
    if os_name != "Linux":
        raise DeployError(
            f"{target.destination}: unsupported target OS {os_name!r} "
            f"(the SSH backend only supports Linux targets)"
        )
    has_systemd = run_remote(target, "command -v systemctl", check=False).returncode == 0
    has_nix = run_remote(target, _NIX_CHECK, check=False).returncode == 0
    has_container_engine = run_remote(
        target, _CONTAINER_ENGINE_CHECK, check=False,
    ).returncode == 0
    return HostInfo(
        os_name=os_name, arch=arch, has_systemd=has_systemd, has_nix=has_nix,
        has_container_engine=has_container_engine,
    )


def install_nix(target: SSHTarget) -> None:
    """Install Nix (multi-user) via the pinned Determinate Systems
    installer. Requires the connecting SSH user to have sudo — this is
    the only bootstrap step that does."""
    run_remote(
        target,
        f"curl --proto '=https' --tlsv1.2 -sSf -L {NIX_INSTALLER_URL} "
        f"| sudo sh -s -- install --no-confirm",
    )


def install_podman(target: SSHTarget) -> None:
    """Install Podman from the target distro's own package repository if
    no container engine is already present.

    Podman has no upstream convenience-script installer the way Docker
    does, and doesn't need one: it is a first-class package on every
    distro rigx targets, so this dispatches on whichever package
    manager the host actually has. Not version-pinned, for the same
    reason `install_nix` is: "whatever this distro currently calls
    podman" is far less likely to hard-fail than a pinned version that
    may not exist in a given target's repo.

    Nothing to enable afterwards — podman is daemonless, so unlike
    `docker.service` there is no socket or service to start, and no
    group to add anyone to. That is also why the deployed units run
    rootful rather than as a socket-group member; see
    `rigx.deploy.units`.
    """
    # Debian/Ubuntu ship apt-daily timers and unattended-upgrades, which
    # take the apt lock on their own schedule. Without a wait, a deploy
    # that lands during one dies with "Could not get lock
    # /var/lib/apt/lists/lock" — a transient collision presented as a
    # deploy failure. `DPkg::Lock::Timeout` (apt 2.0+) blocks instead;
    # older apt ignores unknown -o keys, so it is safe to pass anyway.
    apt = "sudo DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=300"
    install = " ; ".join([
        "if command -v apt-get >/dev/null 2>&1; then "
        f"{apt} update && "
        f"{apt} install -y podman",
        "elif command -v dnf >/dev/null 2>&1; then sudo dnf install -y podman",
        "elif command -v yum >/dev/null 2>&1; then sudo yum install -y podman",
        "elif command -v zypper >/dev/null 2>&1; then "
        "sudo zypper --non-interactive install podman",
        "elif command -v pacman >/dev/null 2>&1; then "
        "sudo pacman -Sy --noconfirm podman",
        "elif command -v apk >/dev/null 2>&1; then sudo apk add --no-cache podman",
        "else echo 'no supported package manager found (tried apt-get, dnf, "
        "yum, zypper, pacman, apk) — install podman or docker manually' >&2; "
        "exit 1",
        "fi",
    ])
    run_remote(target, f"{_CONTAINER_ENGINE_CHECK} || {{ {install} ; }}")


def check_dns_backend(target: SSHTarget) -> str:
    """Return a warning if service-name resolution on a user-defined
    network looks unavailable, else "".

    Services address each other as `<service>:<port>`, which needs the
    engine's DNS backend: netavark (with aardvark-dns) on modern
    podman, or the legacy CNI `dnsname` plugin. The failure mode is the
    reason this check exists at all — without a resolver, containers
    start fine and joining the network succeeds; only the *lookups*
    fail, at runtime, looking like an application bug. Better to say so
    at deploy time.

    Docker's embedded resolver is always present on user-defined
    networks, so docker hosts return "" without a probe.
    """
    proc = run_remote(
        target,
        "command -v podman >/dev/null 2>&1 && "
        "podman info --format '{{.Host.NetworkBackend}}' 2>/dev/null || true",
        check=False,
    )
    backend = proc.stdout.strip()
    if not backend:
        return ""  # docker-only host, or podman too old to report — leave it
    if backend == "netavark":
        return ""
    return (
        f"{target.destination}: podman is using the {backend!r} network "
        f"backend; service-name resolution needs netavark + aardvark-dns "
        f"(or the CNI 'dnsname' plugin). Services will start, but peer "
        f"lookups like 'db:5432' will fail at runtime. Install "
        f"aardvark-dns/netavark, or publish the ports and address peers "
        f"over the host instead."
    )


def ensure_rigx_user(target: SSHTarget) -> None:
    """Create the dedicated unprivileged `rigx` service user if it
    doesn't already exist.

    Subsequent (non-bootstrap) deploys are meant to connect as this
    user rather than root, but the sudoers policy that would let it
    manage systemd units without a password isn't designed yet — see
    the "ongoing privilege model" open item in the project design
    memory. For now this just creates the account.
    """
    run_remote(
        target,
        f"id -u {RIGX_SERVICE_USER} >/dev/null 2>&1 || "
        f"sudo useradd --system --create-home {RIGX_SERVICE_USER}",
    )


def bootstrap(url: str) -> HostInfo:
    """Prepare `url` to receive rigx deployments. Idempotent."""
    target = parse_ssh_url(url)
    info = detect_host(target)
    if not info.has_systemd:
        raise DeployError(
            f"{target.destination}: systemd not found — the SSH backend "
            f"requires a systemd-managed target"
        )
    if not info.has_nix:
        install_nix(target)
        info = detect_host(target)
        if not info.has_nix:
            raise DeployError(
                f"{target.destination}: Nix install reported success but "
                f"`nix` still isn't on PATH"
            )
    if not info.has_container_engine:
        install_podman(target)
        info = detect_host(target)
        if not info.has_container_engine:
            raise DeployError(
                f"{target.destination}: Podman install reported success but "
                f"no container engine is on PATH"
            )
    ensure_rigx_user(target)
    return replace(info, dns_warning=check_dns_backend(target))
