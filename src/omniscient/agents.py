"""Agent definitions live in git (agentrun/*.json) and are pushed to AgentRun."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal, Protocol

NOT_FOUND = 404
PushAction = Literal["created", "updated", "unchanged"]


class AgentDefinitionError(Exception):
    """An agent definition file is malformed."""


class AgentsApi(Protocol):
    def get(self, agent_id: str) -> Any: ...
    def create(self, body: dict[str, Any]) -> Any: ...
    def update(self, agent_id: str, body: dict[str, Any], if_match: str) -> Any: ...


class AgentsClient(Protocol):
    @property
    def agents(self) -> AgentsApi: ...


def load_definitions(directory: Path) -> tuple[dict[str, Any], ...]:
    bodies = []
    for path in sorted(directory.glob("*.json")):
        try:
            body = json.loads(path.read_text(encoding="utf-8"))
            definition = body["definition"]
            if not definition.get("id") or not isinstance(
                definition.get("version"), int
            ):
                raise KeyError(
                    "definition.id and integer definition.version are required"
                )
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise AgentDefinitionError(f"{path.name}: {exc}") from exc
        bodies.append(body)
    return tuple(bodies)


def push_definition(client: AgentsClient, body: dict[str, Any]) -> PushAction:
    """Create the agent, or update it when the git version is newer than the server's."""
    agent_id = body["definition"]["id"]
    try:
        current = client.agents.get(agent_id)
    except (
        Exception
    ) as exc:  # the client raises its own refused error type; 404 means create
        if getattr(exc, "status", None) != NOT_FOUND:
            raise
        client.agents.create(body)
        return "created"
    server_version = current.body.get("definition", {}).get("version")
    if server_version == body["definition"]["version"]:
        return "unchanged"
    client.agents.update(agent_id, body, if_match=current.etag)
    return "updated"
