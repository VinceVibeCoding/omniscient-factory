"""The skills library: scanning tmp homes/repos, install/remove, and the screen via pilot."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest
from textual.app import App
from textual.widgets import Markdown, OptionList, Static

from omniscient.studio.ideas import StudioError
from omniscient.studio.screens import MODAL_CSS
from omniscient.studio.skills import (
    SkillInfo,
    install_skill,
    read_body,
    refresh_installed,
    remove_skill,
    scan_report,
    scan_skills,
)
from omniscient.studio.skills_screen import NO_HERDR, SkillsScreen

SIZE = (140, 40)


def _skill(
    folder: Path, name: str | None, desc: str = "Fait un truc.", body: str = "Corps."
) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    meta = (f"name: {name}\n" if name else "") + f"description: {desc}\n"
    path = folder / "SKILL.md"
    path.write_text(f"---\n{meta}---\n\n{body}\n", encoding="utf-8")
    return path


def _plugin(
    home: Path, market: str, plugin: str, version: str, manifest: dict | None = None
) -> Path:
    root = home / ".claude" / "plugins" / "cache" / market / plugin / version
    root.mkdir(parents=True)
    if manifest is not None:
        (root / ".claude-plugin").mkdir()
        (root / ".claude-plugin" / "plugin.json").write_text(json.dumps(manifest))
    return root


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    _skill(r / ".claude" / "skills" / "omniscient-riff", "omniscient-riff", "Riff.")
    return r


@pytest.fixture
def home(tmp_path: Path) -> Path:
    h = tmp_path / "home"
    skills = h / ".claude" / "skills"
    _skill(skills / "perso", "perso", "Mon skill.", body="# Perso\n\nTexte perso.")
    _skill(skills / "learned" / "appris", "appris")
    _skill(skills / "synced" / "acct" / "skill-creator", "skill-creator", "Crée.")
    _skill(skills / "synced" / "acct" / ".staging" / "x", "x")  # hidden: ignored
    for bad, text in (("broken", "pas de frontmatter\n"), ("liste", "---\n- a\n---\n")):
        (skills / bad).mkdir()
        (skills / bad / "SKILL.md").write_text(text)
    old = _plugin(h, "ecc", "ecc", "1.0.0", {"name": "ecc", "skills": ["./skills/"]})
    _skill(old / "skills" / "vieux", "vieux")
    new = _plugin(h, "ecc", "ecc", "2.0.0", {"name": "ecc", "skills": ["./skills/"]})
    _skill(new / "skills" / "tdd-workflow", "tdd-workflow", "TDD.")
    _skill(new / "skills" / "ecc-tool", "ecc:tool", "Avec deux-points.")
    _skill(new / "docs" / "fr" / "skills" / "doublon", "doublon")  # not declared
    os.utime(old, (time.time() - 1000, time.time() - 1000))
    declared = ["./skills/engineering/tdd", "../../evil"]
    manifest = {"name": "mattpocock-skills", "skills": declared}
    mp = _plugin(h, "mattpocock", "mattpocock-skills", "1.3.2", manifest)
    _skill(mp / "skills" / "engineering" / "tdd", "tdd")
    _skill(mp / "skills" / "in-progress" / "wip", "wip")  # not declared
    nodecl = _plugin(h, "official", "vercel", "0.5.0")
    _skill(nodecl / "skills" / "deploy", "deploy")
    return h


def _names(skills: tuple[SkillInfo, ...]) -> list[tuple[str, str]]:
    return [(s.source, s.name) for s in skills]


# ---------- scan ----------


def test_scan_finds_every_source_sorted_and_reports_bad_files(
    repo: Path, home: Path
) -> None:
    report = scan_report(repo, home)
    assert _names(report.skills) == [
        ("projet", "omniscient-riff"),
        ("personnel", "appris"),
        ("personnel", "perso"),
        ("plugin:anthropic-skills", "skill-creator"),
        ("plugin:ecc", "ecc:tool"),
        ("plugin:ecc", "tdd-workflow"),
        ("plugin:mattpocock-skills", "tdd"),
        ("plugin:vercel", "deploy"),
    ]
    assert [p.split("/")[-2] for p in report.problems] == ["broken", "liste"]
    assert scan_skills(repo, home) == report.skills


def test_scan_survives_missing_dirs(tmp_path: Path) -> None:
    assert scan_report(tmp_path / "nope", tmp_path / "nohome").skills == ()


def test_installed_plugin_version_wins_over_newer_cache(repo: Path, home: Path) -> None:
    older = _plugin(
        home, "official", "mattpocock-skills", "9.9.9", {"name": "mattpocock-skills"}
    )
    _skill(older / "skills" / "autre", "autre")  # newest mtime, but not installed
    installed = (
        home
        / ".claude"
        / "plugins"
        / "cache"
        / "mattpocock"
        / "mattpocock-skills"
        / "1.3.2"
    )
    data = {
        "version": 2,
        "plugins": {"mattpocock-skills@mattpocock": [{"installPath": str(installed)}]},
    }
    (home / ".claude" / "plugins" / "installed_plugins.json").write_text(
        json.dumps(data)
    )
    names = [
        s.name
        for s in scan_skills(repo, home)
        if s.source == "plugin:mattpocock-skills"
    ]
    assert names == ["tdd"]


def test_name_falls_back_to_directory_and_description_is_flattened(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "r"
    path = _skill(
        repo / ".claude" / "skills" / "sans-nom", None, desc="'ligne un\n  ligne deux'"
    )
    (skill,) = scan_skills(repo, tmp_path / "h")
    assert skill.name == "sans-nom" and skill.description == "ligne un ligne deux"
    assert skill.path == path and skill.installed_in_project


def test_read_body_drops_frontmatter(home: Path) -> None:
    body = read_body(home / ".claude" / "skills" / "perso" / "SKILL.md")
    assert body == "# Perso\n\nTexte perso."


def test_scan_is_fast_for_hundreds_of_skills(tmp_path: Path) -> None:
    home = tmp_path / "h"
    root = _plugin(home, "m", "big", "1", {"name": "big"})
    for i in range(400):
        _skill(root / "skills" / f"s{i:03d}", f"s{i:03d}", "x" * 300, body="y\n" * 2000)
    start = time.perf_counter()
    assert len(scan_skills(tmp_path / "r", home)) == 400
    assert time.perf_counter() - start < 1.0


# ---------- install / remove ----------


def _by_name(repo: Path, home: Path, name: str) -> SkillInfo:
    return next(s for s in scan_skills(repo, home) if s.name == name)


def test_install_copies_directory_and_flags_installed(
    repo: Path, home: Path, tmp_path: Path
) -> None:
    skill = _by_name(repo, home, "perso")
    src = skill.path.parent
    (src / "scripts").mkdir()
    (src / "scripts" / "run.py").write_text("print(1)\n")
    for junk in (".git", "node_modules", "__pycache__"):
        (src / junk).mkdir()
        (src / junk / "f").write_text("x")
    secret = tmp_path / "secret.txt"
    secret.write_text("secret")
    (src / "leak.txt").symlink_to(secret)
    (src / "inside.md").symlink_to(src / "SKILL.md")
    target = install_skill(skill, repo)
    assert target == repo / ".claude" / "skills" / "perso"
    assert (target / "scripts" / "run.py").read_text() == "print(1)\n"
    assert not any(
        (target / j).exists()
        for j in (".git", "node_modules", "__pycache__", "leak.txt")
    )
    assert (target / "inside.md").is_file() and not (target / "inside.md").is_symlink()
    assert not [
        p for p in target.parent.iterdir() if p.name.startswith(".")
    ]  # no staging left
    refreshed = refresh_installed(scan_skills(repo, home), repo)
    flags = {(s.source, s.name): s.installed_in_project for s in refreshed}
    assert flags[("personnel", "perso")] and flags[("projet", "perso")]
    assert not flags[("plugin:ecc", "tdd-workflow")]


def test_install_maps_colon_and_refuses_existing_or_bad_names(
    repo: Path, home: Path
) -> None:
    target = install_skill(_by_name(repo, home, "ecc:tool"), repo)
    assert target.name == "ecc-tool"
    with pytest.raises(StudioError, match="existe déjà"):
        install_skill(_by_name(repo, home, "ecc:tool"), repo)
    bad = SkillInfo(
        "Mon Skill",
        "",
        "personnel",
        home / ".claude" / "skills" / "perso" / "SKILL.md",
        False,
    )
    with pytest.raises(StudioError, match="invalide"):
        install_skill(bad, repo)
    with pytest.raises(StudioError, match="invalide"):
        install_skill(SkillInfo("../x", "", "personnel", bad.path, False), repo)


def test_remove_only_touches_project_skills(repo: Path, home: Path) -> None:
    remove_skill("omniscient-riff", repo)
    assert not (repo / ".claude" / "skills" / "omniscient-riff").exists()
    with pytest.raises(StudioError, match="aucun skill"):
        remove_skill("perso", repo)
    with pytest.raises(StudioError, match="invalide"):
        remove_skill("../../home", repo)
    assert (home / ".claude" / "skills" / "perso" / "SKILL.md").is_file()


# ---------- screen ----------


class FakeAtelier:
    def __init__(self, up: bool = True) -> None:
        self.up = up
        self.calls: list[str] = []

    def available(self) -> bool:
        return self.up

    def open_skill_creator(self, text: str) -> str:
        self.calls.append(text)
        return "w1"


class Host(App[None]):
    CSS = MODAL_CSS

    def __init__(self, repo: Path, home: Path, atelier: FakeAtelier | None) -> None:
        super().__init__()
        self.screen_args = (repo, atelier, home)

    def on_mount(self) -> None:
        repo, atelier, home = self.screen_args
        self.push_screen(SkillsScreen(repo, atelier, home=home))


async def _settle(app: App[None], pilot) -> None:
    await app.workers.wait_for_complete()
    await pilot.pause()
    await app.workers.wait_for_complete()
    await pilot.pause()


def _screen(app: App[None]) -> SkillsScreen:
    screen = app.screen_stack[1]
    assert isinstance(screen, SkillsScreen)
    return screen


def _messages(app: App[None]) -> list[str]:
    return [n.message for n in app._notifications]


@pytest.mark.asyncio
async def test_screen_lists_groups_counter_and_preview(repo: Path, home: Path) -> None:
    app = Host(repo, home, FakeAtelier())
    async with app.run_test(size=SIZE) as pilot:
        await _settle(app, pilot)
        screen = _screen(app)
        assert (
            str(screen.query_one("#skills-count", Static).render())
            == "8 skills · 1 dans le projet"
        )
        listing = screen.query_one(OptionList)
        assert listing.option_count == 8 + 6  # 6 source headers
        assert str(listing.get_option_at_index(0).prompt) == "projet"
        assert screen.current().name == "omniscient-riff"
        await pilot.press("down", "down")
        await pilot.pause()
        assert screen.current().name == "perso"
        assert "Texte perso" in screen.query_one("#skills-preview", Markdown).source
        assert any("illisible" in m for m in _messages(app))


@pytest.mark.asyncio
async def test_search_filters(repo: Path, home: Path) -> None:
    app = Host(repo, home, FakeAtelier())
    async with app.run_test(size=SIZE) as pilot:
        await _settle(app, pilot)
        await pilot.press("slash")
        await pilot.pause()
        await pilot.press(*"tdd", "enter")
        await pilot.pause()
        screen = _screen(app)
        assert [s.name for s in screen.shown] == ["tdd-workflow", "tdd"]
        assert "recherche « tdd » : 2" in str(
            screen.query_one("#skills-count", Static).render()
        )


@pytest.mark.asyncio
async def test_install_then_remove_with_confirm(repo: Path, home: Path) -> None:
    app = Host(repo, home, FakeAtelier())
    async with app.run_test(size=SIZE) as pilot:
        await _settle(app, pilot)
        await pilot.press("down", "down", "i")
        await pilot.pause()
        await pilot.press("o")
        await pilot.pause()
        assert (repo / ".claude" / "skills" / "perso" / "SKILL.md").is_file()
        screen = _screen(app)
        assert "9 skills · 2 dans le projet" in str(
            screen.query_one("#skills-count", Static).render()
        )
        assert screen.current().source == "projet" and screen.current().name == "perso"
        await pilot.press("d")
        await pilot.pause()
        await pilot.press("n")  # cancelled: still there
        await pilot.pause()
        assert (repo / ".claude" / "skills" / "perso").is_dir()
        await pilot.press("d")
        await pilot.pause()
        await pilot.press("o")
        await pilot.pause()
        assert not (repo / ".claude" / "skills" / "perso").exists()
        assert any("retiré du projet" in m for m in _messages(app))


@pytest.mark.asyncio
async def test_remove_refuses_non_project_skill(repo: Path, home: Path) -> None:
    app = Host(repo, home, FakeAtelier())
    async with app.run_test(size=SIZE) as pilot:
        await _settle(app, pilot)
        await pilot.press("down", "down", "d")
        await pilot.pause()
        assert "Seuls les skills du projet peuvent être retirés." in _messages(app)
        assert len(app.screen_stack) == 2  # no confirm modal


@pytest.mark.asyncio
async def test_create_opens_skill_creator_atelier(repo: Path, home: Path) -> None:
    atelier = FakeAtelier()
    app = Host(repo, home, atelier)
    async with app.run_test(size=SIZE) as pilot:
        await _settle(app, pilot)
        await pilot.press("c")
        await pilot.pause()
        await pilot.press(*"relire", "enter")
        await _settle(app, pilot)
        assert atelier.calls == ["relire"]
        assert any("Atelier skill ouvert" in m for m in _messages(app))


@pytest.mark.asyncio
@pytest.mark.parametrize("atelier", [None, FakeAtelier(up=False)])
async def test_create_without_herdr_warns(
    repo: Path, home: Path, atelier: FakeAtelier | None
) -> None:
    app = Host(repo, home, atelier)
    async with app.run_test(size=SIZE) as pilot:
        await _settle(app, pilot)
        await pilot.press("c")
        await pilot.pause()
        await pilot.press("x", "enter")
        await _settle(app, pilot)
        assert NO_HERDR in _messages(app)


@pytest.mark.asyncio
async def test_escape_returns_to_board(repo: Path, home: Path) -> None:
    app = Host(repo, home, FakeAtelier())
    async with app.run_test(size=SIZE) as pilot:
        await _settle(app, pilot)
        await pilot.press("escape")
        await pilot.pause()
        assert len(app.screen_stack) == 1


@pytest.mark.asyncio
async def test_stale_highlight_after_search_is_ignored(repo: Path, home: Path) -> None:
    from textual.widgets.option_list import Option

    app = Host(repo, home, FakeAtelier())
    async with app.run_test(size=SIZE) as pilot:
        await _settle(app, pilot)
        screen = _screen(app)
        listing = screen.query_one(OptionList)
        preview = screen.query_one("#skills-preview", Markdown).source
        screen.shown = screen.shown[:2]  # a search shrank the list meanwhile
        for stale in ("7", "99", None):
            event = OptionList.OptionHighlighted(listing, Option("x", id=stale), 0)
            screen.on_option_list_option_highlighted(event)  # must not raise
        assert screen.query_one("#skills-preview", Markdown).source == preview
