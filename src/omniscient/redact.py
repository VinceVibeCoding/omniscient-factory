"""Secret redaction (stdlib only, cheap to import: the omnigroup hook path uses it)."""

from __future__ import annotations

import re

# Anything that must never reach a public page.
SECRET_PATTERNS = (
    re.compile(r"xox[abpr]-[A-Za-z0-9-]{10,}"),
    re.compile(r"xapp-[A-Za-z0-9-]{10,}"),
    re.compile(r"sk-ant-[A-Za-z0-9_-]{10,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{30,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"/home/[a-z_][a-z0-9_-]*/"),  # a local path leaks the user name
)
REDACTED = "[masqué]"


def redact_secrets(text: str) -> str:
    """Text with every secret-looking string (tokens, keys, local paths) masked."""
    for pattern in SECRET_PATTERNS:
        text = pattern.sub(REDACTED, text)
    return text
