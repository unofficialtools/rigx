"""Lite capsule (FROM-scratch OCI image + host-store mount runner)."""

from __future__ import annotations

from rigx.config import Project, Target
from rigx.nix.capsule_common import (
    PLACEHOLDER_BASH_BIN,
    PLACEHOLDER_PATH,
    capsule_image_tag,
    capsule_manifest,
    capsule_runner_script,
)
from rigx.nix.render import nix_id, nix_interp_str, nix_str, nix_value, rewrite_interp


def mk_lite_capsule_derivation(target: Target, project: Project) -> str:
    """Lite capsule (kind = "capsule", backend = "lite"): a
    `unofficialtools/nix-docker`-style FROM-scratch OCI image plus
    host-side runners (`run-<name>`, `shell-<name>`) that mount the
    host's `/nix/store` and Nix daemon socket into the container at
    start time.

    Layout under `$out`:
      bin/run-<name>     standalone runner (host-store mounts + entrypoint)
      bin/shell-<name>   same setup but `--entrypoint` is bash, for poking
      image/image.tar.gz the loadable scratch OCI tarball
      manifest.json      contract for orchestrators (rigx.capsule etc.)
    """

    name = target.qualified_name
    safe_name = nix_id(name)
    hostname = target.hostname or target.name
    manifest = capsule_manifest(target)

    entrypoint_expr = nix_interp_str(
        rewrite_interp(target.entrypoint, project)
    )
    env_pairs: list[str] = []
    for key, val in target.env.items():
        env_pairs.append(
            nix_interp_str(f"{key}=" + rewrite_interp(val, project))
        )
    exposed_ports = (
        " ".join(f'"{p}/tcp" = {{ }};' for p in target.ports)
        if target.ports
        else ""
    )

    lines: list[str] = []
    lines.append("let")
    lines.append(
        f"  capsuleImageRoot_{safe_name} = "
        f"pkgs.runCommand \"rigx-lite-{safe_name}-root\" {{ }} ''"
    )
    lines.append("    mkdir -p $out/etc")
    lines.append("    mkdir -p $out/nix/store")
    lines.append("    mkdir -p $out/nix/var/nix/profiles")
    lines.append("    mkdir -p $out/nix/var/nix/gcroots")
    lines.append("    mkdir -p $out/nix/var/nix/daemon-socket")
    lines.append("    mkdir -p $out/tmp")
    lines.append("    mkdir -p $out/root")
    lines.append("    mkdir -p $out/bin")
    lines.append("    chmod 1777 $out/tmp")
    # /bin/sh -> bash so third-party software that hardcodes /bin/sh
    # (popen, system(3), `#!/bin/sh` scripts) works without forcing the
    # user to list coreutils in deps.nixpkgs and rebuild the symlink at
    # entrypoint time. Bash invoked as `sh` enters POSIX mode.
    lines.append("    ln -s ${pkgs.bash}/bin/bash $out/bin/sh")
    lines.append("    cat > $out/etc/nix.conf <<NIXCONF")
    lines.append("    store = daemon")
    lines.append("    experimental-features = nix-command flakes")
    lines.append("    NIXCONF")
    lines.append("    cat > $out/etc/passwd <<PASSWD")
    lines.append("    root:x:0:0:root:/root:/bin/sh")
    lines.append("    nobody:x:65534:65534:nobody:/var/empty:/bin/false")
    lines.append("    PASSWD")
    lines.append("    cat > $out/etc/group <<GROUP")
    lines.append("    root:x:0:")
    lines.append("    nobody:x:65534:")
    lines.append("    GROUP")
    lines.append("  '';")
    lines.append(
        f"  capsuleImage_{safe_name} = pkgs.dockerTools.buildImage {{"
    )
    lines.append(f"    name = {nix_str('rigx/' + safe_name)};")
    lines.append('    tag = "latest";')
    lines.append(f"    copyToRoot = capsuleImageRoot_{safe_name};")
    lines.append("    config = {")
    lines.append(
        f"      Cmd = [ \"${{pkgs.bash}}/bin/bash\" \"-c\" {entrypoint_expr} ];"
    )
    lines.append(f"      Hostname = {nix_str(hostname)};")
    lines.append('      WorkingDir = "/root";')
    if env_pairs:
        lines.append("      Env = [ " + " ".join(env_pairs) + " ];")
    if exposed_ports:
        lines.append(f"      ExposedPorts = {{ {exposed_ports} }};")
    lines.append("    };")
    lines.append("  };")
    lines.append(
        f"  capsuleManifest_{safe_name} = pkgs.writeText \"manifest.json\""
    )
    lines.append(f"    (builtins.toJSON {nix_value(manifest)});")

    def _bake_runner(body: str) -> str:
        body = body.replace("${", "''${")
        body = body.replace(
            PLACEHOLDER_PATH,
            "${capsulePath_" + safe_name + "}",
        )
        body = body.replace(
            PLACEHOLDER_BASH_BIN,
            "${capsuleBashBin_" + safe_name + "}",
        )
        return body

    run_body = _bake_runner(capsule_runner_script(target, mode="run"))
    shell_body = _bake_runner(capsule_runner_script(target, mode="shell"))
    if target.deps.nixpkgs:
        path_parts = " + \":\" + ".join(
            f"\"${{pkgs.{p}}}/bin\"" for p in target.deps.nixpkgs
        )
        lines.append(f"  capsulePath_{safe_name} = {path_parts};")
    else:
        lines.append(f"  capsulePath_{safe_name} = \"\";")
    lines.append(
        f"  capsuleBashBin_{safe_name} = "
        f"\"${{pkgs.bashInteractive}}/bin/bash\";"
    )
    lines.append(
        f"  capsuleRunner_{safe_name} = pkgs.writeShellScript "
        f"\"run-{target.name}\" ''"
    )
    for ln in run_body.splitlines():
        lines.append(f"    {ln}")
    lines.append("  '';")
    lines.append(
        f"  capsuleShell_{safe_name} = pkgs.writeShellScript "
        f"\"shell-{target.name}\" ''"
    )
    for ln in shell_body.splitlines():
        lines.append(f"    {ln}")
    lines.append("  '';")
    lines.append(
        f"in pkgs.runCommand {nix_str(target.name + '-capsule')} {{"
    )
    lines.append(f"  pname = {nix_str(name)};")
    lines.append(f"  version = {nix_str(project.version)};")
    lines.append("} ''")
    lines.append("  mkdir -p $out/bin $out/image")
    lines.append(
        f"  cp ${{capsuleImage_{safe_name}}} $out/image/image.tar.gz"
    )
    lines.append(
        f"  cp ${{capsuleManifest_{safe_name}}} $out/manifest.json"
    )
    lines.append(
        f"  cp ${{capsuleRunner_{safe_name}}} $out/bin/run-{target.name}"
    )
    lines.append(
        f"  cp ${{capsuleShell_{safe_name}}} $out/bin/shell-{target.name}"
    )
    lines.append(
        f"  chmod +x $out/bin/run-{target.name} $out/bin/shell-{target.name}"
    )
    lines.append("''")
    return "\n".join(lines)
