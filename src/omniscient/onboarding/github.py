"""Read-only GitHub REST helpers: list a user's public repos, describe one as a registry entry.

The token (optional) is only sent as a header; it is never printed or stored here.
`fetch(url, headers) -> (status, json_or_None)` is injectable; tests pass a fake.
"""

from __future__ import annotations

import base64
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx

from omniscient.build.publish import redact_secrets
from omniscient.onboarding.scan import (
    MAX_READ_BYTES,
    EntryFields,
    first_paragraph,
    render_entry,
    slugify,
)

API = "https://api.github.com"
PER_PAGE = 100
MAX_PAGES = 5
TIMEOUT_S = 15
USER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
FULL_NAME_RE = re.compile(r"^[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}$")
URL_RE = re.compile(
    r"^(?:https?://)?(?:www\.)?github\.com/([^/\s]+/[^/\s#?]+?)(?:\.git)?/?$"
)
HTTP_OK, HTTP_FORBIDDEN, HTTP_NOT_FOUND, HTTP_TOO_MANY = 200, 403, 404, 429

Fetch = Callable[[str, dict[str, str]], tuple[int, object]]


class GitHubError(Exception):
    """A GitHub call failed (French, user-facing message)."""


@dataclass(frozen=True)
class GitHubRepo:
    full_name: str
    name: str
    description: str | None
    language: str | None
    fork: bool
    archived: bool


def http_fetch(url: str, headers: dict[str, str]) -> tuple[int, object]:
    try:
        response = httpx.get(
            url, headers=headers, timeout=TIMEOUT_S, follow_redirects=True
        )
    except httpx.HTTPError as exc:
        raise GitHubError(f"GitHub injoignable : {type(exc).__name__}") from exc
    try:
        body: object = response.json()
    except ValueError:
        body = None
    return response.status_code, body


def _headers(token: str | None) -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    return {**headers, "Authorization": f"Bearer {token}"} if token else headers


def _get(url: str, token: str | None, fetch: Fetch, what: str) -> object:
    status, body = fetch(url, _headers(token))
    if status == HTTP_OK:
        return body
    if status == HTTP_NOT_FOUND:
        raise GitHubError(f"GitHub : {what} introuvable (404).")
    if status in (HTTP_FORBIDDEN, HTTP_TOO_MANY):
        raise GitHubError(
            "GitHub : limite de requêtes atteinte ou accès refusé. "
            "Réessaie plus tard ou ajoute un GITHUB_TOKEN."
        )
    raise GitHubError(f"GitHub : erreur {status} pour {what}.")


def _to_repo(item: object) -> GitHubRepo | None:
    if not isinstance(item, dict) or not isinstance(item.get("full_name"), str):
        return None
    desc, lang = item.get("description"), item.get("language")
    return GitHubRepo(
        full_name=item["full_name"],
        name=str(item.get("name") or item["full_name"].split("/")[-1]),
        description=desc if isinstance(desc, str) else None,
        language=lang if isinstance(lang, str) else None,
        fork=bool(item.get("fork")),
        archived=bool(item.get("archived")),
    )


def list_user_repos(
    user: str, token: str | None, fetch: Fetch = http_fetch
) -> tuple[GitHubRepo, ...]:
    """A user's public repos (all pages, capped), sorted by name."""
    if not USER_RE.match(user):
        raise GitHubError(f"nom d'utilisateur GitHub invalide : {user!r}")
    repos: list[GitHubRepo] = []
    for page in range(1, MAX_PAGES + 1):
        url = f"{API}/users/{user}/repos?type=owner&sort=updated&per_page={PER_PAGE}&page={page}"
        body = _get(url, token, fetch, f"l'utilisateur {user}")
        if not isinstance(body, list):
            raise GitHubError("GitHub : réponse inattendue pour la liste des dépôts.")
        repos += [r for r in map(_to_repo, body) if r is not None]
        if len(body) < PER_PAGE:
            break
    return tuple(sorted(repos, key=lambda r: r.name.lower()))


def parse_full_name(source: str) -> str | None:
    """'owner/name' or a github.com URL -> 'owner/name'; else None."""
    text = source.strip()
    match = URL_RE.match(text)
    candidate = match.group(1) if match else text
    candidate = candidate.removesuffix(".git")
    return candidate if FULL_NAME_RE.match(candidate) else None


def _readme_text(full_name: str, token: str | None, fetch: Fetch) -> str | None:
    try:
        body = _get(f"{API}/repos/{full_name}/readme", token, fetch, "le README")
    except GitHubError:
        return None
    if not isinstance(body, dict) or body.get("encoding") != "base64":
        return None
    try:
        raw = base64.b64decode(str(body.get("content", "")))
    except ValueError:
        return None
    # Untrusted text that ends up in agent prompts: capped and secret-masked.
    return redact_secrets(raw[:MAX_READ_BYTES].decode("utf-8", "replace"))


def _stack(languages: object, primary: str | None) -> tuple[str, ...]:
    names = list(languages) if isinstance(languages, dict) else []
    if primary and primary not in names:
        names.insert(0, primary)
    return tuple(dict.fromkeys(n.lower() for n in names if isinstance(n, str)))


def describe_github_repo(
    full_name: str,
    clone_path: Path,
    fetch: Fetch = http_fetch,
    token: str | None = None,
) -> str:
    """Registry markdown for a GitHub repo not cloned yet (path = where it would be cloned)."""
    if not FULL_NAME_RE.match(full_name):
        raise GitHubError(
            f"dépôt GitHub invalide : {full_name!r} (attendu propriétaire/nom)"
        )
    meta = _get(f"{API}/repos/{full_name}", token, fetch, f"le dépôt {full_name}")
    if not isinstance(meta, dict):
        raise GitHubError("GitHub : réponse inattendue pour le dépôt.")
    languages = _get(f"{API}/repos/{full_name}/languages", token, fetch, "les langages")
    readme = _readme_text(full_name, token, fetch)
    desc = meta.get("description") if isinstance(meta.get("description"), str) else None
    purpose = (
        (first_paragraph(readme) if readme else None)
        or desc
        or f"Dépôt GitHub {full_name}."
    )
    topics = meta.get("topics") if isinstance(meta.get("topics"), list) else []
    primary = meta.get("language") if isinstance(meta.get("language"), str) else None
    fields = EntryFields(
        name=slugify(full_name.split("/")[-1]),
        path=str(clone_path.expanduser()),
        purpose=purpose,
        stack=_stack(languages, primary),
        remote=f"github.com/{full_name}",
        git=False,
        agent_docs=("README.md",) if readme else (),
        notes="Pas encore cloné localement."
        + (f" Sujets : {', '.join(map(str, topics))}." if topics else ""),
    )
    body = f"Fiche créée depuis GitHub ({full_name}). Clone le dépôt dans `{fields.path}` avant d'y travailler."
    return render_entry(fields, body)
