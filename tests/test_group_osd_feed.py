"""OSD feed: pricing, transcript tail (tok/min, ticker, subagents), limits, worker."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from omniscient.group import api_osd, osd, pricing
from omniscient.group import transcripts_live as tl
from omniscient.group.config_store import ConfigPaths

NOW = 1_800_000_000.0  # fixed clock


def iso(ts: float) -> str:
    return osd.iso(ts)


def turn_line(
    msg_id: str,
    at: float,
    out: int = 100,
    model: str = "claude-opus-5-5",
    tool: str | None = None,
    **usage: int,
) -> str:
    content: list[dict[str, Any]] = [{"type": "text", "text": "ok"}]
    if tool:
        content.append({"type": "tool_use", "name": tool, "input": {}})
    entry = {
        "type": "assistant",
        "timestamp": iso(at),
        "message": {
            "id": msg_id,
            "role": "assistant",
            "model": model,
            "content": content,
            "usage": {"input_tokens": 10, "output_tokens": out, **usage},
        },
    }
    return json.dumps(entry) + "\n"


def write(path: Path, *lines: str, mtime: float = NOW) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.writelines(lines)
    os.utime(path, (mtime, mtime))
    return path


# ---- pricing ----------------------------------------------------------------------------


def test_prices_use_the_longest_model_prefix() -> None:
    assert pricing.rates_for("claude-opus-5-5") == pricing.RATES["claude-opus-5-5"]
    assert pricing.rates_for("claude-opus-5-5[1m]") == pricing.RATES["claude-opus-5-5"]
    assert pricing.rates_for("claude-opus-5") == pricing.RATES["claude-opus-5"]
    assert (
        pricing.rates_for("claude-haiku-4-5-20251001")
        == pricing.RATES["claude-haiku-4-5"]
    )
    assert pricing.rates_for("gpt-5") is None
    assert pricing.rates_for(None) is None


def test_turn_cost_counts_cache_reads_and_both_write_ttls() -> None:
    usage = {
        "input_tokens": 1_000_000,
        "output_tokens": 1_000_000,
        "cache_read_input_tokens": 1_000_000,
        "cache_creation_input_tokens": 2_000_000,
        "cache_creation": {"ephemeral_1h_input_tokens": 1_000_000},
    }
    # opus 5.5: 4 + 20 + 0.20 + 4*1.25 + 4*2
    assert pricing.turn_cost("claude-opus-5-5", usage) == pytest.approx(37.2)
    # haiku 4.5 has no published cache-read rate here: 0.1 x input
    assert pricing.turn_cost(
        "claude-haiku-4-5", {"cache_read_input_tokens": 1_000_000}
    ) == pytest.approx(0.1)
    assert pricing.turn_cost("unknown", usage) is None
    assert (
        pricing.turn_cost(
            "claude-opus-5-5", {"input_tokens": -5, "output_tokens": True}
        )
        == 0.0
    )


# ---- transcript tail ---------------------------------------------------------------------


def test_tail_merges_lines_of_one_message_and_reads_only_new_bytes(
    tmp_path: Path,
) -> None:
    f = write(
        tmp_path / "projects/p/s1.jsonl",
        turn_line("m1", NOW - 30, out=5),
        turn_line("m1", NOW - 29, out=120, tool="Bash"),
        "not json\n",
        json.dumps({"type": "user", "message": {"role": "user", "content": "hi"}})
        + "\n",
    )
    tail = tl.TranscriptTail(tmp_path)
    tail.scan(NOW, since=NOW - 3600)
    turns = tail.turns()
    assert [(t.msg_id, t.output_tokens, t.tool, t.at) for t in turns] == [
        ("m1", 120, "Bash", NOW - 30)
    ]
    write(f, turn_line("m2", NOW - 10, out=60))
    tail.scan(NOW, since=NOW - 3600)
    assert [t.msg_id for t in tail.turns()] == ["m1", "m2"]


def test_tail_waits_for_a_complete_line(tmp_path: Path) -> None:
    f = tmp_path / "projects/p/s.jsonl"
    line = turn_line("m1", NOW - 5)
    write(f, line[:40])
    tail = tl.TranscriptTail(tmp_path)
    tail.scan(NOW, since=0)
    assert tail.turns() == []
    write(f, line[40:])
    tail.scan(NOW, since=0)
    assert [t.msg_id for t in tail.turns()] == ["m1"]


def test_tail_reads_subagent_transcripts_and_skips_old_files(tmp_path: Path) -> None:
    write(
        tmp_path / "projects/p/s/subagents/workflows/w/agent-a.jsonl",
        turn_line("sub", NOW - 20),
    )
    write(
        tmp_path / "projects/p/old.jsonl",
        turn_line("old", NOW - 99_999),
        mtime=NOW - 99_999,
    )
    write(
        tmp_path / "projects/p/s/subagents/journal.jsonl", turn_line("journal", NOW - 5)
    )
    tail = tl.TranscriptTail(tmp_path)
    tail.scan(NOW, since=NOW - 3600)
    assert [t.msg_id for t in tail.turns()] == ["sub"]
    assert tl.count_active_subagents(tmp_path / "projects", NOW) == 1
    assert tl.count_active_subagents(tmp_path / "projects", NOW + 120) == 0


def test_tail_without_projects_folder_is_empty(tmp_path: Path) -> None:
    tail = tl.TranscriptTail(tmp_path / "nowhere")
    tail.scan(NOW, since=0)
    assert tail.turns() == []


def test_live_rate() -> None:
    t = lambda at, out: tl.Turn(at, f"m{at}", "m", out, None, "")
    assert tl.live_rate([], NOW) == tl.Live(False, 0.0)
    assert tl.live_rate([t(NOW - 200, 900)], NOW) == tl.Live(False, 0.0)  # quiet > 90 s
    live = tl.live_rate([t(NOW - 120, 600), t(NOW - 10, 600)], NOW)
    assert live == tl.Live(True, 600.0)  # 1200 tokens over 2 minutes


# ---- limits + snapshot -----------------------------------------------------------------


def state(tmp_path: Path) -> osd.OsdState:
    paths = ConfigPaths(
        tmp_path / "cfg/omnigroup/config.json", tmp_path / "claude/settings.json"
    )
    return osd.OsdState(
        state_dir=tmp_path / "state", claude_root=tmp_path / "claude", paths=paths
    )


STATUS = {
    "rate_limits": {
        "five_hour": {"used_percentage": 8, "resets_at": NOW + 3600},
        "seven_day": {"used_percentage": 51.04, "resets_at": NOW + 86400},
    }
}


def test_limits_are_parsed_persisted_and_reloaded(tmp_path: Path) -> None:
    s = state(tmp_path)
    assert s.record_limits(STATUS) is True
    assert s.record_limits(STATUS) is False  # unchanged
    assert (
        s.record_limits({"rate_limits": {"five_hour": {"used_percentage": "x"}}})
        is False
    )
    saved = tmp_path / "state" / osd.LIMITS_FILE
    assert oct(saved.stat().st_mode & 0o777) == "0o600"
    again = state(tmp_path)
    assert again.limits == s.limits
    assert again.limits["seven_day"] == {
        "used_pct": 51.0,
        "resets_at": iso(NOW + 86400),
    }


def test_corrupt_limits_file_is_ignored(tmp_path: Path) -> None:
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / osd.LIMITS_FILE).write_text("{broken")
    assert state(tmp_path).limits == {}


def test_snapshot_shape(tmp_path: Path) -> None:
    s = state(tmp_path)
    s.record_limits({"rate_limits": {"five_hour": STATUS["rate_limits"]["five_hour"]}})
    write(
        tmp_path / "claude/projects/p/s.jsonl",
        turn_line("a", NOW - 60, out=300, tool="Edit"),
        turn_line("b", NOW - 5, out=300, model="other"),
    )
    snap = s.snapshot(NOW)
    assert snap["limits"] == [
        {
            "id": "five_hour",
            "label": "Session",
            "used_pct": 8.0,
            "resets_at": iso(NOW + 3600),
        }
    ]
    assert snap["live"]["is_live"] is True
    assert [i["msg_id"] for i in snap["ticker"]] == ["a", "b"]
    assert snap["ticker"][0]["tool"] == "Edit" and snap["ticker"][1]["cost_usd"] is None
    assert snap["today_cost_usd"] == pytest.approx(snap["ticker"][0]["cost_usd"])
    assert snap["subagents"] == 0 and snap["updated_at"] == iso(NOW)


def test_worker_publishes_osd_on_change_and_config_on_file_change(
    tmp_path: Path,
) -> None:
    s = state(tmp_path)
    events: list[tuple[str, dict]] = []
    worker = osd.OsdWorker(s, lambda kind, data: events.append((kind, data)))
    worker.tick(NOW)
    worker.tick(NOW + 2)  # same content, newer updated_at: nothing
    assert [k for k, _ in events] == ["osd"]
    s.record_limits(STATUS)
    s.paths.config.parent.mkdir(parents=True)
    s.paths.config.write_text('{"theme": "terminal"}')
    worker.tick(NOW + 4)
    assert [k for k, _ in events] == ["osd", "osd", "config"]
    assert events[-1][1]["config"]["theme"] == "terminal"


def test_worker_run_survives_a_failing_tick(tmp_path: Path) -> None:
    stop = threading.Event()
    calls: list[int] = []

    class Boom(osd.OsdWorker):
        def tick(self, now: float | None = None) -> None:
            calls.append(1)
            stop.set()
            raise RuntimeError("bad transcript")

    Boom(state(tmp_path), lambda *_: None, tick_s=0.01).run(stop)
    assert calls == [1]


# ---- API handlers ----------------------------------------------------------------------


def fake_app(tmp_path: Path) -> tuple[Any, list]:
    published: list[tuple[str, dict]] = []
    store = SimpleNamespace(_publish=lambda kind, data: published.append((kind, data)))
    return SimpleNamespace(osd=state(tmp_path), store=store), published


def test_api_get_osd_and_config(tmp_path: Path) -> None:
    app, _ = fake_app(tmp_path)
    status, snap = api_osd.get_osd(app, {}, {}, {})
    assert status == 200 and set(snap) >= {"limits", "live", "subagents", "ticker"}
    status, view = api_osd.get_config(app, {}, {}, {})
    assert view["config"] == {
        "theme": "default",
        "osd_view": "bars",
        "show_ticker": True,
    }


def test_api_put_config_publishes_and_maps_errors(tmp_path: Path) -> None:
    from omniscient.group.api import ApiError

    app, published = fake_app(tmp_path)
    status, view = api_osd.put_config(app, {}, {}, {"osd_view": "gauge"})
    assert status == 200 and view["config"]["osd_view"] == "gauge"
    assert published == [("config", view)]
    with pytest.raises(ApiError) as bad:
        api_osd.put_config(app, {}, {}, {"theme": "neon"})
    assert bad.value.status == 400
    app.osd.paths.settings.parent.mkdir(parents=True)
    app.osd.paths.settings.write_text(
        json.dumps({"env": {"OMNIGROUP_THEME": "dashboard"}})
    )
    with pytest.raises(ApiError) as locked:
        api_osd.put_config(app, {}, {}, {"theme": "terminal"})
    assert locked.value.status == 409


def test_api_without_osd_state_is_503(tmp_path: Path) -> None:
    from omniscient.group.api import ApiError

    with pytest.raises(ApiError) as err:
        api_osd.get_osd(SimpleNamespace(store=None), {}, {}, {})
    assert err.value.status == 503
