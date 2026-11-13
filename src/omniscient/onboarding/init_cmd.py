"""`omniscient init`: interactive, idempotent creation/update of a user's home."""

from __future__ import annotations

import getpass
import json
import os
import re
import shutil
import sys
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from omniscient.home import Home, packaged
from omniscient.onboarding.github import Fetch, GitHubError, http_fetch, list_user_repos
from omniscient.onboarding.prereqs import (
    Check,
    check_prereqs,
    missing_required,
    render_checks,
)
from omniscient.onboarding.projects import (
    DEFAULT_PROJECTS_ROOT,
    OnboardingError,
    add_github,
    add_local,
    pick_github_repos,
    projects_root,
)
from omniscient.onboarding.scan import GitRun, run_git, scan_projects_root, slugify
from omniscient.onboarding.secrets import (
    GITHUB,
    Ask,
    Say,
    Secret,
    configure_secrets,
    prompt_secret,
    read_env,
    warn_if_echoing,
    write_env,
)
from omniscient.profile import (
    LANGUAGES,
    Profile,
    load_profile,
    parse_list,
    save_profile,
)

VAULT_DIRS = ("news", "ideas", "tech", "maps", "ops")
CONFIG_TEMPLATE = "omniscient.toml"
PROJECTS_ROOT_RE = re.compile(r"(?m)^projects_root\s*=.*$")


def _yes(answer: str) -> bool:
    return answer.strip().lower() in ("o", "oui", "y", "yes")


def step_prereqs(say: Say, prereqs: Callable[[], tuple[Check, ...]]) -> None:
    say("1/6 Prérequis (rien n'est installé automatiquement)")
    checks = prereqs()
    say(render_checks(checks))
    missing = missing_required(checks)
    if missing:
        names = ", ".join(c.name for c in missing)
        raise OnboardingError(
            f"prérequis manquants : {names}. Installe-les puis relance `omniscient init`."
        )


def _write_config(home: Home, templates: Path, root: str) -> None:
    template = templates / CONFIG_TEMPLATE
    if not template.is_file():
        raise OnboardingError(f"modèle de configuration introuvable : {template}")
    text, count = PROJECTS_ROOT_RE.subn(
        lambda _: f"projects_root = {json.dumps(root)}",
        template.read_text(encoding="utf-8"),
        count=1,
    )
    if count != 1:
        raise OnboardingError(
            "le modèle de configuration n'a pas de ligne projects_root"
        )
    home.config.parent.mkdir(parents=True, exist_ok=True)
    home.config.write_text(text, encoding="utf-8")


def _copy_skills(home: Home, skills: Path, say: Say) -> None:
    if not skills.is_dir():
        say(f"  (aucun skill fourni dans {skills})")
        return
    home.skills.mkdir(parents=True, exist_ok=True)
    added = []
    for source in sorted(p for p in skills.iterdir() if p.is_dir()):
        target = home.skills / source.name
        if not target.exists():
            shutil.copytree(source, target)
            added.append(source.name)
    say(f"  skills : {len(added)} ajouté(s), existants conservés")


def step_layout(home: Home, ask: Ask, say: Say, templates: Path, skills: Path) -> None:
    say(f"2/6 Dossier Omniscient : {home.root}")
    if home.exists:
        say("  configuration existante conservée")
    else:
        root = (
            ask("Dossier qui contient tes projets", DEFAULT_PROJECTS_ROOT).strip()
            or DEFAULT_PROJECTS_ROOT
        )
        _write_config(home, templates, root)
        say(f"  configuration créée : {home.config}")
    for folder in (
        home.repos,
        home.pocs,
        home.state,
        *(home.vault / d for d in VAULT_DIRS),
    ):
        folder.mkdir(parents=True, exist_ok=True)
    _copy_skills(home, skills, say)


def _ask_language(ask: Ask, say: Say, current: str) -> str:
    while True:
        answer = (
            ask("Langue du contenu écrit par les agents (fr/en)", current)
            .strip()
            .lower()
            or current
        )
        if answer in LANGUAGES:
            return answer
        say("  réponds « fr » ou « en »")


def _theme_names(themes: Iterable[object]) -> list[str]:
    return [str(getattr(t, "name", t)) for t in themes]


def step_theme(home: Home, ask: Ask, say: Say) -> None:
    try:
        from omniscient import theme as theme_mod
    except ImportError:
        return
    list_themes = getattr(theme_mod, "list_themes", None)
    set_theme = getattr(theme_mod, "set_theme", None)
    if list_themes is None or set_theme is None:
        return
    names = _theme_names(list_themes())
    if not names:
        return
    while True:
        answer = ask(f"Thème du studio ({', '.join(names)}, vide = garder)", "").strip()
        if not answer:
            return
        if answer in names:
            set_theme(home, answer)
            say(f"  thème : {answer}")
            return
        say(f"  thème inconnu : {answer}")


def step_profile(home: Home, ask: Ask, say: Say) -> Profile:
    say("3/6 Ton profil (oriente les idées)")
    current = load_profile(home.profile)
    language = _ask_language(ask, say, current.language)
    step_theme(home, ask, say)
    name = ask("Ton prénom (optionnel)", current.name or "").strip() or None
    interests = ask(
        "Tes centres d'intérêt (séparés par des virgules)", ", ".join(current.interests)
    )
    avoid = ask("Sujets à éviter (optionnel)", ", ".join(current.avoid))
    github = (
        ask("Ton utilisateur GitHub (optionnel)", current.github_user or "").strip()
        or None
    )
    profile = Profile(
        name=name,
        language=language,  # type: ignore[arg-type]  # validated by _ask_language
        interests=parse_list(interests),
        avoid=parse_list(avoid),
        github_user=github,
    )
    save_profile(home.profile, profile)
    say(f"  profil enregistré : {home.profile}")
    return profile


def _scan_local(home: Home, ask: Ask, say: Say, git_run: GitRun) -> None:
    root = projects_root(home)
    candidates = tuple(
        p
        for p in scan_projects_root(root)
        if not (home.repos / f"{slugify(p.name)}.md").exists()
    )
    if not candidates:
        say(f"  aucun nouveau projet dans {root}")
        return
    say(f"  {len(candidates)} projet(s) trouvé(s) dans {root}")
    take_all = False
    for folder in candidates:
        answer = "o" if take_all else ask(f"  Ajouter {folder.name} ? (o/n/tout)", "o")
        take_all = take_all or answer.strip().lower() == "tout"
        if take_all or _yes(answer):
            written = add_local(home, folder, git_run)
            if written:
                say(f"    → {written.name}")


def _scan_github(
    home: Home, ask: Ask, say: Say, user: str, fetch: Fetch, token: str | None
) -> None:
    try:
        repos = list_user_repos(user, token, fetch)
        for repo in pick_github_repos(repos, ask, say):
            written = add_github(home, repo.full_name, fetch, token)
            say(
                f"    → {written.name}"
                if written
                else f"    {repo.full_name} : déjà présent"
            )
    except GitHubError as exc:
        say(f"  {exc} (étape GitHub ignorée)")


@dataclass(frozen=True)
class SecretIO:
    """How init asks for secrets: the prompt, and whether stdin is a real terminal."""

    secret: Secret
    is_tty: Callable[[], bool]


def _github_token(
    home: Home, ask: Ask, say: Say, io: SecretIO, env: Mapping[str, str]
) -> str | None:
    """The token already known, else the one typed now (saved to .env right away)."""
    known = read_env(home.env_file).get(GITHUB.key) or env.get(GITHUB.key)
    if known:
        return known
    warn_if_echoing(say, io.is_tty)
    typed = prompt_secret(GITHUB, {}, ask, io.secret)
    if typed:
        write_env(home.env_file, {GITHUB.key: typed})
        say(f"  {GITHUB.key} enregistré dans {home.env_file} (600)")
    return typed


def step_projects(
    home: Home,
    ask: Ask,
    say: Say,
    profile: Profile,
    fetch: Fetch,
    git_run: GitRun,
    io: SecretIO,
    env: Mapping[str, str],
) -> bool:
    """Local scan, then GitHub (its optional token is asked right before listing).

    Returns True when the GitHub token was offered here (step 5 then skips it).
    """
    say("4/6 Tes projets (registre repos/)")
    _scan_local(home, ask, say, git_run)
    if not profile.github_user or not _yes(
        ask(f"Parcourir les dépôts GitHub de {profile.github_user} ? (o/N)", "n")
    ):
        return False
    token = _github_token(home, ask, say, io, env)
    _scan_github(home, ask, say, profile.github_user, fetch, token)
    return True


def step_next(say: Say, home: Home) -> None:
    say("6/6 Et maintenant :")
    say("  omniscient agents-push     # envoie les agents à AgentRun (boucles, PoC)")
    say("  omniscient studio          # dans herdr : le tableau d'idées")
    say(
        "  omniscient project add …   # ajouter un projet plus tard (dossier, dépôt ou utilisateur GitHub)"
    )
    say(
        "  omniscient schedule install  # optionnel : minuteur systemd quotidien du cycle"
    )
    if home.root != Path.home() / ".omniscient":
        say(f"  Hors de ce dossier, exporte OMNISCIENT_HOME={home.root}")


def run_init(
    home: Home,
    ask: Ask,
    say: Say,
    secret: Secret = getpass.getpass,
    environ: Mapping[str, str] | None = None,
    *,
    prereqs: Callable[[], tuple[Check, ...]] = check_prereqs,
    fetch: Fetch = http_fetch,
    git_run: GitRun = run_git,
    templates: Path | None = None,
    skills: Path | None = None,
    is_tty: Callable[[], bool] = sys.stdin.isatty,
) -> None:
    """Create or update the home. Safe to re-run: existing data is kept."""
    env = os.environ if environ is None else environ
    step_prereqs(say, prereqs)
    step_layout(
        home, ask, say, templates or packaged("templates"), skills or packaged("skills")
    )
    profile = step_profile(home, ask, say)
    io = SecretIO(secret, is_tty)
    github_offered = step_projects(home, ask, say, profile, fetch, git_run, io, env)
    say("5/6 Secrets (jamais affichés)")
    if not github_offered:  # else already warned right before the GitHub token
        warn_if_echoing(say, is_tty)
    configure_secrets(home.env_file, ask, say, secret, ask_github=not github_offered)
    step_next(say, home)
