"""Secrets piping — design decision #6.

Resolves a testbed's declared secrets locally (`source = "env:VAR"` /
`"file:path"`, from `Target.testbed_secrets`) and pipes each one's raw
bytes, over the same SSH connection bootstrap uses, straight into
`systemd-creds encrypt` on the target. Plaintext never touches disk on
either end, and never touches `/nix/store` — only the encrypted
`.cred` file under `/var/lib/rigx/<testbed>/secrets/` is written,
decrypted automatically by systemd on unit start (`LoadCredentialEncrypted=`
in the rendered unit — see `rigx.deploy.units`) using a TPM-backed key
where available, else a host-local key under `/var/lib/systemd`.

`systemd-creds encrypt` needs privileged access to that host/TPM key,
so this always runs through `sudo` on the target — same as-yet-
undesigned privilege model gap noted on `bootstrap.ensure_rigx_user`
(see the "ongoing privilege model" open item in the design memory);
this isn't a separate policy decision, just the same open question
showing up here too.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from rigx.deploy.bootstrap import DeployError, SSHTarget


def resolve_secret_source(source: str, *, project_root: Path) -> bytes:
    """Resolve a `testbed_secrets[name]` source string to its raw bytes,
    read locally. `env:VAR` reads an environment variable; `file:path`
    reads a file (relative to `project_root` unless absolute)."""
    kind, _, rest = source.partition(":")
    if kind == "env":
        val = os.environ.get(rest)
        if val is None:
            raise DeployError(
                f"secret source 'env:{rest}' — environment variable not set"
            )
        return val.encode()
    if kind == "file":
        path = Path(rest)
        if not path.is_absolute():
            path = project_root / path
        try:
            return path.read_bytes()
        except OSError as e:
            raise DeployError(f"secret source 'file:{rest}' — {e}")
    raise DeployError(
        f"unsupported secret source {source!r} (expected 'env:' or 'file:')"
    )


def push_secret(target: SSHTarget, testbed: str, name: str, value: bytes) -> None:
    """Pipe `value` over ssh straight into `systemd-creds encrypt` on
    `target`, landing at `/var/lib/rigx/<testbed>/secrets/<name>.cred`."""
    cred_dir = f"/var/lib/rigx/{testbed}/secrets"
    cred_path = f"{cred_dir}/{name}.cred"
    remote_cmd = (
        f"sudo install -d -m 700 {cred_dir} && "
        f"sudo systemd-creds encrypt --name={name} - {cred_path}"
    )
    proc = subprocess.run(
        target.ssh_argv() + [remote_cmd],
        input=value,
        capture_output=True,
    )
    if proc.returncode != 0:
        raise DeployError(
            f"{target.destination}: failed to push secret {name!r} (exit "
            f"{proc.returncode}): {proc.stderr.decode(errors='replace').strip()}"
        )


def push_secrets(
    target: SSHTarget,
    testbed: str,
    secret_sources: dict[str, str],
    *,
    project_root: Path,
) -> None:
    """Resolve and push every declared secret for `testbed`, in name
    order (deterministic, no dependency ordering needed — secrets
    don't depend on each other)."""
    for name in sorted(secret_sources):
        value = resolve_secret_source(secret_sources[name], project_root=project_root)
        push_secret(target, testbed, name, value)
