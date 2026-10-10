"""Write `[nixpkgs] rev` into a rigx.toml without disturbing the rest.

`rigx fmt` rewrites the whole file and drops comments, so `rigx lock
--pin` edits the one line instead: it replaces an existing `rev`, or
adds one after `ref` (or after the `[nixpkgs]` header), or appends a
`[nixpkgs]` table. The result is parsed back and compared with the
original, so an edit that would change anything but `[nixpkgs]` fails
instead of corrupting the file.
"""

from __future__ import annotations

import re
import tomllib

_HEADER = re.compile(r"^\s*\[\s*nixpkgs\s*\]\s*(#.*)?$")
_ANY_HEADER = re.compile(r"^\s*\[")
_REV = re.compile(r"^\s*rev\s*=")
_REF = re.compile(r"^\s*ref\s*=")
_OTHER_FORM = re.compile(r"^\s*nixpkgs\s*(\.|=)")


class PinError(ValueError):
    pass


def set_rev(text: str, rev: str, ref: str, note: str = "") -> str:
    """Return `text` with `[nixpkgs] rev = "<rev>"`. `ref` is written only
    when the file has no `[nixpkgs]` table yet; `note` becomes a trailing
    comment on the `rev` line."""
    line = f'rev = "{rev}"' + (f"  # {note}" if note else "")
    lines = text.splitlines(keepends=True)
    header = next((i for i, l in enumerate(lines) if _HEADER.match(l)), None)

    if header is None:
        if any(_OTHER_FORM.match(l) for l in lines):
            raise PinError(
                "rigx.toml sets nixpkgs as a dotted key or inline table; "
                f'add `rev = "{rev}"` to it by hand'
            )
        sep = "" if not text or text.endswith("\n") else "\n"
        new = f'{text}{sep}\n[nixpkgs]\nref = "{ref}"\n{line}\n'
    else:
        end = next(
            (i for i in range(header + 1, len(lines)) if _ANY_HEADER.match(lines[i])),
            len(lines),
        )
        body = range(header + 1, end)
        rev_at = next((i for i in body if _REV.match(lines[i])), None)
        if rev_at is not None:
            lines[rev_at] = line + "\n"
        else:
            ref_at = next((i for i in body if _REF.match(lines[i])), header)
            if not lines[ref_at].endswith("\n"):
                lines[ref_at] += "\n"
            lines.insert(ref_at + 1, line + "\n")
        new = "".join(lines)

    _verify(text, new, rev)
    return new


def _verify(old: str, new: str, rev: str) -> None:
    try:
        before, after = tomllib.loads(old), tomllib.loads(new)
    except tomllib.TOMLDecodeError as e:
        raise PinError(f"editing rigx.toml would break it ({e}); add rev by hand")
    pinned = after.get("nixpkgs", {})
    before.pop("nixpkgs", None)
    after.pop("nixpkgs", None)
    if before != after or not isinstance(pinned, dict) or pinned.get("rev") != rev:
        raise PinError("could not edit [nixpkgs] in rigx.toml safely; add rev by hand")
