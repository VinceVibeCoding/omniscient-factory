"""The home (user data) is separate from the engine and resolved at command time."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from omniscient import cli
from omniscient.config import ConfigError, load_settings
from omniscient.home import ENV_VAR, Home, find_home, packaged
from omniscient.studio import paths
from omniscient.studio.agents import definition_files

REPO = Path(__file__).resolve().parents[1]
TEMPLATE = packaged("templates") / "omniscient.toml"


def _make_home(root: Path, config: str | None = None) -> Home:
    (root / "config").mkdir(parents=True)
    (root / "config" / "omniscient.toml").write_text(
        config if config is not None else TEMPLATE.read_text("utf-8"), "utf-8"
    )
    (root / "vault" / "ideas").mkdir(parents=True)
    return Home(root)


@pytest.fixture
def lazy(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """Drop any attribute an earlier test left behind, so __getattr__ answers."""
    for module in (paths, cli):
        for name in paths.HOME_PATHS:
            if name in vars(module):  # a real attribute shadows __getattr__
                monkeypatch.delattr(module, name)
    return monkeypatch


def test_env_var_wins_over_search_up(tmp_path: Path) -> None:
    home = find_home(cwd=REPO, environ={ENV_VAR: str(tmp_path)})

    assert home.root == tmp_path.resolve()


def test_search_up_finds_the_owner_checkout_from_a_subfolder(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    _make_home(checkout)
    nested = checkout / "src" / "omniscient"
    nested.mkdir(parents=True)

    home = find_home(cwd=nested, environ={})

    assert home.root == checkout.resolve()
    assert home.exists


def test_no_home_falls_back_to_dot_omniscient(tmp_path: Path) -> None:
    home = find_home(cwd=tmp_path, environ={})

    assert home.root == Path.home() / ".omniscient"


def test_packaged_resources_ship_agents_skills_and_templates() -> None:
    agents = {p.stem for p in packaged("agents").glob("*.json")}

    assert {"builder", "news-scout", "visionary", "council", "strategist"} <= agents
    assert (packaged("skills") / "omniscient-riff" / "SKILL.md").is_file()
    assert (packaged("templates") / "routing.toml").is_file()
    for unit in (packaged("templates") / "systemd").glob("*.service"):
        text = unit.read_text("utf-8")
        assert "{HOME_DIR}" in text and "{OMNISCIENT}" in text
        assert "/home/" not in text and "uv run" not in text


def test_template_config_is_neutral_and_valid(tmp_path: Path) -> None:
    home = _make_home(tmp_path)
    settings = load_settings(home.config)

    assert "/home/" not in TEMPLATE.read_text("utf-8")
    assert settings.paths.projects_root == Path("~/projects").expanduser()
    assert settings.paths.vault == tmp_path / "vault"
    assert settings.paths.pocs == tmp_path / "pocs"
    assert not settings.publish.enabled


def test_publish_section_is_validated(tmp_path: Path) -> None:
    base = TEMPLATE.read_text("utf-8")
    ok = base + '\n[publish]\nsite_repo = "me/site"\nsite_url = "https://me.dev"\n'
    path = tmp_path / "ok.toml"
    path.write_text(ok, "utf-8")

    publish = load_settings(path).publish

    assert publish.site_url == "https://me.dev/" and publish.enabled
    path.write_text(base + '\n[publish]\nsite_url = "http://x/"\n', "utf-8")
    with pytest.raises(ConfigError, match="publish.site_url"):
        load_settings(path)


def test_studio_paths_follow_the_env_var(tmp_path: Path, lazy: Any) -> None:
    home = _make_home(tmp_path)
    lazy.setenv(ENV_VAR, str(tmp_path))

    assert paths.REPO == home.root.resolve()
    assert paths.VAULT == home.vault.resolve()
    assert cli.REPOS == home.repos.resolve()
    assert cli.SITE_CLONE == tmp_path.resolve() / ".omniscient" / "pages"


def test_routing_falls_back_to_the_packaged_default(tmp_path: Path, lazy: Any) -> None:
    _make_home(tmp_path)
    lazy.setenv(ENV_VAR, str(tmp_path))

    assert cli.ROUTING == packaged("templates") / "routing.toml"


def test_missing_home_is_a_french_error(tmp_path: Path, lazy: Any) -> None:
    lazy.setenv(ENV_VAR, str(tmp_path / "nowhere"))

    result = CliRunner().invoke(cli.app, ["idea", "list"])

    assert result.exit_code == 1
    assert "Aucun dossier Omniscient trouvé : lance `omniscient init`." in result.output
    with pytest.raises(paths.NoHomeError):
        _ = paths.VAULT


def test_idea_list_works_with_only_a_home(tmp_path: Path, lazy: Any) -> None:
    _make_home(tmp_path)
    lazy.setenv(ENV_VAR, str(tmp_path))

    result = CliRunner().invoke(cli.app, ["idea", "list"])

    assert result.exit_code == 0, result.output


def test_agent_definitions_prefer_the_home_folder(tmp_path: Path) -> None:
    assert definition_files(tmp_path) == tuple(
        sorted(packaged("agents").glob("*.json"))
    )
    (tmp_path / "agentrun").mkdir()
    shutil.copy(packaged("agents") / "debater.json", tmp_path / "agentrun")

    assert [p.name for p in definition_files(tmp_path)] == ["debater.json"]


def test_build_skips_publishing_without_a_site(tmp_path: Path, lazy: Any) -> None:
    _make_home(tmp_path)
    lazy.setenv(ENV_VAR, str(tmp_path))
    called: list[Any] = []
    lazy.setattr(
        "omniscient.build.publish.publish_web_demo", lambda *a: called.append(a)
    )
    result: Any = object()  # never inspected when publishing is disabled

    assert cli._publish(result) is result
    assert called == []
