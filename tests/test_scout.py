import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest

from omniscient.sense.scout import (
    FORMATTER_CAP_USD,
    FORMATTER_ID,
    ScoutFailed,
    ScoutFormatError,
    ScoutReport,
    ingest_report,
    parse_answer,
    run_scout,
)

NOW = datetime(2026, 11, 12, 20, 0, tzinfo=UTC)

GOOD_ITEM = {
    "source": "anthropic",
    "url": "https://www.anthropic.com/news/x",
    "title": "X",
    "published": "2026-11-10",
    "summary": "What it is. What it unlocks.",
    "tags": ["model-release"],
}


def _answer(
    items: list[dict[str, Any]], unreachable: list[dict[str, str]] | None = None
) -> str:
    return json.dumps({"items": items, "unreachable": unreachable or []})


# ---------- parse_answer: strict format ----------


def test_parse_answer_accepts_raw_json() -> None:
    report = parse_answer(_answer([GOOD_ITEM]))

    assert len(report.items) == 1
    assert report.items[0].source == "anthropic"
    assert report.items[0].published == date(2026, 11, 10)


def test_parse_answer_accepts_json_inside_code_fence() -> None:
    report = parse_answer("Here you go:\n```json\n" + _answer([GOOD_ITEM]) + "\n```")

    assert len(report.items) == 1


def test_parse_answer_rejects_unknown_source_with_field_path() -> None:
    bad = {**GOOD_ITEM, "source": "GitHub trending (weekly)"}

    with pytest.raises(ScoutFormatError) as exc:
        parse_answer(_answer([bad]))

    assert any("items.0.source" in e for e in exc.value.errors)


def test_parse_answer_rejects_bad_date_and_non_http_url() -> None:
    bad = {**GOOD_ITEM, "published": "Nov 2026", "url": "ftp://x"}

    with pytest.raises(ScoutFormatError) as exc:
        parse_answer(_answer([bad]))

    joined = " ".join(exc.value.errors)
    assert "items.0.published" in joined
    assert "items.0.url" in joined


def test_parse_answer_rejects_missing_json() -> None:
    with pytest.raises(ScoutFormatError, match="no JSON object"):
        parse_answer("I could not find anything today.")


def test_parse_answer_rejects_invalid_json() -> None:
    with pytest.raises(ScoutFormatError, match="invalid JSON"):
        parse_answer('{"items": [ }')


def test_parse_answer_rejects_extra_fields() -> None:
    with pytest.raises(ScoutFormatError):
        parse_answer(_answer([{**GOOD_ITEM, "score": 9}]))


def test_parse_answer_reads_unreachable_sources() -> None:
    report = parse_answer(_answer([], [{"source": "openai", "reason": "403"}]))

    assert report.unreachable[0].source == "openai"


def test_parse_answer_tolerates_extra_fields_in_diagnostic_unreachable() -> None:
    # Seen live: the scout added title/url to unreachable entries. They are diagnostics,
    # not vault data, so extras are dropped instead of failing the whole run.
    extra = [{"source": "openai", "reason": "403", "title": "t", "url": "https://x"}]

    report = parse_answer(_answer([GOOD_ITEM], extra))

    assert report.unreachable[0].reason == "403"


# ---------- run_scout: repair loop ----------


class FakeRuns:
    """Stands in for agentrun's client.runs: each wait() returns the next scripted run.

    No message() method on purpose: claude-agent runs are not continuable once
    completed (409 not_continuable, seen live), so repairs must start new runs.
    """

    def __init__(self, answers: list[dict[str, Any]]) -> None:
        self._answers = answers
        self.started: list[dict[str, Any]] = []

    def start(self, agent_id: str, body: dict[str, Any]) -> dict[str, Any]:
        self.started.append({"agent_id": agent_id, **body})
        return {"run_id": f"run-{len(self.started)}"}

    def wait(self, run_id: str, timeout: float) -> dict[str, Any]:
        return self._answers.pop(0)


class FakeClient:
    def __init__(self, answers: list[dict[str, Any]]) -> None:
        self.runs = FakeRuns(answers)


def _run(answer: str, status: str = "completed", usd: float = 0.01) -> dict[str, Any]:
    return {
        "run_id": "run-1",
        "status": status,
        "cost": {"usd": usd},
        "result": {"output": None, "final_answer": answer},
        "error": None
        if status == "completed"
        else {"code": "run_failed", "message": "boom"},
    }


def test_run_scout_returns_report_and_cost_on_first_valid_answer() -> None:
    client = FakeClient([_run(_answer([GOOD_ITEM]), usd=0.02)])

    result = run_scout(
        client, seen_urls=["https://seen"], lookback_days=7, max_cost_usd=1.0
    )

    assert len(result.report.items) == 1
    assert result.cost_usd == pytest.approx(0.02)
    assert result.repairs == 0
    sent = json.loads(client.runs.started[0]["input"]["content"])
    assert sent == {"lookback_days": 7, "seen_urls": ["https://seen"]}


def test_run_scout_repairs_bad_format_with_a_formatter_run() -> None:
    bad = _answer([{**GOOD_ITEM, "source": "Anthropic blog"}])
    client = FakeClient([_run(bad, usd=0.02), _run(_answer([GOOD_ITEM]), usd=0.001)])

    result = run_scout(client, seen_urls=[], lookback_days=7, max_cost_usd=1.0)

    assert result.repairs == 1
    assert result.cost_usd == pytest.approx(0.021)
    repair = client.runs.started[1]
    assert repair["agent_id"] == FORMATTER_ID
    assert repair["max_cost_usd"] == pytest.approx(FORMATTER_CAP_USD)
    prompt = repair["input"]["content"]
    assert "items.0.source" in prompt
    assert "github, hackernews, anthropic, openai, gemini, meta" in prompt
    assert "Anthropic blog" in prompt  # the bad answer itself is handed over


def test_run_scout_gives_up_after_max_repairs() -> None:
    bad = _run("not json")
    client = FakeClient([bad, bad, bad])

    with pytest.raises(ScoutFormatError):
        run_scout(
            client, seen_urls=[], lookback_days=7, max_cost_usd=1.0, max_repairs=2
        )

    formatter_runs = [s for s in client.runs.started if s["agent_id"] == FORMATTER_ID]
    assert len(formatter_runs) == 2


def test_run_scout_raises_when_run_fails() -> None:
    client = FakeClient([_run("", status="failed")])

    with pytest.raises(ScoutFailed, match="run_failed"):
        run_scout(client, seen_urls=[], lookback_days=7, max_cost_usd=1.0)


# ---------- ingest_report ----------


def test_ingest_report_writes_new_notes_and_skips_known(tmp_path: Path) -> None:
    report: ScoutReport = parse_answer(_answer([GOOD_ITEM, GOOD_ITEM]))

    paths = ingest_report(tmp_path, report, ingested=NOW)

    assert len(paths) == 1
    assert len(list((tmp_path / "news").glob("*.md"))) == 1


# ---------- owner profile steering ----------


def test_ingest_report_skips_items_matching_avoid(tmp_path: Path) -> None:
    crypto = {**GOOD_ITEM, "url": "https://x.com/c", "title": "Crypto wallet"}
    report = parse_answer(_answer([GOOD_ITEM, crypto]))

    created = ingest_report(tmp_path, report, ingested=NOW, avoid=("crypto",))

    assert len(created) == 1


def test_run_scout_passes_owner_interests_only_when_present() -> None:
    client = FakeClient([_run(_answer([GOOD_ITEM])), _run(_answer([GOOD_ITEM]))])

    run_scout(client, [], lookback_days=3, max_cost_usd=1.0)
    run_scout(
        client, [], lookback_days=3, max_cost_usd=1.0, interests=("course à pied",)
    )

    first, second = (json.loads(s["input"]["content"]) for s in client.runs.started)
    assert "owner_interests" not in first
    assert second["owner_interests"] == ["course à pied"]
