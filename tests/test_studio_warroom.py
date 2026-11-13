"""War room: sources parsed from fixtures, chains, rendering (no network, no herdr)."""

from __future__ import annotations

import json
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import frontmatter
import pytest
from rich.console import Console

from omniscient.studio import warroom
from omniscient.studio.ideas import (
    StudioError,
    _set_section,
    append_atelier,
    new_seed,
)
from omniscient.studio.warroom import (
    SUBJECT_WIDTH,
    Snapshot,
    collect,
    handoffs,
    herdr_agents,
    parse_herdr_agents,
    render,
    run_warroom,
)
from omniscient.studio.warroom_sources import (
    Note,
    SourceError,
    WarEvent,
    actor_for,
    clean_title,
    cost_events,
    drop_logged_runs,
    fetch_runs,
    link_run,
    live_runs,
    note_events,
    read_notes,
    run_event,
)

T0 = datetime(2026, 11, 13, 14, 0, tzinfo=UTC)
TITLE = "Radar pluie : la meteo de chaque sortie"
HERDR_LIST = {
    "id": "cli:agent:list",
    "result": {
        "type": "agent_list",
        "agents": [
            {
                "agent": "claude",
                "agent_status": "working",
                "name": "riff-radar-pluie-la-m-575e6d",
                "pane_id": "w2:p1",
                "workspace_id": "w2",
            },
            {"agent": "copilot", "agent_status": "blocked", "name": "seed-x-1a2b3c"},
        ],
    },
}


def _run(**kw: Any) -> dict[str, Any]:
    base = {
        "run_id": "r1",
        "agent_id": "builder",
        "status": "completed",
        "title": f"# Idée : {TITLE[:40]}…",
        "created_at": "2026-11-13T14:15:58.159Z",
        "ended_at": "2026-11-13T14:17:30.976Z",
        "error": None,
        "cost": {"usd": 0.0939, "cap_usd": 1.5},
    }
    return {**base, **kw}


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    return tmp_path / "vault"


def _seed(vault: Path, title: str = TITLE, **meta: Any) -> str:
    path = new_seed(vault, title=title, pitch="Un pitch.", now=T0)
    post = frontmatter.load(path)
    post.metadata.update(meta)
    path.write_text(frontmatter.dumps(post) + "\n", encoding="utf-8")
    return path.stem


def _add_section(vault: Path, stem: str, heading: str, body: str) -> None:
    path = vault / "ideas" / f"{stem}.md"
    post = frontmatter.load(path)
    post.content = _set_section(post.content, heading, body)
    path.write_text(frontmatter.dumps(post) + "\n", encoding="utf-8")


def _note(vault: Path, stem: str) -> Note:
    notes, _ = read_notes(vault)
    return next(n for n in notes if n.stem == stem)


# ---- actors and runs ----------------------------------------------------------


@pytest.mark.parametrize(
    ("agent_id", "actor"),
    [
        ("visionary", "visionnaire"),
        ("council", "conseil"),
        ("strategist", "stratège"),
        ("builder", "bâtisseur"),
        ("griller", "questionneur"),
        ("researcher-opus", "chercheur"),
        ("news-scout", "éclaireur"),
        ("scout-formatter", "formateur"),
        ("mystery", "mystery"),
        ("", "?"),
    ],
)
def test_actor_for_maps_agent_ids_to_french(agent_id: str, actor: str) -> None:
    assert actor_for(agent_id) == actor


def test_clean_title_drops_markdown_and_idea_prefix() -> None:
    assert clean_title("# Idée : Radar  météo…") == "Radar météo…"


def test_run_links_to_idea_by_truncated_title(vault: Path) -> None:
    stem = _seed(vault)
    notes, _ = read_notes(vault)

    event = run_event(_run(), notes)

    assert event is not None
    assert event.subject == stem
    assert event.actor == "bâtisseur"
    assert event.action == "construit"
    assert event.detail == "✓ terminé"  # linked: the title is not repeated
    assert event.cost_usd == pytest.approx(0.0939)


def test_run_links_by_build_run_id_frontmatter(vault: Path) -> None:
    stem = _seed(vault, title="Autre idée sans rapport", build_run_id="r9")
    notes, _ = read_notes(vault)

    assert link_run(_run(run_id="r9", title="# Idée : rien"), notes) == stem


def test_unlinked_failed_run_shows_error_message() -> None:
    run = _run(
        agent_id="council",
        status="failed",
        title="## Recent news",
        error={"code": "run_failed", "message": "Credit balance is too low"},
        ended_at=None,
    )

    event = run_event(run, ())

    assert event is not None
    assert event.subject is None
    assert event.detail == "✗ échec · Credit balance is too low"
    assert event.at == datetime(2026, 11, 13, 14, 15, 58, 159000, tzinfo=UTC)


def test_run_without_timestamp_is_skipped() -> None:
    assert run_event(_run(created_at=None, ended_at=None), ()) is None


def test_live_runs_keep_only_unfinished_runs() -> None:
    runs = [
        _run(status="working", agent_id="visionary"),
        _run(status="input_required", agent_id="researcher-opus"),
        _run(status="completed"),
    ]

    live = live_runs(runs, ())

    assert [(a.actor, a.state) for a in live] == [
        ("visionnaire", "working"),
        ("chercheur", "blocked"),
    ]


def test_fetch_runs_follows_next_cursor() -> None:
    pages = {None: {"runs": [_run()], "next_cursor": "A"}, "A": {"runs": [_run()]}}
    seen: list[str | None] = []

    def fetch(cursor: str | None) -> Mapping[str, Any]:
        seen.append(cursor)
        return pages[cursor]

    runs, warning = fetch_runs(fetch)

    assert len(runs) == 2
    assert warning is None
    assert seen == [None, "A"]


def test_fetch_runs_stops_at_page_limit() -> None:
    runs, _ = fetch_runs(lambda _c: {"runs": [_run()], "next_cursor": "x"}, 3)
    assert len(runs) == 3


def test_fetch_runs_turns_failure_into_warning() -> None:
    def fetch(cursor: str | None) -> Mapping[str, Any]:
        if cursor:
            raise SourceError("connexion refusée")
        return {"runs": [_run()], "next_cursor": "B"}

    runs, warning = fetch_runs(fetch)

    assert len(runs) == 1
    assert warning is not None and "injoignable" in warning


def test_fetch_runs_rejects_bad_shape() -> None:
    runs, warning = fetch_runs(lambda _c: {"items": []})
    assert runs == ()
    assert warning is not None


# ---- vault notes --------------------------------------------------------------


def test_note_events_cover_creation_council_atelier_and_discussion(
    vault: Path,
) -> None:
    stem = _seed(vault, origin="council")
    _add_section(vault, stem, "## Conseil", "**Verdict :** poursuivre\nBien.")
    append_atelier(vault, stem, "## Idée forte\nsuite", now=T0 + timedelta(hours=1))
    _add_section(
        vault,
        stem,
        "## Discussion",
        "**Propriétaire :** Strava le fait ?\n\n**Débatteur :** En partie.",
    )

    events = note_events(_note(vault, stem))

    summary = [(e.actor, e.action, e.detail) for e in events]
    assert summary == [
        ("visionnaire", "propose", TITLE),
        ("conseil", "tranche", "verdict : poursuivre"),
        ("atelier", "note", "Idée forte"),
        ("propriétaire", "demande", "Strava le fait ?"),
        ("débatteur", "répond", "En partie."),
    ]
    assert events[2].at == (T0 + timedelta(hours=1)).replace(second=0).astimezone()
    assert events[3].at < events[4].at
    assert all(e.subject == stem for e in events)


def test_owner_seed_is_sown_by_the_owner(vault: Path) -> None:
    stem = _seed(vault, origin="owner")
    events = note_events(_note(vault, stem))
    assert [(e.actor, e.action) for e in events] == [("propriétaire", "sème")]


def test_unreadable_note_becomes_a_warning(vault: Path) -> None:
    _seed(vault)
    (vault / "ideas" / "broken.md").write_text("---\n: [\n---\n", encoding="utf-8")

    notes, warnings = read_notes(vault)

    assert len(notes) == 1
    assert warnings == ("notes illisibles : broken.md",)


def test_missing_vault_reads_nothing(tmp_path: Path) -> None:
    assert read_notes(tmp_path / "nope") == ((), ())


# ---- cost log -----------------------------------------------------------------


COSTS = """| date | task | role | model | effort | usd |
|---|---|---|---|---|---|
| 13-11-2026-07-59-47 | think | visionary+council | opus | high | 0.6472 |
| 13-11-2026-09-08-21 | build {stem} | builder | sonnet | medium | 0.0830 |
| not-a-date | broken | builder | x | y | 1 |
"""


def test_cost_events_parse_rows_and_link_builds(vault: Path) -> None:
    stem = _seed(vault)
    notes, _ = read_notes(vault)

    events = cost_events(COSTS.format(stem=stem), notes)

    assert [(e.actor, e.action, e.subject) for e in events] == [
        ("visionnaire+conseil", "imagine", None),
        ("bâtisseur", "construit", stem),
    ]
    assert events[1].detail == "build"
    assert events[0].cost_usd == pytest.approx(0.6472)
    assert events[0].at == datetime(2026, 11, 13, 7, 59, 47).astimezone()


def test_cost_rows_already_shown_as_runs_are_dropped() -> None:
    at = datetime(2026, 11, 13, 9, 0).astimezone()
    row = WarEvent(at, "visionnaire+conseil", "imagine", None, "think", 0.6)
    lone = WarEvent(at, "débatteur", "répond", None, "slack reply", 0.04)
    run = WarEvent(at + timedelta(minutes=2), "conseil", "délibère", None, "", 0.2)

    assert drop_logged_runs([row, lone], [run]) == (lone,)


# ---- herdr --------------------------------------------------------------------


def test_parse_herdr_agents_links_names_to_ideas(vault: Path) -> None:
    stem = _seed(vault)
    notes, _ = read_notes(vault)

    agents = parse_herdr_agents(json.dumps(HERDR_LIST), notes)

    assert [(a.actor, a.state, a.subject) for a in agents] == [
        ("claude", "working", stem),
        ("copilot", "blocked", None),
    ]


def test_herdr_agents_skip_without_runner_and_warn_on_failure() -> None:
    def broken(_args: Sequence[str]) -> str:
        raise SourceError("herdr indisponible")

    assert herdr_agents(None, ()) == ((), None)
    assert herdr_agents(broken, ()) == ((), "herdr indisponible")
    assert herdr_agents(lambda _a: "not json", ())[1] == "réponse herdr inattendue"


# ---- chains, collect, render --------------------------------------------------


def _event(minutes: int, actor: str, subject: str | None = "a") -> WarEvent:
    return WarEvent(T0 + timedelta(minutes=minutes), actor, "agit", subject, "", None)


def test_handoffs_collapse_consecutive_actors_per_subject() -> None:
    events = [
        _event(0, "visionnaire"),
        _event(1, "conseil"),
        _event(2, "conseil"),
        _event(3, "éclaireur", None),
        _event(4, "stratège"),
        _event(5, "bâtisseur"),
    ]

    chains = handoffs(events)

    assert chains == (
        ("a", ("visionnaire", "conseil", "stratège", "bâtisseur")),
        (None, ("éclaireur",)),
    )


def _collect(vault: Path, idea: str | None = None) -> Snapshot:
    pages = {None: {"runs": [_run(), _run(run_id="r2", agent_id="news-scout")]}}
    return collect(
        vault,
        idea,
        lambda cursor: pages[cursor],
        lambda _args: json.dumps(HERDR_LIST),
        today=date(2026, 11, 13),
    )


def test_collect_merges_sources_newest_first(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # costs.md rows are local time: pin the zone the fixture was written in (CI runs UTC)
    monkeypatch.setenv("TZ", "America/Toronto")
    time.tzset()
    stem = _seed(vault)
    (vault / "ops").mkdir()
    (vault / "ops" / "costs.md").write_text(COSTS.format(stem=stem), encoding="utf-8")

    snap = _collect(vault)

    actors = [e.actor for e in snap.events]
    assert actors.count("bâtisseur") == 1  # the cost row duplicates the run
    assert "visionnaire+conseil" in actors
    assert snap.events == tuple(sorted(snap.events, key=lambda e: e.at, reverse=True))
    assert snap.spent_today == pytest.approx(0.7302)
    monkeypatch.delenv("TZ")
    time.tzset()
    assert len(snap.live) == 2
    assert snap.titles[stem] == TITLE


def test_collect_for_one_idea_filters_events_and_live(vault: Path) -> None:
    stem = _seed(vault)

    snap = _collect(vault, stem)

    assert {e.subject for e in snap.events} == {stem}
    assert [a.actor for a in snap.live] == ["claude"]


def _text(renderable: Any, width: int = 90) -> str:
    console = Console(width=width, record=True, color_system=None)
    console.print(renderable)
    return console.export_text()


def test_render_fits_a_small_pane(vault: Path) -> None:
    stem = _seed(vault)
    for i in range(40):
        append_atelier(vault, stem, f"entrée {i}", now=T0 + timedelta(minutes=i))
    now = datetime(2026, 11, 13, 15, 0).astimezone()

    text = _text(render(_collect(vault), None, 15, 90, now=now))

    lines = text.rstrip("\n").splitlines()
    assert len(lines) <= 15
    assert all(len(line) <= 90 for line in lines)
    assert "Salle de crise · tout" in lines[0]
    assert "aujourd'hui 0,00 $" in lines[0]
    assert "en ligne" in text and "au travail" in text and "bloqué" in text
    assert "relais" in text and "→" in text
    assert "entrée 39" in text


def test_render_one_idea_shows_its_title_and_empty_state(vault: Path) -> None:
    stem = _seed(vault)
    snap = Snapshot(events=(), live=(), titles={stem: TITLE})

    text = _text(render(snap, stem, 15, 90))

    assert TITLE[:20] in text
    assert "personne" in text
    assert "Aucun événement" in text


def test_run_warroom_prints_once(vault: Path) -> None:
    stem = _seed(vault)
    console = Console(width=90, record=True, color_system=None)

    run_warroom(
        vault,
        vault.parent,
        stem,
        False,
        fetcher=lambda: lambda _c: {"runs": [_run()]},
        runner=None,
        console=console,
    )

    text = console.export_text()
    assert "bâtisseur" in text and "construit" in text


def test_run_warroom_rejects_unknown_idea(vault: Path) -> None:
    _seed(vault)
    with pytest.raises(StudioError):
        run_warroom(vault, vault.parent, "nope", False, runner=None)


def test_run_warroom_survives_missing_agentrun(vault: Path) -> None:
    _seed(vault)
    console = Console(width=90, record=True, color_system=None)

    def no_token() -> Any:
        raise SourceError("pas de jeton AgentRun")

    run_warroom(
        vault, vault.parent, None, False, fetcher=no_token, runner=None, console=console
    )

    assert "pas de jeton AgentRun" in console.export_text()


def test_follow_stops_cleanly_on_ctrl_c(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(vault)

    def interrupt(_seconds: float) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(warroom.time, "sleep", interrupt)
    console = Console(width=90, height=15, record=True, color_system=None)

    run_warroom(
        vault,
        vault.parent,
        None,
        True,
        fetcher=lambda: lambda _c: {"runs": []},
        runner=None,
        console=console,
    )


@pytest.mark.parametrize("title", ["Foo [/bar] baz", "Plan [1] ok [b]x"])
def test_markup_in_titles_actions_and_details_is_shown_literally(
    vault: Path, title: str
) -> None:
    event = WarEvent(
        at=datetime(2026, 11, 13, 9).astimezone(),
        actor="atelier",
        action=f"a {title}",
        subject="s",
        detail=f"d {title}",
        cost_usd=0.0,
    )
    snap = Snapshot(events=(event,), live=(), titles={"s": title})

    text = _text(render(snap, None, 15, 200))

    assert f"a {title}" in text
    assert title[:SUBJECT_WIDTH] in text
    assert f"d {title}" in text


def test_unreadable_cost_log_is_a_warning(vault: Path) -> None:
    (vault / "ops").mkdir(parents=True)
    (vault / "ops" / "costs.md").write_bytes(b"\xff\xfe broken")

    snap = _collect(vault)

    assert any("journal des coûts illisible" in w for w in snap.warnings)
    assert snap.spent_today == 0.0


def test_herdr_runner_timeout_is_short_and_becomes_a_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    def fake_run(*args: Any, **kwargs: Any) -> Any:
        seen.update(kwargs)
        raise warroom.subprocess.TimeoutExpired("herdr", kwargs["timeout"])

    monkeypatch.setattr(warroom.subprocess, "run", fake_run)
    assert warroom.HERDR_TIMEOUT_S <= warroom.REFRESH_SECONDS
    with pytest.raises(SourceError, match="herdr indisponible"):
        warroom.herdr_runner(["agent", "list"])
    assert seen["timeout"] <= 3
