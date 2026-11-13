"""Read/update <home>/.env (chmod 600) without ever echoing a value.

Values are written double-quoted (see omniscient.envfile), through a private temp file
in the same folder that replaces .env atomically: a crash never leaves a half-written
or world-readable secrets file.
"""

from __future__ import annotations

import os
import re
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from omniscient.envfile import KEY_RE, parse_env, quote_value

ENV_MODE = 0o600
NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
NOT_TTY_WARNING = (
    "Attention : l'entrée n'est pas un terminal, les secrets saisis risquent de "
    "s'afficher en clair. Lance plutôt `omniscient init` dans un terminal interactif."
)

Ask = Callable[[str, str], str]
Say = Callable[[str], None]
Secret = Callable[[str], str]


@dataclass(frozen=True)
class SecretSpec:
    key: str
    label: str


ANTHROPIC = SecretSpec(
    "ANTHROPIC_API_KEY",
    "Clé API Anthropic (boucles de l'usine ; vide = studio seulement)",
)
SLACK = (
    SecretSpec("SLACK_BOT_TOKEN", "Slack bot token (xoxb-…)"),
    SecretSpec("SLACK_APP_TOKEN", "Slack app token (xapp-…)"),
    SecretSpec("SLACK_CHANNEL_ID", "ID du canal Slack"),
    SecretSpec("SLACK_OWNER_ID", "Ton ID membre Slack"),
)
AGENTRUN = SecretSpec(
    "AGENTRUN_API_KEY",
    "Clé API AgentRun (vide = lire ~/.agentrun/.token, créé au premier démarrage "
    "du serveur AgentRun local)",
)
GITHUB = SecretSpec(
    "GITHUB_TOKEN", "Jeton GitHub en lecture (optionnel, évite la limite de requêtes)"
)


def read_env(path: Path) -> dict[str, str]:
    """KEY=VALUE pairs of an env file (quotes and escapes decoded); missing -> {}."""
    if not path.is_file():
        return {}
    return parse_env(path.read_text(encoding="utf-8"))


def _atomic_private_write(path: Path, text: str) -> None:
    """Write text to path via an O_EXCL 0600 temp file in the same dir + os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)  # O_EXCL, 0600
    try:
        os.fchmod(fd, ENV_MODE)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def write_env(path: Path, updates: Mapping[str, str]) -> None:
    """Replace/add keys (values double-quoted), keep every other line, mode 600."""
    for key, value in updates.items():
        if not NAME_RE.match(key) or "\n" in value or "\r" in value:
            raise ValueError(f"entrée .env invalide pour {key}")
    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    pending = dict(updates)
    out: list[str] = []
    for line in lines:
        match = KEY_RE.match(line)
        key = match.group(1) if match and not line.lstrip().startswith("#") else None
        if key in pending:
            out.append(f"{key}={quote_value(pending.pop(key))}")
        else:
            out.append(line)
    out += [f"{key}={quote_value(value)}" for key, value in pending.items()]
    _atomic_private_write(path, "\n".join(out) + "\n")


def warn_if_echoing(say: Say, is_tty: Callable[[], bool]) -> None:
    """getpass echoes when stdin is not a terminal: say so before any secret prompt."""
    if not is_tty():
        say(NOT_TTY_WARNING)


def _yes(answer: str) -> bool:
    return answer.strip().lower() in ("o", "oui", "y", "yes")


def prompt_secret(
    spec: SecretSpec, existing: Mapping[str, str], ask: Ask, secret: Secret
) -> str | None:
    """New value for one key, or None to keep/skip. The value is never echoed."""
    question = f"{spec.key} est déjà défini. Le remplacer ? (o/N)"
    if existing.get(spec.key) and not _yes(ask(question, "n")):
        return None
    value = secret(f"{spec.label} [{spec.key}] (vide = passer) : ").strip()
    if "\n" in value or "\r" in value:
        raise ValueError(f"{spec.key} : valeur invalide (retour à la ligne)")
    return value or None


def configure_secrets(
    env_file: Path, ask: Ask, say: Say, secret: Secret, *, ask_github: bool = True
) -> tuple[str, ...]:
    """Interactive secrets step; returns the keys that were written (never their values).

    ask_github=False when the GitHub token was already offered in the projects step.
    """
    existing = read_env(env_file)
    specs = [ANTHROPIC, AGENTRUN]
    if _yes(ask("Configurer Slack (digest, notifications) ? (o/N)", "n")):
        specs += list(SLACK)
    if ask_github:
        specs.append(GITHUB)
    updates: dict[str, str] = {}
    for spec in specs:
        value = prompt_secret(spec, existing, ask, secret)
        if value is not None:
            updates[spec.key] = value
    if updates:
        write_env(env_file, updates)
        say(f"Secrets enregistrés dans {env_file} (600) : {', '.join(updates)}")
    elif env_file.is_file():
        os.chmod(env_file, ENV_MODE)
    if not (updates.get(ANTHROPIC.key) or existing.get(ANTHROPIC.key)):
        say("Pas de clé Anthropic : le studio marche, les boucles automatiques non.")
    return tuple(updates)
