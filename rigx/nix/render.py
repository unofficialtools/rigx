"""Low-level Nix/shell rendering primitives shared by every backend module."""

from __future__ import annotations

import re
from typing import Iterable

from rigx.config import Project


def nix_str(s: str) -> str:
    """Quote a Python string for Nix (double-quoted form)."""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$") + '"'


def nix_interp_str(s: str) -> str:
    """Quote a Python string for Nix, preserving `${name}` interpolation.
    Escapes backslashes and quotes; leaves `$` alone so Nix interpolates
    `${name}` against the surrounding `rec` scope at flake-eval time.
    Same convention `kind = "run"` args and `kind = "custom"` scripts use
    to refer to other rigx-built deps' store paths."""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def nix_id(qualified: str) -> str:
    """Map a possibly-dotted name ('frontend.app') to a Nix identifier
    ('frontend_app'). Hyphens stay verbatim — they're valid Nix
    identifier characters."""
    return qualified.replace(".", "_")


# Matches `${X.Y}` (and `${X.Y.Z}` etc.) where each segment is identifier-ish
# (hyphens allowed, since `lib-release` is a valid variant attr).
_INTERP_DOTTED = re.compile(
    r"\$\{([A-Za-z_][A-Za-z0-9_\-]*(?:\.[A-Za-z_][A-Za-z0-9_\-]*)+)\}"
)


def rewrite_interp(s: str, project: Project) -> str:
    """Rewrite `${X.Y}` to `${X_Y}` when the dotted ref names either:
    - a cross-flake (`[dependencies.local.X]`) target, or
    - a `[modules]`-merged target (`X.Y` is a key in `project.targets`), or
    - an `[external_inputs.X]` bucket (any user-declared bucket name).
    Other dotted forms (e.g. `pkgs.foo`) are left alone."""
    def sub(m):
        qual = m.group(1)
        if qual.split(".", 1)[0] in project.local_deps:
            return "${" + nix_id(qual) + "}"
        if qual in project.targets:
            return "${" + nix_id(qual) + "}"
        head, _, tail = qual.partition(".")
        ext = project.external_inputs.get(head)
        if ext is not None and tail in ext.buckets:
            return "${" + _external_bucket_var(head, tail) + "}"
        return m.group(0)
    return _INTERP_DOTTED.sub(sub, s)


def _external_bucket_var(name: str, bucket: str) -> str:
    """Nix let-binding identifier for `[external_inputs.<name>]`'s
    `<bucket>` directory. Kept here so both the rewriter and the flake
    emitter agree on the name."""
    return f"ext_{nix_id(name)}_{bucket}"


def nix_list(exprs: list[str]) -> str:
    if not exprs:
        return "[ ]"
    return "[ " + " ".join(exprs) + " ]"


def nix_value(v) -> str:
    """Render a Python scalar / list / dict as a Nix expression."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, str):
        return nix_str(v)
    if isinstance(v, list):
        return "[ " + " ".join(nix_value(x) for x in v) + " ]"
    if isinstance(v, dict):
        items = []
        for k, val in v.items():
            key = k if re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", k) else nix_str(k)
            items.append(f"{key} = {nix_value(val)};")
        if not items:
            return "{ }"
        return "{ " + " ".join(items) + " }"
    if v is None:
        return "null"
    raise TypeError(f"cannot render {v!r} as a Nix value")


def indent(text: str, n: int) -> str:
    prefix = " " * n
    return "".join(
        (prefix + line) if line.strip() else line
        for line in text.splitlines(keepends=True)
    )


def shell_quote(s: str) -> str:
    """Quote a single shell word.

    Kept hand-rolled (rather than delegating to `shlex.quote`) because
    the exact byte output is part of the flake-eval hash — switching
    would change derivation paths for every C/C++/run target without
    actually fixing anything. Functionally equivalent to `shlex.quote`."""
    if not s or any(c in s for c in " \t\n\"'$\\`*?[]{}|&;<>()!#~"):
        return "'" + s.replace("'", "'\\''") + "'"
    return s


def sh_join(parts: Iterable[str]) -> str:
    """Render an iterable as a single, properly-quoted shell command line.

    All build phases that compose `compiler + flags + sources + -o name`
    funnel through this so a filename with a space or an unusual
    character in a TOML field can't escape into bash syntax."""
    return " ".join(shell_quote(str(p)) for p in parts)
