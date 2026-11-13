"""Project registry: parses repos/*.md pointer files (home-repo pattern)."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import frontmatter
import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

INDEX_FILENAME = "README.md"
MIN_PORT = 1
MAX_PORT = 65535

PortSource = Literal["config", "assumed"]


class RegistryError(Exception):
    """Raised when the registry directory or a project file is invalid."""


@dataclass(frozen=True)
class Port:
    """A port a project binds; source says whether it came from config or a default."""

    port: int
    service: str
    source: PortSource


@dataclass(frozen=True)
class Project:
    """One local project, as described by its repos/<name>.md pointer file."""

    name: str
    path: Path
    purpose: str
    stack: tuple[str, ...]
    ports: tuple[Port, ...]
    services: tuple[str, ...]
    remote: str | None
    git: bool
    agent_docs: tuple[str, ...]
    notes: str


class _PortSchema(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    port: int = Field(ge=MIN_PORT, le=MAX_PORT)
    service: str
    source: PortSource


class _ProjectSchema(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: str = Field(min_length=1)
    path: str
    purpose: str = Field(min_length=1)
    stack: list[str] = Field(default_factory=list)
    ports: list[_PortSchema] = Field(default_factory=list)
    services: list[str] = Field(default_factory=list)
    remote: str | None = None
    git: bool
    agent_docs: list[str] = Field(default_factory=list)
    notes: str = ""

    @field_validator("path")
    @classmethod
    def _path_must_be_absolute(cls, value: str) -> str:
        if not Path(value).is_absolute():
            raise ValueError("must be an absolute path")
        return value

    def to_project(self) -> Project:
        return Project(
            name=self.name,
            path=Path(self.path),
            purpose=self.purpose,
            stack=tuple(self.stack),
            ports=tuple(Port(p.port, p.service, p.source) for p in self.ports),
            services=tuple(self.services),
            remote=self.remote,
            git=self.git,
            agent_docs=tuple(self.agent_docs),
            notes=self.notes,
        )


def _describe(exc: ValidationError) -> str:
    parts = []
    for error in exc.errors():
        loc = ".".join(map(str, error["loc"]))
        parts.append(f"{loc}: {error['msg']}" if loc else error["msg"])
    return "; ".join(parts)


def _parse_file(file: Path) -> Project:
    try:
        metadata = frontmatter.load(file).metadata
    except (OSError, ValueError, yaml.YAMLError) as exc:
        raise RegistryError(f"{file.name}: cannot parse frontmatter: {exc}") from exc
    if not metadata:
        raise RegistryError(f"{file.name}: missing frontmatter")
    try:
        return _ProjectSchema.model_validate(metadata).to_project()
    except ValidationError as exc:
        raise RegistryError(f"{file.name}: invalid fields: {_describe(exc)}") from exc


def load_registry(repos_dir: Path) -> tuple[Project, ...]:
    """Load every project pointer file in repos_dir, sorted by name.

    Files are read in filename order so the first error reported is deterministic.
    """
    if not repos_dir.is_dir():
        raise RegistryError(f"registry directory not found: {repos_dir}")
    files = sorted(
        f for f in repos_dir.glob("*.md") if f.is_file() and f.name != INDEX_FILENAME
    )
    projects = tuple(sorted((_parse_file(f) for f in files), key=lambda p: p.name))
    duplicates = sorted(
        n for n, c in Counter(p.name for p in projects).items() if c > 1
    )
    if duplicates:
        raise RegistryError(f"duplicate project name: {', '.join(duplicates)}")
    return projects


def find_port_conflicts(
    projects: tuple[Project, ...], proposed: Project | None = None
) -> dict[int, tuple[str, ...]]:
    """Map each port claimed by 2+ projects to the names of those projects.

    A proposed project replaces any existing project with the same name, so
    re-validating an updated project never conflicts with its old self.
    """
    candidates = (
        projects
        if proposed is None
        else (*(p for p in projects if p.name != proposed.name), proposed)
    )
    owners: dict[int, list[str]] = defaultdict(list)
    for project in candidates:
        for port in {p.port for p in project.ports}:
            owners[port].append(project.name)
    return {
        port: tuple(names) for port, names in sorted(owners.items()) if len(names) > 1
    }
