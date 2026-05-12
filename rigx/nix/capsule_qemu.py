"""qemu capsule (full NixOS VM under qemu)."""

from __future__ import annotations

from rigx.config import Project, Target
from rigx.nix.capsule_common import (
    capsule_manifest,
    qemu_runner_script,
)
from rigx.nix.render import nix_id, nix_interp_str, nix_str, nix_value, rewrite_interp


def mk_qemu_capsule_derivation(target: Target, project: Project) -> str:
    """qemu capsule (kind = "capsule", backend = "qemu"): a full NixOS VM
    booted under qemu. The user's entrypoint runs as a systemd one-shot
    service inside the VM; `deps.nixpkgs` go into `environment.systemPackages`;
    declared `ports` are forwarded host→guest via qemu user-mode networking.

    v1 uses NixOS's `system.build.vm` — the standard nixos-test driver
    shape that 9p-mounts the host's `/nix/store` rather than baking a
    standalone qcow2.

    Layout under `$out`:
      bin/run-<name>     standalone runner (execs the underlying VM script)
      system/vm-script   symlink into the NixOS-VM derivation
      manifest.json      contract for orchestrators (rigx.capsule etc.)
    """
    name = target.qualified_name
    safe_name = nix_id(name)
    hostname = target.hostname or target.name
    manifest = capsule_manifest(target)

    entrypoint = rewrite_interp(target.entrypoint, project)
    entrypoint_expr = nix_interp_str(entrypoint)

    sys_pkgs = " ".join(f"pkgs.{p}" for p in target.deps.nixpkgs)

    if target.ports:
        fwd_entries = "\n          ".join(
            f"{{ from = \"host\"; host.port = {p}; guest.port = {p}; "
            f"proto = \"tcp\"; }}"
            for p in target.ports
        )
        forward_ports_block = (
            "[\n          " + fwd_entries + "\n        ]"
        )
    else:
        forward_ports_block = "[ ]"

    env_lines: list[str] = []
    for key, val in target.env.items():
        env_lines.append(
            f'          {key} = {nix_interp_str(rewrite_interp(val, project))};'
        )
    env_block = "\n".join(env_lines) if env_lines else ""

    if target.target:
        vm_system_line = f'    system = {nix_str(target.target)};'
    else:
        vm_system_line = "    inherit system;"

    lines: list[str] = []
    lines.append("let")
    lines.append(
        f"  qemuCapsuleSystem_{safe_name} = "
        f"(import (nixpkgs + \"/nixos/lib/eval-config.nix\") {{"
    )
    lines.append(vm_system_line)
    lines.append("    modules = [")
    lines.append(
        "      (nixpkgs + \"/nixos/modules/virtualisation/qemu-vm.nix\")"
    )
    lines.append("      ({ config, lib, pkgs, ... }: {")
    lines.append('        system.stateVersion = "24.11";')
    lines.append(f"        networking.hostName = {nix_str(hostname)};")
    lines.append("        networking.firewall.enable = false;")
    if sys_pkgs:
        lines.append(
            f"        environment.systemPackages = [ {sys_pkgs} ];"
        )
    lines.append("        virtualisation = {")
    lines.append("          memorySize = 2048;")
    lines.append("          cores = 2;")
    lines.append("          graphics = false;")
    lines.append(f"          forwardPorts = {forward_ports_block};")
    lines.append("        };")
    lines.append("        systemd.services.rigx-entrypoint = {")
    lines.append('          description = "rigx capsule entrypoint";')
    lines.append('          wantedBy = [ "multi-user.target" ];')
    lines.append('          after = [ "network-online.target" ];')
    lines.append('          wants = [ "network-online.target" ];')
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
    lines.append("  }).config.system.build.vm;")
    lines.append(
        f"  qemuCapsuleManifest_{safe_name} = "
        f"pkgs.writeText \"manifest.json\""
    )
    lines.append(f"    (builtins.toJSON {nix_value(manifest)});")

    runner_body = qemu_runner_script(target)
    runner_escaped = runner_body.replace("${", "''${")
    lines.append(
        f"  qemuCapsuleRunner_{safe_name} = pkgs.writeShellScript "
        f"\"run-{target.name}\" ''"
    )
    for ln in runner_escaped.splitlines():
        lines.append(f"    {ln}")
    lines.append("  '';")
    lines.append(
        f"in pkgs.runCommand {nix_str(target.name + '-qemu-capsule')} {{"
    )
    lines.append(f"  pname = {nix_str(name)};")
    lines.append(f"  version = {nix_str(project.version)};")
    lines.append("} ''")
    lines.append("  mkdir -p $out/bin $out/system")
    lines.append(
        f"  ln -s ${{qemuCapsuleSystem_{safe_name}}} $out/system/vm-script"
    )
    lines.append(
        f"  cp ${{qemuCapsuleManifest_{safe_name}}} $out/manifest.json"
    )
    lines.append(
        f"  cp ${{qemuCapsuleRunner_{safe_name}}} $out/bin/run-{target.name}"
    )
    lines.append(f"  chmod +x $out/bin/run-{target.name}")
    lines.append("''")
    return "\n".join(lines)
