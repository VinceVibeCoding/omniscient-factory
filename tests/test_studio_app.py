"""The studio board, driven headless with Textual's pilot on a throwaway vault."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import frontmatter
import pytest
from textual.widgets import DataTable, Markdown

from omniscient.studio.app import NO_HERDR, StudioApp
from omniscient.studio.ideas import IdeaCard, fork_variant, new_seed

T0 = datetime(2026, 11, 12, 9, tzinfo=UTC)
SIZE = (140, 40)

pytestmark = pytest.mark.asyncio


class FakeAtelier:
    def __init__(self, up: bool = True, error: Exception | None = None) -> None:
        self.up = up
        self.error = error
        self.calls: list[tuple[str, object]] = []

    def available(self) -> bool:
        return self.up

    def _record(self, name: str, arg: object) -> str:
        if self.error is not None:
            raise self.error
        self.calls.append((name, arg))
        return "w1"

    def open_idea(self, card: IdeaCard, move: str, agent: object = None) -> str:
        if agent is None:
            return self._record("idea", (card.stem, move))
        return self._record("idea", (card.stem, move, agent.key))  # type: ignore[attr-defined]

    def open_remix(self, cards: Sequence[IdeaCard]) -> str:
        return self._record("remix", tuple(c.stem for c in cards))

    def open_seed(self, text: str) -> str:
        return self._record("seed", text)

    def open_warroom(self) -> str:
        return self._record("warroom", None)

    def open_skill_creator(self, text: str) -> str:
        return self._record("skill", text)


def _seed(vault: Path, title: str, minutes: int) -> str:
    path = new_seed(
        vault,
        title=title,
        pitch=f"Pitch de {title}.",
        now=T0 + timedelta(minutes=minutes),
    )
    return path.stem


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    v = tmp_path / "vault"
    alpha = _seed(v, "Alpha", 3)  # newest seed → first row
    _seed(v, "Beta", 2)
    _seed(v, "Gamma", 1)
    fork_variant(
        v, alpha, pitch="Alpha pour les équipes.", now=T0 + timedelta(minutes=4)
    )
    return v


def _titles(app: StudioApp) -> list[str]:
    return [card.title for card, _ in app.rows]


def _messages(app: StudioApp) -> list[tuple[str, str]]:
    return [(n.message, n.severity) for n in app._notifications]


async def _settle(app: StudioApp, pilot) -> None:
    await app.workers.wait_for_complete()
    await pilot.pause()


async def test_rows_in_tree_order_with_french_status(vault: Path) -> None:
    app = StudioApp(vault, vault.parent, FakeAtelier())
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        assert _titles(app) == ["Alpha", "v2 · Alpha", "Beta", "Gamma"]
        assert [depth for _, depth in app.rows] == [0, 1, 0, 0]
        table = app.query_one(DataTable)
        first = table.get_row_at(0)
        assert str(first[3]) == "graine"
        assert str(first[1]) == "—"
        assert str(table.get_row_at(1)[2]).startswith("└ ")


async def test_highlight_updates_preview(vault: Path) -> None:
    app = StudioApp(vault, vault.parent, FakeAtelier())
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        assert "Pitch de Alpha" in app.query_one("#preview", Markdown).source
        await pilot.press("down", "down")
        await pilot.pause()
        assert app.current_card().title == "Beta"
        assert "Pitch de Beta" in app.query_one("#preview", Markdown).source


async def test_enter_opens_riff_and_v_opens_variant(vault: Path) -> None:
    atelier = FakeAtelier()
    app = StudioApp(vault, vault.parent, atelier)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        stem = app.current_stem()
        await pilot.press("enter")
        await _settle(app, pilot)
        await pilot.press("v")
        await _settle(app, pilot)
        assert atelier.calls == [("idea", (stem, "riff")), ("idea", (stem, "variant"))]
        messages = [m for m, _ in _messages(app)]
        assert "Ouverture de l'atelier…" in messages
        assert any("Atelier ouvert dans herdr" in m for m in messages)


async def test_space_twice_then_m_remixes_two_ideas(vault: Path) -> None:
    atelier = FakeAtelier()
    app = StudioApp(vault, vault.parent, atelier)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        first = app.current_stem()
        await pilot.press("space", "down", "down", "space")
        await pilot.pause()
        second = app.current_stem()
        assert str(app.query_one(DataTable).get_row_at(0)[0]) == "✓"
        await pilot.press("m")
        await _settle(app, pilot)
        assert atelier.calls == [("remix", (first, second))]
        assert app.selected == ()


async def test_m_with_one_selected_warns_without_calling(vault: Path) -> None:
    atelier = FakeAtelier()
    app = StudioApp(vault, vault.parent, atelier)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        await pilot.press("space", "m")
        await _settle(app, pilot)
        assert atelier.calls == []
        assert any(s == "warning" and "remixer" in m for m, s in _messages(app))


async def test_n_opens_prompt_then_seeds(vault: Path) -> None:
    atelier = FakeAtelier()
    app = StudioApp(vault, vault.parent, atelier)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        await pilot.press("n")
        await pilot.pause()
        await pilot.press(*"un bot", "enter")
        await _settle(app, pilot)
        assert atelier.calls == [("seed", "un bot")]


async def test_x_confirm_drops_the_idea_and_hides_it(vault: Path) -> None:
    app = StudioApp(vault, vault.parent, FakeAtelier())
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        card = app.current_card()
        await pilot.press("x")
        await pilot.pause()
        await pilot.press("o")
        await pilot.pause()
        meta = frontmatter.load(card.path).metadata
        assert meta["status"] == "dropped"
        assert meta["owner_verdict"] == "drop"
        assert card.title not in _titles(app)


async def test_x_cancel_keeps_the_idea(vault: Path) -> None:
    app = StudioApp(vault, vault.parent, FakeAtelier())
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        card = app.current_card()
        await pilot.press("x")
        await pilot.pause()
        await pilot.press("n")
        await pilot.pause()
        assert frontmatter.load(card.path).metadata["status"] == "seed"


async def test_p_records_pursue(vault: Path) -> None:
    app = StudioApp(vault, vault.parent, FakeAtelier())
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        card = app.current_card()
        await pilot.press("p")
        await pilot.pause()
        assert "owner_verdict" not in frontmatter.load(card.path).metadata  # asks first
        await pilot.press("o")
        await pilot.pause()
        assert frontmatter.load(card.path).metadata["owner_verdict"] == "pursue"


async def test_a_broken_note_is_skipped_with_a_warning(vault: Path) -> None:
    (vault / "ideas" / "2026-01-01 cassee.md").write_text("---\ntitle: [oops\n---\n")
    app = StudioApp(vault, vault.parent, FakeAtelier())
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        assert app.rows  # the board still shows the good notes
        assert any("cassee" in n.message for n in app._notifications)


async def test_clicking_a_row_does_not_open_an_atelier(vault: Path) -> None:
    atelier = FakeAtelier()
    app = StudioApp(vault, vault.parent, atelier)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        await pilot.click("#ideas", offset=(10, 1))
        await pilot.click("#ideas", offset=(10, 1))
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert atelier.calls == []


async def test_filter_cycle_hides_then_shows_dropped(vault: Path) -> None:
    from omniscient.slack.core import apply_owner_verdict

    beta = next(p.stem for p in (vault / "ideas").glob("*beta*.md"))
    apply_owner_verdict(vault, beta, "drop")
    app = StudioApp(vault, vault.parent, FakeAtelier())
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        assert "Beta" not in _titles(app)  # actives
        await pilot.press("f")
        await pilot.pause()
        assert "Beta" in _titles(app)  # toutes
        await pilot.press("f")
        await pilot.pause()
        assert "Beta" not in _titles(app) and len(app.rows) == 3  # graines
        assert "graines" in app.sub_title


async def test_slash_filters_by_text(vault: Path) -> None:
    app = StudioApp(vault, vault.parent, FakeAtelier())
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        await pilot.press("slash")
        await pilot.pause()
        await pilot.press(*"gam", "enter")
        await pilot.pause()
        assert _titles(app) == ["Gamma"]


async def test_unavailable_atelier_warns_without_crashing(vault: Path) -> None:
    for atelier in (None, FakeAtelier(up=False)):
        app = StudioApp(vault, vault.parent, atelier)
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            await pilot.press("enter")
            await _settle(app, pilot)
            assert (NO_HERDR, "warning") in _messages(app)
            assert app.is_running


async def test_herdr_error_becomes_error_notification(vault: Path) -> None:
    herdr = pytest.importorskip("omniscient.studio.herdr")
    atelier = FakeAtelier(error=herdr.HerdrError("boom"))
    app = StudioApp(vault, vault.parent, atelier)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        await pilot.press("v")
        await _settle(app, pilot)
        assert ("boom", "error") in _messages(app)


async def test_auto_refresh_picks_up_new_note_and_keeps_cursor(vault: Path) -> None:
    app = StudioApp(vault, vault.parent, FakeAtelier(), poll_seconds=0.05)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        await pilot.press("down", "down")
        await pilot.pause()
        kept = app.current_stem()
        _seed(vault, "Delta", 10)
        await pilot.pause(0.3)
        assert "Delta" in _titles(app)
        assert app.current_stem() == kept


def _agentrun_repo(vault: Path) -> Path:
    repo = vault.parent / "repo"
    (repo / "agentrun").mkdir(parents=True)
    (repo / "agentrun" / "debater.json").write_text(
        '{"definition": {"id": "debater", "version": 1, "max_cost_usd": 0.25}}'
    )
    return repo


async def test_a_picks_an_agent_for_the_atelier(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omniscient.studio import agents
    from omniscient.studio.screens import AgentPickerModal

    monkeypatch.setattr(agents.shutil, "which", lambda exe: None)
    atelier = FakeAtelier()
    app = StudioApp(vault, _agentrun_repo(vault), atelier)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        stem = app.current_stem()
        await pilot.press("a")
        await pilot.pause()
        assert isinstance(app.screen, AgentPickerModal)
        await pilot.press("down", "enter")  # Claude, then Omniscient · debater
        await _settle(app, pilot)
        assert atelier.calls == [("idea", (stem, "riff", "or-debater"))]
        assert any("debater (crédit API)" in m for m, _ in _messages(app))


async def test_a_then_escape_opens_nothing(vault: Path) -> None:
    atelier = FakeAtelier()
    app = StudioApp(vault, vault.parent, atelier)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        await pilot.press("a")
        await pilot.pause()
        await pilot.press("escape")
        await _settle(app, pilot)
        assert atelier.calls == []
        assert not app.screen.is_modal


async def test_w_opens_the_war_room(vault: Path) -> None:
    atelier = FakeAtelier()
    app = StudioApp(vault, vault.parent, atelier)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        await pilot.press("w")
        await _settle(app, pilot)
        assert atelier.calls == [("warroom", None)]
        assert ("Salle de crise ouverte dans herdr", "information") in _messages(app)


async def test_s_without_skills_library_warns(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys

    from omniscient.studio.app import NO_SKILLS

    monkeypatch.setitem(sys.modules, "omniscient.studio.skills_screen", None)
    app = StudioApp(vault, vault.parent, FakeAtelier())
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        await pilot.press("s")
        await pilot.pause()
        assert (NO_SKILLS, "warning") in _messages(app)
        assert app.is_running


async def test_s_pushes_the_skills_screen(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys
    import types

    from textual.screen import Screen

    pushed: list[tuple[Path, object]] = []

    class FakeSkills(Screen[None]):
        def __init__(self, repo: Path, atelier: object) -> None:
            super().__init__()
            pushed.append((repo, atelier))

    module = types.ModuleType("omniscient.studio.skills_screen")
    module.SkillsScreen = FakeSkills  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "omniscient.studio.skills_screen", module)
    atelier = FakeAtelier()
    app = StudioApp(vault, vault.parent, atelier)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        await pilot.press("s")
        await pilot.pause()
        assert isinstance(app.screen, FakeSkills)
        assert pushed == [(vault.parent, atelier)]


@pytest.mark.parametrize("title", ["Foo [/bar] baz", "Plan [1] ok [b]x"])
async def test_confirm_dialog_shows_a_markup_title_literally(
    tmp_path: Path, title: str
) -> None:
    from textual.widgets import Label

    vault = tmp_path / "vault"
    _seed(vault, title, 1)
    app = StudioApp(vault, vault.parent, FakeAtelier())
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        await pilot.press("x")
        await pilot.pause()
        label = app.screen.query_one(".dialog-title", Label)
        assert f"« {title} »" in str(label.render())
        await pilot.press("n")
