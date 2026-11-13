"""Typed, validated view of a home's config/omniscient.toml.

Paths: `~` is expanded; relative `vault` / `pocs` are relative to the home root
(the folder holding config/), never to the current directory.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

WEIGHT_TOLERANCE = 1e-6
_SITE_REPO = re.compile(r"^[A-Za-z0-9-]+/[A-Za-z0-9._-]+$")


class ConfigError(Exception):
    """Raised when the config file is missing or invalid."""


class _Paths(BaseModel):
    model_config = ConfigDict(extra="forbid")
    projects_root: Path
    vault: Path
    pocs: Path


class _Publish(BaseModel):
    """Optional GitHub Pages site for prototype web demos; absent means disabled."""

    model_config = ConfigDict(extra="forbid")
    site_repo: str | None = None
    site_url: str | None = None

    @field_validator("site_repo")
    @classmethod
    def _owner_slash_name(cls, value: str | None) -> str | None:
        if value is not None and not _SITE_REPO.match(value):
            raise ValueError("site_repo must look like owner/name")
        return value

    @field_validator("site_url")
    @classmethod
    def _https_with_slash(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value.startswith("https://"):
            raise ValueError("site_url must start with https://")
        return value if value.endswith("/") else value + "/"

    @property
    def enabled(self) -> bool:
        return self.site_url is not None


class _Budget(BaseModel):
    model_config = ConfigDict(extra="forbid")
    daily_usd: float = Field(gt=0)
    per_cycle_usd: float = Field(gt=0)


class _Gameplan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    wip_limit: int = Field(ge=1)
    ideas_per_cycle: int = Field(ge=1)
    promote_threshold: float = Field(ge=0, le=1)
    tracks: dict[Literal["showcase", "new-build", "upgrade"], int] = Field(
        default_factory=lambda: {"showcase": 2, "new-build": 2, "upgrade": 1}
    )


class _Schedule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cycle_cron: str
    max_cycles_per_day: int = Field(default=2, ge=0)


class _Sources(BaseModel):
    model_config = ConfigDict(extra="forbid")
    github_top_n: int = Field(ge=1)
    hn_top_n: int = Field(ge=1)
    news_lookback_days: int = Field(ge=1, le=30)


class _Scoring(BaseModel):
    model_config = ConfigDict(extra="forbid")
    weights: dict[str, float]

    @model_validator(mode="after")
    def _weights_sum_to_one(self) -> _Scoring:
        if abs(sum(self.weights.values()) - 1.0) > WEIGHT_TOLERANCE:
            raise ValueError("scoring weights must sum to 1")
        return self


class _Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    paths: _Paths
    budget: _Budget
    gameplan: _Gameplan
    scoring: _Scoring
    schedule: _Schedule
    sources: _Sources
    publish: _Publish = Field(default_factory=_Publish)


@dataclass(frozen=True)
class Settings:
    paths: _Paths
    budget: _Budget
    gameplan: _Gameplan
    schedule: _Schedule
    sources: _Sources
    scoring_weights: dict[str, float]
    publish: _Publish = field(default_factory=_Publish)


def _resolve_paths(paths: _Paths, home_root: Path) -> _Paths:
    """Expand `~`; anchor relative vault/pocs (and projects_root) at the home root."""

    def anchor(value: Path) -> Path:
        expanded = value.expanduser()
        return expanded if expanded.is_absolute() else home_root / expanded

    return _Paths(
        projects_root=anchor(paths.projects_root),
        vault=anchor(paths.vault),
        pocs=anchor(paths.pocs),
    )


def load_settings(path: Path) -> Settings:
    if not path.is_file():
        raise ConfigError(f"config not found: {path}")
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
        parsed = _Settings.model_validate(raw)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path.name}: invalid TOML: {exc}") from exc
    except ValidationError as exc:
        fields = "; ".join(
            f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()
        )
        raise ConfigError(f"{path.name}: {fields}") from exc
    home_root = path.resolve().parent.parent  # <home>/config/omniscient.toml
    return Settings(
        paths=_resolve_paths(parsed.paths, home_root),
        budget=parsed.budget,
        gameplan=parsed.gameplan,
        schedule=parsed.schedule,
        sources=parsed.sources,
        scoring_weights=dict(parsed.scoring.weights),
        publish=parsed.publish,
    )
