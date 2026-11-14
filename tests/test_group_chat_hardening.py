"""Security hardening of the widget chat, config store and widget download (review fixes)."""

from __future__ import annotations

import json
import logging
import sys
import threading
import time
import urllib.request
from email.message import Message as HttpHeaders
from pathlib import Path
from typing import Any

import pytest

from omniscient.group import api, api_chat, config_store
from omniscient.group import chat as ch
from omniscient.group import chat_runner as cr
from omniscient.group import widget_release as wr
from omniscient.group.chat_runner import RunnerEvent, TurnHandle, TurnOutcome, TurnSpec
from omniscient.group.chat_store import write_private
from omniscient.home import Home

# ---- chat runner -----------------------------------------------------------------------


def test_argv_pins_the_default_permission_mode() -> None:
    argv = cr.build_argv(["claude"], resume="abc", max_budget_usd=2)
    i = argv.index("--permission-mode")
    assert argv[i + 1] == "default"
    assert argv.count("--permission-mode") == 1


NOISY_CLAUDE = r"""
import json, sys
sys.stdin.read()
print("x" * 5000, flush=True)
print(json.dumps({"type": "system", "subtype": "init", "session_id": "s1"}), flush=True)
print(json.dumps({"type": "result", "is_error": False, "result": "ok",
                  "total_cost_usd": 0.01, "session_id": "s1"}), flush=True)
"""

CRASHING_CLAUDE = r"""
import sys
sys.stdin.read()
sys.stderr.write("boom at /secret/place token=sk-ant-abcdefghijklmnopqrstuvwxyz0123\n")
sys.exit(4)
"""


def _script(tmp_path: Path, name: str, body: str) -> list[str]:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return [sys.executable, str(path)]


def test_oversized_stdout_lines_are_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(cr, "MAX_LINE_CHARS", 1000)
    argv = cr.build_argv(_script(tmp_path, "noisy.py", NOISY_CLAUDE))
    seen: list[RunnerEvent] = []
    with caplog.at_level(logging.WARNING, logger="omniscient.group.chat_runner"):
        outcome = cr.run_turn(TurnSpec(argv, tmp_path, "hi"), seen.append, TurnHandle())
    assert outcome.ok and outcome.session_id == "s1"
    assert [e.kind for e in seen] == ["init", "result"]
    assert any("too long" in r.getMessage() for r in caplog.records)


def test_failure_detail_goes_to_the_log_not_the_widget(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    argv = cr.build_argv(_script(tmp_path, "crash.py", CRASHING_CLAUDE))
    with caplog.at_level(logging.WARNING, logger="omniscient.group.chat_runner"):
        outcome = cr.run_turn(
            TurnSpec(argv, tmp_path, "x"), lambda e: None, TurnHandle()
        )
    assert not outcome.ok
    assert "boom" not in outcome.error and "/secret/place" not in outcome.error
    assert "4" in outcome.error and "journal" in outcome.error
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "boom" in logged and "sk-ant-abcdefghijklmnopqrstuvwxyz0123" not in logged


# ---- chat manager ----------------------------------------------------------------------


class Recorder:
    def __init__(self, text: str = "ok") -> None:
        self.specs: list[TurnSpec] = []
        self.text = text

    def __call__(
        self, spec: TurnSpec, on_event: Any, handle: TurnHandle
    ) -> TurnOutcome:
        self.specs.append(spec)
        on_event(RunnerEvent("text", text=self.text))
        return TurnOutcome(True, "sess-1", 0.01)


@pytest.fixture
def home(isolated_home: Path) -> Home:
    return Home(isolated_home)


def _manager(
    home: Home, runner: Any, events: list | None = None, **kw: Any
) -> ch.ChatManager:
    sink = events if events is not None else []
    return ch.ChatManager(home, lambda k, d: sink.append((k, d)), runner=runner, **kw)


def test_default_budget_cap_is_passed(home: Home) -> None:
    runner = Recorder()
    mgr = _manager(home, runner)
    mgr.join(mgr.create(home.root.name, "salut"), 5)
    argv = list(runner.specs[0].argv)
    assert argv[argv.index("--max-budget-usd") + 1] == f"{ch.DEFAULT_MAX_BUDGET_USD:g}"
    unlimited = _manager(home, Recorder(), max_budget_usd=None)
    runner2 = Recorder()
    unlimited._runner = runner2
    unlimited.join(unlimited.create(home.root.name, "salut"), 5)
    assert "--max-budget-usd" not in runner2.specs[0].argv


def test_turns_per_hour_are_capped(home: Home) -> None:
    now = [1000.0]
    mgr = _manager(home, Recorder(), max_turns_per_hour=2, clock=lambda: now[0])
    first = mgr.create(home.root.name, "un")
    mgr.join(first, 5)
    mgr.send(first, "deux")
    mgr.join(first, 5)
    with pytest.raises(ch.ChatError) as capped:
        mgr.send(first, "trois")
    assert capped.value.status == 429 and "heure" in capped.value.message
    now[0] += 3601
    mgr.send(first, "trois")
    mgr.join(first, 5)


def test_long_assistant_messages_are_clipped(home: Home) -> None:
    events: list = []
    mgr = _manager(home, Recorder(text="a" * 50_000), events)
    chat_id = mgr.create(home.root.name, "salut")
    mgr.join(chat_id, 5)
    _, detail = mgr.get(chat_id)
    text = detail["messages"][1]["text"]
    assert len(text) <= ch.MAX_MESSAGE_CHARS and text.endswith("…")
    sent = [d["text"] for k, d in events if d.get("kind") == "text"]
    assert sent and all(len(t) <= ch.MAX_MESSAGE_CHARS for t in sent)


def test_claude_binary_is_resolved_once(
    home: Home, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(ch.shutil, "which", lambda name: f"/opt/bin/{name}")
    runner = Recorder()
    mgr = _manager(home, runner)
    mgr.join(mgr.create(home.root.name, "salut"), 5)
    assert runner.specs[0].argv[0] == "/opt/bin/claude"
    monkeypatch.setattr(ch.shutil, "which", lambda name: None)
    with caplog.at_level(logging.WARNING, logger="omniscient.group.chat"):
        missing = _manager(home, Recorder())
    assert missing._claude[0] == "claude"
    assert any("claude" in r.getMessage() for r in caplog.records)


def test_write_private_never_follows_a_planted_tmp_symlink(tmp_path: Path) -> None:
    victim = tmp_path / "victim.txt"
    victim.write_text("keep me", encoding="utf-8")
    target = tmp_path / "chat.json"
    (tmp_path / "chat.tmp").symlink_to(victim)
    write_private(target, "{}")
    assert victim.read_text(encoding="utf-8") == "keep me"
    assert target.read_text(encoding="utf-8") == "{}"
    assert (target.stat().st_mode & 0o777) == 0o600


# ---- API -------------------------------------------------------------------------------


def test_limit_rejects_unicode_digits() -> None:
    with pytest.raises(api.ApiError) as bad:
        api_chat._limit("²")
    assert bad.value.status == 400


def test_chat_routes_accept_a_full_non_ascii_text() -> None:
    limits = {r.name: r.max_body for r in api.ROUTES}
    worst = len(json.dumps({"text": "😀" * ch.MAX_TEXT}, ensure_ascii=False).encode())
    assert limits["chat_create"] >= worst + 1024
    assert limits["chat_send"] >= worst + 1024


# ---- config store ----------------------------------------------------------------------


def _paths(tmp_path: Path) -> config_store.ConfigPaths:
    return config_store.ConfigPaths(
        tmp_path / "cfg" / "config.json", tmp_path / "settings.json"
    )


def test_corrupt_config_is_not_overwritten(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    paths.config.parent.mkdir()
    paths.config.write_text('{"theme": "terminal", "mine": 1', encoding="utf-8")
    with pytest.raises(config_store.ConfigError) as refused:
        config_store.update(paths, {"osd_view": "gauge"})
    assert refused.value.status == 500
    assert paths.config.read_text(encoding="utf-8").endswith('"mine": 1')


def test_concurrent_updates_do_not_lose_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _paths(tmp_path)
    real = config_store.write_atomic

    def slow_write(path: Path, text: str) -> None:
        time.sleep(0.05)
        real(path, text)

    monkeypatch.setattr(config_store, "write_atomic", slow_write)
    threads = [
        threading.Thread(target=config_store.update, args=(paths, patch))
        for patch in ({"theme": "terminal"}, {"osd_view": "gauge"})
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    stored = json.loads(paths.config.read_text(encoding="utf-8"))
    assert stored == {"theme": "terminal", "osd_view": "gauge"}


# ---- widget release --------------------------------------------------------------------


def _redirect(url: str) -> Any:
    handler = wr.SafeRedirectHandler()
    request = urllib.request.Request("https://github.com/x")
    return handler.redirect_request(request, None, 302, "Found", HttpHeaders(), url)


def test_redirects_stay_on_https_github_hosts() -> None:
    assert _redirect("https://objects.githubusercontent.com/a") is not None
    for bad in ("http://objects.githubusercontent.com/a", "https://evil.example/a"):
        with pytest.raises(wr.ReleaseError):
            _redirect(bad)
    with pytest.raises(wr.ReleaseError):
        wr.https_fetch("https://evil.example/file", 10)


@pytest.mark.parametrize(
    "name",
    ["..\\evil-setup.exe", "a\\b-setup.exe", "..-setup.exe", ".", "x-setup.exe.bat"],
)
def test_unsafe_asset_names_are_refused(name: str) -> None:
    assert not wr.safe_asset_name(name, installer=True)


def test_safe_asset_names() -> None:
    assert wr.safe_asset_name("Omnigroup_0.2.0_x64-setup.exe", installer=True)
    assert wr.safe_asset_name("SHA256SUMS", installer=False)


EXE = b"MZ fake installer"
NAME = "Omnigroup_0.2.0_x64-setup.exe"


def _release() -> wr.Release:
    base = f"{wr.DOWNLOAD_PREFIX}widget-v0.2.0/"
    return wr.Release(
        "widget-v0.2.0",
        wr.Asset(NAME, base + NAME),
        wr.Asset("SHA256SUMS", base + "SHA256SUMS"),
    )


def _fetch(url: str, max_bytes: int) -> bytes:
    if url.endswith("SHA256SUMS"):
        import hashlib

        return f"{hashlib.sha256(EXE).hexdigest()}  {NAME}\n".encode()
    return EXE


def test_download_refuses_a_symlinked_target(tmp_path: Path) -> None:
    victim = tmp_path / "victim"
    victim.write_bytes(b"keep")
    (tmp_path / NAME).symlink_to(victim)
    with pytest.raises(wr.ReleaseError):
        wr.download_verified(_release(), tmp_path, _fetch, attest=lambda p: "verified")
    assert victim.read_bytes() == b"keep"
    assert not [p for p in tmp_path.iterdir() if p.name.endswith(".part")]


def test_download_refuses_a_failed_attestation(tmp_path: Path) -> None:
    def failing(path: Path) -> str:
        raise wr.ReleaseError("attestation refusée")

    with pytest.raises(wr.ReleaseError):
        wr.download_verified(_release(), tmp_path, _fetch, attest=failing)
    assert list(tmp_path.iterdir()) == []
    ok = wr.download_verified(_release(), tmp_path, _fetch, attest=lambda p: "verified")
    assert ok.read_bytes() == EXE


def test_verify_attestation_with_and_without_gh(tmp_path: Path) -> None:
    file = tmp_path / NAME
    file.write_bytes(EXE)
    assert wr.verify_attestation(file, which=lambda n: None) == "unavailable"
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> tuple[int, str]:
        calls.append(argv)
        return 0, "ok"

    assert (
        wr.verify_attestation(file, which=lambda n: "/usr/bin/gh", runner=runner)
        == "verified"
    )
    assert calls[0][:3] == ["/usr/bin/gh", "attestation", "verify"]
    assert "--repo" in calls[0] and wr.REPO in calls[0]
    with pytest.raises(wr.ReleaseError):
        wr.verify_attestation(
            file, which=lambda n: "/usr/bin/gh", runner=lambda a: (1, "no")
        )
