"""Canonical formatter for `rigx.toml`.

Renders a TOML document in a stable shape: top-level sections in a fixed
order, fields within each section grouped into a known schema order, `=`
aligned within each table for readability, one blank line between
sections.

**Comments are NOT preserved** — `tomllib` discards them on parse and
re-emitting them faithfully would require a comment-preserving parser
(e.g. `tomlkit`). For now, document this caveat and let the user opt in
on a per-file basis."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

# TOML bare-key charset. Anything else must be emitted as a quoted key.
_BARE_KEY_RE = re.compile(r"^[A-Za-z0-9_-]+$")


class FmtError(ValueError):
    """Formatting failed, or the formatted output would not round-trip
    to the same document (in which case nothing is written)."""

# Top-level section order. Anything not listed lands in the catch-all
# `*_OTHER` group, sorted alphabetically.
_TOP_LEVEL_ORDER = [
    "project",
    "nixpkgs",
    "vars",
    "dependencies",
    "modules",
    "targets",
]

# Field order per kind. Listed fields render in this order; anything else
# falls through to alphabetical at the end.
_TARGET_FIELD_ORDER = [
    "kind",
    "language",
    "compiler",
    "backend",
    "sources",
    "includes",
    "public_headers",
    "entrypoint",
    "hostname",
    "ports",
    "volumes",
    "env",
    "python_version",
    "python_project",
    "python_venv_hash",
    "run",
    "args",
    "outputs",
    "cflags",
    "cxxflags",
    "goflags",
    "rustflags",
    "zigflags",
    "nim_flags",
    "ldflags",
    "defines",
    "deps",
    "build_script",
    "install_script",
    "script",
    "sandbox",
    "exclusive",
    "native_build_inputs",
    "nixos_modules",
]


def _escape_basic(v: str, *, multiline: bool = False) -> str:
    """Escape a string per TOML basic-string rules. Backslashes and
    quotes are always escaped so the value can never terminate the
    string early or introduce a stray escape (sed-style `\\1`, embedded
    `\"\"\"`, trailing backslash/quote are all safe). In multiline mode
    literal newlines are kept for readability."""
    out: list[str] = []
    for ch in v:
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\n":
            out.append("\n" if multiline else "\\n")
        elif ch == "\t":
            out.append("\t")
        elif ch == "\r":
            out.append("\\r")
        elif ord(ch) < 0x20 or ch == "\x7f":
            out.append(f"\\u{ord(ch):04X}")
        else:
            out.append(ch)
    return "".join(out)


def _emit_scalar(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, str):
        if "\n" in v:
            # Multi-line basic string; TOML trims the newline right
            # after the opening delimiter.
            return '"""\n' + _escape_basic(v, multiline=True) + '"""'
        return f'"{_escape_basic(v)}"'
    raise FmtError(f"cannot emit scalar: {v!r}")


def _emit_key(k: str) -> str:
    """Render a TOML key: bare when possible, quoted (and escaped)
    otherwise — keys with dots or spaces would otherwise be silently
    restructured into nested tables or emit invalid TOML."""
    if _BARE_KEY_RE.match(k):
        return k
    return f'"{_escape_basic(k)}"'


def _emit_value(v) -> str:
    if isinstance(v, list):
        return "[" + ", ".join(_emit_value(x) for x in v) + "]"
    if isinstance(v, dict):
        # Inline table.
        items = [f"{_emit_key(k)} = {_emit_value(x)}" for k, x in v.items()]
        return "{ " + ", ".join(items) + " }"
    return _emit_scalar(v)


def _ordered_items(d: dict, order: list[str]) -> list[tuple[str, object]]:
    """Yield d's items with `order` first (in that sequence), remaining
    keys appended in alphabetical order. Skips keys that are themselves
    sub-tables (those become their own [...] section, handled separately)."""
    ordered: list[tuple[str, object]] = []
    seen: set[str] = set()
    for k in order:
        if k in d:
            ordered.append((k, d[k]))
            seen.add(k)
    for k in sorted(d.keys()):
        if k not in seen:
            ordered.append((k, d[k]))
    return ordered


def _is_subsection(v) -> bool:
    """A nested dict that should render as [parent.child] rather than an
    inline `{ … }`. Heuristic: dict-of-dicts (which is how TOML
    represents `[targets.X]` etc.) — a flat dict is an inline table."""
    if not isinstance(v, dict):
        return False
    return any(isinstance(x, dict) for x in v.values())


def _emit_kv_block(
    items: list[tuple[str, object]], indent: str = ""
) -> str:
    """Emit `key = value` lines with `=` aligned within the block.
    Keys must already be rendered (via `_emit_key` / `_emit_dotted_keys`)
    — dotted composites like `deps.git` must not be re-quoted here."""
    if not items:
        return ""
    width = max(len(k) for k, _ in items)
    lines = []
    for k, v in items:
        lines.append(f"{indent}{k.ljust(width)} = {_emit_value(v)}")
    return "\n".join(lines)


def _emit_dotted_keys(prefix: str, d: dict) -> list[tuple[str, object]]:
    """Flatten one level of nesting into TOML dotted keys: `deps = {git=…}`
    becomes `deps.git = …` rendered as separate keys."""
    out: list[tuple[str, object]] = []
    for k, v in d.items():
        if isinstance(v, dict) and not _is_subsection(v) and v:
            for kk, vv in v.items():
                out.append((f"{_emit_key(k)}.{_emit_key(kk)}", vv))
        else:
            out.append((_emit_key(k), v))
    return out


def _emit_target(name: str, t: dict) -> str:
    """Render one [targets.<name>] table, including any nested
    [targets.<name>.variants.<v>] sub-tables underneath."""
    chunks: list[str] = []
    chunks.append(f"[targets.{_emit_key(name)}]")
    # Variants are nested sub-tables — separate them out.
    own = {k: v for k, v in t.items() if k != "variants"}
    body = _emit_dotted_keys("", own)
    body = _ordered_items(dict(body), _TARGET_FIELD_ORDER)
    if body:
        chunks.append(_emit_kv_block(body))
    for vname, vconf in (t.get("variants") or {}).items():
        chunks.append("")
        chunks.append(
            f"[targets.{_emit_key(name)}.variants.{_emit_key(vname)}]"
        )
        v_items = _emit_dotted_keys("", vconf)
        v_items = _ordered_items(dict(v_items), _TARGET_FIELD_ORDER)
        if v_items:
            chunks.append(_emit_kv_block(v_items))
    return "\n".join(chunks)


def _emit_section(key: str, value: object) -> str:
    """Render one top-level section. Targets are special-cased because
    each target is its own [targets.X] block."""
    if key == "targets" and isinstance(value, dict):
        if not value:
            return "[targets]"
        return "\n\n".join(_emit_target(n, t) for n, t in value.items())
    if key == "dependencies" and isinstance(value, dict):
        # [dependencies.git.X] / [dependencies.local.X] sub-sections.
        # Non-table values (directly under [dependencies] or under a
        # sub-table) must be preserved as key = value, not dropped.
        chunks: list[str] = []
        scalar_items = [
            (_emit_key(k), v)
            for k, v in value.items()
            if not isinstance(v, dict)
        ]
        sub_keys = sorted(
            k for k, v in value.items() if isinstance(v, dict)
        )
        if scalar_items or not sub_keys:
            chunks.append("[dependencies]")
            if scalar_items:
                chunks.append(_emit_kv_block(scalar_items))
            chunks.append("")
        for sub_key in sub_keys:
            sub = value[sub_key]
            scalar_confs = [
                (_emit_key(n), c)
                for n, c in sub.items()
                if not isinstance(c, dict)
            ]
            table_confs = [
                (n, c) for n, c in sub.items() if isinstance(c, dict)
            ]
            if scalar_confs or not table_confs:
                chunks.append(f"[dependencies.{_emit_key(sub_key)}]")
                if scalar_confs:
                    chunks.append(_emit_kv_block(scalar_confs))
                chunks.append("")
            for name, conf in table_confs:
                chunks.append(
                    f"[dependencies.{_emit_key(sub_key)}.{_emit_key(name)}]"
                )
                if conf:
                    chunks.append(
                        _emit_kv_block(
                            [(_emit_key(k), v) for k, v in conf.items()]
                        )
                    )
                chunks.append("")
        return "\n".join(chunks).rstrip()
    if key == "modules" and isinstance(value, dict):
        chunks = ["[modules]"]
        if value:
            chunks.append(
                _emit_kv_block(
                    [(_emit_key(k), v) for k, v in value.items()]
                )
            )
        return "\n".join(chunks)
    # Plain table → [key] header followed by aligned k = v block.
    chunks = [f"[{_emit_key(key)}]"]
    if isinstance(value, dict):
        chunks.append(
            _emit_kv_block([(_emit_key(k), v) for k, v in value.items()])
        )
    return "\n".join(chunks).rstrip()


def format_toml(source: str) -> str:
    """Parse a TOML string and re-emit it in canonical form. Round-trip
    is structurally idempotent: a second `format_toml` call on the
    output produces the same bytes. The output is verified to reparse
    to the same document; any emitter bug raises `FmtError` instead of
    silently corrupting the file."""
    try:
        data = tomllib.loads(source)
    except tomllib.TOMLDecodeError as e:
        raise FmtError(f"invalid TOML: {e}") from e
    sections: list[str] = []
    # Top-level scalars (rare, but legal TOML) render as a leading
    # key = value block; rewriting them as [key] headers would silently
    # delete the value.
    scalars = [(k, v) for k, v in data.items() if not isinstance(v, dict)]
    seen: set[str] = {k for k, _ in scalars}
    if scalars:
        sections.append(
            _emit_kv_block(
                [
                    (_emit_key(k), v)
                    for k, v in sorted(scalars, key=lambda kv: kv[0])
                ]
            )
        )
    for key in _TOP_LEVEL_ORDER:
        if key in data and key not in seen:
            sections.append(_emit_section(key, data[key]))
            seen.add(key)
    for key in sorted(data.keys()):
        if key not in seen:
            sections.append(_emit_section(key, data[key]))
    canonical = "\n\n".join(s for s in sections if s) + "\n"
    # Safety net: the canonical form must parse back to the exact same
    # document. This turns any residual emitter bug into a visible
    # error instead of silent config corruption on `rigx fmt --write`.
    try:
        reparsed = tomllib.loads(canonical)
    except tomllib.TOMLDecodeError as e:
        raise FmtError(
            f"formatter bug: output is not valid TOML ({e}); "
            "refusing to emit"
        ) from e
    if reparsed != data:
        raise FmtError(
            "formatter bug: formatting would change the file's meaning; "
            "refusing to emit"
        )
    return canonical


def format_file(path: Path, *, write: bool) -> str:
    """Format a `rigx.toml` file. Returns the canonical text. If `write`
    is True, also writes back to `path`."""
    canonical = format_toml(path.read_text())
    if write:
        path.write_text(canonical)
    return canonical
