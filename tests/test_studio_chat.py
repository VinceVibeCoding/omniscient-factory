"""Agent choices, priming, and the `omniscient chat` REPL with a fake AgentRun client."""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

import frontmatter
import pytest

from omniscient.ops import CostRow, append_cost
from omniscient.runs import RunFailed
from omniscient.studio.agents import (
    CLAUDE,
    AgentChoice,
    available_agents,
    chat_command,
    is_chat_agent,
    prime_prompt,
)
from omniscient.studio.chat import load_agent, run_chat
from omniscient.studio.ideas import StudioError, new_seed

TODAY = date(2026, 11, 13)
CONFIG = """
[paths]
projects_root = "/tmp/p"
vault = "/tmp/v"
pocs = "/tmp/pocs"

[budget]
daily_usd = 1.00
per_cycle_usd = 0.50

[gameplan]
wip_limit = 2
ideas_per_cycle = 3
promote_threshold = 0.6

[scoring]
weights = { impact = 1.0 }

[schedule]
cycle_cron = "0 7 * * *"

[sources]
github_top_n = 5
hn_top_n = 5
news_lookback_days = 3
"""


# ---------- agents ----------


def _definition(agent_id: str, cap: object = 0.25) -> str:
    return json.dumps(
        {
            "definition": {
                "id": agent_id,
                "version": 1,
                "model": "anthropic/claude-x",
                "thinking": "medium",
                "max_cost_usd": cap,
            }
        }
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "agentrun").mkdir(parents=True)
    (root / "agentrun" / "debater.json").write_text(_definition("debater"))
    (root / "agentrun" / "visionary.json").write_text(_definition("visionary", 0.6))
    (root / "agentrun" / "griller.json").write_text(_definition("griller"))
    (root / "agentrun" / "researcher-opus.json").write_text(
        _definition("researcher-opus")
    )
    (root / "agentrun" / "broken.json").write_text("{not json")
    (root / "config").mkdir()
    (root / "config" / "omniscient.toml").write_text(CONFIG)
    return root


def test_available_agents_order(repo: Path) -> None:
    on_path = {"copilot", "cursor-agent"}
    agents = available_agents(repo, which=lambda exe: exe if exe in on_path else None)
    assert [a.key for a in agents] == [
        "claude",
        "copilot",
        "cursor",
        "or-debater",
        "or-griller",
        "or-researcher-opus",
    ]
    assert agents[0] == CLAUDE
    assert agents[1].herdr_kind == "copilot" and agents[1].kind == "herdr"
    assert agents[3].label == "Omniscient · debater (crédit API)"
    assert agents[3].kind == "omniscient" and agents[3].agent_id == "debater"


def test_available_agents_without_agentrun_dir_uses_packaged_agents(
    tmp_path: Path,
) -> None:
    agents = available_agents(tmp_path, which=lambda exe: None)

    assert agents[0] == CLAUDE
    assert "debater" in {a.agent_id for a in agents[1:]}
    assert all(a.kind == "omniscient" for a in agents[1:])


def test_claude_keeps_slash_command_others_get_french_prompt() -> None:
    stem = "2026-11-12 jean's idea"
    assert prime_prompt(CLAUDE, "riff", stem) == (
        "/omniscient-riff '2026-11-12 jean'\"'\"'s idea'"
    )
    copilot = AgentChoice("copilot", "Copilot", "herdr", "copilot", None)
    text = prime_prompt(copilot, "riff", "2026-11-12 x")
    assert "aide-moi à riffer sur cette idée" in text
    assert "`uv run omniscient idea append '2026-11-12 x'`" in text
    with pytest.raises(ValueError):
        prime_prompt(copilot, "remix", "x")


def test_chat_command_only_for_omniscient_agents() -> None:
    oracle = AgentChoice("or-x", "x", "omniscient", None, "x")
    assert (
        chat_command(oracle, "2026 a b") == "uv run omniscient chat x --idea '2026 a b'"
    )
    with pytest.raises(ValueError):
        chat_command(CLAUDE, "s")


def test_only_chat_capable_agents_are_offered_and_accepted(repo: Path) -> None:
    assert is_chat_agent("debater") and is_chat_agent("researcher-haiku")
    assert not is_chat_agent("visionary") and not is_chat_agent("researcher")
    keys = [a.key for a in available_agents(repo, which=lambda exe: None)]
    assert "or-visionary" not in keys
    with pytest.raises(StudioError, match="ne sait pas discuter"):
        load_agent(repo, "visionary")
    with pytest.raises(StudioError, match="ne sait pas discuter"):
        run_chat(repo, repo, "visionary", None, ask=lambda p: "x", say=print)


def test_load_agent_validates(repo: Path) -> None:
    agent = load_agent(repo, "debater")
    assert agent.cap_usd == 0.25 and agent.effort == "medium"
    with pytest.raises(StudioError, match="inconnu"):
        load_agent(repo, "researcher-nope")
    (repo / "agentrun" / "free.json").write_text(_definition("researcher-free", 0))
    with pytest.raises(StudioError, match="max_cost_usd"):
        load_agent(repo, "researcher-free")


# ---------- chat ----------


class FakeRuns:
    def __init__(self, answers: list[dict[str, Any]]) -> None:
        self.answers = answers
        self.started: list[tuple[str, dict[str, Any]]] = []

    def start(self, agent_id: str, body: dict[str, Any]) -> dict[str, Any]:
        self.started.append((agent_id, body))
        return {"run_id": f"run-{len(self.started)}"}

    def wait(self, run_id: str, timeout: float) -> dict[str, Any]:
        return self.answers.pop(0)


class FakeClient:
    def __init__(self, *answers: dict[str, Any]) -> None:
        self.runs = FakeRuns(list(answers))


def _done(text: str, usd: float = 0.1) -> dict[str, Any]:
    return {
        "run_id": "r",
        "status": "completed",
        "result": {"final_answer": text},
        "cost": {"usd": usd},
    }


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    v = tmp_path / "vault"
    new_seed(v, title="Alpha", pitch="Pitch Alpha.", now=datetime(2026, 11, 12, 9))
    return v


def _stem(vault: Path) -> str:
    return next((vault / "ideas").glob("*.md")).stem


def _chat(
    vault: Path, repo: Path, lines: list[str], client: FakeClient, idea: str | None
) -> list[str]:
    said: list[str] = []
    feed = iter(lines)

    def ask(prompt: str) -> str:
        try:
            return next(feed)
        except StopIteration:
            raise EOFError from None

    run_chat(
        vault,
        repo,
        "debater",
        idea,
        ask=ask,
        say=said.append,
        client_factory=lambda: client,
        today=lambda: TODAY,
    )
    return said


def test_chat_sends_message_with_note_and_records_everything(
    vault: Path, repo: Path
) -> None:
    stem = _stem(vault)
    client = FakeClient(_done("## Bonne idée\nOui."))
    said = _chat(vault, repo, ["  on  teste ?  ", "/quit"], client, stem)

    assert "jusqu'à 0.25 $ par message" in said[0]
    agent_id, body = client.runs.started[0]
    assert agent_id == "debater" and body["max_cost_usd"] == 0.25
    assert "Pitch Alpha." in body["input"]["content"]
    assert "on teste ?" in body["input"]["content"]
    assert any("### Bonne idée" in s for s in said)
    assert said[-1] == "Au revoir."
    content = frontmatter.load(vault / "ideas" / f"{stem}.md").content
    assert "## Discussion" in content
    assert "**Propriétaire :** on teste ?" in content
    assert "**Débatteur :** ### Bonne idée" in content
    costs = (vault / "ops" / "costs.md").read_text()
    assert f"chat {stem} | debater | anthropic/claude-x | medium | 0.1000" in costs


def test_chat_refuses_over_daily_budget(vault: Path, repo: Path) -> None:
    append_cost(
        vault,
        CostRow(datetime(2026, 11, 13, 8), "cycle", "council", "m", "-", 0.80),
    )
    client = FakeClient(_done("jamais"))
    said = _chat(vault, repo, ["salut"], client, None)
    assert client.runs.started == []
    assert any(s.startswith("Refusé : 0.80 $") for s in said)


def test_chat_without_idea_does_not_touch_notes(vault: Path, repo: Path) -> None:
    stem = _stem(vault)
    before = (vault / "ideas" / f"{stem}.md").read_text()
    client = FakeClient(_done("Bonjour."))
    _chat(vault, repo, ["bonjour"], client, None)
    assert "Le propriétaire t'écrit" in client.runs.started[0][1]["input"]["content"]
    assert (vault / "ideas" / f"{stem}.md").read_text() == before
    assert "| chat - | debater |" in (vault / "ops" / "costs.md").read_text()


def test_failed_run_is_reported_and_repl_continues(vault: Path, repo: Path) -> None:
    failed = {"run_id": "r", "status": "failed", "error": {"code": "x", "message": "y"}}
    client = FakeClient(failed, _done("ok"))
    said = _chat(vault, repo, ["un", "deux"], client, _stem(vault))
    assert any(s.startswith("Échec de l'envoi : run r failed") for s in said)
    assert any("debater › ok" in s for s in said)
    assert len(client.runs.started) == 2


def test_client_factory_error_is_reported(vault: Path, repo: Path) -> None:
    def no_token() -> FakeClient:
        raise RunFailed("pas de jeton")

    said: list[str] = []
    feed = iter(["salut"])
    run_chat(
        vault,
        repo,
        "debater",
        None,
        ask=lambda p: next(feed, "/quit"),
        say=said.append,
        client_factory=no_token,
        today=lambda: TODAY,
    )
    assert "Échec de l'envoi : pas de jeton" in said


def test_unknown_idea_or_agent_fails_before_any_call(vault: Path, repo: Path) -> None:
    client = FakeClient()
    with pytest.raises(StudioError):
        _chat(vault, repo, ["x"], client, "2026-01-01 nope")
    with pytest.raises(StudioError):
        run_chat(vault, repo, "ghost", None, ask=lambda p: "x", say=print)
    assert client.runs.started == []


# ---------- credit spent on failures ----------


def _costs(vault: Path) -> list[str]:
    log = vault / "ops" / "costs.md"
    if not log.exists():
        return []
    return [line for line in log.read_text().splitlines() if "| chat " in line]


def _paused(spent: float | None) -> dict[str, Any]:
    pending: dict[str, Any] = {"kind": "budget", "least_cap_usd": 0.5}
    if spent is not None:
        pending["spent_usd"] = spent
    return {"run_id": "r", "status": "input_required", "pending": pending}


@pytest.mark.parametrize(("spent", "logged"), [(0.2, "0.2000"), (None, "0.2500")])
def test_cap_reached_logs_spent_or_cap(
    vault: Path, repo: Path, spent: float | None, logged: str
) -> None:
    said = _chat(vault, repo, ["salut"], FakeClient(_paused(spent)), None)
    assert any(s.startswith("Échec de l'envoi : run r paused") for s in said)
    rows = _costs(vault)
    assert len(rows) == 1 and rows[0].endswith(f"| {logged} |")


def test_failed_run_logs_reported_cost_else_cap(vault: Path, repo: Path) -> None:
    error = {"code": "x", "message": "y"}
    with_cost = {
        "run_id": "r",
        "status": "failed",
        "error": error,
        "cost": {"usd": 0.07},
    }
    without = {"run_id": "r", "status": "failed", "error": error}
    _chat(vault, repo, ["un", "deux"], FakeClient(with_cost, without), None)
    rows = _costs(vault)
    assert [r.rsplit("|", 2)[1].strip() for r in rows] == ["0.0700", "0.2500"]


def test_client_factory_failure_logs_nothing(vault: Path, repo: Path) -> None:
    def no_token() -> FakeClient:
        raise RunFailed("pas de jeton")

    feed = iter(["salut", "/quit"])
    run_chat(
        vault,
        repo,
        "debater",
        None,
        ask=lambda p: next(feed),
        say=lambda s: None,
        client_factory=no_token,
        today=lambda: TODAY,
    )
    assert _costs(vault) == []


def test_missing_run_id_is_reported_and_cap_counted(vault: Path, repo: Path) -> None:
    class NoIdRuns(FakeRuns):
        def start(self, agent_id: str, body: dict[str, Any]) -> dict[str, Any]:
            return {}

    client = FakeClient()
    client.runs = NoIdRuns([])
    said = _chat(vault, repo, ["salut"], client, None)
    assert any(s.startswith("Échec de l'envoi : 'run_id'") for s in said)
    assert _costs(vault)[0].endswith("| 0.2500 |")


def test_cost_is_logged_before_discussion_and_reply_survives_write_failure(
    vault: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(path: Path, lines: tuple[str, ...]) -> None:
        assert _costs(vault), "cost must be logged before the discussion"
        raise OSError("disque plein")

    monkeypatch.setattr("omniscient.studio.chat.append_discussion", broken)
    said = _chat(vault, repo, ["salut"], FakeClient(_done("Coucou.")), _stem(vault))
    assert any("debater › Coucou." in s for s in said)
    assert any(s.startswith("Attention : échange non écrit") for s in said)
    assert len(_costs(vault)) == 1


def test_ctrl_c_during_send_counts_cap_and_repl_continues(
    vault: Path, repo: Path
) -> None:
    class InterruptedRuns(FakeRuns):
        def wait(self, run_id: str, timeout: float) -> dict[str, Any]:
            if len(self.started) == 1:
                raise KeyboardInterrupt
            return super().wait(run_id, timeout)

    client = FakeClient()
    client.runs = InterruptedRuns([_done("ok")])
    said = _chat(vault, repo, ["un", "deux"], client, None)
    assert any("peut encore consommer du crédit" in s and "0.25 $" in s for s in said)
    assert any("debater › ok" in s for s in said)
    assert [r.rsplit("|", 2)[1].strip() for r in _costs(vault)] == ["0.2500", "0.1000"]
