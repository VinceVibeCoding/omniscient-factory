"""Immutable note types for the shared brain (Obsidian vault)."""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, fields
from datetime import date, datetime
from typing import Literal, get_args
from urllib.parse import urldefrag

ID_LENGTH = 16
WEIGHT_TOLERANCE = 1e-6
ALLOWED_URL_SCHEMES = ("http://", "https://")

IdeaStatus = Literal["seed", "debated", "planned", "building", "shipped", "dropped"]
# showcase: a quick demo of a new model/feature release and its best use cases
# new-build: a new product or tool from combined news; upgrade: news applied to a project
IdeaKind = Literal["showcase", "new-build", "upgrade"]


class BrainError(Exception):
    """Raised when a note is invalid or cannot be read or written."""


def news_id(url: str) -> str:
    """Stable id for a news URL: sha256 of the URL without fragment or trailing slash."""
    normalized = urldefrag(url.strip()).url.rstrip("/")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:ID_LENGTH]


def _require_text(field_name: str, value: str) -> None:
    if not value.strip():
        raise BrainError(f"{field_name} must not be empty")


@dataclass(frozen=True)
class NewsItem:
    source: str
    url: str
    title: str
    published: date
    ingested: datetime
    summary: str
    tags: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_text("source", self.source)
        _require_text("title", self.title)
        if not self.url.startswith(ALLOWED_URL_SCHEMES):
            raise BrainError(f"url must be http(s): {self.url!r}")

    @property
    def id(self) -> str:
        return news_id(self.url)


@dataclass(frozen=True)
class IdeaScores:
    """Each score is 0..1. For effort, higher means more work (inverted when weighted)."""

    novelty: float
    impact: float
    feasibility: float
    fit: float
    effort: float

    def __post_init__(self) -> None:
        for f in fields(self):
            value = getattr(self, f.name)
            if not (0.0 <= value <= 1.0) or math.isnan(value):
                raise BrainError(f"{f.name} score must be within 0..1, got {value}")

    def weighted(self, weights: dict[str, float]) -> float:
        names = {f.name for f in fields(self)}
        if set(weights) != names:
            raise BrainError(f"weights must cover exactly {sorted(names)}")
        if not math.isclose(sum(weights.values()), 1.0, abs_tol=WEIGHT_TOLERANCE):
            raise BrainError("weights must sum to 1")
        values = {n: getattr(self, n) for n in names} | {"effort": 1.0 - self.effort}
        return sum(weights[n] * values[n] for n in names)


@dataclass(frozen=True)
class Idea:
    title: str
    pitch: str
    status: IdeaStatus
    scores: IdeaScores
    sources: tuple[str, ...]
    projects: tuple[str, ...]
    created: datetime
    kind: IdeaKind = "new-build"

    def __post_init__(self) -> None:
        _require_text("title", self.title)
        _require_text("pitch", self.pitch)
        if self.status not in get_args(IdeaStatus):
            raise BrainError(f"status must be one of {get_args(IdeaStatus)}")
        if self.kind not in get_args(IdeaKind):
            raise BrainError(f"kind must be one of {get_args(IdeaKind)}")
