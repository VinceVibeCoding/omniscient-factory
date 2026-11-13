import stat
import sys
import types
from pathlib import Path
from typing import Any

import pytest

import omniscient
from omniscient.home import Home
from omniscient.onboarding import (
    OnboardingError,
    add_project,
    list_projects,
    run_init,
    write_registry_entry,
)
from omniscient.onboarding.prereqs import Check
from omniscient.onboarding.secrets import read_env, write_env
from omniscient.profile import load_profile

OK = (Check("uv", True, "/bin/uv", True), Check("git", True, "/bin/git", True))
TEMPLATE = '[paths]\nprojects_root = "~/projects"\nvault = "vault"\n'


class Script:
    """ask(question, default): first rule whose key is in the question wins; else default."""

    def __init__(self, rules: dict[str, str]) -> None:
        self.rules = rules
        self.asked: list[str] = []
        self.said: list[str] = []

    def ask(self, question: str, default: str) -> str:
        self.asked.append(question)
        return next((a for k, a in self.rules.items() if k in question), default)

    def say(self, text: str) -> None:
        self.said.append(text)


def _resources(tmp: Path) -> tuple[Path, Path]:
    templates, skills = tmp / "res" / "templates", tmp / "res" / "skills"
    templates.mkdir(parents=True)
    (templates / "omniscient.toml").write_text(TEMPLATE, encoding="utf-8")
    (skills / "omniscient-riff").mkdir(parents=True)
    (skills / "omniscient-riff" / "SKILL.md").write_text("packaged", encoding="utf-8")
    return templates, skills


def _projects(tmp: Path) -> Path:
    root = tmp / "projects"
    for name in ("alpha", "beta"):
        (root / name / ".git").mkdir(parents=True)
        (root / name / "README.md").write_text(
            f"# {name}\n\nThe {name} app.\n", encoding="utf-8"
        )
    return root


def _run(
    home: Home,
    script: Script,
    tmp: Path,
    secrets: list[str],
    environ: dict[str, str] | None = None,
    fetch: Any = None,
    is_tty: bool = True,
) -> None:
    templates, skills = (
        _resources(tmp)
        if not (tmp / "res").exists()
        else (tmp / "res/templates", tmp / "res/skills")
    )
    answers = iter(secrets)
    run_init(
        home,
        script.ask,
        script.say,
        secret=lambda _: next(answers, ""),
        environ=environ or {},
        prereqs=lambda: OK,
        fetch=fetch or (lambda url, headers: (404, None)),
        git_run=lambda cmd: None,
        templates=templates,
        skills=skills,
        is_tty=lambda: is_tty,
    )


@pytest.fixture(autouse=True)
def _no_theme(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "omniscient.theme", None)  # import -> ImportError


def test_first_run_creates_home_profile_registry_and_env(tmp_path: Path) -> None:
    # Arrange
    home = Home(tmp_path / "home")
    root = _projects(tmp_path)
    script = Script(
        {
            "contient tes projets": str(root),
            "Langue": "en",
            "prénom": "Alex",
            "intérêt": "LLM locaux, fintech",
            "éviter": "crypto",
            "Ajouter alpha": "o",
            "Ajouter beta": "n",
        }
    )

    # Act
    _run(home, script, tmp_path, secrets=["sk-ant-SECRET", "", "ghp_SECRET"])

    # Assert
    assert home.exists and f'projects_root = "{root}"' in home.config.read_text()
    assert all(
        (home.vault / d).is_dir() for d in ("news", "ideas", "tech", "maps", "ops")
    )
    assert (home.skills / "omniscient-riff" / "SKILL.md").is_file()
    profile = load_profile(home.profile)
    assert (profile.language, profile.name, profile.interests, profile.avoid) == (
        "en",
        "Alex",
        ("LLM locaux", "fintech"),
        ("crypto",),
    )
    assert [p[0] for p in list_projects(home)] == ["alpha"]
    assert read_env(home.env_file) == {
        "ANTHROPIC_API_KEY": "sk-ant-SECRET",
        "GITHUB_TOKEN": "ghp_SECRET",
    }
    assert stat.S_IMODE(home.env_file.stat().st_mode) == 0o600
    assert not any("SECRET" in line for line in script.said + script.asked)


def test_rerun_keeps_data_and_existing_secrets(tmp_path: Path) -> None:
    # Arrange
    home = Home(tmp_path / "home")
    root = _projects(tmp_path)
    first = Script(
        {"contient tes projets": str(root), "intérêt": "fintech", "Ajouter": "tout"}
    )
    _run(home, first, tmp_path, secrets=["sk-1"])
    (home.skills / "omniscient-riff" / "SKILL.md").write_text(
        "edited", encoding="utf-8"
    )
    (home.repos / "alpha.md").write_text(
        (home.repos / "alpha.md").read_text() + "\nmy notes\n"
    )
    write_env(home.env_file, {"OTHER": "keep"})

    # Act
    second = Script({"remplacer": "n"})
    _run(home, second, tmp_path, secrets=[])

    # Assert
    assert (home.skills / "omniscient-riff" / "SKILL.md").read_text() == "edited"
    assert "my notes" in (home.repos / "alpha.md").read_text()
    assert [p[0] for p in list_projects(home)] == ["alpha", "beta"]
    assert load_profile(home.profile).interests == ("fintech",)
    assert read_env(home.env_file) == {"ANTHROPIC_API_KEY": "sk-1", "OTHER": "keep"}
    assert not any("contient tes projets" in q for q in second.asked)


def test_missing_required_prereq_stops_before_writing(tmp_path: Path) -> None:
    home = Home(tmp_path / "home")
    script = Script({})
    with pytest.raises(OnboardingError, match="uv"):
        run_init(
            home,
            script.ask,
            script.say,
            prereqs=lambda: (Check("uv", False, "absent", True),),
        )
    assert not home.root.exists()


def test_theme_step_uses_theme_module_when_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chosen: list[tuple[Home, str]] = []
    fake = types.ModuleType("omniscient.theme")
    fake.list_themes = lambda: ["sombre", "clair"]  # type: ignore[attr-defined]
    fake.set_theme = lambda home, name: chosen.append((home, name))  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "omniscient.theme", fake)
    # `from omniscient import theme` reads the package attribute once it's imported
    monkeypatch.setattr(omniscient, "theme", fake, raising=False)
    home = Home(tmp_path / "home")

    _run(
        home,
        Script({"Thème": "clair", "contient tes projets": str(tmp_path / "none")}),
        tmp_path,
        secrets=[],
    )

    assert chosen == [(home, "clair")]


def test_slack_secrets_only_when_asked(tmp_path: Path) -> None:
    home = Home(tmp_path / "home")
    script = Script({"Slack": "o", "contient tes projets": str(tmp_path / "none")})
    _run(
        home,
        script,
        tmp_path,
        secrets=["", "", "xoxb-1", "xapp-1", "C0TEST", "U0TEST", ""],
    )
    assert read_env(home.env_file) == {
        "SLACK_BOT_TOKEN": "xoxb-1",
        "SLACK_APP_TOKEN": "xapp-1",
        "SLACK_CHANNEL_ID": "C0TEST",
        "SLACK_OWNER_ID": "U0TEST",
    }
    assert any("studio seulement" in s or "studio marche" in s for s in script.said)


def test_write_registry_entry_validates_name_and_overwrite(tmp_path: Path) -> None:
    home = Home(tmp_path)
    assert write_registry_entry(home, "ok-1", "a") == tmp_path / "repos" / "ok-1.md"
    assert write_registry_entry(home, "ok-1", "b") is None
    assert write_registry_entry(home, "ok-1", "c", overwrite=True) is not None
    assert (tmp_path / "repos" / "ok-1.md").read_text() == "c"
    with pytest.raises(OnboardingError):
        write_registry_entry(home, "../evil", "x")


def _github_fetch(url: str, headers: dict[str, str]) -> tuple[int, object]:
    api = "https://api.github.com"
    if url.startswith(f"{api}/users/octocat/repos"):
        return 200, [
            {"name": "one", "full_name": "octocat/one", "description": "1"},
            {
                "name": "two",
                "full_name": "octocat/two",
                "description": None,
                "fork": True,
            },
            {"name": "three", "full_name": "octocat/three", "description": "3"},
        ]
    if url.endswith("/languages"):
        return 200, {"Go": 1}
    if url.endswith("/readme"):
        return 404, None
    return 200, {"description": "From GitHub.", "topics": []}


def test_add_project_from_local_path_url_and_user(tmp_path: Path) -> None:
    # Arrange
    home = Home(tmp_path / "home")
    root = _projects(tmp_path)
    script = Script({"Numéros": "2"})

    # Act
    local = add_project(
        home,
        str(root / "alpha"),
        ask=script.ask,
        say=script.say,
        git_run=lambda c: None,
    )
    url = add_project(
        home,
        "https://github.com/acme/tool.git",
        ask=script.ask,
        say=script.say,
        fetch=_github_fetch,
    )
    user = add_project(
        home, "octocat", ask=script.ask, say=script.say, fetch=_github_fetch
    )
    again = add_project(
        home, "acme/tool", ask=script.ask, say=script.say, fetch=_github_fetch
    )

    # Assert
    assert [p.name for p in local + url + user] == ["alpha.md", "tool.md", "three.md"]
    assert again == ()
    assert [n for n, _ in list_projects(home)] == ["alpha", "three", "tool"]
    assert not any("two" in s for s in script.said)  # forks hidden


def test_add_project_errors(tmp_path: Path) -> None:
    home = Home(tmp_path)
    script = Script({})
    with pytest.raises(OnboardingError, match="source inconnue"):
        add_project(home, "not a/valid source!", ask=script.ask, say=script.say)
    with pytest.raises(OnboardingError, match="introuvable"):
        add_project(
            home,
            "ghost/repo",
            ask=script.ask,
            say=script.say,
            fetch=lambda u, h: (404, None),
        )
    with pytest.raises(OnboardingError):
        add_project(home, "  ", ask=script.ask, say=script.say)


def test_theme_module_without_api_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bare = types.ModuleType("x")
    monkeypatch.setitem(sys.modules, "omniscient.theme", bare)
    monkeypatch.setattr(omniscient, "theme", bare, raising=False)
    home = Home(tmp_path / "home")
    script = Script({"contient tes projets": str(tmp_path / "none")})
    _run(home, script, tmp_path, secrets=[])
    assert not any("Thème" in q for q in script.asked)


def test_packaged_config_template_is_used_by_default(tmp_path: Path) -> None:
    home = Home(tmp_path / "home")
    script = Script({"contient tes projets": "~/code"})
    run_init(
        home,
        script.ask,
        script.say,
        secret=lambda _: "",
        environ={},
        prereqs=lambda: OK,
        git_run=lambda cmd: None,
    )
    assert 'projects_root = "~/code"' in home.config.read_text()


def test_github_token_is_asked_before_listing_github_repos(tmp_path: Path) -> None:
    # Arrange: the GitHub listing must already carry the token typed during init.
    home = Home(tmp_path / "home")
    seen: list[dict[str, str]] = []

    def fetch(url: str, headers: dict[str, str]) -> tuple[int, object]:
        seen.append(headers)
        return _github_fetch(url, headers)

    script = Script(
        {
            "contient tes projets": str(tmp_path / "none"),
            "utilisateur GitHub": "octocat",
            "Parcourir": "o",
            "Numéros": "",
        }
    )

    # Act: secrets in prompt order: GitHub token, then Anthropic, then AgentRun.
    _run(
        home,
        script,
        tmp_path,
        secrets=["ghp_TYPED", "sk-ant-1", "agentrun-key"],
        fetch=fetch,
    )

    # Assert
    assert seen and all("ghp_TYPED" in str(h) for h in seen)
    assert read_env(home.env_file) == {
        "GITHUB_TOKEN": "ghp_TYPED",
        "ANTHROPIC_API_KEY": "sk-ant-1",
        "AGENTRUN_API_KEY": "agentrun-key",
    }
    assert sum("GITHUB_TOKEN" in q for q in script.asked) == 0  # no second ask


def test_agentrun_key_prompt_explains_where_the_key_comes_from(tmp_path: Path) -> None:
    home = Home(tmp_path / "home")
    prompts: list[str] = []
    templates, skills = _resources(tmp_path)

    run_init(
        home,
        Script({"contient tes projets": str(tmp_path / "none")}).ask,
        lambda _: None,
        secret=lambda prompt: prompts.append(prompt) or "",
        environ={},
        prereqs=lambda: OK,
        git_run=lambda cmd: None,
        templates=templates,
        skills=skills,
        is_tty=lambda: True,
    )

    agentrun = next(p for p in prompts if "AGENTRUN_API_KEY" in p)
    assert ".agentrun/.token" in agentrun


@pytest.mark.parametrize(("tty", "warned"), [(False, True), (True, False)])
def test_non_tty_stdin_gets_a_french_warning_before_secrets(
    tmp_path: Path, tty: bool, warned: bool
) -> None:
    home = Home(tmp_path / "home")
    script = Script({"contient tes projets": str(tmp_path / "none")})

    _run(home, script, tmp_path, secrets=[], is_tty=tty)

    warnings = [s for s in script.said if "pas un terminal" in s]
    assert len(warnings) == (1 if warned else 0)
    if warned:
        assert script.said.index(warnings[0]) > script.said.index(
            "5/6 Secrets (jamais affichés)"
        )


def test_bare_name_prefers_a_local_project_folder_over_a_github_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange: ./alpha is a project (.git); ./octocat is just a folder.
    home = Home(tmp_path / "home")
    work = tmp_path / "work"
    (work / "alpha" / ".git").mkdir(parents=True)
    (work / "octocat").mkdir()
    monkeypatch.chdir(work)
    script = Script({"Numéros": "1"})

    # Act
    local = add_project(
        home, "alpha", ask=script.ask, say=script.say, git_run=lambda c: None
    )
    github = add_project(
        home, "octocat", ask=script.ask, say=script.say, fetch=_github_fetch
    )
    forced = add_project(
        home, "./octocat", ask=script.ask, say=script.say, git_run=lambda c: None
    )

    # Assert
    assert [p.name for p in local] == ["alpha.md"]
    assert [p.name for p in github] == ["one.md"]  # listed from GitHub, not local
    assert [p.name for p in forced] == ["octocat.md"]
