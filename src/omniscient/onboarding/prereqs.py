"""Check (never install) the tools Omniscient relies on."""

from __future__ import annotations

import shutil
import socket
import subprocess
from collections.abc import Callable
from dataclasses import dataclass

AGENTRUN_HOST = "127.0.0.1"
AGENTRUN_PORT = 7340
CONNECT_TIMEOUT_S = 0.5
RUN_TIMEOUT_S = 10

Which = Callable[[str], str | None]
Run = Callable[[list[str]], int]
PortOpen = Callable[[str, int], bool]


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str
    required: bool


def _run(cmd: list[str]) -> int:
    try:
        done = subprocess.run(
            cmd, capture_output=True, timeout=RUN_TIMEOUT_S, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return 1
    return done.returncode


def _port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=CONNECT_TIMEOUT_S):
            return True
    except OSError:
        return False


def _tool(name: str, which: Which, required: bool, purpose: str) -> Check:
    found = which(name)
    detail = found if found else f"absent — {purpose}"
    return Check(name, found is not None, detail, required)


def _docker(which: Which, run: Run) -> Check:
    purpose = "nécessaire pour les PoC (AgentRun)"
    if which("docker") is None:
        return Check("docker", False, f"absent — {purpose}", False)
    if run(["docker", "info"]) != 0:
        return Check(
            "docker", False, f"installé mais le démon ne répond pas — {purpose}", False
        )
    return Check("docker", True, "démon joignable", False)


def _agentrun(which: Which, port_open: PortOpen) -> Check:
    if port_open(AGENTRUN_HOST, AGENTRUN_PORT):
        return Check(
            "agentrun", True, f"serveur sur {AGENTRUN_HOST}:{AGENTRUN_PORT}", False
        )
    found = which("agentrun")
    if found:
        return Check("agentrun", True, f"{found} (serveur arrêté)", False)
    return Check(
        "agentrun", False, "absent — nécessaire pour les boucles et les PoC", False
    )


def check_prereqs(
    which: Which = shutil.which, run: Run = _run, port_open: PortOpen = _port_open
) -> tuple[Check, ...]:
    return (
        _tool("uv", which, True, "installe-le : https://docs.astral.sh/uv/"),
        _tool("git", which, True, "nécessaire pour les projets et les PoC"),
        _docker(which, run),
        _tool("herdr", which, False, "nécessaire pour `omniscient studio`"),
        _tool("claude", which, False, "Claude Code, l'agent des ateliers"),
        _agentrun(which, port_open),
    )


def render_checks(checks: tuple[Check, ...]) -> str:
    lines = []
    for check in checks:
        mark = "ok " if check.ok else ("MANQUE" if check.required else "--")
        lines.append(f"  [{mark:^6}] {check.name:<9} {check.detail}")
    return "\n".join(lines)


def missing_required(checks: tuple[Check, ...]) -> tuple[Check, ...]:
    return tuple(c for c in checks if c.required and not c.ok)
