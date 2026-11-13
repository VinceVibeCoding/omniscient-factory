"""How Omniscient reaches the AgentRun server, and a French diagnosis when it can't.

The API key comes from, in order: the AGENTRUN_API_KEY environment variable, the home's
`.env` (written by `omniscient init`), then the token file the local AgentRun server
writes on first start (`~/.agentrun/.token`).
"""

from __future__ import annotations

import socket
from collections.abc import Callable, Mapping
from pathlib import Path
from urllib.parse import urlsplit

from omniscient.envfile import parse_env

API_KEY_VAR = "AGENTRUN_API_KEY"
TOKEN_FILE = Path.home() / ".agentrun" / ".token"
DEFAULT_PORT = 7340
CONNECT_TIMEOUT_S = 0.5
SETUP_HINT = (
    "Démarre le serveur AgentRun (scripts/agentrun-up.sh server, puis runner) : il crée "
    "~/.agentrun/.token. Ou mets AGENTRUN_API_KEY dans le .env de ta maison "
    "(omniscient init). Voir le README, section « AgentRun »."
)

PortOpen = Callable[[str, int], bool]


def port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=CONNECT_TIMEOUT_S):
            return True
    except OSError:
        return False


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8") if path.is_file() else ""
    except (OSError, UnicodeDecodeError):
        return ""


def resolve_api_key(
    env_file: Path, token_file: Path, environ: Mapping[str, str]
) -> str | None:
    """The AgentRun API key from the environment, the home .env or the token file."""
    for candidate in (
        environ.get(API_KEY_VAR, ""),
        parse_env(_read_text(env_file)).get(API_KEY_VAR, ""),
        _read_text(token_file),
    ):
        if candidate.strip():
            return candidate.strip()
    return None


def missing_key_message(token_file: Path) -> str:
    return (
        f"AgentRun n'est pas configuré : aucune clé API (ni {token_file}). {SETUP_HINT}"
    )


def readiness_problem(
    base_url: str,
    env_file: Path,
    token_file: Path,
    environ: Mapping[str, str],
    reachable: PortOpen | None = None,
) -> str | None:
    """None when AgentRun can be used; else ONE French sentence saying what to fix."""
    if resolve_api_key(env_file, token_file, environ) is None:
        return missing_key_message(token_file)
    parts = urlsplit(base_url)
    host = parts.hostname or "127.0.0.1"
    port = parts.port or DEFAULT_PORT
    if not (reachable or port_open)(host, port):
        return f"AgentRun ne répond pas sur {host}:{port}. {SETUP_HINT}"
    return None
