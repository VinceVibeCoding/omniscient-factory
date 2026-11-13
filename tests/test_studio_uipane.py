"""The studio UI pane: dev server (security + live reload), run reading, the Textual pane."""

from __future__ import annotations

import http.client
import os
import socket
import time
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import frontmatter
import pytest
from textual.widgets import Log, Static

from omniscient.build.poc import RUN_ID_KEY
from omniscient.studio import buildwatch
from omniscient.studio.buildwatch import (
    KEEP_EVENTS,
    build_log_path,
    format_event,
    launch_build,
    parse_run,
    read_timeline,
    tail_lines,
)
from omniscient.studio.devserver import (
    HOST,
    RELOAD_SCRIPT,
    VERSION_PATH,
    DevServer,
    DevServerError,
    default_port,
    inject_reload,
    open_in_browser,
    resolve_request,
)
from omniscient.studio.ideas import StudioError, new_seed
from omniscient.studio.uipane import (
    CONFIRM_BUILD,
    UiPaneApp,
    format_elapsed,
    load_info,
    run_ui,
    state_line,
    take_snapshot,
)

T0 = datetime(2026, 11, 13, 9, tzinfo=UTC)
SIZE = (100, 30)
RUN_ID = "run-1"


# ---------- fixtures ----------


def _idea(vault: Path, title: str = "Pluie locale", **meta: Any) -> str:
    path = new_seed(vault, title=title, pitch="Une preuve de pluie.", now=T0)
    post = frontmatter.load(path)
    for key, value in meta.items():
        post[key] = value
    path.write_text(frontmatter.dumps(post) + "\n", encoding="utf-8")
    return path.stem


def _web(repo: Path, stem: str, html: str = "<html><body>salut</body></html>") -> Path:
    slug = stem.split(" ", 1)[-1]
    web = repo / "pocs" / slug / "web"
    web.mkdir(parents=True)
    (web / "index.html").write_text(html, encoding="utf-8")
    return web


def _run(status: str = "working", usd: float = 0.41) -> dict[str, Any]:
    return {
        "run_id": RUN_ID,
        "status": status,
        "created_at": "2026-11-13T09:00:00Z",
        "ended_at": None,
        "cost": {"usd": usd, "cap_usd": 1.5},
    }


def _row(n: int, kind: str = "tool_use") -> dict[str, Any]:
    return {"timestamp": "2026-11-13T09:01:00Z", "kind": kind, "summary": f"étape {n}"}


class FakeSource:
    def __init__(
        self,
        run: dict[str, Any] | None = None,
        pages: list[dict[str, Any]] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.run = run or _run()
        self.pages = (
            pages if pages is not None else [{"rows": [_row(1)], "reset": True}]
        )
        self.error = error
        self.afters: list[int | None] = []

    def get(self, run_id: str) -> Mapping[str, Any]:
        if self.error is not None:
            raise self.error
        return self.run

    def timeline(self, run_id: str, after: int | None) -> Mapping[str, Any]:
        self.afters.append(after)
        index = min(len(self.afters) - 1, len(self.pages) - 1)
        return self.pages[index] if self.pages else {"rows": []}


def _get(url_port: int, path: str, method: str = "GET") -> tuple[int, bytes]:
    conn = http.client.HTTPConnection(HOST, url_port, timeout=5)
    try:
        conn.request(method, path)
        resp = conn.getresponse()
        return resp.status, resp.read()
    finally:
        conn.close()


@pytest.fixture
def served(tmp_path: Path) -> Iterator[tuple[DevServer, Path]]:
    web = tmp_path / "web"
    (web / "sub").mkdir(parents=True)
    (web / "index.html").write_text("<html><body><h1>ok</h1></body></html>", "utf-8")
    (web / "app.js").write_text("console.log(1)", "utf-8")
    (web / "sub" / "page.html").write_text("<p>no body tag</p>", "utf-8")
    (tmp_path / "secret.txt").write_text("TOP SECRET", "utf-8")
    (web / "escape").symlink_to(tmp_path / "secret.txt")
    (web / "escape-dir").symlink_to(tmp_path)
    server = DevServer(web, port=None)
    server.start()
    yield server, web
    server.stop()


def _port(server: DevServer) -> int:
    return int(server.url.rsplit(":", 1)[1].strip("/"))


# ---------- devserver ----------


def test_default_port_is_stable_and_in_range() -> None:
    ports = {default_port(f"idee-{n}") for n in range(300)}
    assert all(8700 <= p < 8900 for p in ports)
    assert default_port("abc") == default_port("abc")
    assert len(ports) > 50


def test_server_binds_loopback_only(served: tuple[DevServer, Path]) -> None:
    server, _ = served
    assert server._server is not None
    assert server._server.server_address[0] == "127.0.0.1"
    assert server.url.startswith("http://localhost:")


def test_html_gets_the_reload_script_and_assets_do_not(
    served: tuple[DevServer, Path],
) -> None:
    server, _ = served
    status, body = _get(_port(server), "/")
    assert status == 200
    assert RELOAD_SCRIPT in body and body.index(RELOAD_SCRIPT) < body.index(b"</body>")
    status, body = _get(_port(server), "/app.js")
    assert status == 200 and body == b"console.log(1)"
    status, body = _get(_port(server), "/sub/page.html")
    assert body.endswith(RELOAD_SCRIPT)


def test_head_sends_no_body(served: tuple[DevServer, Path]) -> None:
    server, _ = served
    status, body = _get(_port(server), "/", method="HEAD")
    assert status == 200 and body == b""


@pytest.mark.parametrize(
    "path",
    [
        "/../secret.txt",
        "/%2e%2e/secret.txt",
        "/%2E%2E%2Fsecret.txt",
        "/sub/../../secret.txt",
        "/..%5csecret.txt",
        "//etc/passwd",
        "/%2Fetc%2Fpasswd",
        "/escape",
        "/escape-dir/secret.txt",
        "/index.html%00.js",
        "/sub/",  # a folder without index.html: no listing
        "/missing.html",
    ],
)
def test_nothing_outside_web_dir_is_served(
    served: tuple[DevServer, Path], path: str
) -> None:
    server, _ = served
    status, body = _get(_port(server), path)
    assert status == 404
    assert b"SECRET" not in body and b"root:" not in body


def test_resolve_request_stays_inside(tmp_path: Path) -> None:
    (tmp_path / "w").mkdir()
    (tmp_path / "w" / "index.html").write_text("x", "utf-8")
    (tmp_path / "out.txt").write_text("x", "utf-8")
    assert (
        resolve_request(tmp_path / "w", "/")
        == (tmp_path / "w" / "index.html").resolve()
    )
    assert resolve_request(tmp_path / "w", "/?q=1") is not None
    assert resolve_request(tmp_path / "w", "/../out.txt") is None


def test_version_changes_when_a_file_changes(served: tuple[DevServer, Path]) -> None:
    server, web = served
    _, before = _get(_port(server), VERSION_PATH)
    later = time.time() + 5
    os.utime(web / "app.js", (later, later))
    _, after = _get(_port(server), VERSION_PATH)
    assert before != after and after.isdigit()


def test_busy_port_falls_back_to_a_free_one(tmp_path: Path) -> None:
    blocker = socket.socket()
    blocker.bind((HOST, 0))
    blocker.listen()
    busy = blocker.getsockname()[1]
    server = DevServer(tmp_path, port=busy)
    try:
        url = server.start()
        assert f":{busy}/" not in url
        assert server.start() == url  # idempotent
    finally:
        server.stop()
        blocker.close()
    assert not server.running
    server.stop()  # stopping twice is fine


def test_missing_web_dir_and_unstarted_url_raise(tmp_path: Path) -> None:
    with pytest.raises(DevServerError):
        DevServer(tmp_path / "nope")
    with pytest.raises(DevServerError):
        _ = DevServer(tmp_path).url


def test_inject_reload_without_body() -> None:
    assert inject_reload(b"<p>x</p>") == b"<p>x</p>" + RELOAD_SCRIPT
    assert inject_reload(b"<BODY>x</BODY>").startswith(b"<BODY>x" + RELOAD_SCRIPT)


def test_open_in_browser_prefers_windows_then_xdg() -> None:
    spawned: list[list[str]] = []
    paths = {"explorer.exe": "/mnt/c/explorer.exe", "xdg-open": "/usr/bin/xdg-open"}
    assert open_in_browser("http://x/", paths.get, spawned.append)
    assert spawned == [["/mnt/c/explorer.exe", "http://x/"]]

    spawned.clear()
    assert open_in_browser("http://x/", {"xdg-open": "/x"}.get, spawned.append)
    assert spawned == [["/x", "http://x/"]]


def test_open_in_browser_never_fails() -> None:
    def broken(argv: list[str]) -> None:
        raise OSError("no")

    assert not open_in_browser("http://x/", lambda name: None)
    assert not open_in_browser("http://x/", lambda name: f"/bin/{name}", broken)


# ---------- buildwatch ----------


def test_parse_run_reads_status_cost_and_times() -> None:
    run = parse_run(_run("completed", usd=0.5))
    assert run.finished and run.cost_usd == 0.5 and run.cap_usd == 1.5
    assert run.created_at == datetime(2026, 11, 13, 9, tzinfo=UTC)
    odd = parse_run({"cost": {"usd": True}, "created_at": "nope"})
    assert odd.status == "unknown" and odd.cost_usd == 0.0 and odd.created_at is None


def test_format_event_skips_noise_and_truncates() -> None:
    assert format_event({"kind": "usage", "summary": "Cost so far"}) is None
    assert format_event({"kind": "user_message", "summary": "prompt"}) is None
    line = format_event({"kind": "tool_use", "summary": "Ran\n`ls`" + "x" * 300})
    assert line is not None and line.startswith("--:--:--  tool_use")
    assert "Ran `ls`" in line and len(line) <= buildwatch.EVENT_WIDTH


def test_read_timeline_pages_forward_and_resets() -> None:
    pages = [
        {"rows": [_row(1), _row(2)], "reset": True, "has_more": True, "next_after": 10},
        {"rows": [_row(3, "usage"), _row(4)], "has_more": False, "next_after": 20},
    ]
    source = FakeSource(pages=pages)
    lines, reset, cursor = read_timeline(source, RUN_ID, None)
    assert reset and cursor == 20 and source.afters == [None, 10]
    assert [ln.rsplit(" ", 1)[1] for ln in lines] == ["1", "2", "4"]


def test_read_timeline_keeps_the_cursor_when_nothing_new() -> None:
    source = FakeSource(pages=[{"rows": [], "has_more": False, "next_after": None}])
    assert read_timeline(source, RUN_ID, 42) == ((), False, 42)


def test_tail_lines_returns_complete_lines_only(tmp_path: Path) -> None:
    log = tmp_path / "b.log"
    assert tail_lines(log, 0) == ((), 0)
    log.write_bytes(b"un\ndeux\ntro")
    lines, offset = tail_lines(log, 0)
    assert lines == ("un", "deux") and offset == 8
    with log.open("ab") as handle:
        handle.write(b"is\n")
    assert tail_lines(log, offset) == (("trois",), 14)


def test_launch_build_runs_the_cli_without_a_shell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[list[str], dict[str, Any]]] = []
    monkeypatch.setattr(
        buildwatch.subprocess, "Popen", lambda argv, **kw: calls.append((argv, kw))
    )
    stem = "2026-11-13 it's; rm -rf ~"
    log = build_log_path(tmp_path, "pluie")
    launch_build(tmp_path, stem, log)
    argv, kw = calls[0]
    assert argv == ["uv", "run", "omniscient", "build", stem]
    assert kw["cwd"] == tmp_path and kw["start_new_session"] and "shell" not in kw
    assert log == tmp_path / ".omniscient" / "builds" / "pluie.log" and log.is_file()


# ---------- pane model ----------


def test_load_info_and_unknown_stem(tmp_path: Path) -> None:
    vault, repo = tmp_path / "vault", tmp_path
    stem = _idea(vault, status="building", **{RUN_ID_KEY: RUN_ID})
    _web(repo, stem)
    (repo / "pocs" / stem.split(" ", 1)[1] / "README.md").write_text(
        "# Pluie\n\nLance ./run.sh\n", "utf-8"
    )
    info = load_info(vault, repo, stem)
    assert info.run_id == RUN_ID and info.has_web and info.status == "building"
    assert info.readme == ("Pluie", "Lance ./run.sh")
    with pytest.raises(StudioError):
        load_info(vault, repo, "../../etc/passwd")
    with pytest.raises(StudioError):
        run_ui(vault, repo, "nope")


def test_state_line_three_states(tmp_path: Path) -> None:
    vault, repo = tmp_path / "vault", tmp_path
    stem = _idea(vault, status="building", **{RUN_ID_KEY: RUN_ID})
    info = load_info(vault, repo, stem)
    now = T0 + timedelta(minutes=2, seconds=14)
    assert (
        state_line(info, parse_run(_run()), now)
        == "● en construction 2m14s · $0.41/1.50"
    )
    assert "indisponible" in state_line(info, None, now)
    assert state_line(info, parse_run(_run("completed")), now) == (
        "○ pas encore de prototype"
    )
    _web(repo, stem)
    ready = load_info(vault, repo, stem)
    assert state_line(ready, parse_run(_run("failed")), now).startswith(
        "✓ prototype prêt"
    )
    done = _idea(vault, "Autre", poc_verdict="partial")
    assert state_line(load_info(vault, repo, done), None, now) == (
        "✓ prototype prêt · verdict partiel"
    )


def test_format_elapsed() -> None:
    assert format_elapsed(134) == "2m14s"
    assert format_elapsed(3720) == "1h02m"
    assert format_elapsed(-5) == "0m00s"


def test_snapshot_reads_the_run_only_while_building(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    idle = _idea(vault)
    source = FakeSource()
    assert take_snapshot(vault, tmp_path, idle, source).run is None
    assert source.afters == []

    stem = _idea(vault, "En cours", status="building", **{RUN_ID_KEY: RUN_ID})
    source.pages = [{"rows": [_row(1)], "reset": True, "next_after": 5}]
    snap = take_snapshot(vault, tmp_path, stem, source)
    assert snap.run is not None and snap.reset and snap.cursor == 5
    take_snapshot(vault, tmp_path, stem, source, (RUN_ID, 5))
    take_snapshot(vault, tmp_path, stem, source, ("other-run", 5))
    assert source.afters == [None, 5, None]


def test_snapshot_reports_an_unreachable_server(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    stem = _idea(vault, status="building", **{RUN_ID_KEY: RUN_ID})
    snap = take_snapshot(vault, tmp_path, stem, FakeSource(error=OSError("refused")))
    assert snap.run is None and snap.error is not None and "injoignable" in snap.error


# ---------- the Textual pane ----------


class FakeProc:
    def __init__(self) -> None:
        self.code: int | None = None

    def poll(self) -> int | None:
        return self.code


def _app(
    vault: Path, repo: Path, stem: str, source: FakeSource | None = None, **kw: Any
) -> UiPaneApp:
    return UiPaneApp(
        vault,
        repo,
        stem,
        source_factory=lambda: source,
        clock=lambda: T0 + timedelta(seconds=75),
        poll_seconds=60,
        port=0,
        **kw,
    )


def _text(app: UiPaneApp, wid: str) -> str:
    return str(app.query_one(wid, Static).render())


@pytest.mark.asyncio
async def test_pane_shows_build_progress_and_events(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    stem = _idea(vault, status="building", **{RUN_ID_KEY: RUN_ID})
    rows = [_row(n) for n in range(12)]
    source = FakeSource(pages=[{"rows": rows, "reset": True, "next_after": 3}])
    app = _app(vault, tmp_path, stem, source)
    async with app.run_test(size=SIZE) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert "● en construction 1m15s · $0.41/1.50" in _text(app, "#state")
        log = app.query_one("#events", Log)
        assert log.line_count == KEEP_EVENTS
        assert log.lines[-1].endswith("étape 11")
        assert app.server is None


@pytest.mark.asyncio
async def test_pane_serves_the_prototype_and_opens_it(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    stem = _idea(vault, poc_verdict="works", poc_url="https://example.org/pluie/")
    _web(tmp_path, stem)
    opened: list[str] = []
    app = _app(vault, tmp_path, stem, opener=lambda url: opened.append(url) or True)
    async with app.run_test(size=SIZE) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app.server is not None and app.server.running
        links = _text(app, "#links")
        assert app.server.url in links and "rechargement auto" in links
        assert "https://example.org/pluie/" in links and "ça marche" in links
        assert "✓ prototype prêt · verdict ça marche" in _text(app, "#state")
        await pilot.press("o")
        await app.workers.wait_for_complete()
        assert opened == [app.server.url]
        server = app.server
    assert not server.running  # stopped on quit


@pytest.mark.asyncio
async def test_build_needs_confirmation(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    stem = _idea(vault)
    launched: list[tuple[Path, str, Path]] = []
    proc = FakeProc()

    def launcher(repo: Path, s: str, log: Path) -> FakeProc:
        launched.append((repo, s, log))
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("Building…\nPrototype prêt\n", "utf-8")
        return proc

    app = _app(vault, tmp_path, stem, launcher=launcher)
    async with app.run_test(size=SIZE) as pilot:
        await app.workers.wait_for_complete()
        await pilot.press("b")
        assert "1,50 $ de crédit API" in CONFIRM_BUILD
        await pilot.press("n")
        assert launched == []
        await pilot.press("b")
        await pilot.press("o")
        await pilot.pause()
        assert launched and launched[0][1] == stem
        await pilot.press("b")  # already running: refused without a modal
        await pilot.pause()
        assert len(launched) == 1 and len(app.screen_stack) == 1
        proc.code = 0
        app.tail_build_log()
        lines = list(app.query_one("#events", Log).lines)
        assert "│ Building…" in lines and "■ construction terminée (code 0)" in lines


@pytest.mark.asyncio
async def test_build_refused_when_a_prototype_exists_or_runs(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    done = _idea(vault)
    _web(tmp_path, done)
    launched: list[object] = []
    app = _app(vault, tmp_path, done, launcher=lambda *a: launched.append(a))
    async with app.run_test(size=SIZE) as pilot:
        await app.workers.wait_for_complete()
        await pilot.press("b")
        await pilot.pause()
        assert len(app.screen_stack) == 1 and launched == []

    vault2 = tmp_path / "v2"
    busy = _idea(vault2, status="building", **{RUN_ID_KEY: RUN_ID})
    app = _app(vault2, tmp_path / "r2", busy, FakeSource(), launcher=launched.append)
    async with app.run_test(size=SIZE) as pilot:
        await app.workers.wait_for_complete()
        await pilot.press("b")
        await pilot.pause()
        assert len(app.screen_stack) == 1 and launched == []


@pytest.mark.asyncio
async def test_pane_without_web_page_says_so(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    stem = _idea(vault)
    opened: list[str] = []
    app = _app(vault, tmp_path, stem, opener=opened.append)
    async with app.run_test(size=SIZE) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert "○ pas encore de prototype" in _text(app, "#state")
        await pilot.press("o")
        await app.workers.wait_for_complete()
        assert opened == []
