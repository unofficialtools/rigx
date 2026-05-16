"""Backward-compat shim — the implementation lives in `rigx.nix`.

External callers and tests still import `rigx.nix_gen`, so this module
re-exports the same `generate` function plus every helper the tests
reach into. New code should import directly from `rigx.nix.<module>`.
"""

from __future__ import annotations

from rigx.nix import c_family as _c_family
from rigx.nix import capsule_common as _capsule_common
from rigx.nix import capsule_lite as _capsule_lite
from rigx.nix import capsule_nixos as _capsule_nixos
from rigx.nix import capsule_qemu as _capsule_qemu
from rigx.nix import cross as _cross
from rigx.nix import flake as _flake
from rigx.nix import go as _go
from rigx.nix import nim as _nim
from rigx.nix import python as _python
from rigx.nix import render as _render
from rigx.nix import rust as _rust
from rigx.nix import tests as _tests
from rigx.nix import zig as _zig

# Public entry point.
generate = _flake.generate

# Rendering / quoting helpers (some asserted in tests).
_nix_str = _render.nix_str
_nix_interp_str = _render.nix_interp_str
_nix_id = _render.nix_id
_rewrite_interp = _render.rewrite_interp
_nix_list = _render.nix_list
_nix_value = _render.nix_value
_indent = _render.indent
_shell_quote = _render.shell_quote
sh_join = _render.sh_join
_INTERP_DOTTED = _render._INTERP_DOTTED

# Cross-compilation helpers.
CROSS_TARGETS = _cross.CROSS_TARGETS
DEFAULT_COMPILER_FOR_LANG = _cross.DEFAULT_COMPILER_FOR_LANG
_cross_info = _cross.cross_info
_effective_target = _cross.effective_target
_effective_compiler = _cross.effective_compiler
_stdenv_attr = _cross.stdenv_attr
_toolchain_pkgs = _cross.toolchain_pkgs

# C-family helpers and build phases.
_obj_name = _c_family.obj_name
_effective_cxxflags = _c_family.effective_cxxflags
_effective_cflags = _c_family.effective_cflags
_effective_ldflags = _c_family.effective_ldflags
_build_phase_cxx_executable = _c_family.build_phase_cxx_executable
_build_phase_c_executable = _c_family.build_phase_c_executable
_build_phase_cxx_static_library = _c_family.build_phase_cxx_static_library
_build_phase_c_static_library = _c_family.build_phase_c_static_library
_build_phase_cxx_shared_library = _c_family.build_phase_cxx_shared_library
_build_phase_c_shared_library = _c_family.build_phase_c_shared_library

# Per-language helpers.
_effective_goflags = _go.effective_goflags
_build_phase_go_executable = _go.build_phase_go_executable
_effective_rustflags = _rust.effective_rustflags
_build_phase_rust_executable = _rust.build_phase_rust_executable
_build_phase_rust_static_library = _rust.build_phase_rust_static_library
_build_phase_rust_shared_library = _rust.build_phase_rust_shared_library
_effective_zigflags = _zig.effective_zigflags
_build_phase_zig_executable = _zig.build_phase_zig_executable
_effective_nim_flags = _nim.effective_nim_flags
_build_phase_nim_executable = _nim.build_phase_nim_executable

# Python derivation helpers.
FAKE_SHA256 = _python.FAKE_SHA256
_python_pkg_attr = _python.python_pkg_attr
_venv_extra_pairs = _python.venv_extra_pairs
_mk_python_derivation = _python.mk_python_derivation

# Capsule helpers.
_capsule_image_tag = _capsule_common.capsule_image_tag
_capsule_manifest = _capsule_common.capsule_manifest
_capsule_runner_script = _capsule_common.capsule_runner_script
_emit_toml_volumes_array = _capsule_common.emit_toml_volumes_array
_emit_volume_helpers = _capsule_common.emit_volume_helpers
_PLACEHOLDER_PATH = _capsule_common.PLACEHOLDER_PATH
_PLACEHOLDER_BASH_BIN = _capsule_common.PLACEHOLDER_BASH_BIN
_mk_lite_capsule_derivation = _capsule_lite.mk_lite_capsule_derivation
_mk_nixos_capsule_derivation = _capsule_nixos.mk_nixos_capsule_derivation
_mk_qemu_capsule_derivation = _capsule_qemu.mk_qemu_capsule_derivation
_mk_test_derivation = _tests.mk_test_derivation

# Flake-assembly helpers.
_git_input_url = _flake.git_input_url
_is_cross_flake_ref = _flake.is_cross_flake_ref
_build_inputs = _flake.build_inputs
_internal_link_args = _flake.internal_link_args
_internal_include_args = _flake.internal_include_args
_install_phase_executable = _flake.install_phase_executable
_install_phase_shared_library = _flake.install_phase_shared_library
_install_phase_static_library = _flake.install_phase_static_library
_install_phase_run = _flake.install_phase_run
_build_phase_run = _flake.build_phase_run
_mk_custom_derivation = _flake.mk_custom_derivation
_mk_capsule_derivation = _flake.mk_capsule_derivation
_mk_derivation = _flake.mk_derivation
_flake_attrs = _flake.flake_attrs
_local_dep_url = _flake.local_dep_url
_collect_src_bindings = _flake.collect_src_bindings
_target_block = _flake.target_block

__all__ = ["generate"]
