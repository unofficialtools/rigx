"""Python (uv-managed venv) derivations."""

from __future__ import annotations

from rigx.config import Project, Target
from rigx.nix.render import indent, nix_str


FAKE_SHA256 = "sha256-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="


def python_pkg_attr(version: str) -> str:
    """'3.12' -> 'python312'."""
    major_minor = version.replace(".", "")
    return f"python{major_minor}"


def venv_extra_pairs(target: Target) -> list[tuple[str, str]]:
    """Map each `python_venv_extra` entry to (src_in_flake, dest_in_FOD).

    Entries land in `target.python_venv_extra` as project-root-relative paths
    (the loader's expansion already prefixed them with `python_project`).
    The FOD source has `pyproject.toml` at its root, so destinations are
    re-rooted to be python_project-relative."""
    pdir = target.python_project.strip("/")
    pairs: list[tuple[str, str]] = []
    for entry in target.python_venv_extra:
        src = entry
        if pdir in (".", ""):
            dest = entry
        elif entry.startswith(pdir + "/"):
            dest = entry[len(pdir) + 1:]
        elif entry == pdir:
            dest = ""
        else:
            dest = entry
        pairs.append((src, dest))
    return pairs


def mk_python_derivation(target: Target, project: Project) -> str:
    """Python scripts: build a uv-managed venv (FOD) and wrap the entry script.

    The venv is produced by a fixed-output derivation that runs
    `uv sync --frozen` against the user's pyproject.toml + uv.lock. Network is
    permitted (FODs can fetch); reproducibility is pinned by the outputHash.
    """
    if not target.sources:
        raise ValueError(f"python_script {target.name!r} needs at least one source")
    entry = target.sources[0]
    entry_dir = "/".join(entry.split("/")[:-1]) or "."

    pdir = target.python_project.strip("/")
    if pdir == "" or pdir == ".":
        pyproject_nix = "./pyproject.toml"
        uvlock_nix = "./uv.lock"
    else:
        pyproject_nix = f"./{pdir}/pyproject.toml"
        uvlock_nix = f"./{pdir}/uv.lock"

    venv_hash = target.python_venv_hash or FAKE_SHA256
    python_attr = python_pkg_attr(target.python_version)

    copy_lines = ["mkdir -p $out/share/" + target.name, "mkdir -p $out/bin"]
    for src in target.sources:
        parent = "/".join(src.split("/")[:-1])
        if parent:
            copy_lines.append(f"mkdir -p $out/share/{target.name}/{parent}")
        copy_lines.append(f"cp {src} $out/share/{target.name}/{src}")

    site_pkgs = f"lib/python{target.python_version}/site-packages"

    install_phase = (
        "runHook preInstall\n"
        + "\n".join(copy_lines)
        + "\n"
        f"makeWrapper ${{pythonPkg}}/bin/python3 $out/bin/{target.name} \\\n"
        f"  --add-flags $out/share/{target.name}/{entry} \\\n"
        f"  --prefix PYTHONPATH : $out/share/{target.name}/{entry_dir} \\\n"
        f"  --prefix PYTHONPATH : ${{pythonVenv}}/{site_pkgs}\n"
        "runHook postInstall\n"
    )

    lines: list[str] = []
    lines.append("let")
    lines.append(f"  pythonPkg = pkgs.{python_attr};")
    lines.append("  venvSrc = pkgs.runCommand \"" + target.name + "-pysrc\" {} ''")
    lines.append("    mkdir -p $out")
    lines.append(f"    cp ${{{pyproject_nix}}} $out/pyproject.toml")
    lines.append(f"    cp ${{{uvlock_nix}}} $out/uv.lock")
    for src_rel, dest_rel in venv_extra_pairs(target):
        parent = "/".join(dest_rel.split("/")[:-1])
        if parent:
            lines.append(f"    mkdir -p $out/{parent}")
        lines.append(f"    cp -r ${{./{src_rel}}} $out/{dest_rel}")
    lines.append("  '';")
    lines.append("  pythonVenv = pkgs.stdenv.mkDerivation {")
    lines.append(f"    name = \"{target.name}-venv\";")
    lines.append("    src = venvSrc;")
    lines.append("    nativeBuildInputs = [ pkgs.uv pythonPkg pkgs.cacert ];")
    lines.append("    dontConfigure = true;")
    lines.append("    SSL_CERT_FILE = \"${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt\";")
    lines.append("    UV_PYTHON_DOWNLOADS = \"never\";")
    lines.append("    UV_PYTHON_PREFERENCE = \"only-system\";")
    lines.append("    UV_LINK_MODE = \"copy\";")
    lines.append("    UV_NO_CACHE = \"1\";")
    lines.append("    SOURCE_DATE_EPOCH = \"1\";")
    lines.append("    buildPhase = ''")
    lines.append("      runHook preBuild")
    lines.append("      export HOME=$TMPDIR")
    lines.append("      uv sync --frozen --no-install-project --python ${pythonPkg}/bin/python3")
    lines.append("      rm -f .venv/pyvenv.cfg")
    lines.append("      find .venv -type l | while IFS= read -r link; do")
    lines.append("        t=$(readlink \"$link\")")
    lines.append("        case \"$t\" in /nix/store/*) rm \"$link\" ;; esac")
    lines.append("      done")
    lines.append("      find .venv -type d -name __pycache__ -prune -exec rm -rf {} +")
    lines.append("      rm -rf .venv/bin")
    lines.append("      find .venv -exec touch -h -d @1 {} +")
    lines.append("      runHook postBuild")
    lines.append("    '';")
    lines.append("    installPhase = ''")
    lines.append("      runHook preInstall")
    lines.append("      cp -r .venv/. $out")
    lines.append("      runHook postInstall")
    lines.append("    '';")
    lines.append("    outputHashMode = \"recursive\";")
    lines.append("    outputHashAlgo = \"sha256\";")
    lines.append(f"    outputHash = \"{venv_hash}\";")
    lines.append("  };")
    lines.append("in pkgs.stdenv.mkDerivation {")
    lines.append(f"  pname = {nix_str(target.qualified_name)};")
    lines.append(f"  version = {nix_str(project.version)};")
    lines.append("  inherit src;")
    lines.append("  nativeBuildInputs = [ pkgs.makeWrapper ];")
    lines.append("  buildInputs = [ pythonPkg ];")
    lines.append("  dontConfigure = true;")
    lines.append("  dontBuild = true;")
    lines.append("  installPhase = ''")
    lines.append(indent(install_phase, 4).rstrip() + "\n")
    lines.append("  '';")
    lines.append("}")
    return "\n".join(lines)
