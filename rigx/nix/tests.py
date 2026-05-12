"""Sandboxed test derivation."""

from __future__ import annotations

from rigx.config import Project, Target
from rigx.nix.render import indent, nix_list, nix_str, rewrite_interp


def mk_test_derivation(target: Target, project: Project) -> str:
    """Sandboxed test (`kind = "test"`): the user's `script` becomes the
    derivation's buildPhase, success means the build succeeds. We
    synthesize a minimal `$out` so Nix doesn't complain about a missing
    output. Caching is automatic — unchanged inputs → cached pass."""
    from rigx.nix.flake import build_inputs as _build_inputs
    build_inputs = _build_inputs(target, project)
    body = rewrite_interp((target.script or "").strip("\n"), project)
    lines = [
        "pkgs.stdenv.mkDerivation {",
        f"  pname = {nix_str(target.qualified_name)};",
        f"  version = {nix_str(project.version)};",
        "  inherit src;",
        f"  buildInputs = {nix_list(build_inputs)};",
        "  dontConfigure = true;",
        "  buildPhase = ''",
        "    runHook preBuild",
        indent(body, 4),
        "    runHook postBuild",
        "  '';",
        "  installPhase = ''",
        "    mkdir -p $out",
        "    touch $out/passed",
        "  '';",
        "}",
    ]
    return "\n".join(lines)
