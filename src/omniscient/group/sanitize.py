"""Text hygiene shared by every omnigroup model: clipping and secret redaction.

Step texts and session titles are redacted with local paths masked. A pending's payload
is redacted leaf by leaf; what the owner answers to — a permission's preview
(`tool_input`, herdr's dialog `text`) and AskUserQuestion's `questions` (their text is
the answer key) — keeps paths, but secrets still read "[secret masqué]". Every string
leaf is clipped, and the whole payload is bounded.
"""

from __future__ import annotations

import json
from typing import Any

from omniscient.redact import redact_secrets

ELLIPSIS = "…"
PAYLOAD_TEXT_MAX = 64 * 1024  # one string leaf (the approval preview included)
PAYLOAD_MAX = 192 * 1024  # the whole payload, as JSON
PREVIEW_KEYS = {"permission": frozenset({"tool_input", "text"}), "question": frozenset({"questions"})}  # fmt: skip
MAX_DEPTH = 8


class PayloadError(ValueError):
    """A pending payload is too large or too deep."""


def clip(text: str, limit: int) -> str:
    """`text` cut to `limit` characters (ending with an ellipsis when cut)."""
    return text if len(text) <= limit else text[: limit - 1] + ELLIPSIS


def clean_text(text: str, limit: int) -> str:
    """Secret-redacted then clipped (redaction first: a cut must not hide a secret)."""
    return clip(redact_secrets(text), limit)


def _leaf(value: Any, preview: bool, depth: int) -> Any:
    if depth > MAX_DEPTH:
        raise PayloadError("pending.payload is nested too deeply")
    if isinstance(value, str):
        return clip(redact_secrets(value, keep_paths=preview), PAYLOAD_TEXT_MAX)
    if isinstance(value, dict):
        return {k: _leaf(v, preview, depth + 1) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_leaf(v, preview, depth + 1) for v in value]
    return value


def clean_payload(kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    """A redacted, clipped copy of a pending payload (previews keep their paths)."""
    out = {
        key: _leaf(value, key in PREVIEW_KEYS.get(kind, ()), 1)
        for key, value in payload.items()
    }
    size = len(json.dumps(out, ensure_ascii=False, default=str))
    if size > PAYLOAD_MAX:
        raise PayloadError(f"pending.payload too large ({size} > {PAYLOAD_MAX})")
    return out
