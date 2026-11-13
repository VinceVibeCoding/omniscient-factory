"""Sense loop: drive the AgentRun news-scout, enforce its output format, write news notes.

The claude-agent harness does not enforce `output_schema`, and completed claude-agent
runs cannot be continued. So Omniscient enforces the format itself: strict validation,
then up to `max_repairs` fresh scout-formatter runs given the exact errors and the bad
answer, then a loud failure. Nothing unvalidated reaches the vault.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from omniscient.brain import NewsItem, find_news, news_id, write_news
from omniscient.runs import (
    DEFAULT_WAIT_SECONDS,
    AgentRunClient,
    AnswerNotJson,
    extract_json,
    run_once,
)
from omniscient.runs import RunFailed as ScoutFailed
from omniscient.sense.interests import is_avoided

AGENT_ID = "news-scout"
FORMATTER_ID = "scout-formatter"
FORMATTER_CAP_USD = 0.10
DEFAULT_MAX_REPAIRS = 2
SOURCES = ("github", "hackernews", "anthropic", "openai", "gemini", "meta")
Source = Literal["github", "hackernews", "anthropic", "openai", "gemini", "meta"]

__all__ = [
    "AGENT_ID",
    "FORMATTER_CAP_USD",
    "FORMATTER_ID",
    "AgentRunClient",
    "ScoutFailed",
    "ScoutFormatError",
    "ScoutItem",
    "ScoutReport",
    "ScoutResult",
    "ingest_report",
    "parse_answer",
    "run_scout",
]


class ScoutFormatError(Exception):
    """The scout's answer does not match the required shape."""

    def __init__(self, message: str, errors: tuple[str, ...] = ()) -> None:
        super().__init__(message)
        self.errors = errors or (message,)


class ScoutItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    source: Source
    url: str = Field(pattern=r"^https?://\S+$")
    title: str = Field(min_length=1)
    published: date
    summary: str
    tags: tuple[str, ...] = ()


class Unreachable(BaseModel):
    """Diagnostic only (never written to the vault), so unknown extra fields are dropped."""

    model_config = ConfigDict(extra="ignore", frozen=True)
    source: str
    reason: str


class ScoutReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    items: tuple[ScoutItem, ...]
    unreachable: tuple[Unreachable, ...] = ()


@dataclass(frozen=True)
class ScoutResult:
    run_id: str
    report: ScoutReport
    cost_usd: float
    repairs: int


def parse_answer(answer: str) -> ScoutReport:
    """Extract the JSON object from a final answer (fenced or raw) and validate it strictly."""
    try:
        data = extract_json(answer)
    except AnswerNotJson as exc:
        raise ScoutFormatError(str(exc)) from exc
    try:
        return ScoutReport.model_validate(data)
    except ValidationError as exc:
        errors = tuple(
            f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()
        )
        raise ScoutFormatError("answer does not match the schema", errors) from exc


def _repair_prompt(error: ScoutFormatError, answer: str) -> str:
    problems = "\n".join(f"- {e}" for e in error.errors)
    return (
        "Rewrite the ANSWER below so it matches the required format exactly. Keep every "
        "fact; drop anything that cannot be made valid. Reply with ONE raw JSON object "
        "only (no code fences, no prose).\n\n"
        f"Problems found:\n{problems}\n\n"
        'Shape: {"items": [{"source", "url", "title", "published", "summary", "tags"}], '
        '"unreachable": [{"source", "reason"}]}\n'
        f"`source` must be exactly one of: {', '.join(SOURCES)}. "
        "`published` is YYYY-MM-DD. `url` starts with http:// or https://. No other fields."
        f"\n\nANSWER:\n{answer}"
    )


def run_scout(
    client: AgentRunClient,
    seen_urls: list[str],
    lookback_days: int,
    max_cost_usd: float,
    max_repairs: int = DEFAULT_MAX_REPAIRS,
    wait_seconds: float = DEFAULT_WAIT_SECONDS,
    interests: tuple[str, ...] = (),
) -> ScoutResult:
    """Start one scout run and return a validated report, repairing the format if needed.

    `interests` (the owner's profile) are passed to the scout as extra topics to watch.
    """
    payload: dict[str, object] = {
        "lookback_days": lookback_days,
        "seen_urls": seen_urls,
    }
    if interests:
        payload["owner_interests"] = list(interests)
    content = json.dumps(payload, ensure_ascii=False)
    scout_run_id, answer, cost = run_once(
        client, AGENT_ID, content, max_cost_usd, wait_seconds
    )
    for repairs in range(max_repairs + 1):
        try:
            report = parse_answer(answer)
        except ScoutFormatError as error:
            if repairs == max_repairs:
                raise
            _, answer, repair_cost = run_once(
                client,
                FORMATTER_ID,
                _repair_prompt(error, answer),
                FORMATTER_CAP_USD,
                wait_seconds,
            )
            cost += repair_cost
            continue
        return ScoutResult(
            run_id=scout_run_id, report=report, cost_usd=cost, repairs=repairs
        )
    raise AssertionError(
        "unreachable: loop always returns or raises"
    )  # pragma: no cover


def ingest_report(
    vault: Path,
    report: ScoutReport,
    ingested: datetime,
    avoid: tuple[str, ...] = (),
) -> tuple[Path, ...]:
    """Write each new item as a news note; return only the notes created by this call.

    Items matching the owner's `avoid` keywords are never written.
    """
    created: list[Path] = []
    for item in report.items:
        if is_avoided(item.model_dump(), avoid):
            continue
        if find_news(vault, news_id(item.url)) is not None:
            continue
        news = NewsItem(
            source=item.source,
            url=item.url,
            title=item.title,
            published=item.published,
            ingested=ingested,
            summary=item.summary,
            tags=item.tags,
        )
        created.append(write_news(vault, news))
    return tuple(created)
