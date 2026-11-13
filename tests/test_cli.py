import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
import typer
from typer.testing import CliRunner

from omniscient import cli

REAL_AGENTRUN_PROBLEM = cli._agentrun_problem

GOOD = {
    "items": [
        {
            "source": "openai",
            "url": "https://openai.com/index/x",
            "title": "X",
            "published": "2026-11-10",
            "summary": "s",
            "tags": [],
        }
    ],
    "unreachable": [],
}


class _Runs:
    def __init__(self) -> None:
        self.started: list[dict[str, Any]] = []

    def start(self, agent_id: str, body: dict[str, Any]) -> dict[str, Any]:
        self.started.append(body)
        return {"run_id": "run-9"}

    def wait(self, run_id: str, timeout: float) -> dict[str, Any]:
        return {
            "run_id": run_id,
            "status": "completed",
            "cost": {"usd": 0.0123},
            "result": {"output": None, "final_answer": json.dumps(GOOD)},
        }

    def message(self, run_id: str, body: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError("no repair expected")


class _Client:
    def __init__(self) -> None:
        self.runs = _Runs()


@pytest.fixture(autouse=True)
def _isolated_vault(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No CLI test may touch the real vault (a cycle test once wrote vault/ops/cycles.md)."""
    monkeypatch.setattr(cli, "VAULT", tmp_path_factory.mktemp("vault"))
    # Cycle tests fake every stage; the AgentRun preflight has its own tests below.
    monkeypatch.setattr(cli, "_agentrun_problem", lambda: None)


@pytest.fixture
def vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "news").mkdir()
    client = _Client()
    monkeypatch.setattr(cli, "make_client", lambda: client)
    monkeypatch.setattr(cli, "VAULT", tmp_path)
    return tmp_path


def test_ingest_writes_notes_and_logs_cost(vault: Path) -> None:
    result = CliRunner().invoke(cli.app, ["ingest"])

    assert result.exit_code == 0, result.output
    assert "1 new" in result.output
    assert len(list((vault / "news").glob("*.md"))) == 1
    assert "0.0123" in (vault / "ops" / "costs.md").read_text()


def test_ingest_reports_scout_failure_and_exits_nonzero(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _FailingRuns(_Runs):
        def wait(self, run_id: str, timeout: float) -> dict[str, Any]:
            return {
                "run_id": run_id,
                "status": "failed",
                "error": {"code": "run_failed", "message": "Credit balance is too low"},
            }

    failing = _Client()
    failing.runs = _FailingRuns()
    monkeypatch.setattr(cli, "make_client", lambda: failing)

    result = CliRunner().invoke(cli.app, ["ingest"])

    assert result.exit_code == 1
    assert "Credit balance is too low" in result.output
    assert not (vault / "ops" / "costs.md").exists()


def test_make_client_uses_env_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTRUN_API_KEY", "test-key")

    client = cli.make_client()

    assert hasattr(client, "runs")


def test_make_client_explains_missing_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("AGENTRUN_API_KEY", raising=False)
    monkeypatch.setattr(cli, "AGENTRUN_TOKEN_FILE", tmp_path / "missing")

    with pytest.raises(typer.Exit) as exited:
        cli.make_client()

    assert exited.value.exit_code == 1
    assert "AGENTRUN_API_KEY" in capsys.readouterr().err


def test_make_client_reads_the_key_from_the_home_env_file(
    isolated_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AGENTRUN_API_KEY", raising=False)
    monkeypatch.setattr(cli, "AGENTRUN_TOKEN_FILE", tmp_path / "missing")
    (isolated_home / ".env").write_text('AGENTRUN_API_KEY="from-env-file"\n')

    assert hasattr(cli.make_client(), "runs")


def test_think_without_agentrun_key_says_so_without_usage_banner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AGENTRUN_API_KEY", raising=False)
    monkeypatch.setattr(cli, "AGENTRUN_TOKEN_FILE", tmp_path / "missing")

    for command in ("think", "plan", "agents-push"):
        result = CliRunner().invoke(cli.app, [command])

        assert result.exit_code == 1, command
        assert "AgentRun n'est pas configuré" in result.output
        assert "Usage:" not in result.output
        assert result.exception is None or isinstance(result.exception, SystemExit)


def test_cycle_without_agentrun_stops_once_with_one_french_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import date

    from omniscient.ops import cycles_on

    calls: list[str] = []
    monkeypatch.setattr(cli, "_agentrun_problem", REAL_AGENTRUN_PROBLEM)
    monkeypatch.delenv("AGENTRUN_API_KEY", raising=False)
    monkeypatch.setattr(cli, "AGENTRUN_TOKEN_FILE", tmp_path / "missing")
    monkeypatch.setattr(cli, "ingest", _recorder(calls, "ingest"))

    result = CliRunner().invoke(cli.app, ["cycle"])

    assert result.exit_code == 1
    assert result.output.count("AgentRun n'est pas configuré") == 1
    assert "Cycle annulé" in result.output and "==" not in result.output
    assert "Traceback" not in result.output and calls == []
    assert cycles_on(cli._vault(), date.today()) == 0  # no cycle slot was used


def test_cycle_with_agentrun_down_says_it_is_unreachable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "_agentrun_problem", REAL_AGENTRUN_PROBLEM)
    monkeypatch.setenv("AGENTRUN_API_KEY", "k")
    monkeypatch.setattr(cli, "AGENTRUN_BASE_URL", "http://127.0.0.1:9/v2")
    monkeypatch.setattr("omniscient.agentrun_access.port_open", lambda h, p: False)

    result = CliRunner().invoke(cli.app, ["cycle"])

    assert result.exit_code == 1
    assert "ne répond pas sur 127.0.0.1:9" in result.output


def test_make_client_reads_token_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AGENTRUN_API_KEY", raising=False)
    token = tmp_path / ".token"
    token.write_text("file-key\n")
    monkeypatch.setattr(cli, "AGENTRUN_TOKEN_FILE", token)

    assert hasattr(cli.make_client(), "runs")


def test_ingest_refuses_cap_above_cycle_budget(vault: Path) -> None:
    result = CliRunner().invoke(cli.app, ["ingest", "--max-cost-usd", "999"])

    assert result.exit_code != 0
    assert "per_cycle_usd" in result.output


def test_learn_sync_mirrors_notes_for_repo_agents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _Learning:
        def notes(
            self, agent_id: str, *, status: str, cursor: str | None = None
        ) -> dict[str, Any]:
            text = "---\nname: n\n---\nfact\n"
            return {"items": [{"file": "fact.md", "text": text}], "next_cursor": None}

    class _LearnClient:
        learning = _Learning()

    monkeypatch.setattr(cli, "make_client", lambda: _LearnClient())
    monkeypatch.setattr(cli, "VAULT", tmp_path)

    result = CliRunner().invoke(cli.app, ["learn-sync"])

    assert result.exit_code == 0, result.output
    # one note per agent defined in agentrun/
    agents = len(list(cli.AGENTS_DIR.glob("*.json")))
    assert f"{agents} written" in result.output
    assert (tmp_path / "tech" / "learned" / "news-scout" / "fact.md").exists()
    assert (tmp_path / "tech" / "learned" / "scout-formatter" / "fact.md").exists()


def test_cycle_runs_every_stage_in_order_and_reports_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def ok(name: str) -> Any:
        return lambda **_: calls.append(name)

    def fails(name: str) -> Any:
        def run(**_: Any) -> None:
            calls.append(name)
            raise typer.Exit(code=1)

        return run

    def crashes(**_: Any) -> None:
        calls.append("plan")
        raise RuntimeError("boom")

    monkeypatch.setattr(cli, "ingest", ok("ingest"))
    monkeypatch.setattr(cli, "learn_sync", ok("learn-sync"))
    monkeypatch.setattr(cli, "think", fails("think"))
    monkeypatch.setattr(cli, "plan", crashes)
    monkeypatch.setattr(cli, "work", ok("work"))
    monkeypatch.setattr(cli, "grill", ok("grill"))
    monkeypatch.setattr(cli, "slack_digest", ok("slack-digest"))

    result = CliRunner().invoke(cli.app, ["cycle"])

    assert calls == [
        "ingest",
        "learn-sync",
        "think",
        "plan",
        "work",
        "grill",
        "slack-digest",
    ]
    assert result.exit_code == 1
    assert "failed stages: think, plan" in result.output


def test_slack_digest_explains_slack_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from slack_sdk.errors import SlackApiError

    def fail(*_: Any, **__: Any) -> Any:
        raise SlackApiError("failed", {"ok": False, "error": "channel_not_found"})

    monkeypatch.setattr(
        cli,
        "_slack_env",
        lambda: {
            "SLACK_BOT_TOKEN": "x",
            "SLACK_APP_TOKEN": "y",
            "SLACK_CHANNEL_ID": "C1",
        },
    )
    monkeypatch.setattr("omniscient.slack.core.post_digest", fail)

    result = CliRunner().invoke(cli.app, ["slack-digest"])

    assert result.exit_code == 1
    assert "channel_not_found" in result.output
    assert "/invite @omniscient" in result.output


def test_cycle_skips_stages_the_daily_budget_cannot_cover(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    for attr, name in [
        ("ingest", "ingest"),
        ("learn_sync", "learn-sync"),
        ("think", "think"),
        ("plan", "plan"),
        ("work", "work"),
        ("grill", "grill"),
        ("slack_digest", "slack-digest"),
    ]:
        monkeypatch.setattr(cli, attr, _recorder(calls, name))
    monkeypatch.setattr(cli, "_spent_today", lambda: 4.20)  # daily_usd is 5.00

    result = CliRunner().invoke(cli.app, ["cycle"])

    # only stages whose worst case fits in the $0.80 left run; the rest are skipped
    assert calls == ["learn-sync", "slack-digest"]
    assert "daily budget" in result.output
    assert result.exit_code == 0


def _recorder(calls: list[str], name: str) -> Any:
    def record(**_: Any) -> None:
        calls.append(name)

    return record


def test_cycle_refuses_when_daily_cycle_limit_is_reached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omniscient.ops import record_cycle

    calls: list[str] = []
    monkeypatch.setattr(cli, "VAULT", tmp_path)
    monkeypatch.setattr(cli, "ingest", _recorder(calls, "ingest"))
    record_cycle(tmp_path, datetime.now().astimezone())

    result = CliRunner().invoke(cli.app, ["cycle"])

    assert result.exit_code == 0
    assert "1/1" in result.output
    assert calls == []


def test_cycle_records_itself_in_the_cycle_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import date

    from omniscient.ops import cycles_on

    monkeypatch.setattr(cli, "VAULT", tmp_path)
    for attr, name in [
        ("ingest", "ingest"),
        ("learn_sync", "learn-sync"),
        ("think", "think"),
        ("plan", "plan"),
        ("work", "work"),
        ("grill", "grill"),
        ("slack_digest", "slack-digest"),
    ]:
        monkeypatch.setattr(cli, attr, _recorder([], name))

    CliRunner().invoke(cli.app, ["cycle"])

    assert cycles_on(tmp_path, date.today()) == 1


class _FilesClient(_Client):
    def __init__(self) -> None:
        super().__init__()
        self.files = object()


def _capture_work(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    from omniscient.work import engine

    seen: dict[str, Any] = {}

    def fake_run_afk(client: Any, vault: Path, **kwargs: Any) -> Any:
        seen.update(kwargs, client=client)
        return engine.WorkResult(kwargs["now"], (), (), (), (), 0.0)

    monkeypatch.setattr(engine, "run_afk", fake_run_afk)
    return seen


def test_work_attaches_project_files_through_the_files_api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omniscient.work.attachments import AgentRunUploader

    repos = tmp_path / "repos"
    repos.mkdir()
    (repos / "cards.md").write_text(
        "---\nname: cards\npath: /tmp/cards\npurpose: p\ngit: true\n---\n"
    )
    client = _FilesClient()
    monkeypatch.setattr(cli, "make_client", lambda: client)
    monkeypatch.setattr(cli, "REPOS", repos)
    seen = _capture_work(monkeypatch)

    result = CliRunner().invoke(cli.app, ["work"])

    assert result.exit_code == 0, result.output
    context = seen["context"]
    assert [p.name for p in context.projects] == ["cards"]
    assert context.uploader == AgentRunUploader(client.files)
    assert seen["client"] is client


def test_work_runs_without_projects_when_the_registry_is_broken(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "make_client", _FilesClient)
    monkeypatch.setattr(cli, "REPOS", tmp_path / "missing")
    seen = _capture_work(monkeypatch)

    result = CliRunner().invoke(cli.app, ["work"])

    assert result.exit_code == 0, result.output
    assert "registry" in result.output
    assert seen["context"].projects == ()


def _build_result(tmp_path: Path) -> Any:
    from omniscient.build.poc import BuildReport, BuildResult

    report = BuildReport(
        summary="A tiny demo.",
        run_command="./run.sh",
        demo_output="hello",
        question="Does it work?",
        verdict="works",
        verdict_detail="it printed hello",
        simulated="",
    )
    return BuildResult(
        tmp_path / "ideas" / "x.md",
        tmp_path / "pocs" / "x",
        "prototype/x",
        "run-b",
        0.42,
        report,
    )


@pytest.fixture
def build_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Offline build: no AgentRun, build_poc faked; returns the stems it built."""
    built: list[str] = []

    def fake_build_poc(client: Any, vault: Path, stem: str, pocs: Path, data: Path):  # type: ignore[no-untyped-def]
        built.append(stem)
        return _build_result(tmp_path)

    monkeypatch.setattr(cli, "make_client", lambda: object())
    monkeypatch.setattr(cli, "build_poc", fake_build_poc)
    monkeypatch.setattr(cli, "_spent_today", lambda: 0.0)
    return built


def test_build_command_builds_echoes_reply_and_logs_cost(build_env: list[str]) -> None:
    result = CliRunner().invoke(cli.app, ["build", "2026-11-13 my-idea"])

    assert result.exit_code == 0, result.output
    assert build_env == ["2026-11-13 my-idea"]
    assert "Prototype prêt" in result.output and "prototype/x" in result.output
    costs = (cli.VAULT / "ops" / "costs.md").read_text()
    assert "build 2026-11-13 my-idea" in costs and "0.42" in costs
    assert cli.BUILDER_ID in costs and cli.BUILDER_MODEL in costs


def test_build_refuses_when_the_daily_budget_cannot_cover_it(
    build_env: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "_spent_today", lambda: 4.20)  # daily_usd is 5.00

    result = CliRunner().invoke(cli.app, ["build", "x"])

    assert result.exit_code == 2
    assert "build refused" in result.output
    assert build_env == []


def test_build_reports_failure_and_logs_known_cap_spend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omniscient.runs import CapReached

    def capped(*args: Any) -> Any:
        raise CapReached("run-b", 1.5, 2.0)

    monkeypatch.setattr(cli, "make_client", lambda: object())
    monkeypatch.setattr(cli, "build_poc", capped)
    monkeypatch.setattr(cli, "_spent_today", lambda: 0.0)

    result = CliRunner().invoke(cli.app, ["build", "x"])

    assert result.exit_code == 1 and "build failed" in result.output
    assert "1.5" in (cli.VAULT / "ops" / "costs.md").read_text()


def test_build_failure_without_known_cost_logs_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(*args: Any) -> Any:
        raise cli.BuildError("no idea note named 'x'")

    monkeypatch.setattr(cli, "make_client", lambda: object())
    monkeypatch.setattr(cli, "build_poc", broken)
    monkeypatch.setattr(cli, "_spent_today", lambda: 0.0)

    result = CliRunner().invoke(cli.app, ["build", "x"])

    assert result.exit_code == 1 and "no idea note" in result.output
    assert not (cli.VAULT / "ops" / "costs.md").exists()


class _Web:
    def __init__(self) -> None:
        self.posts: list[dict[str, Any]] = []

    def chat_postMessage(self, **kwargs: Any) -> None:
        self.posts.append(kwargs)


def _join_build_threads() -> None:
    import threading

    for thread in threading.enumerate():
        if thread.name.startswith("build "):
            thread.join(timeout=5)


def test_launch_build_posts_the_reply_in_the_thread(build_env: list[str]) -> None:
    web = _Web()

    cli._launch_build(web, "my-idea", "C1", "1.1")
    _join_build_threads()

    assert build_env == ["my-idea"]
    assert web.posts[0]["thread_ts"] == "1.1"
    assert "Prototype prêt" in web.posts[0]["text"]
    assert not cli._BUILD_LOCK.locked()


def test_launch_build_reports_failure_in_the_thread(
    build_env: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*args: Any) -> Any:
        raise cli.BuildError("the builder produced no files")

    monkeypatch.setattr(cli, "build_poc", broken)
    web = _Web()

    cli._launch_build(web, "my-idea", "C1", "1.1")
    _join_build_threads()

    assert web.posts[0]["text"] == (
        ":x: Le prototype a échoué : the builder produced no files"
    )
    assert not cli._BUILD_LOCK.locked()


def test_launch_build_refuses_a_second_build_while_one_runs(
    build_env: list[str],
) -> None:
    web = _Web()
    assert cli._BUILD_LOCK.acquire(blocking=False)
    try:
        cli._launch_build(web, "my-idea", "C1", "1.1")
    finally:
        cli._BUILD_LOCK.release()

    assert build_env == []
    assert web.posts == [{"channel": "C1", "thread_ts": "1.1", "text": cli.BUILD_BUSY}]
