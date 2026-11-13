import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import frontmatter
import pytest

from omniscient.brain import NewsItem, write_news
from omniscient.think.ideation import (
    COUNCIL_ID,
    VISIONARY_ID,
    ThinkFailed,
    build_context,
    run_think,
)


@dataclass(frozen=True)
class _Profile:
    name: str | None = None
    language: str = "en"
    interests: tuple[str, ...] = ()
    avoid: tuple[str, ...] = ()
    github_user: str | None = None


NOW = datetime(2026, 11, 12, 21, 0, tzinfo=UTC)
WEIGHTS = {
    "novelty": 0.25,
    "impact": 0.25,
    "feasibility": 0.2,
    "fit": 0.15,
    "effort": 0.15,
}
PROJECTS = ("run-tracker", "weekly-report")


def _seed_news(vault: Path) -> list[str]:
    items = [
        NewsItem(
            "anthropic",
            "https://a.com/fable",
            "Fable 5.2",
            date(2026, 11, 9),
            NOW,
            "New top model.",
            ("model-release",),
        ),
        NewsItem(
            "github",
            "https://github.com/vercel-labs/agent-browser",
            "agent-browser",
            date(2026, 11, 12),
            NOW,
            "Browser CLI for agents.",
            ("agents",),
        ),
    ]
    for item in items:
        write_news(vault, item)
    return [i.id for i in items]


def _idea(ids: list[str], **overrides: Any) -> dict[str, Any]:
    idea = {
        "title": "Run coach from FIT files",
        "pitch": "Combine Fable 5.2 reasoning with agent-browser to turn watch data into coaching.",
        "why_now": "Fable 5.2 makes long-horizon analysis cheap enough.",
        "sources": ids,
        "projects": ["run-tracker"],
        "kind": "upgrade",
    }
    return {**idea, **overrides}


def _review(title: str, verdict: str = "pursue") -> dict[str, Any]:
    return {
        "title": title,
        "challenge": "Watch data is noisy.",
        "simplest_version": "Weekly email from last 7 runs.",
        "risks": ["Health data privacy"],
        "scores": {
            "novelty": 0.7,
            "impact": 0.6,
            "feasibility": 0.8,
            "fit": 0.9,
            "effort": 0.3,
        },
        "verdict": verdict,
    }


class _Runs:
    def __init__(self, answers: dict[str, list[str]]) -> None:
        self.answers = answers
        self.started: list[dict[str, Any]] = []

    def start(self, agent_id: str, body: dict[str, Any]) -> dict[str, Any]:
        self.started.append({"agent_id": agent_id, **body})
        return {"run_id": f"{agent_id}-{len(self.started)}"}

    def wait(self, run_id: str, timeout: float) -> dict[str, Any]:
        agent_id = run_id.rsplit("-", 1)[0]
        return {
            "run_id": run_id,
            "status": "completed",
            "cost": {"usd": 0.10},
            "result": {"output": None, "final_answer": self.answers[agent_id].pop(0)},
        }


class _Client:
    def __init__(self, answers: dict[str, list[str]]) -> None:
        self.runs = _Runs(answers)


def _client(ideas: list[dict[str, Any]], reviews: list[dict[str, Any]]) -> _Client:
    return _Client(
        {
            VISIONARY_ID: [json.dumps({"ideas": ideas})],
            COUNCIL_ID: [json.dumps({"reviews": reviews})],
        }
    )


def test_build_context_lists_recent_news_projects_and_existing_ideas(
    tmp_path: Path,
) -> None:
    ids = _seed_news(tmp_path)

    context = build_context(tmp_path, PROJECTS, existing_ideas=("Old idea",), limit=10)

    assert all(i in context for i in ids)
    assert "run-tracker" in context
    assert "Old idea" in context


def test_run_think_writes_scored_debated_ideas(tmp_path: Path) -> None:
    ids = _seed_news(tmp_path)
    client = _client([_idea(ids)], [_review("Run coach from FIT files")])

    result = run_think(
        client, tmp_path, PROJECTS, WEIGHTS, max_ideas=5, budget_usd=2.0, now=NOW
    )

    assert len(result.written) == 1
    post = frontmatter.load(result.written[0])
    assert post["status"] == "debated"
    assert post["score"] == pytest.approx(
        0.25 * 0.7 + 0.25 * 0.6 + 0.2 * 0.8 + 0.15 * 0.9 + 0.15 * 0.7
    )
    assert "Weekly email from last 7 runs." in post.content
    assert "Health data privacy" in post.content
    assert result.cost_usd == pytest.approx(0.20)
    assert [s["agent_id"] for s in client.runs.started] == [VISIONARY_ID, COUNCIL_ID]


def test_run_think_drops_ideas_with_unknown_sources_or_projects(tmp_path: Path) -> None:
    ids = _seed_news(tmp_path)
    ideas = [
        _idea(ids),
        _idea(["0000000000000000"], title="Hallucinated source"),
        _idea(ids, title="Unknown project", projects=["not-a-project"]),
    ]
    client = _client(ideas, [_review("Run coach from FIT files")])

    result = run_think(
        client, tmp_path, PROJECTS, WEIGHTS, max_ideas=5, budget_usd=2.0, now=NOW
    )

    assert len(result.written) == 1
    reasons = " ".join(result.dropped)
    assert "Hallucinated source" in reasons and "unknown news" in reasons
    assert "Unknown project" in reasons and "unknown project" in reasons


def test_run_think_marks_council_drop_verdict_as_dropped(tmp_path: Path) -> None:
    ids = _seed_news(tmp_path)
    client = _client(
        [_idea(ids)], [_review("Run coach from FIT files", verdict="drop")]
    )

    result = run_think(
        client, tmp_path, PROJECTS, WEIGHTS, max_ideas=5, budget_usd=2.0, now=NOW
    )

    assert frontmatter.load(result.written[0])["status"] == "dropped"


def test_run_think_caps_ideas_to_max(tmp_path: Path) -> None:
    ids = _seed_news(tmp_path)
    ideas = [_idea(ids, title=f"Idea {n}") for n in range(4)]
    reviews = [_review(f"Idea {n}") for n in range(4)]
    client = _client(ideas, reviews)

    result = run_think(
        client, tmp_path, PROJECTS, WEIGHTS, max_ideas=2, budget_usd=2.0, now=NOW
    )

    assert len(result.written) == 2


def test_run_think_skips_ideas_the_council_did_not_review(tmp_path: Path) -> None:
    ids = _seed_news(tmp_path)
    client = _client([_idea(ids)], [])

    result = run_think(
        client, tmp_path, PROJECTS, WEIGHTS, max_ideas=5, budget_usd=2.0, now=NOW
    )

    assert result.written == ()
    assert "no council review" in " ".join(result.dropped)


def test_run_think_refuses_council_when_budget_is_spent(tmp_path: Path) -> None:
    ids = _seed_news(tmp_path)
    client = _client([_idea(ids)], [_review("Run coach from FIT files")])

    with pytest.raises(ThinkFailed, match="budget"):
        run_think(
            client, tmp_path, PROJECTS, WEIGHTS, max_ideas=5, budget_usd=0.15, now=NOW
        )

    assert [s["agent_id"] for s in client.runs.started] == [VISIONARY_ID]


def test_run_think_fails_loudly_on_non_json_visionary_answer(tmp_path: Path) -> None:
    _seed_news(tmp_path)
    client = _Client({VISIONARY_ID: ["I have some thoughts..."], COUNCIL_ID: []})

    with pytest.raises(ThinkFailed, match="visionary"):
        run_think(
            client, tmp_path, PROJECTS, WEIGHTS, max_ideas=5, budget_usd=2.0, now=NOW
        )


def test_run_think_needs_news(tmp_path: Path) -> None:
    client = _client([], [])

    with pytest.raises(ThinkFailed, match="no news"):
        run_think(
            client, tmp_path, PROJECTS, WEIGHTS, max_ideas=5, budget_usd=2.0, now=NOW
        )


def test_finish_think_writes_ideas_from_already_finished_runs(tmp_path: Path) -> None:
    from omniscient.think.ideation import finish_think

    ids = _seed_news(tmp_path)
    visionary = json.dumps({"ideas": [_idea(ids)]})
    council = json.dumps({"reviews": [_review("Run coach from FIT files")]})

    result = finish_think(tmp_path, visionary, council, PROJECTS, WEIGHTS, 5, NOW)

    assert len(result.written) == 1
    assert result.cost_usd == 0.0


TRACKS = {"showcase": 2, "new-build": 2, "upgrade": 1}


def test_run_think_enforces_track_quotas_and_records_kind(tmp_path: Path) -> None:
    ids = _seed_news(tmp_path)
    ideas = [
        _idea(ids, title="Demo A", kind="showcase", projects=[]),
        _idea(ids, title="Demo B", kind="showcase", projects=[]),
        _idea(ids, title="Demo C", kind="showcase", projects=[]),
        _idea(ids, title="Upgrade A", kind="upgrade"),
        _idea(ids, title="Upgrade B", kind="upgrade"),
    ]
    reviews = [_review(i["title"]) for i in ideas]
    client = _client(ideas, reviews)

    result = run_think(
        client,
        tmp_path,
        PROJECTS,
        WEIGHTS,
        max_ideas=5,
        budget_usd=2.0,
        now=NOW,
        tracks=TRACKS,
    )

    titles = sorted(frontmatter.load(p)["title"] for p in result.written)
    assert titles == ["Demo A", "Demo B", "Upgrade A"]
    kinds = {
        frontmatter.load(p)["title"]: frontmatter.load(p)["kind"]
        for p in result.written
    }
    assert kinds["Demo A"] == "showcase" and kinds["Upgrade A"] == "upgrade"
    assert "track quota" in " ".join(result.dropped)


def test_run_think_drops_upgrade_without_a_project(tmp_path: Path) -> None:
    ids = _seed_news(tmp_path)
    client = _client([_idea(ids, kind="upgrade", projects=[])], [])

    result = run_think(
        client,
        tmp_path,
        PROJECTS,
        WEIGHTS,
        max_ideas=5,
        budget_usd=2.0,
        now=NOW,
        tracks=TRACKS,
    )

    assert result.written == ()
    assert "upgrade must name a project" in " ".join(result.dropped)


def test_build_context_puts_releases_first_with_tags_and_urls(tmp_path: Path) -> None:
    _seed_news(tmp_path)

    context = build_context(tmp_path, PROJECTS, existing_ideas=(), limit=10)

    news = context.split("## The owner's projects")[0]
    assert news.index("Fable 5.2") < news.index("agent-browser")  # model-release first
    assert "https://a.com/fable" in news and "model-release" in news


def test_visionary_prompt_explains_tracks_and_quotas(tmp_path: Path) -> None:
    ids = _seed_news(tmp_path)
    client = _client([_idea(ids)], [_review("Run coach from FIT files")])

    run_think(
        client,
        tmp_path,
        PROJECTS,
        WEIGHTS,
        max_ideas=5,
        budget_usd=2.0,
        now=NOW,
        tracks=TRACKS,
    )

    prompt = client.runs.started[0]["input"]["content"]
    assert (
        "showcase: 2" in prompt and "new-build: 2" in prompt and "upgrade: 1" in prompt
    )


def test_visionary_prompt_has_no_interests_block_without_profile(
    tmp_path: Path,
) -> None:
    ids = _seed_news(tmp_path)
    client = _client([_idea(ids)], [_review("Run coach from FIT files")])

    run_think(client, tmp_path, PROJECTS, WEIGHTS, max_ideas=5, budget_usd=2.0, now=NOW)

    assert "owner's interests" not in client.runs.started[0]["input"]["content"]


def test_visionary_prompt_asks_for_an_idea_tied_to_owner_interests(
    tmp_path: Path,
) -> None:
    ids = _seed_news(tmp_path)
    client = _client([_idea(ids)], [_review("Run coach from FIT files")])
    profile = _Profile(language="fr", interests=("running", "local LLMs"))

    run_think(
        client,
        tmp_path,
        PROJECTS,
        WEIGHTS,
        max_ideas=5,
        budget_usd=2.0,
        now=NOW,
        tracks=TRACKS,
        profile=profile,
    )

    prompt = client.runs.started[0]["input"]["content"]
    assert "## The owner's interests" in prompt
    assert "running, local LLMs" in prompt
    assert "at least ONE idea must be tied" in prompt
    assert "no existing repo" in prompt
    assert "Write every human-readable value in French" in prompt
    assert "showcase: 2" in prompt  # quotas kept


def test_build_context_drops_avoided_news_and_ranks_interests_first(
    tmp_path: Path,
) -> None:
    _seed_news(tmp_path)
    write_news(
        tmp_path,
        NewsItem(
            "hackernews",
            "https://hn.com/coin",
            "New crypto wallet",
            date(2026, 11, 12),
            NOW,
            "A wallet.",
            ("crypto",),
        ),
    )
    profile = _Profile(interests=("Browser",), avoid=("Crypto",))

    context = build_context(
        tmp_path, PROJECTS, existing_ideas=(), limit=1, profile=profile
    )

    news = context.split("## The owner's projects")[0]
    assert "crypto wallet" not in news
    assert "agent-browser" in news and "[interests: Browser]" in news
    assert "Fable 5.2" not in news  # limit=1: the interest match wins the slot
