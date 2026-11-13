import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from omniscient.agents import AgentDefinitionError, load_definitions, push_definition
from omniscient.home import packaged


@dataclass
class _Tagged:
    body: dict[str, Any]
    etag: str


class _NotFound(Exception):
    status = 404


class _Agents:
    def __init__(self, existing: dict[str, Any] | None) -> None:
        self.existing = existing
        self.created: list[dict[str, Any]] = []
        self.updated: list[tuple[str, dict[str, Any], str]] = []

    def get(self, agent_id: str) -> _Tagged:
        if self.existing is None:
            raise _NotFound()
        return _Tagged(self.existing, '"etag-1"')

    def create(self, body: dict[str, Any]) -> dict[str, Any]:
        self.created.append(body)
        return body

    def update(self, agent_id: str, body: dict[str, Any], if_match: str) -> _Tagged:
        self.updated.append((agent_id, body, if_match))
        return _Tagged(body, '"etag-2"')


class _Client:
    def __init__(self, existing: dict[str, Any] | None) -> None:
        self.agents = _Agents(existing)


BODY = {"definition": {"id": "news-scout", "version": 3, "name": "Scout"}}


def test_push_creates_agent_when_missing() -> None:
    client = _Client(existing=None)

    action = push_definition(client, BODY)

    assert action == "created"
    assert client.agents.created == [BODY]


def test_push_updates_existing_agent_with_etag() -> None:
    client = _Client(existing={"definition": {"id": "news-scout", "version": 2}})

    action = push_definition(client, BODY)

    assert action == "updated"
    assert client.agents.updated == [("news-scout", BODY, '"etag-1"')]


def test_push_skips_when_version_is_unchanged() -> None:
    client = _Client(existing={"definition": {"id": "news-scout", "version": 3}})

    assert push_definition(client, BODY) == "unchanged"
    assert client.agents.updated == []


def test_load_definitions_reads_repo_agents_dir() -> None:
    bodies = load_definitions(packaged("agents"))

    assert "news-scout" in {b["definition"]["id"] for b in bodies}


def test_load_definitions_rejects_missing_id(tmp_path: Path) -> None:
    (tmp_path / "bad.json").write_text(json.dumps({"definition": {"name": "x"}}))

    with pytest.raises(AgentDefinitionError, match="bad.json"):
        load_definitions(tmp_path)
