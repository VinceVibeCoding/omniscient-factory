"""Edit top-level members of a JSON settings file, keeping every other byte.

Used by the omnigroup hook installer: only the members whose value changes are
re-rendered (with the file's own indentation and line endings); the rest of the text,
other members included, stays byte for byte.
"""

from __future__ import annotations

import json
from typing import Any

REMOVE: Any = object()  # "no such member"


class JsonEditError(ValueError):
    """The edit would change more than the requested members."""


def _render(value: Any, indent: str, nl: str) -> str:
    unit = len(indent) or 2  # "hooks" sits one level deep: its indent is the unit
    return json.dumps(value, indent=unit, ensure_ascii=False).replace("\n", nl + indent)


def _member_span(text: str, wanted: str) -> tuple[int, int, int] | None:
    """(key start, value start, value end) of a top-level member of the JSON text."""
    decoder = json.JSONDecoder()
    i = text.index("{") + 1
    while True:
        while i < len(text) and text[i] in " \t\r\n,":
            i += 1
        if text[i] == "}":
            return None
        key_start = i
        key, i = decoder.raw_decode(text, i)
        i = text.index(":", i) + 1
        while text[i] in " \t\r\n":
            i += 1
        _, end = decoder.raw_decode(text, i)
        if key == wanted:
            return key_start, i, end
        i = end


def _set_member(text: str, key: str, value: Any) -> str:
    """The JSON text with one top-level member replaced, added, or removed (REMOVE)."""
    nl = "\r\n" if "\r\n" in text else "\n"
    span = _member_span(text, key)
    if span is None and value is REMOVE:
        return text
    if span is None:
        root = text.index("{") + 1
        rest = text[root:].lstrip()
        comma = "" if rest.startswith("}") else ","
        return f'{text[:root]}{nl}  "{key}": {_render(value, "  ", nl)}{comma}{text[root:]}'
    key_start, start, end = span
    if value is not REMOVE:
        line = text[text.rfind("\n", 0, start) + 1 : start]
        indent = line[: len(line) - len(line.lstrip())]
        return text[:start] + _render(value, indent, nl) + text[end:]
    return _remove_member(text, key_start, end)


def _remove_member(text: str, key_start: int, end: int) -> str:
    j = end
    while text[j] in " \t":
        j += 1
    if text[j] == ",":  # not the last member: drop it with its comma and line
        j += 1
        while text[j] in " \t":
            j += 1
        j += 2 if text.startswith("\r\n", j) else 1 if text[j] == "\n" else 0
        line_start = text.rfind("\n", 0, key_start) + 1
        own_line = not text[line_start:key_start].strip()
        return text[: line_start if own_line else key_start] + text[j:]
    p = len(text[:key_start].rstrip()) - 1
    return text[:p] + text[end:] if text[p] == "," else text[:key_start] + text[end:]


def rewrite(text: str | None, old: dict[str, Any], new: dict[str, Any]) -> str:
    """`text` with only the top-level members that differ between old and new changed."""
    if text is None or not text.strip():
        return json.dumps(new, indent=2, ensure_ascii=False) + "\n"
    out = text
    for key in dict.fromkeys([*old, *new]):
        if old.get(key, REMOVE) != new.get(key, REMOVE):
            out = _set_member(out, key, new.get(key, REMOVE))
    if json.loads(out) != new:  # paranoia: nothing else moved
        raise JsonEditError("résultat inattendu : rien n'a été modifié")
    return out
