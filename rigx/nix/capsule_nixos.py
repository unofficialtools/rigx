"""NixOS-in-container capsule (systemd userspace under docker)."""

from __future__ import annotations

from rigx.config import Project, Target
from rigx.nix.capsule_common import (
    capsule_image_tag,
    capsule_manifest,
    nixos_runner_script,
)
from rigx.nix.render import nix_id, nix_interp_str, nix_str, nix_value, rewrite_interp


def mk_nixos_capsule_derivation(target: Target, project: Project) -> str:
    """NixOS-in-container capsule (kind = "capsule", backend = "nixos"):
    a real NixOS userspace running under systemd inside a docker/podman
    container, with the host's `/nix/store` mounted at runtime.

    Layout under `$out`:
      bin/run-<name>     standalone runner (host-store mount + systemd init)
      image/image.tar.gz the loadable OCI tarball whose Cmd is `${toplevel}/init`
      manifest.json      contract for orchestrators (rigx.capsule etc.)
    """
    name = target.qualified_name
    safe_name = nix_id(name)
    hostname = target.hostname or target.name
    manifest = capsule_manifest(target)

    entrypoint = rewrite_interp(target.entrypoint, project)
    entrypoint_expr = nix_interp_str(entrypoint)

    sys_pkgs = " ".join(f"pkgs.{p}" for p in target.deps.nixpkgs)

    env_lines: list[str] = []
    for key, val in target.env.items():
        env_lines.append(
            f'          {key} = {nix_interp_str(rewrite_interp(val, project))};'
        )
    env_block = "\n".join(env_lines) if env_lines else ""

    exposed_ports_block = (
        " ".join(f'"{p}/tcp" = {{ }};' for p in target.ports)
        if target.ports
        else ""
    )

    if target.target:
        eval_system_line = f'    system = {nix_str(target.target)};'
    else:
        eval_system_line = "    inherit system;"

    lines: list[str] = []
    lines.append("let")
    lines.append(
        f"  capsuleNixosSystem_{safe_name} = "
        f"(import (nixpkgs + \"/nixos/lib/eval-config.nix\") {{"
    )
    lines.append(eval_system_line)
    lines.append("    modules = [")
    lines.append("      ({ config, lib, pkgs, ... }: {")
    lines.append('        system.stateVersion = "24.11";')
    lines.append(f'        networking.hostName = {nix_str(hostname)};')
    lines.append("        networking.firewall.enable = false;")
    lines.append("        networking.useDHCP = false;")
    lines.append("        boot.isContainer = true;")
    if sys_pkgs:
        lines.append(f"        environment.systemPackages = [ {sys_pkgs} ];")
    lines.append("        systemd.services.rigx-entrypoint = {")
    lines.append('          description = "rigx capsule entrypoint";')
    lines.append('          wantedBy = [ "multi-user.target" ];')
    lines.append('          after = [ "network.target" ];')
    if env_block:
        lines.append("          environment = {")
        lines.append(env_block)
        lines.append("          };")
    lines.append("          serviceConfig = {")
    lines.append('            Type = "simple";')
    lines.append(
        "            ExecStart = \"${pkgs.bash}/bin/bash -c \" + "
        f"(lib.escapeShellArg {entrypoint_expr});"
    )
    lines.append('            StandardOutput = "journal+console";')
    lines.append('            StandardError = "journal+console";')
    lines.append('            Restart = "no";')
    lines.append("          };")
    lines.append("        };")
    lines.append("      })")
    for mod_path in target.nixos_modules:
        lines.append(f"      (src + {nix_str('/' + mod_path)})")
    lines.append("    ];")
    lines.append("  }).config.system.build.toplevel;")
    lines.append(
        f"  capsuleImageRoot_{safe_name} = "
        f"pkgs.runCommand \"rigx-nixos-{safe_name}-root\" {{ }} ''"
    )
    lines.append("    mkdir -p $out/etc $out/nix/store")
    lines.append("    mkdir -p $out/tmp $out/run $out/root $out/var/log")
    lines.append("    chmod 1777 $out/tmp")
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
        f"      Cmd = [ \"${{capsuleNixosSystem_{safe_name}}}/init\" ];"
    )
    lines.append(f"      Hostname = {nix_str(hostname)};")
    if exposed_ports_block:
        lines.append(f"      ExposedPorts = {{ {exposed_ports_block} }};")
    lines.append('      StopSignal = "SIGRTMIN+3";')
    lines.append("    };")
    lines.append("  };")
    lines.append(
        f"  capsuleManifest_{safe_name} = "
        f"pkgs.writeText \"manifest.json\""
    )
    lines.append(f"    (builtins.toJSON {nix_value(manifest)});")

    runner_body = nixos_runner_script(target)
    runner_escaped = runner_body.replace("${", "''${")
    lines.append(
        f"  capsuleRunner_{safe_name} = pkgs.writeShellScript "
        f"\"run-{target.name}\" ''"
    )
    for ln in runner_escaped.splitlines():
        lines.append(f"    {ln}")
    lines.append("  '';")
    lines.append(
        f"in pkgs.runCommand {nix_str(target.name + '-nixos-capsule')} {{"
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
    lines.append(f"  chmod +x $out/bin/run-{target.name}")
    lines.append("''")
    return "\n".join(lines)
