"""Owner-aware prompt text: language rule, owner name and interests block.

Agent instructions carry placeholders the engine fills from the owner's profile
(`config/profile.toml`), so one set of packaged agents serves every user:

    {{OWNER_LANGUAGE_RULE}}  -> a LANGUAGE paragraph in French or English
    {{OWNER_LANGUAGE}}       -> "French" | "English" (e.g. "a {{OWNER_LANGUAGE}} UI")
    {{OWNER_NAME}}           -> the owner's name, or "the owner"
"""

from __future__ import annotations

import re
from typing import Protocol

LANGUAGE_RULE_PLACEHOLDER = "{{OWNER_LANGUAGE_RULE}}"
LANGUAGE_NAME_PLACEHOLDER = "{{OWNER_LANGUAGE}}"
NAME_PLACEHOLDER = "{{OWNER_NAME}}"
DEFAULT_OWNER_NAME = "the owner"
LANGUAGE_NAMES = {"fr": "French", "en": "English"}
_RULE_TEMPLATE = (
    "LANGUAGE: the owner is {adjective}. Write every human-readable value in "
    "{language} (titles, pitches, explanations, questions, replies). Keep JSON keys "
    "and the allowed enum values exactly as specified in English. Proper nouns and "
    "product names stay as they are."
)
_ADJECTIVES = {"fr": "French-speaking", "en": "English-speaking"}
_LEFTOVER = re.compile(r"\{\{OWNER_[A-Z_]+\}\}")


class OwnerProfile(Protocol):
    """The slice of `omniscient.profile.Profile` that prompts need."""

    @property
    def name(self) -> str | None: ...

    @property
    def language(self) -> str: ...

    @property
    def interests(self) -> tuple[str, ...]: ...

    @property
    def avoid(self) -> tuple[str, ...]: ...


class PromptError(ValueError):
    """A prompt could not be rendered for this profile."""


def _check_language(language: str) -> str:
    if language not in LANGUAGE_NAMES:
        raise PromptError(
            f"unsupported language {language!r}: expected one of "
            f"{', '.join(sorted(LANGUAGE_NAMES))}"
        )
    return language


def language_rule(language: str) -> str:
    """The LANGUAGE paragraph agents follow when writing human-readable values."""
    lang = _check_language(language)
    return _RULE_TEMPLATE.format(
        adjective=_ADJECTIVES[lang], language=LANGUAGE_NAMES[lang]
    )


def render_instructions(text: str, profile: OwnerProfile) -> str:
    """Fill the owner placeholders in an agent's instructions; fail on unknown ones."""
    lang = _check_language(profile.language)
    name = (profile.name or "").strip() or DEFAULT_OWNER_NAME
    rendered = (
        text.replace(LANGUAGE_RULE_PLACEHOLDER, language_rule(lang))
        .replace(LANGUAGE_NAME_PLACEHOLDER, LANGUAGE_NAMES[lang])
        .replace(NAME_PLACEHOLDER, name)
    )
    leftover = sorted(set(_LEFTOVER.findall(rendered)))
    if leftover:
        raise PromptError(f"unknown owner placeholders: {', '.join(leftover)}")
    return rendered


def interests_block(profile: OwnerProfile | None) -> str:
    """The visionary's owner-interests section; empty when there is nothing to say."""
    if profile is None or not (profile.interests or profile.avoid):
        return ""
    lines = ["## The owner's interests"]
    if profile.interests:
        lines.append("Topics the owner cares about: " + ", ".join(profile.interests))
        lines.append(
            "Within the track quotas, at least ONE idea must be tied to one of these "
            "interests. It may be a brand-new product with no existing repo "
            "(kind new-build, `projects` empty) - do not force it onto a project."
        )
    if profile.avoid:
        lines.append("Never propose ideas about: " + ", ".join(profile.avoid) + ".")
    lines.append(language_rule(profile.language))
    return "\n".join(lines)
