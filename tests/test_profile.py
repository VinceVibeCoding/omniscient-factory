from pathlib import Path

import pytest

from omniscient.profile import (
    Profile,
    ProfileError,
    load_profile,
    parse_list,
    save_profile,
)


def test_missing_file_gives_french_defaults(tmp_path: Path) -> None:
    profile = load_profile(tmp_path / "profile.toml")

    assert profile == Profile(None, "fr", (), (), None)


def test_save_then_load_round_trips_unicode_and_quotes(tmp_path: Path) -> None:
    # Arrange
    path = tmp_path / "config" / "profile.toml"
    original = Profile(
        name='Alex "A"',
        language="en",
        interests=("LLM locaux", "course à pied"),
        avoid=("crypto",),
        github_user="octocat",
    )

    # Act
    save_profile(path, original)

    # Assert
    assert load_profile(path) == original


def test_load_reads_contract_example(tmp_path: Path) -> None:
    path = tmp_path / "profile.toml"
    path.write_text(
        '[profile]\nlanguage = "fr"\ninterests = ["fintech", " ", "fintech"]\n'
        '[github]\nuser = "octocat"\n',
        encoding="utf-8",
    )

    profile = load_profile(path)

    assert profile.interests == ("fintech",)
    assert profile.github_user == "octocat"
    assert profile.name is None


@pytest.mark.parametrize(
    "body",
    [
        '[profile]\nlanguage = "de"\n',
        "[profile]\ninterests = 3\n",
        "[profile]\nname = 4\n",
        "profile = 1\n",
        "not toml = = =\n",
    ],
)
def test_invalid_profile_raises(tmp_path: Path, body: str) -> None:
    path = tmp_path / "profile.toml"
    path.write_text(body, encoding="utf-8")

    with pytest.raises(ProfileError):
        load_profile(path)


def test_parse_list_trims_and_dedupes() -> None:
    assert parse_list(" a, b ,, a ") == ("a", "b")
    assert parse_list("") == ()


def test_save_profile_keeps_the_theme_chosen_elsewhere(tmp_path: Path) -> None:
    from omniscient.profile import Profile, load_profile, save_profile

    path = tmp_path / "config" / "profile.toml"
    path.parent.mkdir()
    path.write_text('[profile]\nlanguage = "fr"\ntheme = "nord"\n', encoding="utf-8")

    save_profile(path, Profile(None, "en", ("rust",), (), None))

    text = path.read_text(encoding="utf-8")
    assert 'theme = "nord"' in text and load_profile(path).language == "en"
