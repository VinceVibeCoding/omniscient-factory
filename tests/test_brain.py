from datetime import UTC, date, datetime
from pathlib import Path

import frontmatter
import pytest

from omniscient.brain import (
    BrainError,
    Idea,
    IdeaScores,
    NewsItem,
    find_news,
    news_id,
    read_news,
    write_idea,
    write_news,
)

INGESTED = datetime(2026, 11, 12, 13, 0, tzinfo=UTC)


def _news(**overrides: object) -> NewsItem:
    fields: dict[str, object] = {
        "source": "anthropic",
        "url": "https://www.anthropic.com/news/claude-opus-5-5",
        "title": "Introducing Claude Opus 5.5",
        "published": date(2026, 11, 3),
        "ingested": INGESTED,
        "summary": "A new frontier model.",
        "tags": ("model-release",),
    }
    fields.update(overrides)
    return NewsItem(**fields)  # type: ignore[arg-type]


def test_news_id_is_stable_16_hex_and_ignores_trailing_slash_and_fragment() -> None:
    a = news_id("https://example.com/post")
    b = news_id("https://example.com/post/#comments")

    assert a == b
    assert len(a) == 16
    assert int(a, 16) >= 0


def test_write_news_creates_readable_note_with_frontmatter(tmp_path: Path) -> None:
    # Arrange
    item = _news()

    # Act
    path = write_news(tmp_path, item)

    # Assert
    assert path.parent == tmp_path / "news"
    assert path.name.startswith("2026-11-03 introducing-claude-opus-5-5")
    post = frontmatter.load(path)
    assert post["type"] == "news"
    assert post["id"] == item.id
    assert post["source"] == "anthropic"
    assert post["published"] == "2026-11-03"
    assert "A new frontier model." in post.content


def test_write_news_is_idempotent_by_url(tmp_path: Path) -> None:
    first = write_news(tmp_path, _news())
    second = write_news(tmp_path, _news(title="Retitled"))

    assert first == second
    assert len(list((tmp_path / "news").glob("*.md"))) == 1


def test_read_news_round_trips(tmp_path: Path) -> None:
    item = _news()
    path = write_news(tmp_path, item)

    assert read_news(path) == item


def test_find_news_returns_none_when_absent(tmp_path: Path) -> None:
    assert find_news(tmp_path, "0123456789abcdef") is None


def test_news_rejects_non_http_url() -> None:
    with pytest.raises(BrainError, match="url"):
        _news(url="javascript:alert(1)")


def test_news_rejects_empty_title() -> None:
    with pytest.raises(BrainError, match="title"):
        _news(title="   ")


def test_read_news_raises_on_corrupt_note(tmp_path: Path) -> None:
    bad = tmp_path / "bad.md"
    bad.write_text("---\ntype: news\n---\n", encoding="utf-8")

    with pytest.raises(BrainError, match="bad.md"):
        read_news(bad)


def test_slug_strips_unsafe_filename_characters(tmp_path: Path) -> None:
    path = write_news(tmp_path, _news(title='GPT/6: "the" <future>?'))

    assert "/" not in path.name.removesuffix(".md")
    assert path.name.startswith("2026-11-03 gpt-6-the-future")


def test_idea_scores_validate_range() -> None:
    with pytest.raises(BrainError, match="novelty"):
        IdeaScores(novelty=1.5, impact=0.5, feasibility=0.5, fit=0.5, effort=0.5)


def test_weighted_score_inverts_effort() -> None:
    weights = {
        "novelty": 0.25,
        "impact": 0.25,
        "feasibility": 0.2,
        "fit": 0.15,
        "effort": 0.15,
    }
    scores = IdeaScores(novelty=1.0, impact=1.0, feasibility=1.0, fit=1.0, effort=0.0)

    assert scores.weighted(weights) == pytest.approx(1.0)


def test_weighted_score_rejects_weights_not_summing_to_one() -> None:
    scores = IdeaScores(novelty=1, impact=1, feasibility=1, fit=1, effort=1)

    with pytest.raises(BrainError, match="sum to 1"):
        scores.weighted(
            {
                "novelty": 0.5,
                "impact": 0.1,
                "feasibility": 0.1,
                "fit": 0.1,
                "effort": 0.1,
            }
        )


def test_write_idea_links_sources_and_projects(tmp_path: Path) -> None:
    idea = Idea(
        title="Opus 5.5 coach for weekly runs",
        pitch="Use the new model to turn FIT files into weekly coaching notes.",
        status="seed",
        scores=IdeaScores(
            novelty=0.7, impact=0.6, feasibility=0.8, fit=0.9, effort=0.3
        ),
        sources=("abc123abc123abc1",),
        projects=("run-tracker",),
        created=INGESTED,
    )

    path = write_idea(tmp_path, idea, score=0.74)

    post = frontmatter.load(path)
    assert path.parent == tmp_path / "ideas"
    assert post["status"] == "seed"
    assert post["score"] == 0.74
    assert "[[run-tracker]]" in post.content
    assert "abc123abc123abc1" in post.content


def test_write_idea_refuses_to_overwrite_existing_note(tmp_path: Path) -> None:
    idea = Idea(
        title="Same idea",
        pitch="p",
        status="seed",
        scores=IdeaScores(novelty=0, impact=0, feasibility=0, fit=0, effort=0),
        sources=(),
        projects=(),
        created=INGESTED,
    )
    write_idea(tmp_path, idea, score=0.1)

    with pytest.raises(BrainError, match="already exists"):
        write_idea(tmp_path, idea, score=0.2)


def test_idea_rejects_unknown_status() -> None:
    with pytest.raises(BrainError, match="status"):
        Idea(
            title="x",
            pitch="y",
            status="maybe",  # type: ignore[arg-type]
            scores=IdeaScores(novelty=0, impact=0, feasibility=0, fit=0, effort=0),
            sources=(),
            projects=(),
            created=INGESTED,
        )


def test_write_idea_links_known_sources_to_their_news_notes(tmp_path: Path) -> None:
    news_path = write_news(tmp_path, _news())
    idea = Idea(
        title="Linked idea",
        pitch="p",
        status="seed",
        scores=IdeaScores(novelty=0, impact=0, feasibility=0, fit=0, effort=0),
        sources=(_news().id,),
        projects=(),
        created=INGESTED,
    )

    path = write_idea(tmp_path, idea, score=0.1)

    assert f"[[{news_path.stem}]]" in frontmatter.load(path).content


def test_write_idea_records_kind(tmp_path: Path) -> None:
    idea = Idea(
        title="Demo of a model",
        pitch="p",
        status="seed",
        scores=IdeaScores(novelty=0, impact=0, feasibility=0, fit=0, effort=0),
        sources=(),
        projects=(),
        created=INGESTED,
        kind="showcase",
    )

    path = write_idea(tmp_path, idea, score=0.1)

    assert frontmatter.load(path)["kind"] == "showcase"


def test_idea_rejects_unknown_kind() -> None:
    with pytest.raises(BrainError, match="kind"):
        Idea(
            "x",
            "y",
            "seed",
            IdeaScores(novelty=0, impact=0, feasibility=0, fit=0, effort=0),
            (),
            (),
            INGESTED,
            kind="vibes",
        )  # type: ignore[arg-type]
