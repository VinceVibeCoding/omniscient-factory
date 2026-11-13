from omniscient.sense.interests import (
    is_avoided,
    matched_keywords,
    normalize,
    relevance,
    steer_news,
)


def _item(title: str, summary: str = "", tags: tuple[str, ...] = ()) -> dict:
    return {"title": title, "summary": summary, "tags": list(tags)}


def test_normalize_strips_accents_and_case() -> None:
    assert normalize("  Économie  CRÉATIVE ") == "economie creative"


def test_match_is_case_and_accent_insensitive() -> None:
    item = _item("Les LLM locaux sur la course a pied")

    assert matched_keywords(item, ("LLM locaux", "Course à pied")) == (
        "LLM locaux",
        "Course à pied",
    )


def test_match_respects_word_boundaries() -> None:
    assert matched_keywords(_item("Shipping via the API"), ("IA",)) == ()
    assert matched_keywords(_item("L'IA générative"), ("IA",)) == ("IA",)


def test_match_looks_at_summary_and_hyphenated_tags() -> None:
    item = _item("Release", "A new fintech API", ("model-release",))

    assert matched_keywords(item, ("fintech", "model release")) == (
        "fintech",
        "model release",
    )


def test_blank_keywords_and_missing_fields_never_match() -> None:
    assert matched_keywords({}, ("", "  ", "x")) == ()
    assert matched_keywords({"tags": "crypto"}, ("crypto",)) == ("crypto",)


def test_relevance_counts_distinct_interest_matches() -> None:
    item = _item("Fintech meets running")

    assert relevance(item, ("fintech", "running", "chess")) == 2.0
    assert relevance(item, ()) == 0.0


def test_is_avoided() -> None:
    assert is_avoided(_item("Crypto wallet"), ("crypto",))
    assert not is_avoided(_item("Crypto wallet"), ())


def test_steer_news_drops_avoided_and_ranks_by_relevance_stably() -> None:
    items = [
        _item("Plain A"),
        _item("Crypto news"),
        _item("Running app"),
        _item("Plain B"),
        _item("Running fintech"),
    ]

    steered = steer_news(items, interests=("running", "fintech"), avoid=("crypto",))

    assert [i["title"] for i in steered] == [
        "Running fintech",
        "Running app",
        "Plain A",
        "Plain B",
    ]


def test_steer_news_without_profile_keeps_order() -> None:
    items = [_item("B"), _item("A")]

    assert steer_news(items) == items
