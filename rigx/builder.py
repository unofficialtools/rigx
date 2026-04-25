"""Drive Nix to build targets and materialize outputs."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from rigx import nix_gen
from rigx.config import Project

OUTPUT_DIR = "output"
FLAKE_FILE = "flake.nix"
NIX_EXPERIMENTAL = ["--extra-experimental-features", "nix-command flakes"]


class BuildError(RuntimeError):
    pass


class NixNotFoundError(BuildError):
    """Raised when the `nix` binary is not on PATH."""


NIX_INSTALL_INSTRUCTIONS = """\
Nix is required but was not found on PATH.

rigx runs all builds inside Nix's sandbox. Install Nix, restart your shell,
then re-run `rigx`.

Install instructions
--------------------

macOS / Linux (official installer):
  sh <(curl -L https://nixos.org/nix/install) --daemon

macOS / Linux (Determinate Systems installer, recommended for most users):
  curl --proto '=https' --tlsv1.2 -sSf -L \\
    https://install.determinate.systems/nix | sh -s -- install

After installing, restart your shell or source:
  . /nix/var/nix/profiles/default/etc/profile.d/nix-daemon.sh

Docs: https://nixos.org/download
"""


def _flake_path(project: Project) -> Path:
    return project.root / FLAKE_FILE


def _output_dir(project: Project) -> Path:
    return project.root / OUTPUT_DIR


def _nix_bin() -> str:
    nix = shutil.which("nix")
    if not nix:
        raise NixNotFoundError(NIX_INSTALL_INSTRUCTIONS)
    return nix


def write_flake(project: Project) -> Path:
    """Regenerate flake.nix at the project root. Returns the file path.

    Recursively regenerates flake.nix for every transitive local-dep so the
    sub-flakes referenced as path inputs are themselves valid flakes."""
    for ldep in project.local_deps.values():
        if ldep.sub_project:
            write_flake(ldep.sub_project)
    flake_path = _flake_path(project)
    new_content = nix_gen.generate(project)
    changed = (
        not flake_path.is_file()
        or flake_path.read_text() != new_content
    )
    flake_path.write_text(new_content)
    if changed:
        _hint_commit_generated(project, ["flake.nix"])
    return flake_path


def _is_git_work_tree(root: Path) -> bool:
    git = shutil.which("git")
    if not git:
        return False
    r = subprocess.run(
        [git, "rev-parse", "--is-inside-work-tree"],
        cwd=root, check=False, capture_output=True, text=True,
    )
    return r.returncode == 0 and r.stdout.strip() == "true"


def _hint_commit_generated(project: Project, files: list[str]) -> None:
    """Print a one-line reminder when rigx-generated files change inside a
    git work-tree. Stays silent outside git (no actionable advice). rigx
    never touches the index itself — committing is the user's call."""
    if not _is_git_work_tree(project.root):
        return
    joined = " ".join(files)
    print(
        f"[rigx] regenerated {joined} — commit when stable so future "
        f"runs reuse the same lock.",
        file=sys.stderr,
    )


def _flake_ref(project: Project, attr: str | None = None) -> str:
    # Use the explicit "path:" scheme so Nix treats the directory as a flake
    # regardless of any enclosing git repo (which otherwise hides untracked
    # files like the generated flake.nix).
    ref = f"path:{project.root.resolve()}"
    return f"{ref}#{attr}" if attr else ref


def run_nixpkgs_tool(
    project: Project,
    attr: str,
    args: list[str],
    cwd: Path | None = None,
) -> int:
    """Invoke a binary from the project's pinned nixpkgs via `nix run`.

    Lets rigx wrap tools like `uv` without requiring the user to install them
    on their host — the project's `[nixpkgs].ref` (pinned via flake.lock)
    provides a reproducible binary.
    """
    nix = _nix_bin()
    cmd = [
        nix,
        *NIX_EXPERIMENTAL,
        "run",
        f"nixpkgs/{project.nixpkgs_ref}#{attr}",
        "--",
        *args,
    ]
    result = subprocess.run(cmd, cwd=cwd, check=False)
    return result.returncode


def update_lock(project: Project) -> None:
    write_flake(project)
    lock_path = project.root / "flake.lock"
    before = lock_path.read_bytes() if lock_path.is_file() else b""
    nix = _nix_bin()
    cmd = [nix, *NIX_EXPERIMENTAL, "flake", "lock", _flake_ref(project)]
    result = subprocess.run(cmd, check=False)
    if result.returncode != 0:
        raise BuildError(f"nix flake lock failed (exit {result.returncode})")
    after = lock_path.read_bytes() if lock_path.is_file() else b""
    if before != after:
        _hint_commit_generated(project, ["flake.lock"])


def _resolve_attr(project: Project, spec: str) -> str:
    """Map a user target spec to a Nix attr name. Accepted shapes:

    - `hello`                — own target, no variant.
    - `hello@debug`          — own target, variant.
    - `frontend.app`         — local-dep (cross-flake) target, no variant.
    - `frontend.app@release` — local-dep target, variant.

    Cross-flake variants are validated against the sub-project's metadata
    (loaded recursively at config time), so a typo'd variant fails fast here
    instead of producing a missing-attr error from `nix build`."""
    if "@" in spec:
        name, variant = spec.split("@", 1)
    else:
        name, variant = spec, None

    dotted = "." in name
    if dotted and name in project.targets:
        # B-merged target (`frontend.greet` is a key in this flake's targets).
        target = project.targets[name]
    elif dotted:
        # A-style cross-flake ref into a sibling local-dep.
        resolved = project.find_target(name)
        if resolved is None:
            raise BuildError(f"no such target: {name}")
        owning, target_name = resolved
        target = owning.targets[target_name]
    else:
        if name not in project.targets:
            raise BuildError(f"no such target: {name}")
        target = project.targets[name]

    if variant is not None and variant not in target.variants:
        available = ", ".join(target.variant_names()) or "(none)"
        raise BuildError(
            f"target {name!r} has no variant {variant!r}; available: {available}"
        )

    raw = name if variant is None else f"{name}-{variant}"
    # Any dotted form (B-merged or cross-flake) gets sanitized for the Nix
    # attr; plain own-target names pass through unchanged so existing
    # hyphenated variant attrs (`hello-debug`) keep working.
    return _nix_id(raw) if dotted else raw


def _nix_id(name: str) -> str:
    """Map a possibly-dotted/hyphenated rigx name to a Nix identifier.
    Mirrors `nix_gen._nix_id` — kept duplicated to avoid a builder→nix_gen
    private-symbol import."""
    return name.replace(".", "_").replace("-", "_")


def _all_attrs(project: Project) -> list[str]:
    """Every build attribute (targets with variants expand to one per variant).

    `script`-kind targets are excluded so `rigx build` with no arguments does
    not inadvertently run side-effecting tasks (publish, deploy, etc.). Run
    them explicitly with `rigx build <name>`.

    Names are sanitized when dotted (B-merged module targets) so they match
    the Nix attrs emitted by `nix_gen._target_block`.
    """
    attrs: list[str] = []
    for name, target in project.targets.items():
        if target.kind in ("script", "test"):
            continue
        attr_base = _nix_id(name) if "." in name else name
        if not target.variants:
            attrs.append(attr_base)
        else:
            for v in target.variant_names():
                attrs.append(f"{attr_base}-{v}")
    return attrs


def run_script_target(
    project: Project, target, extra_args: list[str] | None = None
) -> int:
    """Execute a `kind = "script"` target via `nix shell` on the host.

    Tools listed in `deps.nixpkgs` are brought onto PATH from the project's
    pinned nixpkgs. Runs in the project root; no sandbox. The target's script
    is passed to `bash -eo pipefail`. `extra_args` (typed after `--` on the
    command line) are forwarded as `$1`, `$2`, … inside the script.
    """
    assert target.script is not None
    nix = _nix_bin()
    refs = [
        f"nixpkgs/{project.nixpkgs_ref}#{pkg}"
        for pkg in target.deps.nixpkgs
    ]
    cmd = [
        nix,
        *NIX_EXPERIMENTAL,
        "shell",
        *refs,
        "--command",
        "bash",
        "-eo",
        "pipefail",
        "-c",
        target.script,
        target.name,
        *(extra_args or []),
    ]
    result = subprocess.run(cmd, cwd=project.root, check=False)
    return result.returncode


def run_named_script(
    project: Project, name: str, extra_args: list[str] | None = None
) -> None:
    """CLI helper: look up a script target by name and execute it."""
    if name not in project.targets:
        raise BuildError(f"no such target: {name}")
    target = project.targets[name]
    if target.kind != "script":
        raise BuildError(
            f"target {name!r} (kind={target.kind!r}) is not a script target; "
            f"use `rigx build {name}` instead"
        )
    print(f"[rigx] running {name}")
    rc = run_script_target(project, target, extra_args)
    if rc != 0:
        raise BuildError(f"script target {name!r} failed (exit {rc})")


def run_tests(
    project: Project, filters: list[str] | None = None
) -> list[tuple[str, int]]:
    """Discover and execute every `kind = "test"` target. Returns
    [(qualified_name, exit_code), …] in execution order. A non-empty
    `filters` list narrows discovery to those exact target names.

    Tests reuse the `script` execution path: each runs in a `nix shell`
    with `deps.nixpkgs` on PATH, host-side, exit 0 = pass."""
    selected: list[tuple[str, object]] = []
    for name, target in project.targets.items():
        if target.kind != "test":
            continue
        if filters and name not in filters:
            continue
        selected.append((name, target))
    results: list[tuple[str, int]] = []
    for name, target in selected:
        print(f"[rigx test] {name}")
        rc = run_script_target(project, target)
        results.append((name, rc))
    return results


def build(project: Project, specs: list[str]) -> list[tuple[str, Path]]:
    """Build the given target specs (empty list = all). Returns (attr, out_link).

    `script`-kind targets are NOT buildable — they produce no artifact. Name a
    script here and you'll get an error pointing at `rigx run`. `rigx build`
    with no args skips them (see `_all_attrs`).
    """
    if specs:
        for spec in specs:
            name = spec.split("@", 1)[0]
            tgt_kind = (
                project.targets[name].kind
                if name in project.targets else None
            )
            if tgt_kind == "test":
                raise BuildError(
                    f"target {name!r} is a test target; use `rigx test {name}` instead"
                )
            if tgt_kind == "script":
                raise BuildError(
                    f"target {name!r} is a script target (produces no artifact); "
                    f"use `rigx run {name}` instead"
                )
        attrs = [_resolve_attr(project, s) for s in specs]
    else:
        attrs = _all_attrs(project)

    if not attrs:
        return []

    write_flake(project)
    output_dir = _output_dir(project)
    output_dir.mkdir(exist_ok=True)

    nix = _nix_bin()
    results: list[tuple[str, Path]] = []

    for attr in attrs:
        out_link = output_dir / attr
        flake_ref = _flake_ref(project, attr)
        cmd = [
            nix,
            *NIX_EXPERIMENTAL,
            "build",
            flake_ref,
            "--out-link",
            str(out_link),
        ]
        print(f"[rigx] building {attr}")
        result = subprocess.run(cmd, check=False)
        if result.returncode != 0:
            raise BuildError(f"nix build {attr} failed (exit {result.returncode})")
        results.append((attr, out_link))

    return results


def clean(project: Project) -> None:
    output_dir = _output_dir(project)
    if output_dir.exists():
        # Remove symlinks and their contents (but not the Nix store paths themselves).
        for child in output_dir.iterdir():
            if child.is_symlink():
                child.unlink()
            elif child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
        output_dir.rmdir()
