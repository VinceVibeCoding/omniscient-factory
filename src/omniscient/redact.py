"""Secret redaction (stdlib only, cheap to import: the omnigroup hook path uses it).

`redact_secrets(text)` masks tokens, keys, credentials in URLs and headers,
`*_TOKEN=value` assignments, and local `/home/<user>/` paths. With `keep_paths=True`
paths stay and each secret reads "[secret masqué]": the form used for approval
previews, where the owner must see what is being approved. Redaction is idempotent.
"""

from __future__ import annotations

import re

REDACTED = "[masqué]"
SECRET_MARK = "[secret masqué]"
_NOT_MASKED = r"(?!\[(?:secret )?masqué\])"
_ASSIGNED = r"(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s\"',;]+)"

# A pattern with a `keep` group keeps that prefix (header or variable name, URL scheme)
# and masks the rest.
CREDENTIAL_PATTERNS = (
    re.compile(r"xox[abpr]-[A-Za-z0-9-]{10,}"),
    re.compile(r"xapp-[A-Za-z0-9-]{10,}"),
    re.compile(r"sk-ant-[A-Za-z0-9_-]{10,}"),
    re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}"),  # OpenAI
    re.compile(r"\b[sr]k_live_[0-9A-Za-z]{16,}"),  # Stripe
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}"),  # Google API key
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{30,}"),
    re.compile(r"\bnpm_[A-Za-z0-9]{36}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),  # JWT
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(
        r"(?i)(?P<keep>\bAuthorization[\"']?+\s*+[:=]\s*+[\"']?+)"
        + _NOT_MASKED
        + r"[^\r\n\"']+"
    ),
    re.compile(r"(?i)(?P<keep>\bBearer\s++)" + _NOT_MASKED + r"[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"(?i)(?P<keep>\b[a-z][a-z0-9+.-]*://)[^\s/@\[\]]+@"),  # URL userinfo
    re.compile(
        r"(?i)(?P<keep>\b[A-Za-z0-9_]*(?:_KEY|_TOKEN|_SECRET|PASSWORD)[\"']?+\s*+[=:]\s*+)"
        + _NOT_MASKED
        + _ASSIGNED
    ),
)
PATH_PATTERN = re.compile(
    r"/home/[a-z_][a-z0-9_-]*/"
)  # a local path leaks the user name
# Anything that must never reach a public page (build/publish scans for these).
SECRET_PATTERNS = (*CREDENTIAL_PATTERNS, PATH_PATTERN)


def _mask(mark: str, match: re.Match[str]) -> str:
    keep = match.groupdict().get("keep") or ""
    userinfo = "://" in keep and match.group(0).endswith("@")
    return keep + mark + ("@" if userinfo else "")


def redact_secrets(text: str, *, keep_paths: bool = False) -> str:
    """Text with every secret-looking string masked (and local paths, unless kept)."""
    mark = SECRET_MARK if keep_paths else REDACTED
    for pattern in CREDENTIAL_PATTERNS:
        text = pattern.sub(lambda m: _mask(mark, m), text)
    if not keep_paths:
        text = PATH_PATTERN.sub(REDACTED, text)
    return text
