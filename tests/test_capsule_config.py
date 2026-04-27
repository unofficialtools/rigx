"""Tests for `kind = "capsule"` parsing, validation, and Nix gen."""

import json
import re
import tempfile
import unittest
from pathlib import Path
from textwrap import dedent

from rigx import config, nix_gen
from rigx.config import ConfigError


class TempProject:
    """Tempdir with a rigx.toml and optional auxiliary files."""

    def __init__(self, toml_body: str, files: dict[str, str] | None = None):
        self.toml_body = dedent(toml_body).lstrip()
        self.files = files or {}

    def __enter__(self) -> Path:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        (root / "rigx.toml").write_text(self.toml_body)
        for rel, body in self.files.items():
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(body)
        return root

    def __exit__(self, *exc) -> None:
        self._tmp.cleanup()


class CapsuleParse(unittest.TestCase):
    def test_loads_minimal_lite_capsule(self):
        body = """
            [project]
            name = "p"

            [targets.svc]
            kind       = "capsule"
            backend    = "lite"
            entrypoint = "/bin/true"
        """
        with TempProject(body) as root:
            proj = config.load(root)
        svc = proj.targets["svc"]
        self.assertEqual(svc.kind, "capsule")
        self.assertEqual(svc.backend, "lite")
        self.assertEqual(svc.entrypoint, "/bin/true")
        self.assertEqual(svc.ports, [])
        self.assertEqual(svc.env, {})

    def test_loads_capsule_with_ports_env_hostname(self):
        body = """
            [project]
            name = "p"

            [targets.svc]
            kind       = "capsule"
            backend    = "lite"
            entrypoint = "/bin/svc --port $PORT"
            ports      = [5000, 5001]
            hostname   = "service-host"
            env        = { PORT = "5000", LOG_LEVEL = "info" }
        """
        with TempProject(body) as root:
            proj = config.load(root)
        svc = proj.targets["svc"]
        self.assertEqual(svc.ports, [5000, 5001])
        self.assertEqual(svc.hostname, "service-host")
        self.assertEqual(svc.env, {"PORT": "5000", "LOG_LEVEL": "info"})

    def test_capsule_requires_backend(self):
        body = """
            [project]
            name = "p"

            [targets.svc]
            kind       = "capsule"
            entrypoint = "/bin/true"
        """
        with TempProject(body) as root:
            with self.assertRaises(ConfigError) as ctx:
                config.load(root)
            self.assertIn("requires 'backend'", str(ctx.exception))

    def test_capsule_rejects_reserved_backend(self):
        for be in ("docker",):
            body = f"""
                [project]
                name = "p"

                [targets.svc]
                kind       = "capsule"
                backend    = "{be}"
                entrypoint = "/bin/true"
            """
            with TempProject(body) as root:
                with self.assertRaises(ConfigError) as ctx:
                    config.load(root)
                self.assertIn("reserved", str(ctx.exception))

    def test_capsule_rejects_unknown_backend(self):
        body = """
            [project]
            name = "p"

            [targets.svc]
            kind       = "capsule"
            backend    = "vmware"
            entrypoint = "/bin/true"
        """
        with TempProject(body) as root:
            with self.assertRaises(ConfigError) as ctx:
                config.load(root)
            self.assertIn("unknown backend", str(ctx.exception))

    def test_capsule_requires_entrypoint(self):
        body = """
            [project]
            name = "p"

            [targets.svc]
            kind    = "capsule"
            backend = "lite"
        """
        with TempProject(body) as root:
            with self.assertRaises(ConfigError) as ctx:
                config.load(root)
            self.assertIn("entrypoint", str(ctx.exception))

    def test_env_must_be_string_table(self):
        body = """
            [project]
            name = "p"

            [targets.svc]
            kind       = "capsule"
            backend    = "lite"
            entrypoint = "/bin/true"
            env        = { PORT = 5000 }
        """
        with TempProject(body) as root:
            with self.assertRaises(ConfigError) as ctx:
                config.load(root)
            self.assertIn("env must be a table", str(ctx.exception))

    def test_env_key_must_be_valid_var_name(self):
        body = """
            [project]
            name = "p"

            [targets.svc]
            kind       = "capsule"
            backend    = "lite"
            entrypoint = "/bin/true"
            env        = { "0BAD" = "no" }
        """
        with TempProject(body) as root:
            with self.assertRaises(ConfigError) as ctx:
                config.load(root)
            self.assertIn("not a", str(ctx.exception))

    def test_ports_must_be_int_list(self):
        body = """
            [project]
            name = "p"

            [targets.svc]
            kind       = "capsule"
            backend    = "lite"
            entrypoint = "/bin/true"
            ports      = ["5000"]
        """
        with TempProject(body) as root:
            with self.assertRaises(ConfigError) as ctx:
                config.load(root)
            self.assertIn("ports", str(ctx.exception))

    def test_short_name_collision_in_deps_internal(self):
        """`deps.internal = ["myapp", "frontend.myapp"]` — both expose
        as `${myapp}` inside the entrypoint. Validator catches this."""
        body = """
            [project]
            name = "p"

            [targets.myapp]
            kind     = "executable"
            sources  = ["src/main.cpp"]

            [dependencies.local.frontend]
            path = "./frontend"

            [targets.svc]
            kind          = "capsule"
            backend       = "lite"
            entrypoint    = "${myapp}/bin/myapp"
            deps.internal = ["myapp", "frontend.myapp"]
        """
        files = {
            "src/main.cpp": "int main(){return 0;}\n",
            "frontend/rigx.toml": dedent("""
                [project]
                name = "frontend"

                [targets.myapp]
                kind     = "executable"
                sources  = ["src/main.cpp"]
            """).lstrip(),
            "frontend/src/main.cpp": "int main(){return 0;}\n",
        }
        with TempProject(body, files) as root:
            with self.assertRaises(ConfigError) as ctx:
                config.load(root)
            self.assertIn("${myapp}", str(ctx.exception))


class CapsuleNixGen(unittest.TestCase):
    """Smoke-checks that the generated flake has the expected shape."""

    _BODY = """
        [project]
        name = "p"

        [targets.svc_bin]
        kind     = "executable"
        sources  = ["src/svc.go"]

        [targets.svc]
        kind          = "capsule"
        backend       = "lite"
        deps.internal = ["svc_bin"]
        entrypoint    = "${svc_bin}/bin/svc_bin --port $PORT"
        ports         = [5000]
        env           = { PORT = "5000" }
    """
    _FILES = {"src/svc.go": "package main\nfunc main(){}\n"}

    def test_emits_from_scratch_image_with_nix_conf(self):
        with TempProject(self._BODY, self._FILES) as root:
            proj = config.load(root)
            flake = nix_gen.generate(proj)
        # FROM-scratch image (dockerTools.buildImage with copyToRoot
        # we built ourselves) — not buildLayeredImage with NixOS toplevel.
        self.assertIn("pkgs.dockerTools.buildImage", flake)
        self.assertIn("copyToRoot = capsuleImageRoot_svc", flake)
        self.assertIn("store = daemon", flake)         # the nix.conf
        # The user's entrypoint with ${svc_bin} interpolation lands
        # bare (not `\$`-escaped) in Cmd so Nix substitutes the rec
        # attr at flake eval time.
        self.assertIn(
            'Cmd = [ "${pkgs.bash}/bin/bash" "-c" '
            '"${svc_bin}/bin/svc_bin --port $PORT" ];',
            flake,
        )
        self.assertIn('Hostname = "svc"', flake)
        self.assertIn('"5000/tcp" = { }', flake)

    def test_emits_run_and_shell_runners_with_nix_escapes(self):
        """Bash `${RIGX_NAME}` etc. inside the runner body must be
        emitted as `''${RIGX_NAME}` so Nix's indented-string parser
        leaves them as literal bash expansions instead of trying to
        look up `RIGX_NAME` in the rec scope."""
        with TempProject(self._BODY, self._FILES) as root:
            proj = config.load(root)
            flake = nix_gen.generate(proj)
        self.assertIn('writeShellScript "run-svc"', flake)
        self.assertIn('writeShellScript "shell-svc"', flake)
        # Nix-escape on bash-only expansions
        self.assertIn("''${RIGX_NAME", flake)
        self.assertNotRegex(
            flake,
            r"(?<!')\$\{RIGX_NAME",
        )
        # shell-runner overrides --entrypoint with bash from a Nix-let
        # binding so the store path is baked in (image is FROM scratch
        # and bash isn't on PATH unless deps.nixpkgs lists it).
        self.assertIn('--entrypoint "${capsuleBashBin_svc}"', flake)
        self.assertIn(
            'capsuleBashBin_svc = "${pkgs.bashInteractive}/bin/bash"',
            flake,
        )
        # PATH is opt-in via deps.nixpkgs. Empty deps → empty PATH
        # (entrypoint works because Cmd uses absolute store paths).
        # The `svc` capsule in this test has no nixpkgs deps, so the
        # let-binding is the empty string.
        self.assertIn('capsulePath_svc = "";', flake)
        self.assertIn('PATH=${capsulePath_svc}', flake)

    def test_emits_manifest_json_with_contract_fields(self):
        with TempProject(self._BODY, self._FILES) as root:
            proj = config.load(root)
            flake = nix_gen.generate(proj)
        # manifest is computed at python level then embedded via
        # builtins.toJSON — assert the key/value pairs we depend on.
        self.assertIn("manifest.json", flake)
        self.assertIn('builtins.toJSON', flake)
        self.assertIn('name = "svc"', flake)
        self.assertIn('backend = "lite"', flake)
        self.assertIn('kind = "oci-tarball-host-store"', flake)


class CapsuleManifestPython(unittest.TestCase):
    """Sanity checks on `_capsule_manifest` / `_capsule_image_tag` at the
    Python level — these are the contract `rigx.capsule` reads."""

    def test_manifest_includes_env_when_set(self):
        from rigx.config import Target
        t = Target(
            name="svc", kind="capsule", backend="lite",
            entrypoint="/bin/true",
            ports=[5000, 5001],
            env={"FOO": "bar"},
            hostname="svc-host",
        )
        manifest = nix_gen._capsule_manifest(t)
        self.assertEqual(manifest["name"], "svc")
        self.assertEqual(manifest["backend"], "lite")
        self.assertEqual(manifest["hostname"], "svc-host")
        self.assertEqual(manifest["ports"], [5000, 5001])
        self.assertEqual(manifest["env"], {"FOO": "bar"})
        self.assertEqual(manifest["image"]["kind"], "oci-tarball-host-store")
        self.assertEqual(manifest["image"]["tag"], "rigx/svc:latest")

    def test_image_tag_handles_dotted_qualified_names(self):
        from rigx.config import Target
        t = Target(name="web", kind="capsule", backend="lite",
                   entrypoint="x", namespace="frontend")
        # namespace makes qualified_name = "frontend.web"; tag must be a
        # valid OCI ref (no dots in the path component).
        tag = nix_gen._capsule_image_tag(t)
        self.assertEqual(tag, "rigx/frontend_web:latest")


class CapsulePathFromDeps(unittest.TestCase):
    """`deps.nixpkgs` on a capsule lands as `${pkgs.<dep>}/bin` entries
    on the container's PATH at Nix-eval time. Strict opt-in — no
    framework-supplied default coreutils/nix."""

    def test_single_dep_appears_in_path(self):
        body = """
            [project]
            name = "p"

            [targets.svc]
            kind         = "capsule"
            backend      = "lite"
            entrypoint   = "/bin/true"
            deps.nixpkgs = ["coreutils"]
        """
        with TempProject(body) as root:
            proj = config.load(root)
            flake = nix_gen.generate(proj)
        self.assertIn(
            'capsulePath_svc = "${pkgs.coreutils}/bin";',
            flake,
        )

    def test_multiple_deps_colon_joined(self):
        body = """
            [project]
            name = "p"

            [targets.svc]
            kind         = "capsule"
            backend      = "lite"
            entrypoint   = "/bin/true"
            deps.nixpkgs = ["coreutils", "gnugrep", "findutils"]
        """
        with TempProject(body) as root:
            proj = config.load(root)
            flake = nix_gen.generate(proj)
        self.assertIn(
            'capsulePath_svc = '
            '"${pkgs.coreutils}/bin" + ":" + '
            '"${pkgs.gnugrep}/bin" + ":" + '
            '"${pkgs.findutils}/bin";',
            flake,
        )


class QemuCapsuleParse(unittest.TestCase):
    def test_loads_minimal_qemu_capsule(self):
        body = """
            [project]
            name = "p"

            [targets.svc]
            kind       = "capsule"
            backend    = "qemu"
            entrypoint = "/bin/true"
        """
        with TempProject(body) as root:
            proj = config.load(root)
        svc = proj.targets["svc"]
        self.assertEqual(svc.kind, "capsule")
        self.assertEqual(svc.backend, "qemu")
        self.assertEqual(svc.entrypoint, "/bin/true")


class QemuCapsuleNixGen(unittest.TestCase):
    """Smoke-checks for the qemu-backed capsule Nix output."""

    _BODY = """
        [project]
        name = "p"

        [targets.svc_bin]
        kind     = "executable"
        sources  = ["src/svc.go"]

        [targets.svc]
        kind          = "capsule"
        backend       = "qemu"
        deps.internal = ["svc_bin"]
        deps.nixpkgs  = ["coreutils"]
        entrypoint    = "${svc_bin}/bin/svc_bin --port $PORT"
        ports         = [5000, 5001]
        env           = { PORT = "5000" }
        hostname      = "vm-host"
    """
    _FILES = {"src/svc.go": "package main\nfunc main(){}\n"}

    def setUp(self):
        with TempProject(self._BODY, self._FILES) as root:
            proj = config.load(root)
            self.flake = nix_gen.generate(proj)

    def test_evaluates_nixos_via_eval_config(self):
        # The qemu path drives a NixOS evaluation against
        # nixpkgs/nixos/lib/eval-config.nix with the qemu-vm module —
        # that's the seam between rigx and a real NixOS system.
        self.assertIn(
            '(import (nixpkgs + "/nixos/lib/eval-config.nix")',
            self.flake,
        )
        self.assertIn(
            '(nixpkgs + "/nixos/modules/virtualisation/qemu-vm.nix")',
            self.flake,
        )
        self.assertIn(".config.system.build.vm", self.flake)

    def test_entrypoint_runs_as_systemd_service(self):
        self.assertIn("systemd.services.rigx-entrypoint", self.flake)
        # ${svc_bin} must land bare so Nix interpolates it at eval time
        # against the rec scope; lib.escapeShellArg quotes the result for
        # systemd's ExecStart.
        self.assertIn("lib.escapeShellArg", self.flake)
        self.assertIn(
            '"${svc_bin}/bin/svc_bin --port $PORT"',
            self.flake,
        )

    def test_ports_become_forwardports_entries(self):
        # virtualisation.forwardPorts is the declarative qemu hostfwd.
        self.assertIn("virtualisation = {", self.flake)
        self.assertIn("forwardPorts = [", self.flake)
        self.assertIn("host.port = 5000;", self.flake)
        self.assertIn("host.port = 5001;", self.flake)
        self.assertIn('proto = "tcp"', self.flake)

    def test_systempackages_includes_nixpkgs_deps(self):
        self.assertIn(
            "environment.systemPackages = [ pkgs.coreutils ];",
            self.flake,
        )

    def test_runner_execs_underlying_vm_script(self):
        # Runner under $out/bin/run-svc just execs the NixOS-generated
        # vm script (the one shipped under $out/system/vm-script).
        self.assertIn('writeShellScript "run-svc"', self.flake)
        self.assertIn(
            '"$HERE/system/vm-script/bin/run-vm-host-vm"',
            self.flake,
        )

    def test_manifest_records_qemu_image_kind(self):
        self.assertIn('kind = "qemu-nixos-vm"', self.flake)
        self.assertIn('path = "system/vm-script"', self.flake)
        self.assertIn('backend = "qemu"', self.flake)


class QemuCapsuleCrossArch(unittest.TestCase):
    """`target = "aarch64-linux"` on a qemu capsule pivots the NixOS
    evaluation so the same definition produces an ARM VM."""

    def test_target_shifts_vm_system(self):
        body = """
            [project]
            name = "p"

            [targets.svc]
            kind       = "capsule"
            backend    = "qemu"
            target     = "aarch64-linux"
            entrypoint = "/bin/true"
        """
        with TempProject(body) as root:
            proj = config.load(root)
            flake = nix_gen.generate(proj)
        # Without target the eval-config call uses `inherit system`; with
        # it set the call hard-codes the VM's system.
        self.assertIn('system = "aarch64-linux"', flake)
        self.assertNotIn("inherit system;\n        modules", flake)

    def test_no_target_falls_back_to_inherit_system(self):
        body = """
            [project]
            name = "p"

            [targets.svc]
            kind       = "capsule"
            backend    = "qemu"
            entrypoint = "/bin/true"
        """
        with TempProject(body) as root:
            proj = config.load(root)
            flake = nix_gen.generate(proj)
        self.assertIn("inherit system;", flake)


class QemuCapsuleNixOsModules(unittest.TestCase):
    """`nixos_modules = ["./extras.nix"]` splices user NixOS modules
    into the VM's eval-config, alongside the rigx-generated module."""

    def test_modules_appear_in_eval_config_modules_list(self):
        body = """
            [project]
            name = "p"

            [targets.svc]
            kind          = "capsule"
            backend       = "qemu"
            entrypoint    = "/bin/true"
            nixos_modules = ["vm/openssh.nix", "vm/users.nix"]
        """
        files = {
            "vm/openssh.nix": "{ services.openssh.enable = true; }\n",
            "vm/users.nix": "{ users.users.alice.isNormalUser = true; }\n",
        }
        with TempProject(body, files) as root:
            proj = config.load(root)
            flake = nix_gen.generate(proj)
        self.assertIn('(src + "/vm/openssh.nix")', flake)
        self.assertIn('(src + "/vm/users.nix")', flake)

    def test_module_glob_expanded_against_project_root(self):
        body = """
            [project]
            name = "p"

            [targets.svc]
            kind          = "capsule"
            backend       = "qemu"
            entrypoint    = "/bin/true"
            nixos_modules = ["vm/*.nix"]
        """
        files = {
            "vm/a.nix": "{ }\n",
            "vm/b.nix": "{ }\n",
        }
        with TempProject(body, files) as root:
            proj = config.load(root)
            flake = nix_gen.generate(proj)
        self.assertIn('(src + "/vm/a.nix")', flake)
        self.assertIn('(src + "/vm/b.nix")', flake)

    def test_modules_rejected_on_lite_backend(self):
        body = """
            [project]
            name = "p"

            [targets.svc]
            kind          = "capsule"
            backend       = "lite"
            entrypoint    = "/bin/true"
            nixos_modules = ["vm/extras.nix"]
        """
        files = {"vm/extras.nix": "{ }\n"}
        with TempProject(body, files) as root:
            with self.assertRaisesRegex(
                ConfigError, "nixos_modules.*'qemu'.*'nixos'"
            ):
                config.load(root)

    def test_no_modules_when_field_omitted(self):
        body = """
            [project]
            name = "p"

            [targets.svc]
            kind       = "capsule"
            backend    = "qemu"
            entrypoint = "/bin/true"
        """
        with TempProject(body) as root:
            proj = config.load(root)
            flake = nix_gen.generate(proj)
        self.assertNotIn("(src +", flake)


class NixosCapsuleParse(unittest.TestCase):
    def test_loads_minimal_nixos_capsule(self):
        body = """
            [project]
            name = "p"

            [targets.svc]
            kind       = "capsule"
            backend    = "nixos"
            entrypoint = "/bin/true"
        """
        with TempProject(body) as root:
            proj = config.load(root)
        svc = proj.targets["svc"]
        self.assertEqual(svc.backend, "nixos")

    def test_nixos_modules_allowed_on_nixos_backend(self):
        body = """
            [project]
            name = "p"

            [targets.svc]
            kind          = "capsule"
            backend       = "nixos"
            entrypoint    = "/bin/true"
            nixos_modules = ["vm/extras.nix"]
        """
        files = {"vm/extras.nix": "{ }\n"}
        with TempProject(body, files) as root:
            proj = config.load(root)
        self.assertEqual(proj.targets["svc"].nixos_modules, ["vm/extras.nix"])


class NixosCapsuleNixGen(unittest.TestCase):
    """`backend = "nixos"` produces a docker image whose Cmd is the
    NixOS toplevel's `/init` (so PID 1 is systemd) plus a runner that
    docker-runs it with --privileged + tmpfs mounts."""

    _BODY = """
        [project]
        name = "p"

        [targets.web_bin]
        kind     = "executable"
        sources  = ["src/web.go"]

        [targets.web]
        kind          = "capsule"
        backend       = "nixos"
        deps.internal = ["web_bin"]
        deps.nixpkgs  = ["coreutils"]
        entrypoint    = "${web_bin}/bin/web --port 8080"
        ports         = [8080]
        env           = { LOG_LEVEL = "info" }
        hostname      = "webcap"
        nixos_modules = ["vm/openssh.nix"]
    """
    _FILES = {
        "src/web.go": "package main\nfunc main(){}\n",
        "vm/openssh.nix": "{ services.openssh.enable = true; }\n",
    }

    def setUp(self):
        with TempProject(self._BODY, self._FILES) as root:
            proj = config.load(root)
            self.flake = nix_gen.generate(proj)

    def test_image_cmd_is_nixos_init(self):
        # The container's PID 1 is the NixOS init script (which exec's
        # systemd after activation), not the user's entrypoint directly.
        self.assertIn(
            'Cmd = [ "${capsuleNixosSystem_web}/init" ]',
            self.flake,
        )

    def test_uses_boot_isContainer_not_qemu_vm(self):
        # `boot.isContainer = true` is what shapes the toplevel for
        # systemd-in-a-container (no kernel modules, no bootloader).
        # Crucially, the qemu-vm.nix module must NOT be imported here.
        self.assertIn("boot.isContainer = true;", self.flake)
        # The qemu-vm import is scoped to the qemu backend; for the
        # nixos web capsule's let-binding it should not appear.
        # (We do a coarse check — any qemu-vm.nix in the flake would
        # only come from a qemu capsule we're not declaring here.)
        self.assertNotIn(
            "qemu-vm.nix", self.flake,
            "boot.isContainer-only — should not import qemu-vm.nix",
        )

    def test_entrypoint_is_systemd_service(self):
        self.assertIn("systemd.services.rigx-entrypoint", self.flake)
        self.assertIn(
            '"${web_bin}/bin/web --port 8080"',
            self.flake,
        )

    def test_nixos_modules_spliced_into_eval(self):
        self.assertIn('(src + "/vm/openssh.nix")', self.flake)

    def test_runner_uses_privileged_and_tmpfs(self):
        # systemd inside docker needs --privileged + tmpfs /run /tmp
        # /var/log to manage cgroups and write its runtime state.
        self.assertIn("--privileged", self.flake)
        self.assertIn("--tmpfs /run", self.flake)
        self.assertIn("--tmpfs /tmp", self.flake)
        self.assertIn("--tmpfs /var/log", self.flake)

    def test_runner_mounts_host_nix_store(self):
        self.assertIn("-v /nix/store:/nix/store:ro", self.flake)

    def test_manifest_records_nixos_image_kind(self):
        self.assertIn('kind = "oci-tarball-nixos-systemd"', self.flake)
        self.assertIn('backend = "nixos"', self.flake)


class NixosCapsuleManifestPython(unittest.TestCase):
    def test_manifest_for_nixos_backend(self):
        from rigx.config import Target
        t = Target(
            name="svc", kind="capsule", backend="nixos",
            entrypoint="/bin/true",
            ports=[5000],
            hostname="svc-host",
        )
        manifest = nix_gen._capsule_manifest(t)
        self.assertEqual(manifest["backend"], "nixos")
        self.assertEqual(manifest["image"]["kind"], "oci-tarball-nixos-systemd")
        self.assertEqual(manifest["image"]["path"], "image/image.tar.gz")
        self.assertEqual(manifest["image"]["tag"], "rigx/svc:latest")


class QemuCapsuleManifestPython(unittest.TestCase):
    def test_manifest_for_qemu_backend(self):
        from rigx.config import Target
        t = Target(
            name="svc", kind="capsule", backend="qemu",
            entrypoint="/bin/true",
            ports=[5000],
            hostname="vm-host",
        )
        manifest = nix_gen._capsule_manifest(t)
        self.assertEqual(manifest["backend"], "qemu")
        self.assertEqual(manifest["image"]["kind"], "qemu-nixos-vm")
        self.assertEqual(manifest["image"]["path"], "system/vm-script")
        self.assertNotIn("tag", manifest["image"])


if __name__ == "__main__":
    unittest.main()
