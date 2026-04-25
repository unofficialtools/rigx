"""Drive Nix to build targets and materialize outputs."""

from __future__ import annotations

import fnmatch
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
    """Map a possibly-dotted rigx name to a Nix identifier. Mirrors
    `nix_gen._nix_id` — kept duplicated to avoid a builder→nix_gen
    private-symbol import. Hyphens are valid Nix identifier characters
    and pass through unchanged."""
    return name.replace(".", "_")


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
    [(qualified_name, exit_code), …] in execution order.

    Each entry in `filters` is treated as an fnmatch pattern (literal names
    work because fnmatch is exact-match for non-wildcard patterns). A test
    is selected if it matches *any* filter. `None`/empty filters → all.

    Tests reuse the `script` execution path: each runs in a `nix shell`
    with `deps.nixpkgs` on PATH, host-side, exit 0 = pass."""
    selected: list[tuple[str, object]] = []
    for name, target in project.targets.items():
        if target.kind != "test":
            continue
        if filters and not any(fnmatch.fnmatchcase(name, f) for f in filters):
            continue
        selected.append((name, target))
    results: list[tuple[str, int]] = []
    for name, target in selected:
        print(f"[rigx test] {name}")
        rc = run_script_target(project, target)
        results.append((name, rc))
    return results


def _has_glob(s: str) -> bool:
    return any(c in s for c in "*?[")


def _expand_build_spec(project: Project, spec: str) -> list[str]:
    """Resolve one CLI build spec to a list of Nix attrs.

    - Literal `hello`            → existing alias resolution.
    - Literal `hello@release`    → existing variant resolution.
    - Glob `hello*`              → match target names (variants ignored when
                                   matching), expand all variants per match.
                                   `*` alone is equivalent to `rigx build`
                                   with no args.

    Globs match own and `[modules]`-merged target names (`project.targets`).
    Cross-flake (A) refs aren't reachable through globs in v1 — name them
    explicitly or run `rigx build` from inside the sibling project."""
    base = spec.split("@", 1)[0]
    if _has_glob(base):
        if "@" in spec:
            raise BuildError(
                f"glob spec {spec!r} cannot include @variant — variants "
                f"are expanded automatically for matched targets"
            )
        names = sorted(
            n for n in project.targets if fnmatch.fnmatchcase(n, base)
        )
        if not names:
            raise BuildError(f"glob {spec!r} matched no targets")
        attrs: list[str] = []
        for name in names:
            target = project.targets[name]
            if target.kind in ("script", "test"):
                continue
            attr_base = _nix_id(name) if "." in name else name
            if not target.variants:
                attrs.append(attr_base)
            else:
                for v in target.variant_names():
                    attrs.append(f"{attr_base}-{v}")
        if not attrs:
            raise BuildError(
                f"glob {spec!r} matched only non-buildable targets "
                f"(script/test)"
            )
        return attrs
    name = base
    tgt_kind = (
        project.targets[name].kind if name in project.targets else None
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
    return [_resolve_attr(project, spec)]


def build(
    project: Project, specs: list[str], jobs: int | None = None,
) -> list[tuple[str, Path]]:
    """Build the given target specs (empty list = all). Returns (attr, out_link).

    Specs accept three shapes:
      - `hello`            — own / B-merged target by exact name.
      - `hello@release`    — pick a specific variant.
      - `hello*` / `*`     — fnmatch glob over target names. Variants are
                             ignored when matching; matched targets expand
                             all variants automatically.

    `script` and `test` targets are NOT buildable — naming one literally
    errors with a pointer at `rigx run` / `rigx test`; globs skip them
    silently.
    """
    if specs:
        attrs: list[str] = []
        for spec in specs:
            attrs.extend(_expand_build_spec(project, spec))
    else:
        attrs = _all_attrs(project)

    if not attrs:
        return []

    write_flake(project)
    output_dir = _output_dir(project)
    output_dir.mkdir(exist_ok=True)

    nix = _nix_bin()
    flake_refs = [_flake_ref(project, attr) for attr in attrs]

    # Build everything in a single `nix build` call so Nix's daemon can:
    #   - share the flake evaluation pass across attrs;
    #   - schedule independent derivations concurrently up to `--max-jobs`
    #     (forwarded from rigx's `--jobs N` flag).
    # We use `--no-link` + `--print-out-paths` and create our own symlinks
    # so the per-target `output/<attr>` UX is preserved exactly. Nix's
    # progress goes to stderr (passthrough); the captured stdout is just
    # the newline-separated store paths.
    cmd = [
        nix,
        *NIX_EXPERIMENTAL,
        "build",
        *flake_refs,
        "--no-link",
        "--print-out-paths",
    ]
    if jobs is not None:
        cmd += ["--max-jobs", str(jobs)]

    print(f"[rigx] building {len(attrs)} target(s): {', '.join(attrs)}")
    result = subprocess.run(
        cmd,
        check=False,
        stdout=subprocess.PIPE,
        stderr=None,
        text=True,
    )
    if result.returncode != 0:
        raise BuildError(f"nix build failed (exit {result.returncode})")

    out_paths = [line for line in result.stdout.split("\n") if line.strip()]
    if len(out_paths) != len(attrs):
        raise BuildError(
            f"nix build returned {len(out_paths)} store path(s) but "
            f"{len(attrs)} attr(s) were requested ({attrs!r}); cannot pair "
            f"outputs to symlinks"
        )

    results: list[tuple[str, Path]] = []
    for attr, store_path in zip(attrs, out_paths):
        out_link = output_dir / attr
        # Replace any existing symlink/file in-place so re-runs always point
        # at the freshest store path.
        if out_link.is_symlink() or out_link.exists():
            out_link.unlink()
        out_link.symlink_to(store_path)
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
