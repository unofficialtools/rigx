"""Nix flake-generation package.

Public API: `generate(project)` returns the flake.nix body. Other
modules (render, c_family, capsule_*) are accessed via dotted import
when callers need to reuse a helper (e.g. tests).
"""

from rigx.nix.flake import generate

__all__ = ["generate"]
