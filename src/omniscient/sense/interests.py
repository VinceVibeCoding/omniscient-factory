"""Owner interests steer the news: matching items rank higher, avoided ones are dropped.

Matching is a keyword match on a news item's title, summary and tags, insensitive to
case and accents ("Économie" matches "economie"), on word boundaries so "IA" does not
match "via". Pure functions only.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

MATCH_BOOST = 1.0


def normalize(text: str) -> str:
    """Casefold and strip accents; collapse whitespace."""
    decomposed = unicodedata.normalize("NFKD", text)
    plain = "".join(c for c in decomposed if not unicodedata.combining(c))
    return " ".join(plain.casefold().split())


def _news_text(item: Mapping[str, Any]) -> str:
    tags = item.get("tags") or ()
    if isinstance(tags, str):
        tags = (tags,)
    parts = [str(item.get("title") or ""), str(item.get("summary") or "")]
    parts.extend(str(t).replace("-", " ") for t in tags)
    return normalize(" ".join(parts))


def _pattern(keyword: str) -> re.Pattern[str] | None:
    words = normalize(keyword.replace("-", " ")).split()
    if not words:
        return None
    body = r"[\s\-]+".join(re.escape(w) for w in words)
    return re.compile(rf"(?<!\w){body}(?!\w)")


def matched_keywords(
    item: Mapping[str, Any], keywords: Iterable[str]
) -> tuple[str, ...]:
    """The keywords (as given) found in the item's title, summary or tags."""
    text = _news_text(item)
    found: list[str] = []
    for keyword in keywords:
        pattern = _pattern(keyword)
        if pattern is not None and pattern.search(text):
            found.append(keyword)
    return tuple(found)


def is_avoided(item: Mapping[str, Any], avoid: Iterable[str]) -> bool:
    return bool(matched_keywords(item, avoid))


def relevance(item: Mapping[str, Any], interests: Iterable[str]) -> float:
    """Boost: MATCH_BOOST per distinct interest the item matches (0 when none)."""
    return MATCH_BOOST * len(matched_keywords(item, interests))


def steer_news(
    items: Sequence[Mapping[str, Any]],
    interests: Sequence[str] = (),
    avoid: Sequence[str] = (),
) -> list[Mapping[str, Any]]:
    """Drop avoided items, then order by interest relevance (stable: ties keep order)."""
    kept = [item for item in items if not is_avoided(item, avoid)]
    if not interests:
        return kept
    return sorted(kept, key=lambda item: -relevance(item, interests))
