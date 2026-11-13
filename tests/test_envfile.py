"""The .env dialect: written by onboarding, read back identically by every reader."""

import os
import stat
from pathlib import Path

import pytest

from omniscient.envfile import parse_env, parse_value, quote_value
from omniscient.onboarding import secrets
from omniscient.onboarding.secrets import read_env, write_env
from omniscient.slack.core import load_env_file

TRICKY = [
    "plain",
    'with "double" quotes',
    "back\\slash\\",
    "hash # not a comment",
    "single 'quotes'",
    "  spaces kept  ",
    "a=b=c",
    "",
    '\\"',
]


@pytest.mark.parametrize("value", TRICKY)
def test_quote_then_parse_is_identity(value: str) -> None:
    assert parse_value(quote_value(value)) == value


def test_quote_value_refuses_line_breaks() -> None:
    with pytest.raises(ValueError):
        quote_value("a\nb")


def test_parse_env_reads_legacy_unquoted_and_single_quoted_lines() -> None:
    text = "# c\nexport A=1  # note\nB='x \"y\"'\nC=\nnot a line\n"

    assert parse_env(text) == {"A": "1", "B": 'x "y"', "C": ""}


def test_write_env_round_trips_through_every_reader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange
    env = tmp_path / ".env"
    env.write_text("# keep me\nOLD=legacy\n", encoding="utf-8")
    values = {f"K{i}": v for i, v in enumerate(TRICKY) if v}
    for key in values:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("OLD", raising=False)

    # Act
    write_env(env, values)

    # Assert
    assert read_env(env) == {"OLD": "legacy", **values}
    assert load_env_file(env) == {"OLD": "legacy", **values}
    assert env.read_text(encoding="utf-8").startswith("# keep me\nOLD=legacy\n")


def test_write_env_is_private_and_leaves_no_temp_file(tmp_path: Path) -> None:
    env = tmp_path / "home" / ".env"

    write_env(env, {"A": "1"})
    write_env(env, {"B": "2"})

    assert stat.S_IMODE(env.stat().st_mode) == 0o600
    assert sorted(p.name for p in env.parent.iterdir()) == [".env"]


def test_write_env_failure_keeps_the_old_file_and_cleans_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange
    env = tmp_path / ".env"
    env.write_text('A="old"\n', encoding="utf-8")

    def boom(*_: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(secrets.os, "replace", boom)

    # Act
    with pytest.raises(OSError):
        write_env(env, {"A": "new"})

    # Assert
    assert read_env(env) == {"A": "old"}
    assert [p.name for p in tmp_path.iterdir()] == [".env"]


def test_write_env_temp_file_is_created_exclusive_and_0600(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    modes: list[int] = []
    real_replace = os.replace

    def spy(src: str, dst: Path) -> None:
        modes.append(stat.S_IMODE(os.stat(src).st_mode))
        real_replace(src, dst)

    monkeypatch.setattr(secrets.os, "replace", spy)

    write_env(tmp_path / ".env", {"A": "1"})

    assert modes == [0o600]
