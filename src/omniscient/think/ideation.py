"""Think loop: the visionary proposes ideas from news, the council debates and scores them.

Every idea is validated before it reaches the vault: it must cite news ids that exist
and projects that are in the registry. Ideas that fail are dropped and reported, never
written. The council's verdict sets the idea's status.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

import frontmatter
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from omniscient import i18n
from omniscient.brain import BrainError, Idea, IdeaKind, IdeaScores, write_idea
from omniscient.prompting import OwnerProfile, interests_block
from omniscient.runs import (
    DEFAULT_WAIT_SECONDS,
    AgentRunClient,
    AnswerNotJson,
    extract_json,
    run_once,
)
from omniscient.sense.interests import matched_keywords, steer_news

VISIONARY_ID = "visionary"
COUNCIL_ID = "council"
VISIONARY_CAP_USD = 0.75
COUNCIL_CAP_USD = 0.75
CONTEXT_NEWS_LIMIT = 40
RELEASE_TAGS = frozenset({"model-release", "api", "sdk", "feature"})
TRACK_GUIDE = (
    "Track meanings:\n"
    "- showcase: a new model or feature release -> a quick demo of it in action plus 2-3 "
    "concrete use cases (what the first prototype shows). `projects` may be empty.\n"
    "- new-build: combine several news items into a new product or tool, not tied to "
    "the owner's projects. `projects` is usually empty; it may be a brand-new "
    "project the owner has no repo for yet.\n"
    "- upgrade: apply a news item to ONE of the owner's projects; name it in `projects`."
)


class ThinkFailed(Exception):
    """The Think loop could not run to completion."""


class _VisionIdea(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    title: str = Field(min_length=3, max_length=120)
    pitch: str = Field(min_length=20)
    why_now: str = Field(min_length=5)
    sources: tuple[str, ...] = Field(min_length=1)
    projects: tuple[str, ...] = ()
    kind: IdeaKind = "new-build"


class _Scores(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    novelty: float = Field(ge=0, le=1)
    impact: float = Field(ge=0, le=1)
    feasibility: float = Field(ge=0, le=1)
    fit: float = Field(ge=0, le=1)
    effort: float = Field(ge=0, le=1)


class _Review(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    title: str
    challenge: str
    simplest_version: str
    risks: tuple[str, ...] = ()
    scores: _Scores
    verdict: Literal["pursue", "reshape", "drop"]


@dataclass(frozen=True)
class ThinkResult:
    written: tuple[Path, ...]
    dropped: tuple[str, ...]
    cost_usd: float


def _news_notes(vault: Path) -> list[dict[str, Any]]:
    notes = [frontmatter.load(p).metadata for p in (vault / "news").glob("*.md")]
    return sorted(notes, key=lambda m: str(m.get("published", "")), reverse=True)


def _is_release(meta: dict[str, Any]) -> bool:
    return bool(RELEASE_TAGS & set(meta.get("tags") or []))


def _apply_tracks(
    ideas: list[_VisionIdea], tracks: dict[str, int] | None
) -> tuple[list[_VisionIdea], list[str]]:
    """Keep at most `tracks[kind]` ideas per kind, in the visionary's order."""
    if not tracks:
        return ideas, []
    kept: list[_VisionIdea] = []
    dropped: list[str] = []
    used: dict[str, int] = {}
    for idea in ideas:
        if used.get(idea.kind, 0) >= tracks.get(idea.kind, 0):
            dropped.append(f"{idea.title}: over the {idea.kind} track quota")
            continue
        used[idea.kind] = used.get(idea.kind, 0) + 1
        kept.append(idea)
    return kept, dropped


def build_context(
    vault: Path,
    projects: tuple[str, ...],
    existing_ideas: tuple[str, ...],
    limit: int = CONTEXT_NEWS_LIMIT,
    project_purposes: dict[str, str] | None = None,
    profile: OwnerProfile | None = None,
) -> str:
    """Render the brain's current state as the agents' shared context.

    With a profile, news matching `avoid` is left out and news matching the owner's
    interests is picked first and flagged.
    """
    purposes = project_purposes or {}
    interests = profile.interests if profile else ()
    avoid = profile.avoid if profile else ()
    steered = steer_news(_news_notes(vault), interests, avoid)[:limit]
    notes = sorted(steered, key=lambda m: not _is_release(m))
    news = "\n".join(_news_line(m, interests) for m in notes)
    owned = "\n".join(f"- {p}: {purposes.get(p, '')}".rstrip(": ") for p in projects)
    ideas = "\n".join(f"- {t}" for t in existing_ideas) or "- none yet"
    return (
        f"## Recent news (id in brackets; releases first)\n{news}\n\n"
        f"## The owner's projects\n{owned}\n\n"
        f"## Ideas already in the brain (do not repeat)\n{ideas}"
    )


def _news_line(meta: Mapping[str, Any], interests: tuple[str, ...]) -> str:
    matched = matched_keywords(meta, interests)
    flag = f" [interests: {', '.join(matched)}]" if matched else ""
    return (
        f"- [{meta.get('id')}] {meta.get('published')} ({meta.get('source')}) "
        f"{meta.get('title')}: {meta.get('summary', '')} "
        f"[tags: {', '.join(meta.get('tags') or []) or 'none'}]{flag} <{meta.get('url')}>"
    )


def _visionary_prompt(
    context: str,
    max_ideas: int,
    tracks: dict[str, int] | None = None,
    profile: OwnerProfile | None = None,
) -> str:
    quota = (
        "\n".join(f"- {kind}: {count}" for kind, count in tracks.items())
        if tracks
        else f"- any mix, up to {max_ideas} in total"
    )
    owner = interests_block(profile)
    owner_section = f"{owner}\n\n" if owner else ""
    return (
        f"{context}\n\n## Tracks for this cycle (ideas per track)\n{quota}\n\n"
        f"{TRACK_GUIDE}\n\n{owner_section}Propose up to {max_ideas} ideas. Answer with ONE raw JSON "
        'object: {"ideas": [{"kind": "showcase"|"new-build"|"upgrade", "title", "pitch", '
        '"why_now", "sources": [news ids], "projects": [project names, may be empty]}]}'
    )


def _council_prompt(context: str, ideas: list[_VisionIdea]) -> str:
    payload = json.dumps([i.model_dump() for i in ideas], ensure_ascii=False)
    return (
        f"{context}\n\n## Ideas to review\n{payload}\n\nReview every idea. Answer with ONE "
        'raw JSON object: {"reviews": [{"title" (exactly as given), "challenge", '
        '"simplest_version", "risks": [..], "scores": {"novelty", "impact", "feasibility", '
        '"fit", "effort"} each 0..1 (effort: 1 = a lot of work), '
        '"verdict": "pursue" | "reshape" | "drop"}]}'
    )


def _parse(answer: str, key: str, who: str) -> list[Any]:
    try:
        data = extract_json(answer)
    except AnswerNotJson as exc:
        raise ThinkFailed(f"{who} answer unusable: {exc}") from exc
    value = data.get(key)
    if not isinstance(value, list):
        raise ThinkFailed(f"{who} answer has no '{key}' list")
    return value


def _valid_ideas(
    raw: list[Any], news_ids: set[str], projects: tuple[str, ...]
) -> tuple[list[_VisionIdea], list[str]]:
    ideas: list[_VisionIdea] = []
    dropped: list[str] = []
    for entry in raw:
        try:
            idea = _VisionIdea.model_validate(entry)
        except ValidationError as exc:
            dropped.append(
                f"malformed idea {str(entry)[:60]!r}: {exc.error_count()} errors"
            )
            continue
        if idea.kind == "upgrade" and not idea.projects:
            dropped.append(f"{idea.title}: an upgrade must name a project")
            continue
        unknown_news = set(idea.sources) - news_ids
        unknown_projects = set(idea.projects) - set(projects)
        if unknown_news:
            dropped.append(f"{idea.title}: unknown news ids {sorted(unknown_news)}")
        elif unknown_projects:
            dropped.append(f"{idea.title}: unknown projects {sorted(unknown_projects)}")
        else:
            ideas.append(idea)
    return ideas, dropped


def _council_markdown(review: _Review) -> str:
    risks = "\n".join(f"- {r}" for r in review.risks) or i18n.NO_RISK
    return (
        f"{i18n.L_VERDICT} {i18n.VERDICT_FR[review.verdict]}\n\n"
        f"{i18n.L_CHALLENGER} {review.challenge}\n\n"
        f"{i18n.L_SIMPLIFIER} {review.simplest_version}\n\n"
        f"{i18n.L_RISKS}\n{risks}"
    )


def _reviews_by_title(raw: list[Any]) -> dict[str, _Review]:
    reviews: dict[str, _Review] = {}
    for entry in raw:
        try:
            review = _Review.model_validate(entry)
        except ValidationError:
            continue
        reviews[review.title.strip().casefold()] = review
    return reviews


def run_think(
    client: AgentRunClient,
    vault: Path,
    projects: tuple[str, ...],
    weights: dict[str, float],
    max_ideas: int,
    budget_usd: float,
    now: datetime,
    project_purposes: dict[str, str] | None = None,
    wait_seconds: float = DEFAULT_WAIT_SECONDS,
    tracks: dict[str, int] | None = None,
    profile: OwnerProfile | None = None,
) -> ThinkResult:
    """One Think cycle: visionary, then council, then validated idea notes.

    `profile` (omniscient.profile.Profile) steers news selection and asks the
    visionary for ideas tied to the owner's interests; None = no steering.
    """
    news_ids = {str(m.get("id")) for m in _news_notes(vault)}
    if not news_ids:
        raise ThinkFailed("no news in the vault: run `omniscient ingest` first")
    existing = tuple(
        str(frontmatter.load(p).get("title")) for p in (vault / "ideas").glob("*.md")
    )
    context = build_context(
        vault, projects, existing, project_purposes=project_purposes, profile=profile
    )

    _, answer, cost = run_once(
        client,
        VISIONARY_ID,
        _visionary_prompt(context, max_ideas, tracks, profile),
        min(VISIONARY_CAP_USD, budget_usd),
        wait_seconds,
    )
    ideas, dropped = _valid_ideas(
        _parse(answer, "ideas", "visionary"), news_ids, projects
    )
    ideas, over_quota = _apply_tracks(ideas, tracks)
    dropped += over_quota
    ideas = ideas[:max_ideas]
    if not ideas:
        return ThinkResult((), tuple(dropped), cost)
    if cost + COUNCIL_CAP_USD > budget_usd:
        raise ThinkFailed(
            f"budget: ${cost:.4f} spent, council cap ${COUNCIL_CAP_USD} "
            f"would exceed ${budget_usd}"
        )

    _, answer, council_cost = run_once(
        client,
        COUNCIL_ID,
        _council_prompt(context, ideas),
        COUNCIL_CAP_USD,
        wait_seconds,
    )
    cost += council_cost
    reviews = _reviews_by_title(_parse(answer, "reviews", "council"))
    written, write_dropped = _write_reviewed(vault, ideas, reviews, weights, now)
    return ThinkResult(written, tuple(dropped) + write_dropped, cost)


def finish_think(
    vault: Path,
    visionary_answer: str,
    council_answer: str,
    projects: tuple[str, ...],
    weights: dict[str, float],
    max_ideas: int,
    now: datetime,
    tracks: dict[str, int] | None = None,
) -> ThinkResult:
    """Complete a cycle from runs that already finished (durable runs outlive a crash)."""
    news_ids = {str(m.get("id")) for m in _news_notes(vault)}
    ideas, dropped = _valid_ideas(
        _parse(visionary_answer, "ideas", "visionary"), news_ids, projects
    )
    ideas, over_quota = _apply_tracks(ideas, tracks)
    dropped += over_quota
    reviews = _reviews_by_title(_parse(council_answer, "reviews", "council"))
    written, write_dropped = _write_reviewed(
        vault, ideas[:max_ideas], reviews, weights, now
    )
    return ThinkResult(written, tuple(dropped) + write_dropped, 0.0)


def _write_reviewed(
    vault: Path,
    ideas: list[_VisionIdea],
    reviews: dict[str, _Review],
    weights: dict[str, float],
    now: datetime,
) -> tuple[tuple[Path, ...], tuple[str, ...]]:
    written: list[Path] = []
    dropped: list[str] = []
    for idea in ideas:
        review = reviews.get(idea.title.strip().casefold())
        if review is None:
            dropped.append(f"{idea.title}: no council review")
            continue
        scores = IdeaScores(**review.scores.model_dump())
        note = Idea(
            title=idea.title,
            pitch=f"{idea.pitch}\n\n{i18n.WHY_NOW} {idea.why_now}",
            status="dropped" if review.verdict == "drop" else "debated",
            scores=scores,
            sources=idea.sources,
            projects=idea.projects,
            created=now,
            kind=idea.kind,
        )
        try:
            written.append(
                write_idea(
                    vault, note, scores.weighted(weights), _council_markdown(review)
                )
            )
        except BrainError as exc:
            dropped.append(f"{idea.title}: {exc}")
    return tuple(written), tuple(dropped)
