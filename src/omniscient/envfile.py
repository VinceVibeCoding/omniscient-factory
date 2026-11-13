"""One `.env` dialect, shared by the writer (onboarding) and every reader.

    KEY=value                 unquoted: trimmed, ` #` starts a comment
    KEY="va\\"lue # x"        double quotes: `\\` and `\"` are the only escapes
    KEY='raw value'           single quotes: taken literally
    export KEY=value          `export ` is accepted and ignored

`quote_value` always writes the double-quoted form, so any value without a line break
reads back byte for byte (`parse_value(quote_value(v)) == v`).
"""

from __future__ import annotations

import re

KEY_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")
_ESCAPES = {"\\": "\\", '"': '"'}


def quote_value(value: str) -> str:
    if "\n" in value or "\r" in value:
        raise ValueError("valeur .env invalide (retour à la ligne)")
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _double_quoted(raw: str) -> str:
    out: list[str] = []
    index = 1
    while index < len(raw):
        char = raw[index]
        if char == "\\" and index + 1 < len(raw) and raw[index + 1] in _ESCAPES:
            out.append(_ESCAPES[raw[index + 1]])
            index += 2
            continue
        if char == '"':
            break
        out.append(char)
        index += 1
    return "".join(out)


def parse_value(raw: str) -> str:
    """The value part of a KEY=... line, unquoted and unescaped."""
    raw = raw.strip()
    if raw.startswith('"'):
        return _double_quoted(raw)
    if raw.startswith("'"):
        end = raw.find("'", 1)
        return raw[1:end] if end > 0 else raw[1:]
    return raw.split(" #", 1)[0].strip()


def parse_line(line: str) -> tuple[str, str] | None:
    """(key, value) for a KEY=... line; None for blanks, comments and junk."""
    if line.lstrip().startswith("#"):
        return None
    match = KEY_RE.match(line)
    return (match.group(1), parse_value(match.group(2))) if match else None


def parse_env(text: str) -> dict[str, str]:
    pairs = (parse_line(line) for line in text.splitlines())
    return dict(pair for pair in pairs if pair is not None)
