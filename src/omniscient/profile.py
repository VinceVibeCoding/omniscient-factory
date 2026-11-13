"""The owner's profile: <home>/config/profile.toml (interests, language, GitHub user).

[profile]
name = "Alex"                 # optional
language = "fr"               # "fr" | "en": language of agent-written content
interests = ["LLM locaux", "fintech"]
avoid = ["crypto"]            # optional
[github]
user = "octocat"              # optional
"""

from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

Language = Literal["fr", "en"]
LANGUAGES: tuple[Language, ...] = ("fr", "en")
DEFAULT_LANGUAGE: Language = "fr"


class ProfileError(Exception):
    """Raised when profile.toml cannot be read or holds invalid values."""


@dataclass(frozen=True)
class Profile:
    name: str | None = None
    language: Language = DEFAULT_LANGUAGE
    interests: tuple[str, ...] = ()
    avoid: tuple[str, ...] = ()
    github_user: str | None = None


def _clean_list(value: object, field: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ProfileError(f"profile.toml : `{field}` doit être une liste de textes")
    seen: list[str] = []
    for item in (v.strip() for v in value):
        if item and item not in seen:
            seen.append(item)
    return tuple(seen)


def _optional_text(value: object, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ProfileError(f"profile.toml : `{field}` doit être un texte")
    return value.strip() or None


def _language(value: object) -> Language:
    if value is None:
        return DEFAULT_LANGUAGE
    if value not in LANGUAGES:
        raise ProfileError(
            f"profile.toml : `language` doit être « fr » ou « en » (reçu {value!r})"
        )
    return cast(Language, value)


def load_profile(path: Path) -> Profile:
    """Read profile.toml; a missing file gives the defaults (French, no interests)."""
    if not path.is_file():
        return Profile()
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ProfileError(f"profile.toml illisible : {exc}") from exc
    section = data.get("profile", {})
    github = data.get("github", {})
    if not isinstance(section, dict) or not isinstance(github, dict):
        raise ProfileError(
            "profile.toml : [profile] et [github] doivent être des tables"
        )
    return Profile(
        name=_optional_text(section.get("name"), "name"),
        language=_language(section.get("language")),
        interests=_clean_list(section.get("interests"), "interests"),
        avoid=_clean_list(section.get("avoid"), "avoid"),
        github_user=_optional_text(github.get("user"), "user"),
    )


def _toml_str(value: str) -> str:
    # A JSON string (non-ASCII kept) is a valid TOML basic string.
    return json.dumps(value, ensure_ascii=False)


def _toml_list(values: tuple[str, ...]) -> str:
    return "[" + ", ".join(_toml_str(v) for v in values) + "]"


def render_profile(profile: Profile) -> str:
    lines = ["[profile]"]
    if profile.name:
        lines.append(f"name = {_toml_str(profile.name)}")
    lines += [
        f"language = {_toml_str(_language(profile.language))}",
        f"interests = {_toml_list(profile.interests)}",
        f"avoid = {_toml_list(profile.avoid)}",
    ]
    if profile.github_user:
        lines += ["", "[github]", f"user = {_toml_str(profile.github_user)}"]
    return "\n".join(lines) + "\n"


def save_profile(path: Path, profile: Profile) -> None:
    """Write profile.toml (creates config/ if needed), keeping a saved [profile] theme."""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = render_profile(profile)
    theme = _saved_theme(path)
    if theme:  # owned by omniscient.theme; Profile does not model it
        marker = "\ninterests = "
        text = text.replace(marker, f"\ntheme = {_toml_str(theme)}{marker}", 1)
    path.write_text(text, encoding="utf-8")


def _saved_theme(path: Path) -> str | None:
    if not path.is_file():
        return None
    try:
        theme = (
            tomllib.loads(path.read_text(encoding="utf-8"))
            .get("profile", {})
            .get("theme")
        )
    except (OSError, tomllib.TOMLDecodeError):
        return None
    return theme if isinstance(theme, str) and theme else None


def parse_list(text: str) -> tuple[str, ...]:
    """'a, b ,, a' -> ('a', 'b'): the comma list typed at a prompt."""
    return _clean_list(text.split(","), "liste")
