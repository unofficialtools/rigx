"""Canonical formatter for `rigx.toml`.

Renders a TOML document in a stable shape: top-level sections in a fixed
order, fields within each section grouped into a known schema order, `=`
aligned within each table for readability, two blank lines between
sections.

**Comments are NOT preserved** — `tomllib` discards them on parse and
re-emitting them faithfully would require a comment-preserving parser
(e.g. `tomlkit`). For now, document this caveat and let the user opt in
on a per-file basis."""

from __future__ import annotations

import tomllib
from pathlib import Path

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
    "sources",
    "includes",
    "public_headers",
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
    "native_build_inputs",
]


def _emit_scalar(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, str):
        if "\n" in v:
            return '"""\n' + v + '"""'
        # Use double-quoted form; escape backslashes and quotes.
        escaped = v.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    raise ValueError(f"cannot emit scalar: {v!r}")


def _emit_value(v) -> str:
    if isinstance(v, list):
        return "[" + ", ".join(_emit_value(x) for x in v) + "]"
    if isinstance(v, dict):
        # Inline table.
        items = [f"{k} = {_emit_value(x)}" for k, x in v.items()]
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
    """Emit `key = value` lines with `=` aligned within the block."""
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
                out.append((f"{k}.{kk}", vv))
        else:
            out.append((k, v))
    return out


def _emit_target(name: str, t: dict) -> str:
    """Render one [targets.<name>] table, including any nested
    [targets.<name>.variants.<v>] sub-tables underneath."""
    chunks: list[str] = []
    chunks.append(f"[targets.{name}]")
    # Variants are nested sub-tables — separate them out.
    own = {k: v for k, v in t.items() if k != "variants"}
    body = _emit_dotted_keys("", own)
    body = _ordered_items(dict(body), _TARGET_FIELD_ORDER)
    if body:
        chunks.append(_emit_kv_block(body))
    for vname, vconf in (t.get("variants") or {}).items():
        chunks.append("")
        chunks.append(f"[targets.{name}.variants.{vname}]")
        v_items = _emit_dotted_keys("", vconf)
        v_items = _ordered_items(dict(v_items), _TARGET_FIELD_ORDER)
        if v_items:
            chunks.append(_emit_kv_block(v_items))
    return "\n".join(chunks)


def _emit_section(key: str, value: object) -> str:
    """Render one top-level section. Targets are special-cased because
    each target is its own [targets.X] block."""
    if key == "targets" and isinstance(value, dict):
        return "\n\n".join(_emit_target(n, t) for n, t in value.items())
    if key == "dependencies" and isinstance(value, dict):
        # [dependencies.git.X] / [dependencies.local.X] sub-sections.
        chunks: list[str] = []
        for sub_key in sorted(value.keys()):
            sub = value[sub_key]
            if not isinstance(sub, dict):
                continue
            for name, conf in sub.items():
                chunks.append(f"[dependencies.{sub_key}.{name}]")
                if isinstance(conf, dict) and conf:
                    chunks.append(_emit_kv_block(list(conf.items())))
                chunks.append("")
        return "\n".join(chunks).rstrip()
    if key == "modules" and isinstance(value, dict):
        chunks = ["[modules]"]
        if value:
            chunks.append(_emit_kv_block(list(value.items())))
        return "\n".join(chunks)
    # Plain table → [key] header followed by aligned k = v block.
    chunks = [f"[{key}]"]
    if isinstance(value, dict):
        chunks.append(_emit_kv_block(list(value.items())))
    return "\n".join(chunks).rstrip()


def format_toml(source: str) -> str:
    """Parse a TOML string and re-emit it in canonical form. Round-trip
    is structurally idempotent: a second `format_toml` call on the
    output produces the same bytes."""
    data = tomllib.loads(source)
    sections: list[str] = []
    seen: set[str] = set()
    for key in _TOP_LEVEL_ORDER:
        if key in data:
            sections.append(_emit_section(key, data[key]))
            seen.add(key)
    for key in sorted(data.keys()):
        if key not in seen:
            sections.append(_emit_section(key, data[key]))
    return "\n\n".join(s for s in sections if s) + "\n"


def format_file(path: Path, *, write: bool) -> str:
    """Format a `rigx.toml` file. Returns the canonical text. If `write`
    is True, also writes back to `path`."""
    canonical = format_toml(path.read_text())
    if write:
        path.write_text(canonical)
    return canonical
