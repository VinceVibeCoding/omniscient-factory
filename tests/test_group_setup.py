"""`omniscient group setup`: release lookup, SHA256 check, preview vs --write, plugin files."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from omniscient.group import cli_group_setup as setup
from omniscient.group import widget_release as wr

ROOT = Path(__file__).resolve().parents[1]
EXE = b"MZ fake installer"
NAME = "Omnigroup_0.2.0_x64-setup.exe"
BASE = f"https://github.com/{wr.REPO}/releases/download"


def releases(tag: str = "widget-v0.2.0", **extra: Any) -> list[dict]:
    good = {
        "tag_name": tag, "draft": False, "prerelease": False,
        "assets": [
            {"name": NAME, "browser_download_url": f"{BASE}/{tag}/{NAME}", "size": len(EXE)},
            {"name": "SHA256SUMS", "browser_download_url": f"{BASE}/{tag}/SHA256SUMS", "size": 90},
        ],
    }  # fmt: skip
    good.update(extra)
    return [
        {"tag_name": "v9.9.9", "draft": False, "prerelease": False, "assets": []},
        {"tag_name": "widget-v9.0.0", "draft": True, "prerelease": False, "assets": []},
        {"tag_name": "widget-v8.0.0", "draft": False, "prerelease": True, "assets": []},
        good,
    ]


def fake_fetch(sums: str | None = None, listing: list | None = None) -> Any:
    digest = hashlib.sha256(EXE).hexdigest()
    body = {
        "releases": json.dumps(listing if listing is not None else releases()).encode(),
        "SHA256SUMS": (sums if sums is not None else f"{digest}  {NAME}\n").encode(),
        NAME: EXE,
    }
    calls: list[str] = []

    def fetch(url: str, max_bytes: int) -> bytes:
        calls.append(url)
        key = "releases" if "api.github.com" in url else url.rsplit("/", 1)[-1]
        return body[key]

    fetch.calls = calls  # type: ignore[attr-defined]
    return fetch


def test_latest_release_skips_drafts_prereleases_and_other_tags() -> None:
    release = wr.latest_widget_release(fake_fetch())
    assert release is not None and release.tag == "widget-v0.2.0"
    assert release.installer.name == NAME
    assert wr.latest_widget_release(fake_fetch(listing=releases()[:3])) is None


def test_foreign_download_urls_are_refused() -> None:
    listing = releases()
    listing[-1]["assets"][0]["browser_download_url"] = "https://evil.example/x.exe"
    assert wr.latest_widget_release(fake_fetch(listing=listing)) is None


def test_expected_sha_parsing() -> None:
    digest = "a" * 64
    assert wr.expected_sha(f"{digest}  {NAME}\n", NAME) == digest
    assert wr.expected_sha(f"{digest} *{NAME}\n", NAME) == digest
    with pytest.raises(wr.ReleaseError):
        wr.expected_sha(f"{digest}  other.exe\n", NAME)


def test_download_verified_writes_only_on_match(tmp_path: Path) -> None:
    release = wr.latest_widget_release(fake_fetch())
    assert release is not None
    path = wr.download_verified(release, tmp_path, fake_fetch())
    assert path.read_bytes() == EXE
    bad = fake_fetch(sums=f"{'0' * 64}  {NAME}\n")
    with pytest.raises(wr.ReleaseError):
        wr.download_verified(release, tmp_path / "other", bad)
    assert not (tmp_path / "other" / NAME).exists()


def test_https_only_fetch() -> None:
    with pytest.raises(wr.ReleaseError):
        wr.https_fetch("http://github.com/x", 10)


def test_windows_downloads_via_wsl_runner(tmp_path: Path) -> None:
    downloads = tmp_path / "Downloads"
    downloads.mkdir()

    def runner(argv: list[str]) -> tuple[int, str]:
        if argv[0] == "cmd.exe":
            return 0, "C:\\Users\\someone\r\n"
        return 0, f"{tmp_path}\n"

    assert wr.windows_downloads(runner) == downloads
    assert wr.windows_downloads(lambda argv: (1, "")) is None


class Recorder:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self.hooks: list[dict] = []

    def say(self, line: str) -> None:
        self.lines.append(line)

    def hooks_fn(self, **kw: Any) -> None:
        self.hooks.append(kw)


def deps(rec: Recorder, tmp_path: Path, healthy: bool = True) -> setup.SetupDeps:
    return setup.SetupDeps(
        say=rec.say,
        health=lambda port: healthy,
        fetch=fake_fetch(),
        hooks=rec.hooks_fn,
        downloads=lambda: tmp_path,
    )


def test_preview_writes_nothing(tmp_path: Path) -> None:
    rec = Recorder()
    setup.run_setup(setup.SetupOptions(write=False), deps(rec, tmp_path, healthy=False))
    text = "\n".join(rec.lines)
    assert "omniscient group serve" in text
    assert rec.hooks[0]["write"] is False and rec.hooks[0]["statusline"] is True
    assert not (tmp_path / NAME).exists()
    assert "--write" in text


def test_write_downloads_and_verifies(tmp_path: Path) -> None:
    rec = Recorder()
    setup.run_setup(setup.SetupOptions(write=True), deps(rec, tmp_path))
    assert (tmp_path / NAME).read_bytes() == EXE
    assert rec.hooks[0]["write"] is True
    assert any("SHA256" in line for line in rec.lines)


def test_no_release_is_explained(tmp_path: Path) -> None:
    rec = Recorder()
    d = deps(rec, tmp_path)
    d = setup.SetupDeps(
        rec.say, d.health, fake_fetch(listing=[]), rec.hooks_fn, d.downloads
    )
    setup.run_setup(setup.SetupOptions(write=True), d)
    assert any("Aucune version publiée" in line for line in rec.lines)


# ---- Claude Code plugin ----------------------------------------------------------------


def frontmatter(path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    head = text.split("---\n")[1]
    return dict(
        line.split(": ", 1) for line in head.strip().splitlines() if ": " in line
    )


def test_marketplace_and_plugin_manifests() -> None:
    market = json.loads(
        (ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8")
    )
    entry = next(p for p in market["plugins"] if p["name"] == "omnigroup")
    assert entry["source"] == "./plugins/omnigroup" and market["owner"]["name"]
    plugin_dir = ROOT / "plugins" / "omnigroup"
    manifest = json.loads(
        (plugin_dir / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8")
    )
    assert manifest["name"] == entry["name"]
    for skill in ("setup", "theme", "view", "status"):
        meta = frontmatter(plugin_dir / "skills" / skill / "SKILL.md")
        assert meta["name"] == skill and meta["description"]
