"""Python helper for orchestrating built rigx capsules from a test script.

Usage from a `kind = "test"` target (with `sandbox = false`):

    from rigx.capsule import start

    with start("simulator", publish={5000: 5050}) as sim:
        sim.wait_for_port(5000, timeout=10)
        # ...interact with the running container at 127.0.0.1:5050

`start(name)` reads `output/<name>/manifest.json` (produced by the lite
capsule's Nix build), invokes `output/<name>/bin/run-<name>` in detached
mode with the right env vars, and returns a `Capsule` handle. The
context manager stops the container on exit.

This module deliberately stays thin — subprocess + a couple of
conveniences. The fault-injection / network-simulation layer lives in
`rigx.testbed`.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator


def _container_engine() -> str:
    """Return the first container engine on PATH. `docker` first because
    `podman` is the documented fallback in nix-docker.sh."""
    for engine in ("docker", "podman"):
        if shutil.which(engine):
            return engine
    raise RuntimeError(
        "rigx.capsule: docker or podman is required on PATH to run capsules"
    )


def _find_output_dir(start_dir: Path | None = None) -> Path:
    """Locate the project's `output/` directory by walking up from
    `start_dir` (default: cwd) until we find a `rigx.toml`. Tests
    invoked from anywhere under the project root resolve correctly
    without the test author hard-coding paths."""
    cur = (start_dir or Path.cwd()).resolve()
    for d in [cur, *cur.parents]:
        if (d / "rigx.toml").is_file():
            return d / "output"
    raise RuntimeError(
        "rigx.capsule: cannot locate rigx.toml ancestor — pass output_dir=…"
    )


PublishSpec = int | tuple[str, int]
"""Publish-mapping value — either a host port (binds on 0.0.0.0) or a
`(host_addr, host_port)` tuple (binds on the given loopback alias).
The testbed sets the tuple form when distinct addresses are in use."""


def _split_publish(value: PublishSpec) -> tuple[str, int]:
    """Normalize a publish-mapping value to `(host_addr, host_port)`."""
    if isinstance(value, tuple):
        return value
    return ("127.0.0.1", value)


@dataclass
class Capsule:
    """Handle to a running capsule. Returned by `start()`; the context
    manager calls `stop()` on exit so explicit teardown is rare."""

    name: str
    container_name: str
    capsule_dir: Path
    manifest: dict[str, Any]
    publish: dict[int, PublishSpec] = field(default_factory=dict)
    publish_udp: dict[int, PublishSpec] = field(default_factory=dict)
    # `lite` and `nixos` use docker/podman, which is detached/named —
    # no process handle to track. `qemu` runs the runner via `Popen`
    # (the VM is a child of the test process), so we keep the handle
    # plus a log path so `stop()` and `logs()` know where to look.
    _proc: subprocess.Popen | None = None
    _log_path: Path | None = None
    _stopped: bool = False

    @property
    def backend(self) -> str:
        return self.manifest.get("backend", "lite")

    def host_port(self, container_port: int, *, proto: str = "tcp") -> int:
        """Host-side port mapped to the given container port. `proto`
        selects TCP (default) or UDP; the two have independent maps
        because TCP/UDP port spaces don't conflict."""
        _, port = self.host_endpoint(container_port, proto=proto)
        return port

    def host_address(
        self, container_port: int, *, proto: str = "tcp"
    ) -> str:
        """Host-side address the container port is published on."""
        addr, _ = self.host_endpoint(container_port, proto=proto)
        return addr

    def host_endpoint(
        self, container_port: int, *, proto: str = "tcp"
    ) -> tuple[str, int]:
        """Full `(addr, port)` host-side endpoint for the given
        container port. `proto` selects TCP (default) or UDP. Errors
        clearly when no mapping was configured — the most common cause
        of a confusing 'connection refused' downstream."""
        pmap = self.publish_udp if proto == "udp" else self.publish
        if container_port not in pmap:
            raise KeyError(
                f"capsule {self.name!r}: container port {container_port}/"
                f"{proto} was not published (start() got "
                f"publish={self.publish or None}, "
                f"publish_udp={self.publish_udp or None})"
            )
        return _split_publish(pmap[container_port])

    def wait_for_port(
        self,
        container_port: int,
        *,
        timeout: float = 10.0,
        interval: float = 0.1,
    ) -> None:
        """Block until the (TCP) host-mapped port accepts a connection.
        UDP "readiness" doesn't really exist — there's nothing to
        accept — so this method is TCP-only. For UDP, sleep briefly or
        send-and-retry from the test orchestrator."""
        host_addr, host_port = self.host_endpoint(container_port, proto="tcp")
        deadline = time.monotonic() + timeout
        last_err: OSError | None = None
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(
                    (host_addr, host_port), timeout=interval
                ):
                    return
            except OSError as e:
                last_err = e
                time.sleep(interval)
        raise TimeoutError(
            f"capsule {self.name!r}: port {container_port} "
            f"(host {host_addr}:{host_port}) not accepting connections "
            f"after {timeout:.1f}s (last: {last_err})"
        )

    def exec(
        self, cmd: list[str], *, capture: bool = False, timeout: float | None = None
    ) -> subprocess.CompletedProcess:
        """Run a command inside the capsule via `docker exec`. `capture=True`
        returns stdout/stderr as text; otherwise inherits from parent.

        Not available on qemu capsules — the VM has no docker shell-out
        equivalent. Use SSH (configure via `nixos_modules`) and connect
        through a forwarded port instead."""
        if self.backend == "qemu":
            raise NotImplementedError(
                f"capsule {self.name!r}: exec() is not supported for "
                f"backend=qemu (the VM has no docker exec equivalent). "
                f"Enable SSH via nixos_modules and connect through a "
                f"forwarded port."
            )
        engine = _container_engine()
        full = [engine, "exec", self.container_name, *cmd]
        if capture:
            return subprocess.run(
                full,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        return subprocess.run(full, timeout=timeout, check=False)

    def logs(self) -> str:
        """Snapshot of the capsule's stdout+stderr — useful for failure
        diagnostics in tests. Returns empty string if there's nothing
        captured yet."""
        if self.backend == "qemu":
            # qemu output is captured to a temp file by start() (the
            # subprocess is a child, not a daemon), so we just read the
            # file. Errors are swallowed; logs() must never raise.
            if self._log_path is None:
                return ""
            try:
                return self._log_path.read_text(errors="replace")
            except OSError:
                return ""
        engine = _container_engine()
        r = subprocess.run(
            [engine, "logs", self.container_name],
            capture_output=True,
            text=True,
            check=False,
        )
        return (r.stdout or "") + (r.stderr or "")

    def stop(self) -> None:
        """Idempotent stop. For lite/nixos: `docker stop <name>` (errors
        swallowed if the container is already gone — the runner uses
        `--rm`). For qemu: terminate the runner subprocess so qemu
        shuts down. In either case it's best-effort.

        Tolerates a missing container engine: if neither docker nor
        podman is on PATH, we have no running container to stop. This
        keeps `with start(...) as cap: ...` from raising on context
        exit when the runner was a stub (sandboxed unit tests) or when
        the engine vanished mid-flight."""
        if self._stopped:
            return
        self._stopped = True
        if self._proc is not None:
            # qemu path: signal the runner subprocess (and therefore
            # qemu, which runs as its child via `exec`). SIGTERM gives
            # qemu a chance to flush; if it ignores us, escalate.
            proc = self._proc
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    try:
                        proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        pass
            return
        try:
            engine = _container_engine()
        except RuntimeError:
            return
        subprocess.run(
            [engine, "stop", self.container_name],
            capture_output=True,
            check=False,
        )


@contextmanager
def start(
    name: str,
    *,
    env: dict[str, str] | None = None,
    publish: dict[int, PublishSpec] | None = None,
    publish_udp: dict[int, PublishSpec] | None = None,
    network: str | None = None,
    container_name: str | None = None,
    output_dir: Path | str | None = None,
) -> Iterator[Capsule]:
    """Start a built capsule as a detached container; stop on context exit.

    Supports `backend = "lite"` and `backend = "nixos"` — both share the
    docker/podman runner contract. `backend = "qemu"` uses a different
    transport and isn't supported here yet.

    Args:
      name: capsule target name (matches `[targets.<name>]` in rigx.toml).
            The capsule must have been built — the runner script lives
            at `output/<name>/bin/run-<name>`.
      env: extra env vars set inside the container. Override the
            image's `Env` defaults.
      publish: TCP container_port → host mapping. Each value is either
            an `int` (host port; binds on `0.0.0.0`) or a
            `(host_addr, host_port)` tuple (binds on the given
            loopback alias — `rigx.testbed` produces this form when
            a subnet is configured). `None` = no TCP port forwarding.
      publish_udp: UDP container_port → host mapping, same value
            shapes as `publish`. Renders to `docker -p` flags with
            the `/udp` suffix.
      network: docker network the container joins (lets multiple
            capsules reach each other by container name).
      container_name: stable container name. Default: `rigx-<name>-<uuid8>`.
      output_dir: project `output/` dir. Defaults to walking up from cwd.

    Yields a `Capsule` handle. On context exit: `docker stop <name>` is
    invoked unconditionally; the `--rm` in the runner cleans up the
    container record.
    """
    output = Path(output_dir) if output_dir else _find_output_dir()
    capsule_dir = (output / name).resolve()
    if not capsule_dir.is_dir():
        raise RuntimeError(
            f"capsule {name!r} not built (no {capsule_dir}); "
            f"try `rigx build {name}`"
        )

    manifest_path = capsule_dir / "manifest.json"
    if not manifest_path.is_file():
        raise RuntimeError(
            f"capsule {name!r}: manifest.json missing at {manifest_path} — "
            f"the capsule may have been built with an older rigx"
        )
    manifest = json.loads(manifest_path.read_text())
    # Three supported backends:
    #   `lite`/`nixos` — docker/podman-shaped: same runner contract
    #     (RIGX_NAME / RIGX_PUBLISH / RIGX_NETWORK / RIGX_ENV /
    #     RIGX_DETACH), same `docker stop`/`docker exec`/`docker logs`
    #     lifecycle. nixos additionally needs `--privileged` for
    #     systemd, handled inside the runner.
    #   `qemu` — runner is a wrapper around the NixOS vm script. We
    #     start it via Popen and keep the process handle so stop()
    #     can terminate it. RIGX_PUBLISH is translated to
    #     `QEMU_NET_OPTS=hostfwd=...` by the runner. exec()/network
    #     don't apply (different transport).
    backend = manifest.get("backend")
    if backend not in ("lite", "nixos", "qemu"):
        raise RuntimeError(
            f"capsule {name!r}: rigx.capsule supports backend in "
            f"{{lite, nixos, qemu}}; got {backend!r}"
        )

    runner = capsule_dir / "bin" / f"run-{name}"
    if not runner.is_file():
        raise RuntimeError(
            f"capsule {name!r}: runner not found at {runner}"
        )

    if container_name is None:
        container_name = f"rigx-{name}-{uuid.uuid4().hex[:8]}"

    # Forward the runner's contract via env vars. For lite/nixos,
    # `RIGX_DETACH=1` makes the runner `docker run -d` and exit once the
    # container is spawned. For qemu the runner can't detach (qemu runs
    # as the runner's foreground child), so we keep RIGX_DETACH unset
    # and start the runner via Popen instead.
    runner_env = dict(os.environ)
    runner_env["RIGX_NAME"] = container_name
    if backend != "qemu":
        runner_env["RIGX_DETACH"] = "1"
    if network:
        runner_env["RIGX_NETWORK"] = network
    if publish or publish_udp:
        # docker -p syntax: `host:cont` (binds 0.0.0.0) or
        # `host_addr:host_port:cont` (binds the given address). UDP
        # entries get the `/udp` suffix; TCP is the default and
        # doesn't need one. The qemu runner translates this to
        # `QEMU_NET_OPTS=hostfwd=...` at the start of its body so the
        # underlying NixOS vm script sees an additional hostfwd rule
        # per declared port.
        def _fmt(spec: PublishSpec, cont: int, proto: str) -> str:
            if isinstance(spec, tuple):
                addr, host_port = spec
                base = f"{addr}:{host_port}:{cont}"
            else:
                base = f"{spec}:{cont}"
            return f"{base}/udp" if proto == "udp" else base

        parts: list[str] = []
        for cont, spec in (publish or {}).items():
            parts.append(_fmt(spec, cont, "tcp"))
        for cont, spec in (publish_udp or {}).items():
            parts.append(_fmt(spec, cont, "udp"))
        runner_env["RIGX_PUBLISH"] = ",".join(parts)
    if env:
        runner_env["RIGX_ENV"] = ",".join(f"{k}={v}" for k, v in env.items())

    proc: subprocess.Popen | None = None
    log_path: Path | None = None

    if backend == "qemu":
        # Pipe runner output through a temp file; the parent test
        # process is doing other things (waiting on TCP, talking to
        # peers) and shouldn't have to drain Popen pipes to keep qemu
        # from blocking. The file gets cleaned up in stop().
        log_handle = tempfile.NamedTemporaryFile(
            mode="wb", delete=False,
            prefix=f"rigx-qemu-{name}-",
            suffix=".log",
        )
        log_path = Path(log_handle.name)
        try:
            proc = subprocess.Popen(
                [str(runner)],
                env=runner_env,
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except Exception:
            log_handle.close()
            try:
                log_path.unlink()
            except OSError:
                pass
            raise
        finally:
            log_handle.close()
        # If qemu died immediately (bad args, missing binary, image
        # mismatch) fail fast with the captured output instead of
        # silently letting the test sit on a `wait_for_port`.
        time.sleep(0.05)
        if proc.poll() is not None:
            tail = ""
            try:
                tail = log_path.read_text(errors="replace")
            except OSError:
                pass
            raise RuntimeError(
                f"capsule {name!r}: runner exited immediately "
                f"(exit {proc.returncode})\n--- output ---\n{tail}"
            )
    else:
        result = subprocess.run(
            [str(runner)],
            env=runner_env,
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"capsule {name!r}: runner failed (exit {result.returncode})\n"
                f"--- stdout ---\n{result.stdout}\n"
                f"--- stderr ---\n{result.stderr}"
            )

    cap = Capsule(
        name=name,
        container_name=container_name,
        capsule_dir=capsule_dir,
        manifest=manifest,
        publish=dict(publish or {}),
        publish_udp=dict(publish_udp or {}),
        _proc=proc,
        _log_path=log_path,
    )
    try:
        yield cap
    finally:
        cap.stop()
        if log_path is not None:
            try:
                log_path.unlink()
            except OSError:
                pass
