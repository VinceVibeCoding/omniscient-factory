"""The Windows widget installer from the public GitHub releases, verified before use.

`latest_widget_release` lists the repository's releases (GitHub REST API, no token) and
keeps the newest published `widget-v*` one that carries a `*-setup.exe` and `SHA256SUMS`
served from this repository's own download URLs. `download_verified` fetches both over
HTTPS (size-capped) and writes the installer only when its SHA-256 matches the published
sum. `windows_downloads` finds the Windows Downloads folder from WSL.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO = "VinceVibeCoding/omniscient-factory"
API_RELEASES = f"https://api.github.com/repos/{REPO}/releases?per_page=30"
DOWNLOAD_PREFIX = f"https://github.com/{REPO}/releases/download/"
TAG_PREFIX = "widget-v"
SUMS_NAME = "SHA256SUMS"
LIST_MAX = 2 * 1024 * 1024
SUMS_MAX = 64 * 1024
INSTALLER_MAX = 64 * 1024 * 1024
TIMEOUT_S = 30
USER_AGENT = "omniscient-group-setup"
_SHA = re.compile(r"^([0-9a-fA-F]{64})\s+\*?(.+)$")
Fetch = Callable[[str, int], bytes]
Runner = Callable[[list[str]], tuple[int, str]]


class ReleaseError(Exception):
    """The release cannot be found, fetched or trusted."""


@dataclass(frozen=True)
class Asset:
    name: str
    url: str


@dataclass(frozen=True)
class Release:
    tag: str
    installer: Asset
    sums: Asset


def https_fetch(url: str, max_bytes: int) -> bytes:
    """GET over HTTPS only, at most `max_bytes` (more is an error, never truncated)."""
    if not url.startswith("https://"):
        raise ReleaseError(f"adresse non HTTPS refusée : {url}")
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as resp:
            data = resp.read(max_bytes + 1)
    except OSError as exc:
        raise ReleaseError(f"téléchargement impossible ({exc})") from exc
    if len(data) > max_bytes:
        raise ReleaseError(f"réponse trop grande : {url}")
    return data


def _asset(raw: Any, tag: str, wanted: Callable[[str], bool]) -> Asset | None:
    if not isinstance(raw, list):
        return None
    for item in raw:
        if not isinstance(item, dict):
            continue
        name, url = item.get("name"), item.get("browser_download_url")
        if not isinstance(name, str) or not isinstance(url, str) or not wanted(name):
            continue
        if "/" in name or url != f"{DOWNLOAD_PREFIX}{tag}/{name}":
            continue  # only this repository's own download URL for this tag
        return Asset(name, url)
    return None


def latest_widget_release(fetch: Fetch = https_fetch) -> Release | None:
    """The newest published widget release with an installer and its sums, or None."""
    try:
        listing = json.loads(fetch(API_RELEASES, LIST_MAX))
    except ValueError as exc:
        raise ReleaseError("réponse GitHub illisible") from exc
    if not isinstance(listing, list):
        raise ReleaseError("réponse GitHub inattendue")
    for item in listing:
        if not isinstance(item, dict) or item.get("draft") or item.get("prerelease"):
            continue
        tag = item.get("tag_name")
        if not isinstance(tag, str) or not tag.startswith(TAG_PREFIX):
            continue
        installer = _asset(item.get("assets"), tag, lambda n: n.endswith("-setup.exe"))
        sums = _asset(item.get("assets"), tag, lambda n: n == SUMS_NAME)
        if installer is not None and sums is not None:
            return Release(tag, installer, sums)
    return None


def expected_sha(sums_text: str, name: str) -> str:
    for line in sums_text.splitlines():
        found = _SHA.match(line.strip())
        if found and found.group(2).strip() == name:
            return found.group(1).lower()
    raise ReleaseError(f"{name} absent de {SUMS_NAME}")


def download_verified(
    release: Release, dest_dir: Path, fetch: Fetch = https_fetch
) -> Path:
    """Download the installer into `dest_dir` only if its SHA-256 matches SHA256SUMS."""
    sums = fetch(release.sums.url, SUMS_MAX).decode("utf-8", errors="replace")
    want = expected_sha(sums, release.installer.name)
    data = fetch(release.installer.url, INSTALLER_MAX)
    got = hashlib.sha256(data).hexdigest()
    if got != want:
        raise ReleaseError(
            f"SHA256 différent pour {release.installer.name} : fichier rejeté"
        )
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / release.installer.name
    tmp = target.with_name(target.name + ".part")
    tmp.write_bytes(data)
    os.replace(tmp, target)
    return target


def windows_downloads(runner: Runner) -> Path | None:
    """The Windows user's Downloads folder as a WSL path, or None when unreachable."""
    code, out = runner(["cmd.exe", "/c", "echo %USERPROFILE%"])
    profile = out.strip().splitlines()[-1].strip() if code == 0 and out.strip() else ""
    if not re.match(r"^[A-Za-z]:\\", profile):
        return None
    code, out = runner(["wslpath", "-u", profile])
    if code != 0 or not out.strip():
        return None
    folder = Path(out.strip()) / "Downloads"
    return folder if folder.is_dir() else None
