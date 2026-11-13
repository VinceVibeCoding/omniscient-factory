import json
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from omniscient.runs import RunFailed
from omniscient.work import grilling as grilling_mod
from omniscient.work.grilling import (
    GRILLER_CAP_USD,
    GRILLER_ID,
    GrillingError,
    build_prompt,
    find_grilling_by_thread,
    gist_of,
    handle_grilling_reply,
    post_rounds,
    prepare_round,
    round_blocks,
)
from omniscient.work.tickets import TicketError, parse_ticket

MAP = """---
idea: '[[2026-11-12 coach]]'
type: map
---

# Coach course

Idée source : [[2026-11-12 coach]]

## Destination
Décider si le coach part en prototype.

## Notes
Ne touchez pas aux données de production.

## Decisions so far

<!-- one line per resolved ticket: gist + [[link]] -->

## Not yet specified
- brouillard
"""

QUESTIONS = [
    {
        "title": "Public cible",
        "question": "Le coach vise-t-il seulement toi ?",
        "recommended": "Oui, toi seul pour le prototype.",
    },
    {
        "title": "Source",
        "question": "Fichiers FIT ou API de la montre ?",
        "recommended": "Fichiers FIT, sans compte connecté.",
    },
]


def _ticket(
    n: int, kind: str = "grilling", status: str = "open", blocked: str = ""
) -> str:
    lines = [f"# Ticket {n}", "", f"Type: {kind}", f"Status: {status}"]
    if blocked:
        lines.append(f"Blocked by: {blocked}")
    lines += [
        "Effort: S",
        "Intelligence: skilled",
        "Route: griller · anthropic/claude-opus-5-5 · high (HITL: the owner takes part)",
        "",
        "## Question",
        f"Question {n} ?",
        "",
    ]
    return "\n".join(lines)


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    d = tmp_path / "maps" / "coach"
    (d / "issues").mkdir(parents=True)
    (d / "map.md").write_text(MAP, encoding="utf-8")
    research = _ticket(1, "research", "resolved") + "\n## Answer\nLes FIT suffisent.\n"
    (d / "issues" / "01-a.md").write_text(research, encoding="utf-8")
    (d / "issues" / "02-b.md").write_text(_ticket(2, blocked="01"), encoding="utf-8")
    (d / "issues" / "03-c.md").write_text(_ticket(3, "research"), encoding="utf-8")
    (d / "issues" / "04-d.md").write_text(_ticket(4, blocked="02"), encoding="utf-8")
    (tmp_path / "ideas").mkdir()
    (tmp_path / "ideas" / "2026-11-12 coach.md").write_text(
        "---\ntitle: Coach\n---\nUn coach à partir des fichiers FIT.\n",
        encoding="utf-8",
    )
    return tmp_path


class _Runs:
    def __init__(self, answers: list[str], cost: float = 0.2) -> None:
        self.answers = answers
        self.cost = cost
        self.started: list[dict[str, Any]] = []

    def start(self, agent_id: str, body: dict[str, Any]) -> dict[str, Any]:
        self.started.append({"agent_id": agent_id, **body})
        return {"run_id": f"run-{len(self.started)}"}

    def wait(self, run_id: str, timeout: float) -> dict[str, Any]:
        answer = self.answers.pop(0)
        if answer == "FAIL":
            return {"run_id": run_id, "status": "failed", "error": {"code": "x"}}
        return {
            "run_id": run_id,
            "status": "completed",
            "cost": {"usd": self.cost},
            "result": {"output": None, "final_answer": answer},
        }


class _Client:
    def __init__(self, answers: list[str], cost: float = 0.2) -> None:
        self.runs = _Runs(answers, cost)


class _Web:
    def __init__(self, fail: bool = False) -> None:
        self.posts: list[dict[str, Any]] = []
        self.fail = fail

    def chat_postMessage(self, **kwargs: Any) -> dict[str, Any]:
        if self.fail:
            raise RuntimeError("slack down")
        self.posts.append(kwargs)
        return {"ok": True, "ts": f"17000000{len(self.posts)}.0001"}


def _answer(questions: list[dict[str, str]] | None = None) -> str:
    return json.dumps({"questions": QUESTIONS if questions is None else questions})


def _issue(vault: Path, name: str) -> Path:
    return vault / "maps" / "coach" / "issues" / name


def _posted(vault: Path) -> Path:
    client = _Client([_answer()])
    post_rounds(client, _Web(), "C1", vault, budget_usd=5.0)
    return _issue(vault, "02-b.md")


# --- prompt and round ---------------------------------------------------------


def test_build_prompt_includes_question_map_idea_and_blocker_answers(
    vault: Path,
) -> None:
    ticket = parse_ticket(_issue(vault, "02-b.md"))

    prompt = build_prompt(ticket, vault)

    assert "Question 2 ?" in prompt
    assert "Décider si le coach part en prototype." in prompt
    assert "Ne touchez pas aux données de production." in prompt
    assert "Un coach à partir des fichiers FIT." in prompt
    assert "Les FIT suffisent." in prompt


def test_build_prompt_survives_missing_idea_note(vault: Path) -> None:
    (vault / "ideas" / "2026-11-12 coach.md").unlink()
    ticket = parse_ticket(_issue(vault, "02-b.md"))

    prompt = build_prompt(ticket, vault)

    assert "_aucune note d'idée_" in prompt


def test_build_prompt_refuses_idea_link_escaping_the_vault(vault: Path) -> None:
    map_md = vault / "maps" / "coach" / "map.md"
    map_md.write_text(MAP.replace("2026-11-12 coach]]'", "../../etc/passwd]]'"))
    ticket = parse_ticket(_issue(vault, "02-b.md"))

    prompt = build_prompt(ticket, vault)

    assert "_aucune note d'idée_" in prompt


def test_prepare_round_runs_griller_and_validates_questions(vault: Path) -> None:
    client = _Client([_answer()])
    ticket = parse_ticket(_issue(vault, "02-b.md"))

    grilling = prepare_round(client, ticket, vault)

    started = client.runs.started[0]
    assert started["agent_id"] == GRILLER_ID
    assert started["max_cost_usd"] == GRILLER_CAP_USD
    assert "Question 2 ?" in started["input"]["content"]
    assert [q.title for q in grilling.questions] == ["Public cible", "Source"]
    assert grilling.cost_usd == pytest.approx(0.2)
    assert grilling.ticket == ticket


@pytest.mark.parametrize(
    "answer",
    [
        "pas de json",
        json.dumps({"questions": []}),
        json.dumps({"questions": QUESTIONS * 3}),
        json.dumps({"questions": [{"title": "T", "question": "?"}]}),
    ],
)
def test_prepare_round_rejects_bad_answers(vault: Path, answer: str) -> None:
    ticket = parse_ticket(_issue(vault, "02-b.md"))

    with pytest.raises(GrillingError):
        prepare_round(_Client([answer]), ticket, vault)


def test_round_blocks_number_questions_with_recommendations(vault: Path) -> None:
    ticket = parse_ticket(_issue(vault, "02-b.md"))
    grilling = prepare_round(_Client([_answer()]), ticket, vault)

    blocks = round_blocks(grilling)

    text = json.dumps(blocks, ensure_ascii=False)
    assert "Coach course" in text and "Ticket 2" in text
    assert "❓ *Q1 - Public cible* : Le coach vise-t-il seulement toi ?" in text
    assert "➡️ Fichiers FIT, sans compte connecté." in text
    assert "*Q2 - Source*" in text
    assert "Réponds dans ce fil (« oui » accepte toutes les recommandations)" in text


# --- posting --------------------------------------------------------------------


def test_post_rounds_posts_only_frontier_grilling_and_records_thread(
    vault: Path,
) -> None:
    web = _Web()
    client = _Client([_answer()])

    posted = post_rounds(client, web, "C1", vault, budget_usd=5.0)

    path = _issue(vault, "02-b.md")
    assert posted == (path,)
    assert len(web.posts) == 1 and web.posts[0]["channel"] == "C1"
    assert "Public cible" in web.posts[0]["text"]
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    route = next(i for i, line in enumerate(lines) if line.startswith("Route:"))
    assert lines[route + 1] == "Slack: C1 170000001.0001"
    assert "## Grilling round" in text and "Public cible" in text
    ticket = parse_ticket(path)
    assert ticket.status == "claimed" and ticket.question == "Question 2 ?"


def test_post_rounds_does_not_repost(vault: Path) -> None:
    _posted(vault)
    web = _Web()
    client = _Client([_answer()])

    assert post_rounds(client, web, "C1", vault, budget_usd=5.0) == ()
    assert web.posts == [] and client.runs.started == []


def test_post_rounds_skips_open_ticket_already_linked_to_slack(vault: Path) -> None:
    path = _issue(vault, "02-b.md")
    path.write_text(
        path.read_text().replace("\n\n## Question", "\nSlack: C1 1.1\n\n## Question")
    )
    client = _Client([_answer()])

    assert post_rounds(client, _Web(), "C1", vault, budget_usd=5.0) == ()


def test_post_rounds_releases_ticket_when_griller_fails(vault: Path) -> None:
    errors: list[tuple[Path, Exception]] = []
    client = _Client(["FAIL"])

    posted = post_rounds(
        client,
        _Web(),
        "C1",
        vault,
        budget_usd=5.0,
        on_error=lambda p, e: errors.append((p, e)),
    )

    assert posted == ()
    assert parse_ticket(_issue(vault, "02-b.md")).status == "open"
    assert isinstance(errors[0][1], RunFailed)


def test_post_rounds_releases_ticket_when_slack_fails(vault: Path) -> None:
    errors: list[tuple[Path, Exception]] = []

    posted = post_rounds(
        _Client([_answer()]),
        _Web(fail=True),
        "C1",
        vault,
        budget_usd=5.0,
        on_error=lambda p, e: errors.append((p, e)),
    )

    assert posted == ()
    text = _issue(vault, "02-b.md").read_text()
    assert "Status: open" in text and "Slack:" not in text
    assert str(errors[0][1]) == "slack down"


def test_post_rounds_reports_with_default_logger(
    vault: Path, caplog: pytest.LogCaptureFixture
) -> None:
    post_rounds(_Client(["pas de json"]), _Web(), "C1", vault, budget_usd=5.0)

    assert "02-b.md" in caplog.text


def test_post_rounds_reports_malformed_map(vault: Path) -> None:
    (vault / "maps" / "coach" / "issues" / "09-bad.md").write_text("# rien\n")
    errors: list[tuple[Path, Exception]] = []

    posted = post_rounds(
        _Client([]),
        _Web(),
        "C1",
        vault,
        budget_usd=5.0,
        on_error=lambda p, e: errors.append((p, e)),
    )

    assert posted == () and errors[0][0] == vault / "maps" / "coach"


def test_post_rounds_stops_when_budget_cannot_cover_a_round(vault: Path) -> None:
    client = _Client([_answer()])

    posted = post_rounds(client, _Web(), "C1", vault, budget_usd=0.1)

    assert posted == () and client.runs.started == []
    assert parse_ticket(_issue(vault, "02-b.md")).status == "open"


def test_post_rounds_reports_costs(vault: Path) -> None:
    costs: list[float] = []

    post_rounds(
        _Client([_answer()]),
        _Web(),
        "C1",
        vault,
        budget_usd=5.0,
        on_cost=lambda r: costs.append(r.cost_usd),
    )

    assert costs == [pytest.approx(0.2)]


def test_post_rounds_rejects_negative_budget(vault: Path) -> None:
    with pytest.raises(ValueError):
        post_rounds(_Client([]), _Web(), "C1", vault, budget_usd=-1)


def test_post_rounds_rejects_empty_channel(vault: Path) -> None:
    with pytest.raises(ValueError):
        post_rounds(_Client([]), _Web(), " ", vault, budget_usd=1)


# --- replies --------------------------------------------------------------------


def test_find_grilling_by_thread(vault: Path) -> None:
    path = _posted(vault)

    assert find_grilling_by_thread(vault, "170000001.0001", channel="C1") == path
    assert find_grilling_by_thread(vault, "999.1", channel="C1") is None
    assert find_grilling_by_thread(vault, "", channel="C1") is None


def test_owner_reply_resolves_ticket_with_questions_and_verbatim_answer(
    vault: Path,
) -> None:
    path = _posted(vault)

    reply = handle_grilling_reply(
        vault,
        "170000001.0001",
        "Oui, mais Q2 : l'API. Le reste ok.",
        True,
        channel="C1",
    )

    assert reply is not None and reply.ticket_path == path
    assert reply.gist == "Oui, mais Q2 : l'API."
    assert "Ticket 2" in reply.text and reply.gist in reply.text
    ticket = parse_ticket(path)
    assert ticket.status == "resolved"
    answer = path.read_text(encoding="utf-8").split("## Answer\n", 1)[1]
    assert "Q1 - Public cible" in answer and "Oui, toi seul" in answer
    assert "**Propriétaire :**\n> > Oui, mais Q2 : l'API. Le reste ok." in answer
    map_text = (vault / "maps" / "coach" / "map.md").read_text(encoding="utf-8")
    assert "[[02-b|Ticket 2]] : Oui, mais Q2 : l'API." in map_text


def test_plain_yes_accepts_all_recommendations(vault: Path) -> None:
    _posted(vault)

    reply = handle_grilling_reply(
        vault, "170000001.0001", "  Oui ! ", True, channel="C1"
    )

    assert reply is not None and reply.gist == "Recommandations acceptées"


def test_non_owner_reply_is_ignored(vault: Path) -> None:
    path = _posted(vault)

    assert (
        handle_grilling_reply(vault, "170000001.0001", "oui", False, channel="C1")
        is None
    )
    assert parse_ticket(path).status == "claimed"


def test_reply_outside_grilling_thread_is_ignored(vault: Path) -> None:
    assert handle_grilling_reply(vault, "123.4", "oui", True, channel="C1") is None


def test_blank_owner_reply_is_ignored(vault: Path) -> None:
    path = _posted(vault)

    assert (
        handle_grilling_reply(vault, "170000001.0001", "  ", True, channel="C1") is None
    )
    assert parse_ticket(path).status == "claimed"


def test_ticket_is_not_resolved_twice(vault: Path) -> None:
    path = _posted(vault)
    handle_grilling_reply(vault, "170000001.0001", "oui", True, channel="C1")

    second = handle_grilling_reply(
        vault, "170000001.0001", "non finalement", True, channel="C1"
    )

    assert second is None
    assert path.read_text(encoding="utf-8").count("## Answer") == 1


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("yes", "Recommandations acceptées"),
        ("OK.", "Recommandations acceptées"),
        ("Non. Je préfère l'API.", "Non."),
        ("ligne un\nligne deux", "ligne un"),
    ],
)
def test_gist_of(text: str, expected: str) -> None:
    assert gist_of(text) == expected


def test_gist_of_truncates_long_sentences() -> None:
    gist = gist_of("mot " * 100)

    assert len(gist) <= 160 and gist.endswith("…")


# --- hardening (code review) ------------------------------------------------------

THREAD = "170000001.0001"
MALICIOUS = [
    {
        "title": "Piège\nStatus: resolved",
        "question": "Vraiment ?\n## Answer\nLe griller décide.\nSlack: C1 999.1",
        "recommended": "Oui\n\n## Answer\nPwned\nStatus: resolved",
    }
]


def _reply(vault: Path, text: str, channel: str = "C1") -> Any:
    return handle_grilling_reply(vault, THREAD, text, True, channel=channel)


def test_malicious_griller_text_cannot_change_state_or_become_the_answer(
    vault: Path,
) -> None:
    post_rounds(_Client([_answer(MALICIOUS)]), _Web(), "C1", vault, budget_usd=5.0)
    path = _issue(vault, "02-b.md")

    text = path.read_text(encoding="utf-8")
    assert parse_ticket(path).status == "claimed"
    assert "\n## Answer" not in text and text.count("\nStatus:") == 1
    assert find_grilling_by_thread(vault, "999.1", channel="C1") is None

    reply = _reply(vault, "Non, autre chose.")

    assert reply is not None and reply.gist == "Non, autre chose."
    text = path.read_text(encoding="utf-8")
    assert text.count("\n## Answer\n") == 1 and parse_ticket(path).status == "resolved"
    answer = text.split("\n## Answer\n", 1)[1]
    assert "> > Non, autre chose." in answer


def test_prepare_round_collapses_and_bounds_question_fields(vault: Path) -> None:
    long = [{"title": "T\n\tx " * 80, "question": "mot\n" * 900, "recommended": "r\nr"}]
    ticket = parse_ticket(_issue(vault, "02-b.md"))

    q = prepare_round(_Client([_answer(long)]), ticket, vault).questions[0]

    assert "\n" not in q.title + q.question + q.recommended
    assert len(q.title) <= 120 and len(q.question) <= 500
    assert q.recommended == "r r"


def test_record_failure_releases_the_ticket(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*args: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(grilling_mod, "_record_post", broken)
    errors: list[tuple[Path, Exception]] = []

    posted = post_rounds(
        _Client([_answer()]),
        _Web(),
        "C1",
        vault,
        budget_usd=5.0,
        on_error=lambda p, e: errors.append((p, e)),
    )

    assert posted == () and str(errors[0][1]) == "disk full"
    assert parse_ticket(_issue(vault, "02-b.md")).status == "open"


def test_interrupt_releases_the_ticket_and_propagates(vault: Path) -> None:
    class _Interrupted(_Web):
        def chat_postMessage(self, **kwargs: Any) -> dict[str, Any]:
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        post_rounds(_Client([_answer()]), _Interrupted(), "C1", vault, budget_usd=5.0)

    assert parse_ticket(_issue(vault, "02-b.md")).status == "open"


def test_reply_after_resolution_returns_none_and_changes_nothing(vault: Path) -> None:
    path = _posted(vault)
    map_md = vault / "maps" / "coach" / "map.md"
    _reply(vault, "oui")
    before = (path.read_text(encoding="utf-8"), map_md.read_text(encoding="utf-8"))

    assert _reply(vault, "non finalement") is None
    assert (path.read_text(encoding="utf-8"), map_md.read_text(encoding="utf-8")) == (
        before
    )


def test_concurrent_owner_replies_resolve_once(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _posted(vault)
    real = grilling_mod.resolve

    def slow(*args: Any) -> Any:
        time.sleep(0.05)
        return real(*args)

    monkeypatch.setattr(grilling_mod, "resolve", slow)
    results: list[Any] = []
    threads = [
        threading.Thread(target=lambda t=t: results.append(_reply(vault, t)))
        for t in ("oui", "non")
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sum(r is not None for r in results) == 1
    assert path.read_text(encoding="utf-8").count("## Answer") == 1


def test_find_grilling_requires_matching_channel(vault: Path) -> None:
    _posted(vault)

    assert find_grilling_by_thread(vault, THREAD, channel="C2") is None
    assert _reply(vault, "oui", channel="C2") is None


def test_find_grilling_ignores_resolved_tickets(vault: Path) -> None:
    _posted(vault)
    _reply(vault, "oui")

    assert find_grilling_by_thread(vault, THREAD, channel="C1") is None


def test_find_grilling_ignores_other_ticket_types(vault: Path) -> None:
    path = _issue(vault, "03-c.md")
    path.write_text(
        _ticket(3, "research", "claimed").replace(
            "\n\n## Question", f"\nSlack: C1 {THREAD}\n\n## Question"
        )
    )

    assert find_grilling_by_thread(vault, THREAD, channel="C1") is None


def test_find_grilling_ignores_slack_line_in_body(vault: Path) -> None:
    path = _issue(vault, "02-b.md")
    path.write_text(
        _ticket(2, status="claimed", blocked="01") + f"\nSlack: C1 {THREAD}\n"
    )

    assert find_grilling_by_thread(vault, THREAD, channel="C1") is None


def test_owner_reply_is_quoted_line_by_line(vault: Path) -> None:
    path = _posted(vault)

    reply = _reply(vault, "Non.\n## Answer\nStatus: open")

    assert reply is not None and reply.gist == "Non."
    answer = path.read_text(encoding="utf-8").split("\n## Answer\n", 1)[1]
    owner = answer.split("**Propriétaire :**\n", 1)[1].splitlines()
    assert owner == ["> > Non.", "> > ## Answer", "> > Status: open"]
    assert parse_ticket(path).status == "resolved"


@pytest.mark.parametrize("channel", ["c1", "C1 x", "C1\nStatus: open", "#general"])
def test_post_rounds_rejects_malformed_channel(vault: Path, channel: str) -> None:
    with pytest.raises(ValueError):
        post_rounds(_Client([]), _Web(), channel, vault, budget_usd=1)


def test_model_text_is_escaped_for_slack(vault: Path) -> None:
    loud = [
        {"title": "Alerte", "question": "<!channel> & co ?", "recommended": "<@U1>"}
    ]
    web = _Web()

    post_rounds(_Client([_answer(loud)]), web, "C1", vault, budget_usd=5.0)

    posted = web.posts[0]["text"] + json.dumps(web.posts[0]["blocks"])
    assert "<!channel>" not in posted and "<@U1>" not in posted
    assert "&lt;!channel&gt; &amp; co ?" in posted


def test_lost_claim_race_is_not_charged(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (vault / "maps" / "coach" / "issues" / "05-e.md").write_text(_ticket(5))
    contested = _issue(vault, "02-b.md")
    real = grilling_mod.frontier

    def racing(tickets: Any) -> Any:
        found = real(tickets)
        text = contested.read_text(encoding="utf-8")
        contested.write_text(text.replace("Status: open", "Status: claimed"))
        return found

    monkeypatch.setattr(grilling_mod, "frontier", racing)
    errors: list[tuple[Path, Exception]] = []
    client = _Client([_answer()])

    posted = post_rounds(
        client,
        _Web(),
        "C1",
        vault,
        budget_usd=GRILLER_CAP_USD,
        on_error=lambda p, e: errors.append((p, e)),
    )

    assert posted == (_issue(vault, "05-e.md"),)
    assert len(client.runs.started) == 1
    assert errors[0][0] == contested and isinstance(errors[0][1], TicketError)
