"""Secret redaction: every credential family, approval-preview mode, idempotence."""

from __future__ import annotations

import pytest

from omniscient.redact import REDACTED, SECRET_MARK, redact_secrets

SECRETS = [
    "sk-proj-" + "a1B2" * 10,
    "sk-" + "Z9y8" * 8,
    "sk_live_" + "4eC39HqLyjWDarjtT1zdp7dc",
    "rk_live_" + "4eC39HqLyjWDarjtT1zdp7dc",
    "AIza" + "SyA-" * 8 + "abc",
    "npm_" + "a" * 36,
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
    "ghp_" + "x" * 36,
    "xoxb-1234567890-abcdef",
    "AKIA" + "ABCDEFGHIJKLMNOP",
]


@pytest.mark.parametrize("secret", SECRETS)
def test_each_credential_family_is_masked(secret: str) -> None:
    out = redact_secrets(f"avant {secret} après")
    assert secret not in out and REDACTED in out
    assert out.startswith("avant ") and out.endswith(" après")


@pytest.mark.parametrize(
    ("text", "kept", "gone"),
    [
        (
            "curl -H 'Authorization: Bearer abcdef0123456789'",
            "Authorization: ",
            "abcdef0123456789",
        ),
        ("Authorization: Basic dXNlcjpwYXNz", "Authorization: ", "dXNlcjpwYXNz"),
        ("token is Bearer abcdefghijklmnop", "Bearer ", "abcdefghijklmnop"),
        ("git clone https://user:hunter2@example.com/r.git", "https://", "hunter2"),
        ("https://ghtoken123456@github.com/x", "@github.com/x", "ghtoken123456"),
        ("export GITHUB_TOKEN=abc123def", "GITHUB_TOKEN=", "abc123def"),
        ("API_KEY = 'quoted value'", "API_KEY = ", "quoted value"),
        ('{"aws_secret": "s3cr3t"}', '"aws_secret": ', "s3cr3t"),
        ("DB_PASSWORD=pw123 ./run", "./run", "pw123"),
        ("password: letmein", "password: ", "letmein"),
    ],
)
def test_headers_urls_and_assignments(text: str, kept: str, gone: str) -> None:
    out = redact_secrets(text)
    assert gone not in out and kept in out and REDACTED in out


def test_paths_are_masked_by_default_and_kept_for_previews() -> None:
    text = "cat /home/alice/notes.txt with GITHUB_TOKEN=abc"
    assert (
        redact_secrets(text) == f"cat {REDACTED}notes.txt with GITHUB_TOKEN={REDACTED}"
    )
    preview = redact_secrets(text, keep_paths=True)
    assert preview == f"cat /home/alice/notes.txt with GITHUB_TOKEN={SECRET_MARK}"


@pytest.mark.parametrize("keep_paths", [False, True])
def test_redaction_is_idempotent(keep_paths: bool) -> None:
    text = (
        "Authorization: Bearer abcdefghijklmnop\nAPI_KEY=xyz https://u:p@h.example/ "
        + " ".join(SECRETS)
    )
    once = redact_secrets(text, keep_paths=keep_paths)
    assert redact_secrets(once, keep_paths=keep_paths) == once


def test_plain_text_is_untouched() -> None:
    text = "ls -la && git status --short; echo key=value"
    assert redact_secrets(text) == text
