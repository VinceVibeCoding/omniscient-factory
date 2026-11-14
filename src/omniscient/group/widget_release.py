"""The Windows widget installer from the public GitHub releases, verified before use.

`latest_widget_release` lists the repository's releases (GitHub REST API, no token) and
keeps the newest published `widget-v*` one that carries a `*-setup.exe` and `SHA256SUMS`
served from this repository's own download URLs. `download_verified` fetches both over
HTTPS (size-capped, redirects only to HTTPS GitHub hosts) and writes the installer only
when its SHA-256 matches the published sum. Both files come from the same release, so the
checksum only proves integrity: when the GitHub CLI is available, the build provenance
attestation is verified too (`gh attestation verify`), and a failure refuses the file.
The installer is written through a unique temp file and never replaces a symlink.
`windows_downloads` finds the Windows Downloads folder from WSL.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import urllib.parse
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
ATTEST_TIMEOUT_S = 120
ALLOWED_HOSTS = frozenset(
    {
        "api.github.com",
        "github.com",
        "objects.githubusercontent.com",
        "release-assets.githubusercontent.com",
    }
)
USER_AGENT = "omniscient-group-setup"
_SHA = re.compile(r"^([0-9a-fA-F]{64})\s+\*?(.+)$")
Fetch = Callable[[str, int], bytes]
Runner = Callable[[list[str]], tuple[int, str]]
Attest = Callable[[Path], str]


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


def check_url(url: str) -> None:
    """Only HTTPS to GitHub's own hosts (API, releases, release asset storage)."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or (parts.hostname or "") not in ALLOWED_HOSTS:
        raise ReleaseError(f"adresse refusée (HTTPS GitHub uniquement) : {url[:200]}")


class SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Follows a redirect only to an HTTPS GitHub host (no downgrade, no elsewhere)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        check_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def https_fetch(url: str, max_bytes: int) -> bytes:
    """GET over HTTPS only, at most `max_bytes` (more is an error, never truncated)."""
    check_url(url)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    opener = urllib.request.build_opener(SafeRedirectHandler())
    try:
        with opener.open(request, timeout=TIMEOUT_S) as resp:
            data = resp.read(max_bytes + 1)
    except OSError as exc:
        raise ReleaseError(f"téléchargement impossible ({exc})") from exc
    if len(data) > max_bytes:
        raise ReleaseError(f"réponse trop grande : {url}")
    return data


def safe_asset_name(name: str, installer: bool) -> bool:
    """A plain file name (no folder, `..` or backslash): `*-setup.exe` or SHA256SUMS."""
    plain = (
        bool(name)
        and Path(name).name == name
        and "\\" not in name
        and ".." not in name
        and name not in (".", "..")
    )
    if not plain:
        return False
    return name.endswith("-setup.exe") if installer else name == SUMS_NAME


def _asset(raw: Any, tag: str, installer: bool) -> Asset | None:
    if not isinstance(raw, list):
        return None
    for item in raw:
        if not isinstance(item, dict):
            continue
        name, url = item.get("name"), item.get("browser_download_url")
        if not isinstance(name, str) or not isinstance(url, str):
            continue
        if not safe_asset_name(name, installer):
            continue
        if url != f"{DOWNLOAD_PREFIX}{tag}/{name}":
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
        installer = _asset(item.get("assets"), tag, installer=True)
        sums = _asset(item.get("assets"), tag, installer=False)
        if installer is not None and sums is not None:
            return Release(tag, installer, sums)
    return None


def expected_sha(sums_text: str, name: str) -> str:
    for line in sums_text.splitlines():
        found = _SHA.match(line.strip())
        if found and found.group(2).strip() == name:
            return found.group(1).lower()
    raise ReleaseError(f"{name} absent de {SUMS_NAME}")


def _run(argv: list[str]) -> tuple[int, str]:
    try:
        done = subprocess.run(  # argv list, no shell
            argv, capture_output=True, text=True, timeout=ATTEST_TIMEOUT_S, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)
    return done.returncode, (done.stdout or "") + (done.stderr or "")


def verify_attestation(
    path: Path,
    which: Callable[[str], str | None] = shutil.which,
    runner: Runner = _run,
) -> str:
    """ "verified" (gh accepted the provenance), "unavailable" (no gh); refusal raises."""
    gh = which("gh")
    if gh is None:
        return "unavailable"
    code, out = runner([gh, "attestation", "verify", str(path), "--repo", REPO])
    if code != 0:
        raise ReleaseError(f"attestation de provenance refusée : {out.strip()[-300:]}")
    return "verified"


def download_verified(
    release: Release,
    dest_dir: Path,
    fetch: Fetch = https_fetch,
    attest: Attest = verify_attestation,
) -> Path:
    """Download the installer into `dest_dir` only if its SHA-256 matches SHA256SUMS
    and `attest` accepts it; the file only gets its final name after both checks."""
    name = release.installer.name
    if not safe_asset_name(name, installer=True):
        raise ReleaseError(f"nom de fichier refusé : {name[:100]}")
    sums = fetch(release.sums.url, SUMS_MAX).decode("utf-8", errors="replace")
    want = expected_sha(sums, name)
    data = fetch(release.installer.url, INSTALLER_MAX)
    if hashlib.sha256(data).hexdigest() != want:
        raise ReleaseError(f"SHA256 différent pour {name} : fichier rejeté")
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / name
    if target.is_symlink():
        raise ReleaseError(f"{target} est un lien symbolique : supprime-le d'abord")
    fd, tmp_name = tempfile.mkstemp(dir=dest_dir, prefix=".omnigroup-", suffix=".part")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        attest(tmp)
        if target.is_symlink():
            raise ReleaseError(f"{target} est un lien symbolique : supprime-le d'abord")
        os.replace(tmp, target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
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
