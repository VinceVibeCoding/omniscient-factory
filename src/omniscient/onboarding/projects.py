"""Add projects to a home's registry (repos/<name>.md): local folders or GitHub repos."""

from __future__ import annotations

import tomllib
from collections.abc import Callable
from pathlib import Path

from omniscient.home import Home
from omniscient.onboarding.github import (
    USER_RE,
    Fetch,
    GitHubError,
    GitHubRepo,
    describe_github_repo,
    http_fetch,
    list_user_repos,
    parse_full_name,
)
from omniscient.onboarding.scan import (
    MANIFESTS,
    NAME_RE,
    GitRun,
    describe_local_project,
    run_git,
    slugify,
)
from omniscient.registry.core import load_registry

DEFAULT_PROJECTS_ROOT = "~/projects"
Ask = Callable[[str, str], str]
Say = Callable[[str], None]


class OnboardingError(Exception):
    """User-facing (French) onboarding failure."""


def write_registry_entry(
    home: Home, name: str, markdown: str, overwrite: bool = False
) -> Path | None:
    """Write <home>/repos/<name>.md; returns None when it exists and overwrite is False."""
    if not NAME_RE.match(name):
        raise OnboardingError(
            f"nom de projet invalide : {name!r} (lettres minuscules, chiffres, . _ -)"
        )
    home.repos.mkdir(parents=True, exist_ok=True)
    target = home.repos / f"{name}.md"
    if target.exists() and not overwrite:
        return None
    target.write_text(markdown, encoding="utf-8")
    return target


def projects_root(home: Home) -> Path:
    """[paths] projects_root of the home's config, ~ expanded (default ~/projects)."""
    raw: object = DEFAULT_PROJECTS_ROOT
    if home.config.is_file():
        try:
            data = tomllib.loads(home.config.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as exc:
            raise OnboardingError(f"config/omniscient.toml illisible : {exc}") from exc
        raw = data.get("paths", {}).get("projects_root", DEFAULT_PROJECTS_ROOT)
    return Path(str(raw)).expanduser()


def list_projects(home: Home) -> tuple[tuple[str, str], ...]:
    """(name, purpose) of every registered project — for `omniscient project list`."""
    if not home.repos.is_dir():
        return ()
    return tuple((p.name, p.purpose) for p in load_registry(home.repos))


def add_local(home: Home, folder: Path, git_run: GitRun = run_git) -> Path | None:
    markdown = describe_local_project(folder, git_run)
    return write_registry_entry(
        home, slugify(folder.expanduser().resolve().name), markdown
    )


def add_github(
    home: Home, full_name: str, fetch: Fetch, token: str | None
) -> Path | None:
    name = slugify(full_name.split("/")[-1])
    if (home.repos / f"{name}.md").exists():
        return None
    markdown = describe_github_repo(full_name, projects_root(home) / name, fetch, token)
    return write_registry_entry(home, name, markdown)


def parse_selection(answer: str, count: int) -> tuple[int, ...]:
    """'tout' | '' | '1,3-4' -> 0-based indexes (invalid numbers are an error)."""
    text = answer.strip().lower()
    if text in ("tout", "all", "*"):
        return tuple(range(count))
    picked: list[int] = []
    for part in filter(None, (p.strip() for p in text.split(","))):
        lo, _, hi = part.partition("-")
        if not lo.isdigit() or (hi and not hi.isdigit()):
            raise OnboardingError(f"sélection invalide : {part!r}")
        for number in range(int(lo), int(hi or lo) + 1):
            if not 1 <= number <= count:
                raise OnboardingError(f"numéro hors liste : {number}")
            picked.append(number - 1)
    return tuple(dict.fromkeys(picked))


def pick_github_repos(
    repos: tuple[GitHubRepo, ...], ask: Ask, say: Say
) -> tuple[GitHubRepo, ...]:
    shown = tuple(r for r in repos if not r.fork and not r.archived)
    if not shown:
        say("Aucun dépôt public (hors forks et archives).")
        return ()
    for number, repo in enumerate(shown, start=1):
        say(
            f"  {number:>3}. {repo.full_name} — {repo.description or '(sans description)'}"
        )
    while True:
        answer = ask("Numéros à ajouter (ex. 1,3-5), « tout », vide = aucun", "")
        try:
            return tuple(shown[i] for i in parse_selection(answer, len(shown)))
        except OnboardingError as exc:
            say(str(exc))


def _report(path: Path | None, label: str, say: Say) -> tuple[Path, ...]:
    if path is None:
        say(f"  {label} : déjà dans le registre, conservé.")
        return ()
    say(f"  {label} → {path.name}")
    return (path,)


def _is_project_folder(folder: Path) -> bool:
    return (folder / ".git").exists() or any((folder / m).is_file() for m in MANIFESTS)


def _prefer_local(text: str, folder: Path) -> bool:
    """An existing folder wins, unless a bare name also reads as a GitHub user and the
    folder is not a project (see add_project)."""
    if not USER_RE.match(text):
        return True
    return _is_project_folder(folder)


def add_project(
    home: Home,
    source: str,
    *,
    ask: Ask,
    say: Say,
    fetch: Fetch = http_fetch,
    token: str | None = None,
    git_run: GitRun = run_git,
) -> tuple[Path, ...]:
    """`omniscient project add SOURCE`: local path, GitHub URL / owner/name, or GitHub user.

    A bare NAME that is both a folder of the current directory and a valid GitHub user
    name is read as the local folder only when that folder is a project (it holds .git
    or a manifest such as pyproject.toml or package.json); otherwise it is the GitHub
    user. Write `./NAME` to force the folder.
    """
    text = source.strip()
    if not text:
        raise OnboardingError(
            "indique un dossier, un dépôt GitHub ou un utilisateur GitHub"
        )
    local = Path(text).expanduser()
    if local.is_dir() and _prefer_local(text, local):
        return _report(add_local(home, local, git_run), local.name, say)
    full_name = parse_full_name(text)
    try:
        if full_name:
            return _report(add_github(home, full_name, fetch, token), full_name, say)
        if USER_RE.match(text):
            chosen = pick_github_repos(list_user_repos(text, token, fetch), ask, say)
            written: tuple[Path, ...] = ()
            for repo in chosen:
                written += _report(
                    add_github(home, repo.full_name, fetch, token), repo.full_name, say
                )
            return written
    except GitHubError as exc:
        raise OnboardingError(str(exc)) from exc
    raise OnboardingError(
        f"source inconnue : {text!r} (ni dossier, ni dépôt, ni utilisateur GitHub)"
    )
