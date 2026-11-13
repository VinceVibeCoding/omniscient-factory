from dataclasses import dataclass

import pytest

from omniscient.prompting import (
    PromptError,
    interests_block,
    language_rule,
    render_instructions,
)


@dataclass(frozen=True)
class _Profile:
    name: str | None = None
    language: str = "fr"
    interests: tuple[str, ...] = ()
    avoid: tuple[str, ...] = ()
    github_user: str | None = None


INSTRUCTIONS = "You help {{OWNER_NAME}}.\n\n{{OWNER_LANGUAGE_RULE}}\nBuild a {{OWNER_LANGUAGE}} UI."


def test_render_instructions_in_french_with_name() -> None:
    text = render_instructions(INSTRUCTIONS, _Profile(name="Alex", language="fr"))

    assert text.startswith("You help Alex.")
    assert "the owner is French-speaking" in text
    assert "Write every human-readable value in French" in text
    assert "Build a French UI." in text
    assert "{{" not in text


def test_render_instructions_in_english_without_name() -> None:
    text = render_instructions(INSTRUCTIONS, _Profile(language="en"))

    assert text.startswith("You help the owner.")
    assert "the owner is English-speaking" in text
    assert "Write every human-readable value in English" in text
    assert "Keep JSON keys" in text


def test_render_instructions_leaves_text_without_placeholders_unchanged() -> None:
    assert render_instructions("plain text", _Profile()) == "plain text"


def test_render_instructions_rejects_unknown_owner_placeholder() -> None:
    with pytest.raises(PromptError, match="OWNER_AGE"):
        render_instructions("{{OWNER_AGE}}", _Profile())


def test_language_rule_rejects_unsupported_language() -> None:
    with pytest.raises(PromptError, match="unsupported language"):
        language_rule("de")


def test_interests_block_is_empty_without_profile_or_interests() -> None:
    assert interests_block(None) == ""
    assert interests_block(_Profile()) == ""


def test_interests_block_lists_interests_avoid_and_language() -> None:
    block = interests_block(
        _Profile(language="en", interests=("fintech", "running"), avoid=("crypto",))
    )

    assert "fintech, running" in block
    assert "at least ONE idea" in block
    assert "Never propose ideas about: crypto." in block
    assert "in English" in block


def test_interests_block_with_only_avoid_does_not_demand_an_interest_idea() -> None:
    block = interests_block(_Profile(avoid=("crypto",)))

    assert "crypto" in block and "at least ONE idea" not in block
