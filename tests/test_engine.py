import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from omniscient.home import packaged
from omniscient.runs import RunFailed
from omniscient.work import engine
from omniscient.work.engine import (
    HAIKU_CAP_USD,
    OPUS_CAP_USD,
    SONNET_CAP_USD,
    build_prompt,
    researcher_for,
    run_afk,
)
from omniscient.work.tickets import TicketError, load_map, parse_ticket

NOW = datetime(2026, 11, 12, 21, 0, tzinfo=UTC)
SONNET = "anthropic/claude-sonnet-5-5"
HAIKU = "anthropic/claude-haiku-5-5"
OPUS = "anthropic/claude-opus-5-5"
MAP_MD = """---
created: '2026-11-12T20:39:36+00:00'
idea: '[[2026-11-12 card-verifier]]'
type: map
---

# Vérificateur d'annonces

## Destination
Une décision sur le vérificateur.

## Notes
Citez les documents primaires.

## Decisions so far

<!-- one line per resolved ticket -->

## Not yet specified
- Rien
"""


def _ticket_md(
    title: str,
    *,
    type_: str = "research",
    status: str = "open",
    model: str = SONNET,
    blocked: str = "",
    hitl: bool = False,
    answer: str = "",
    effort: str = "medium",
) -> str:
    route = f"researcher · {model} · {effort}" + (" · HITL" if hitl else "")
    lines = [f"# {title}", "", f"Type: {type_}", f"Status: {status}"]
    if blocked:
        lines.append(f"Blocked by: {blocked}")
    lines += ["Effort: M", "Intelligence: skilled", f"Route: {route}", ""]
    lines += ["## Question", f"Question de {title} ?"]
    if answer:
        lines += ["", "## Answer", answer]
    return "\n".join(lines) + "\n"


def _vault(tmp_path: Path, tickets: dict[str, str], idea: bool = True) -> Path:
    vault = tmp_path / "vault"
    map_dir = vault / "maps" / "card-verifier"
    (map_dir / "issues").mkdir(parents=True)
    (map_dir / "map.md").write_text(MAP_MD, encoding="utf-8")
    for name, text in tickets.items():
        (map_dir / "issues" / name).write_text(text, encoding="utf-8")
    if idea:
        (vault / "ideas").mkdir()
        (vault / "ideas" / "2026-11-12 card-verifier.md").write_text(
            "# Idée\nUn vérificateur d'annonces asynchrone.\n", encoding="utf-8"
        )
    return vault


def _issue(vault: Path, name: str) -> Path:
    return vault / "maps" / "card-verifier" / "issues" / name


def _good(answer: str = "Réponse détaillée https://x.dev", gist: str = "Gist.") -> str:
    return json.dumps({"answer": answer, "gist": gist})


class _Runs:
    def __init__(self, answers: list[Any], cost: float = 0.2) -> None:
        self.answers = answers
        self.cost = cost
        self.started: list[dict[str, Any]] = []
        self.raised: list[tuple[str, dict[str, Any]]] = []

    def raise_cap(self, run_id: str, body: dict[str, Any]) -> dict[str, Any]:
        self.raised.append((run_id, body))
        return {"run_id": run_id, "status": "running"}

    def start(self, agent_id: str, body: dict[str, Any]) -> dict[str, Any]:
        self.started.append({"agent_id": agent_id, **body})
        return {"run_id": f"run-{len(self.started)}"}

    def wait(self, run_id: str, timeout: float) -> dict[str, Any]:
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        if isinstance(answer, dict):
            return {"run_id": run_id, **answer}
        if answer is None:
            return {"run_id": run_id, "status": "failed", "error": {"code": "cap"}}
        return {
            "run_id": run_id,
            "status": "completed",
            "cost": {"usd": self.cost},
            "result": {"output": None, "final_answer": answer},
        }


class _Client:
    def __init__(self, answers: list[Any], cost: float = 0.2) -> None:
        self.runs = _Runs(answers, cost)


def test_researcher_for_maps_each_tier_to_its_agent_and_cap() -> None:
    # Arrange / Act
    tiers = {m: researcher_for(m) for m in (HAIKU, SONNET, OPUS, "x/unknown")}

    # Assert
    assert tiers[HAIKU].agent_id == "researcher-haiku"
    assert tiers[HAIKU].cap_usd == HAIKU_CAP_USD
    assert tiers[SONNET].agent_id == "researcher-sonnet"
    assert tiers[SONNET].cap_usd == SONNET_CAP_USD
    assert tiers[OPUS].agent_id == "researcher-opus"
    assert tiers[OPUS].cap_usd == OPUS_CAP_USD
    assert tiers["x/unknown"] is None


def test_agent_definitions_match_the_routing_constants() -> None:
    # Arrange
    tiers = {"haiku": HAIKU, "sonnet": SONNET, "opus": OPUS}

    for name, model in tiers.items():
        # Act
        definition = json.loads(
            (packaged("agents") / f"researcher-{name}.json").read_text()
        )["definition"]

        # Assert
        researcher = researcher_for(model)
        assert researcher is not None
        assert definition["id"] == researcher.agent_id
        assert definition["model"] == model
        assert definition["max_cost_usd"] == researcher.cap_usd
        assert definition["tools"] == ["web_search", "web_fetch", "read"]
        assert definition["place"] == {"mode": "runner"}
        assert "LANGUAGE" in definition["instructions"]


def test_build_prompt_includes_question_map_idea_and_blocker_answers(
    tmp_path: Path,
) -> None:
    # Arrange
    vault = _vault(
        tmp_path,
        {
            "01-apis.md": _ticket_md(
                "APIs", status="resolved", answer="eBay a une API."
            ),
            "02-tos.md": _ticket_md("CGU", blocked="01"),
        },
    )
    tickets = load_map(vault / "maps" / "card-verifier")

    # Act
    prompt = build_prompt(vault, tickets[1], tickets)

    # Assert
    assert "Question de CGU ?" in prompt
    assert "Une décision sur le vérificateur." in prompt
    assert "Citez les documents primaires." in prompt
    assert "Un vérificateur d'annonces asynchrone." in prompt
    assert "eBay a une API." in prompt
    assert '"answer"' in prompt and '"gist"' in prompt


def test_build_prompt_tolerates_a_missing_idea_note(tmp_path: Path) -> None:
    # Arrange
    vault = _vault(tmp_path, {"01-apis.md": _ticket_md("APIs")}, idea=False)
    tickets = load_map(vault / "maps" / "card-verifier")

    # Act
    prompt = build_prompt(vault, tickets[0], tickets)

    # Assert
    assert "Question de APIs ?" in prompt
    assert "introuvable" in prompt


def test_run_afk_resolves_frontier_research_tickets(tmp_path: Path) -> None:
    # Arrange
    vault = _vault(
        tmp_path,
        {
            "01-apis.md": _ticket_md("APIs", model=HAIKU),
            "02-tos.md": _ticket_md("CGU", blocked="01"),
        },
    )
    client = _Client([_good("eBay expose Browse API https://developer.ebay.com")])

    # Act
    result = run_afk(client, vault, budget_usd=1.0, now=NOW)

    # Assert
    ticket = parse_ticket(_issue(vault, "01-apis.md"))
    assert ticket.status == "resolved"
    assert result.resolved == (ticket.path,)
    assert result.cost_usd == pytest.approx(0.2)
    assert result.ran_at == NOW
    started = client.runs.started[0]
    assert started["agent_id"] == "researcher-haiku"
    assert started["max_cost_usd"] == HAIKU_CAP_USD
    assert "Gist." in (vault / "maps" / "card-verifier" / "map.md").read_text()
    assert parse_ticket(_issue(vault, "02-tos.md")).status == "open"


def test_run_afk_claims_before_running(tmp_path: Path) -> None:
    # Arrange
    vault = _vault(tmp_path, {"01-apis.md": _ticket_md("APIs")})
    seen: list[str] = []

    class _Spy(_Runs):
        def start(self, agent_id: str, body: dict[str, Any]) -> dict[str, Any]:
            seen.append(parse_ticket(_issue(vault, "01-apis.md")).status)
            return super().start(agent_id, body)

    client = _Client([])
    client.runs = _Spy([_good()])

    # Act
    run_afk(client, vault, budget_usd=1.0, now=NOW)

    # Assert
    assert seen == ["claimed"]


@pytest.mark.parametrize(
    ("answer", "reason"),
    [
        (None, "run"),
        ("pas de json", "JSON"),
        (json.dumps({"answer": "", "gist": "x"}), "invalid"),
        (json.dumps({"answer": "x"}), "invalid"),
        (RunFailed("boom"), "boom"),
    ],
)
def test_run_afk_releases_the_ticket_on_failure(
    tmp_path: Path, answer: Any, reason: str
) -> None:
    # Arrange
    vault = _vault(tmp_path, {"01-apis.md": _ticket_md("APIs")})

    # Act
    result = run_afk(_Client([answer]), vault, budget_usd=1.0, now=NOW)

    # Assert
    assert parse_ticket(_issue(vault, "01-apis.md")).status == "open"
    assert result.resolved == ()
    assert len(result.failed) == 1
    assert reason in result.failed[0].reason


def test_run_afk_records_unexpected_errors_and_keeps_going(tmp_path: Path) -> None:
    # Arrange
    vault = _vault(tmp_path, {"01-a.md": _ticket_md("A"), "02-b.md": _ticket_md("B")})
    client = _Client([ConnectionError("down\nmore detail"), _good()])

    # Act
    result = run_afk(client, vault, budget_usd=5.0, now=NOW)

    # Assert
    assert parse_ticket(_issue(vault, "01-a.md")).status == "open"
    assert result.failed[0].reason == "ConnectionError: down"
    assert result.resolved == (_issue(vault, "02-b.md"),)
    assert result.cost_usd == pytest.approx(0.2)


def test_run_afk_charges_the_cap_of_an_unexpected_error(tmp_path: Path) -> None:
    # Arrange
    vault = _vault(tmp_path, {"01-a.md": _ticket_md("A"), "02-b.md": _ticket_md("B")})

    # Act
    result = run_afk(
        _Client([RuntimeError("x"), _good()]), vault, budget_usd=0.9, now=NOW
    )

    # Assert
    assert result.cost_usd == 0.0
    assert [s.path for s in result.skipped] == [_issue(vault, "02-b.md")]


def test_run_afk_truncates_long_failure_reasons(tmp_path: Path) -> None:
    # Arrange
    vault = _vault(tmp_path, {"01-a.md": _ticket_md("A")})

    # Act
    result = run_afk(
        _Client([RuntimeError("x" * 1000)]), vault, budget_usd=1.0, now=NOW
    )

    # Assert
    assert len(result.failed[0].reason) <= engine.REASON_MAX_CHARS


@pytest.mark.parametrize("interrupt", [KeyboardInterrupt(), SystemExit(1)])
def test_run_afk_releases_and_reraises_interrupts(
    tmp_path: Path, interrupt: BaseException
) -> None:
    # Arrange
    vault = _vault(tmp_path, {"01-apis.md": _ticket_md("APIs")})

    # Act / Assert
    with pytest.raises(type(interrupt)):
        run_afk(_Client([interrupt]), vault, budget_usd=1.0, now=NOW)
    assert parse_ticket(_issue(vault, "01-apis.md")).status == "open"


def test_run_afk_reports_a_failed_release_in_the_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange
    vault = _vault(tmp_path, {"01-a.md": _ticket_md("A")})

    def broken_release(ticket: Any) -> Any:
        raise OSError("disk gone")

    monkeypatch.setattr(engine, "release", broken_release)

    # Act
    result = run_afk(_Client([None]), vault, budget_usd=1.0, now=NOW)

    # Assert
    assert "release failed" in result.failed[0].reason
    assert "disk gone" in result.failed[0].reason


def test_run_afk_keeps_a_ticket_claimed_when_resolve_fails_after_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange
    vault = _vault(tmp_path, {"01-a.md": _ticket_md("A")})

    def half_resolve(ticket: Any, answer: str, gist: str) -> Any:
        text = ticket.path.read_text(encoding="utf-8")
        ticket.path.write_text(f"{text}\n## Answer\n{answer}\n", encoding="utf-8")
        raise TicketError("map write failed")

    monkeypatch.setattr(engine, "resolve", half_resolve)

    # Act
    result = run_afk(_Client([_good()]), vault, budget_usd=1.0, now=NOW)

    # Assert
    assert parse_ticket(_issue(vault, "01-a.md")).status == "claimed"
    assert "answer already written" in result.failed[0].reason
    assert result.cost_usd == pytest.approx(0.2)


def test_run_afk_releases_when_resolve_fails_before_writing(tmp_path: Path) -> None:
    # Arrange
    vault = _vault(tmp_path, {"01-a.md": _ticket_md("A")})
    map_md = vault / "maps" / "card-verifier" / "map.md"
    map_md.write_text(MAP_MD.replace("## Decisions so far", "## Autre"))

    # Act
    result = run_afk(_Client([_good()]), vault, budget_usd=1.0, now=NOW)

    # Assert
    assert parse_ticket(_issue(vault, "01-a.md")).status == "open"
    assert "Decisions so far" in result.failed[0].reason
    assert "## Answer" not in _issue(vault, "01-a.md").read_text()


@pytest.mark.parametrize(
    "payload",
    [
        {"answer": "   ", "gist": "x"},
        {"answer": "x", "gist": "  "},
    ],
)
def test_run_afk_rejects_invalid_model_output(
    tmp_path: Path, payload: dict[str, str]
) -> None:
    # Arrange
    vault = _vault(tmp_path, {"01-a.md": _ticket_md("A")})

    # Act
    result = run_afk(_Client([json.dumps(payload)]), vault, budget_usd=1.0, now=NOW)

    # Assert
    assert parse_ticket(_issue(vault, "01-a.md")).status == "open"
    assert "invalid answer" in result.failed[0].reason


def test_run_afk_answer_with_control_lines_cannot_change_the_ticket(
    tmp_path: Path,
) -> None:
    # Arrange
    vault = _vault(tmp_path, {"01-a.md": _ticket_md("A")})
    hostile = "Réponse.\nStatus: open\n## Question\nQuestion piratée ?"

    # Act
    result = run_afk(_Client([_good(answer=hostile)]), vault, budget_usd=1.0, now=NOW)

    # Assert
    ticket = parse_ticket(_issue(vault, "01-a.md"))
    assert result.resolved == (ticket.path,)
    assert ticket.status == "resolved"
    assert ticket.question == "Question de A ?"


def test_run_afk_reports_the_actual_cost_of_a_failed_run(tmp_path: Path) -> None:
    # Arrange
    vault = _vault(tmp_path, {"01-a.md": _ticket_md("A"), "02-b.md": _ticket_md("B")})
    failed = {"status": "failed", "error": {"code": "cap"}, "cost": {"usd": 0.1}}

    # Act
    result = run_afk(_Client([failed, _good()]), vault, budget_usd=0.9, now=NOW)

    # Assert
    assert len(result.failed) == 1
    assert result.resolved == (_issue(vault, "02-b.md"),)
    assert result.cost_usd == pytest.approx(0.3)


@pytest.mark.parametrize(
    ("effort", "expected"), [("low", "low"), ("high", "high"), ("xhigh", None)]
)
def test_run_afk_passes_the_route_effort_to_the_run(
    tmp_path: Path, effort: str, expected: str | None
) -> None:
    # Arrange
    vault = _vault(tmp_path, {"01-a.md": _ticket_md("A", effort=effort)})
    client = _Client([_good()])

    # Act
    run_afk(client, vault, budget_usd=1.0, now=NOW)

    # Assert
    assert client.runs.started[0].get("effort") == expected


def test_build_prompt_refuses_an_idea_link_outside_the_ideas_folder(
    tmp_path: Path,
) -> None:
    # Arrange
    vault = _vault(tmp_path, {"01-a.md": _ticket_md("A")})
    (tmp_path / "secret.md").write_text("TOP SECRET", encoding="utf-8")
    map_md = vault / "maps" / "card-verifier" / "map.md"
    map_md.write_text(MAP_MD.replace("2026-11-12 card-verifier", "../../secret"))
    tickets = load_map(vault / "maps" / "card-verifier")

    # Act
    prompt = build_prompt(vault, tickets[0], tickets)

    # Assert
    assert "TOP SECRET" not in prompt
    assert "introuvable" in prompt


def test_build_prompt_reports_a_map_without_an_idea_link(tmp_path: Path) -> None:
    # Arrange
    vault = _vault(tmp_path, {"01-a.md": _ticket_md("A")})
    map_md = vault / "maps" / "card-verifier" / "map.md"
    map_md.write_text(MAP_MD.replace("idea: '[[2026-11-12 card-verifier]]'\n", ""))
    tickets = load_map(vault / "maps" / "card-verifier")

    # Act
    prompt = build_prompt(vault, tickets[0], tickets)

    # Assert
    assert "aucun lien" in prompt


def test_run_afk_reports_task_tickets_as_waiting_for_owner(tmp_path: Path) -> None:
    # Arrange
    vault = _vault(
        tmp_path,
        {
            "01-export.md": _ticket_md("Export", type_="task"),
            "02-grill.md": _ticket_md("Règles", type_="grilling", hitl=True),
            "03-hitl.md": _ticket_md("Recherche HITL", hitl=True),
        },
    )
    client = _Client([])

    # Act
    result = run_afk(client, vault, budget_usd=1.0, now=NOW)

    # Assert
    assert result.waiting == (_issue(vault, "01-export.md"),)
    assert client.runs.started == []
    assert parse_ticket(_issue(vault, "01-export.md")).status == "open"


def test_run_afk_stops_when_the_next_cap_exceeds_budget(tmp_path: Path) -> None:
    # Arrange
    vault = _vault(
        tmp_path,
        {
            "01-a.md": _ticket_md("A", model=SONNET),
            "02-b.md": _ticket_md("B", model=OPUS),
            "03-c.md": _ticket_md("C", model=HAIKU),
        },
    )
    client = _Client([_good()], cost=0.4)

    # Act
    result = run_afk(client, vault, budget_usd=1.0, now=NOW)

    # Assert
    assert result.resolved == (_issue(vault, "01-a.md"),)
    assert [s.path for s in result.skipped] == [
        _issue(vault, "02-b.md"),
        _issue(vault, "03-c.md"),
    ]
    assert all("budget" in s.reason for s in result.skipped)
    assert parse_ticket(_issue(vault, "02-b.md")).status == "open"


def test_run_afk_counts_the_cap_of_a_failed_run_against_budget(
    tmp_path: Path,
) -> None:
    # Arrange
    vault = _vault(
        tmp_path,
        {"01-a.md": _ticket_md("A"), "02-b.md": _ticket_md("B")},
    )

    # Act
    result = run_afk(_Client([None, _good()]), vault, budget_usd=0.9, now=NOW)

    # Assert
    assert len(result.failed) == 1
    assert result.cost_usd == 0.0
    assert [s.path for s in result.skipped] == [_issue(vault, "02-b.md")]


def test_run_afk_honours_max_tickets(tmp_path: Path) -> None:
    # Arrange
    vault = _vault(tmp_path, {"01-a.md": _ticket_md("A"), "02-b.md": _ticket_md("B")})

    # Act
    result = run_afk(_Client([_good()]), vault, budget_usd=5.0, now=NOW, max_tickets=1)

    # Assert
    assert len(result.resolved) == 1
    assert "max_tickets" in result.skipped[0].reason


def test_run_afk_skips_unknown_models_without_claiming(tmp_path: Path) -> None:
    # Arrange
    vault = _vault(tmp_path, {"01-a.md": _ticket_md("A", model="openai/gpt-x")})

    # Act
    result = run_afk(_Client([]), vault, budget_usd=5.0, now=NOW)

    # Assert
    assert "openai/gpt-x" in result.skipped[0].reason
    assert parse_ticket(_issue(vault, "01-a.md")).status == "open"


def test_run_afk_records_a_malformed_map_and_continues(tmp_path: Path) -> None:
    # Arrange
    vault = _vault(tmp_path, {"01-a.md": _ticket_md("A")})
    broken = vault / "maps" / "broken"
    (broken / "issues").mkdir(parents=True)
    (broken / "map.md").write_text(MAP_MD, encoding="utf-8")
    (broken / "issues" / "01-bad.md").write_text("# Bad\n", encoding="utf-8")

    # Act
    result = run_afk(_Client([_good()]), vault, budget_usd=5.0, now=NOW)

    # Assert
    assert result.resolved == (_issue(vault, "01-a.md"),)
    assert result.failed[0].path == broken
    assert "01-bad.md" in result.failed[0].reason


def test_run_afk_with_no_maps_does_nothing(tmp_path: Path) -> None:
    # Act
    result = run_afk(_Client([]), tmp_path, budget_usd=1.0, now=NOW)

    # Assert
    assert result.resolved == () and result.cost_usd == 0.0


@pytest.mark.parametrize(
    "kwargs", [{"budget_usd": -1.0}, {"max_tickets": 0}, {"wait_seconds": 0}]
)
def test_run_afk_validates_arguments(tmp_path: Path, kwargs: dict[str, Any]) -> None:
    # Act / Assert
    with pytest.raises(ValueError):
        run_afk(_Client([]), tmp_path, now=NOW, **{"budget_usd": 1.0, **kwargs})


def test_run_afk_skips_a_ticket_another_worker_claimed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange
    vault = _vault(tmp_path, {"01-a.md": _ticket_md("A")})

    def taken(ticket: Any) -> Any:
        raise TicketError("01-a.md: cannot claim a claimed ticket")

    monkeypatch.setattr(engine, "claim", taken)
    client = _Client([])

    # Act
    result = run_afk(client, vault, budget_usd=1.0, now=NOW)

    # Assert
    assert "cannot claim" in result.skipped[0].reason
    assert client.runs.started == []


def test_run_afk_shortens_a_long_gist_instead_of_rejecting_the_answer(
    tmp_path: Path,
) -> None:
    # Arrange: seen live, every researcher wrote a gist over 200 chars and all
    # four answers were thrown away; the gist is a summary line, so shorten it.
    vault = _vault(tmp_path, {"01-a.md": _ticket_md("A")})
    long_gist = "mot " * 120

    # Act
    result = run_afk(_Client([_good(gist=long_gist)]), vault, budget_usd=1.0, now=NOW)

    # Assert
    assert parse_ticket(_issue(vault, "01-a.md")).status == "resolved"
    assert result.failed == ()
    pointer = next(
        l
        for l in (vault / "maps" / "card-verifier" / "map.md").read_text().splitlines()
        if "01-a" in l
    )
    assert pointer.endswith("…") and len(pointer) < 300


def _paused(least: float, spent: float = 0.395) -> dict[str, Any]:
    """A run AgentRun paused at its cap (seen live on researcher-opus)."""
    return {
        "status": "input_required",
        "cost": {"usd": spent},
        "pending": {
            "kind": "budget",
            "cap_usd": OPUS_CAP_USD,
            "spent_usd": spent,
            "needs_usd": least - spent,
            "least_cap_usd": least,
            "suggested_cap_usd": 5,
        },
    }


def _done(cost: float) -> dict[str, Any]:
    return {
        "status": "completed",
        "cost": {"usd": cost},
        "result": {"output": None, "final_answer": _good()},
    }


def test_run_afk_raises_the_cap_once_and_resolves_a_paused_run(
    tmp_path: Path,
) -> None:
    # Arrange
    vault = _vault(tmp_path, {"01-a.md": _ticket_md("A", model=OPUS)})
    client = _Client([_paused(0.79), _done(0.7)])

    # Act
    result = run_afk(client, vault, budget_usd=2.0, now=NOW)

    # Assert
    assert client.runs.raised == [("run-1", {"max_cost_usd": 0.79})]
    assert len(client.runs.started) == 1
    assert result.resolved == (_issue(vault, "01-a.md"),)
    assert result.cost_usd == pytest.approx(0.7)  # the run's total, not 0.395 + 0.7


def test_run_afk_refuses_a_raise_over_twice_the_tier_cap(tmp_path: Path) -> None:
    # Arrange
    vault = _vault(tmp_path, {"01-a.md": _ticket_md("A", model=OPUS)})
    client = _Client([_paused(2 * OPUS_CAP_USD + 0.01)])

    # Act
    result = run_afk(client, vault, budget_usd=10.0, now=NOW)

    # Assert
    assert client.runs.raised == []
    assert parse_ticket(_issue(vault, "01-a.md")).status == "open"
    reason = result.failed[0].reason
    assert "run-1" in reason and "2x" in reason
    assert result.cost_usd == pytest.approx(0.395)


def test_run_afk_refuses_a_raise_the_cycle_budget_cannot_cover(
    tmp_path: Path,
) -> None:
    # Arrange: $0.78 left; the cap of $0.75 is reserved, the extra $0.04 is not
    vault = _vault(tmp_path, {"01-a.md": _ticket_md("A", model=OPUS)})
    client = _Client([_paused(0.79)])

    # Act
    result = run_afk(client, vault, budget_usd=0.78, now=NOW)

    # Assert
    assert client.runs.raised == []
    assert parse_ticket(_issue(vault, "01-a.md")).status == "open"
    assert "run-1" in result.failed[0].reason
    assert "budget" in result.failed[0].reason


def test_run_afk_refuses_a_raise_without_a_least_cap(tmp_path: Path) -> None:
    # Arrange
    vault = _vault(tmp_path, {"01-a.md": _ticket_md("A", model=OPUS)})
    paused = _paused(0.79)
    paused["pending"] = {"kind": "budget"}

    # Act
    result = run_afk(_Client([paused]), vault, budget_usd=5.0, now=NOW)

    # Assert
    assert "run-1" in result.failed[0].reason
    assert "unknown" in result.failed[0].reason


def test_run_afk_releases_after_a_second_pause_and_raises_only_once(
    tmp_path: Path,
) -> None:
    # Arrange: seen live, the raised run paused again at $0.54 needing $1.02
    vault = _vault(tmp_path, {"01-a.md": _ticket_md("A", model=OPUS)})
    client = _Client([_paused(0.79), _paused(1.02, spent=0.54)])

    # Act
    result = run_afk(client, vault, budget_usd=5.0, now=NOW)

    # Assert
    assert client.runs.raised == [("run-1", {"max_cost_usd": 0.79})]
    assert parse_ticket(_issue(vault, "01-a.md")).status == "open"
    reason = result.failed[0].reason
    assert "run-1" in reason and "0.79" in reason
    assert result.cost_usd == pytest.approx(0.54)


def test_run_afk_releases_when_the_resumed_run_fails(tmp_path: Path) -> None:
    # Arrange
    vault = _vault(tmp_path, {"01-a.md": _ticket_md("A", model=OPUS)})
    client = _Client([_paused(0.79), None])

    # Act
    result = run_afk(client, vault, budget_usd=5.0, now=NOW)

    # Assert
    assert parse_ticket(_issue(vault, "01-a.md")).status == "open"
    assert "run-1" in result.failed[0].reason


def test_run_afk_charges_the_raised_cap_when_the_resumed_cost_is_unknown(
    tmp_path: Path,
) -> None:
    # Arrange: $0.79 (raised) + $0.50 > $1.27, whereas $0.75 + $0.50 would fit
    vault = _vault(
        tmp_path,
        {"01-a.md": _ticket_md("A", model=OPUS), "02-b.md": _ticket_md("B")},
    )
    client = _Client([_paused(0.79), RuntimeError("lost"), _good()])

    # Act
    result = run_afk(client, vault, budget_usd=1.27, now=NOW)

    # Assert
    assert "lost" in result.failed[0].reason
    assert [s.path for s in result.skipped] == [_issue(vault, "02-b.md")]
    assert "$0.79 spent" in result.skipped[0].reason


@pytest.mark.parametrize("interrupt", [KeyboardInterrupt(), SystemExit(1)])
def test_run_afk_releases_and_reraises_interrupts_while_resuming(
    tmp_path: Path, interrupt: BaseException
) -> None:
    # Arrange
    vault = _vault(tmp_path, {"01-a.md": _ticket_md("A", model=OPUS)})

    # Act / Assert
    with pytest.raises(type(interrupt)):
        run_afk(_Client([_paused(0.79), interrupt]), vault, budget_usd=5.0, now=NOW)
    assert parse_ticket(_issue(vault, "01-a.md")).status == "open"
