"""Describe local projects as registry entries (repos/<name>.md, same frontmatter).

Reads only harmless files: README, manifests, compose files, .env.example. Never .env,
secrets, data/, node_modules, .venv or .git internals. Never follows a symlink nor reads a
file resolving outside the project folder, reads at most 64 KB per file, and masks
anything secret-looking (tokens, keys, local paths) before it is written to an entry.
"""

from __future__ import annotations

import json
import re
import subprocess
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from omniscient.build.publish import redact_secrets

PURPOSE_MAX = 200
GIT_TIMEOUT_S = 10
MAX_DOCS = 6
MIN_PORT, MAX_PORT = 1, 65535
MAX_READ_BYTES = 64 * 1024

README_NAMES = ("README.md", "README.rst", "README.txt", "README", "readme.md")
COMPOSE_NAMES = (
    "docker-compose.yml",
    "docker-compose.yaml",
    "compose.yml",
    "compose.yaml",
)
AGENT_DOC_NAMES = ("AGENTS.md", "CLAUDE.md", "README.md", "CONTRIBUTING.md")
MANIFESTS: dict[str, str] = {
    "pyproject.toml": "python",
    "requirements.txt": "python",
    "setup.py": "python",
    "package.json": "node",
    "go.mod": "go",
    "Cargo.toml": "rust",
    "Gemfile": "ruby",
    "pom.xml": "java",
    "build.gradle": "java",
    "build.gradle.kts": "kotlin",
    "composer.json": "php",
    "pubspec.yaml": "dart",
    "Dockerfile": "docker",
}
NODE_FRAMEWORKS = {
    "next": "nextjs",
    "react": "react",
    "vue": "vue",
    "svelte": "svelte",
    "vite": "vite",
}
ENV_PORT_RE = re.compile(r"^\s*([A-Z0-9_]*PORT)\s*=\s*['\"]?(\d{1,5})['\"]?\s*$")
NAME_RE = re.compile(r"^[a-z0-9._-]+$")
GitRun = Callable[[list[str]], str | None]


@dataclass(frozen=True)
class EntryFields:
    """Frontmatter of a registry entry (mirrors registry.core._ProjectSchema)."""

    name: str
    path: str
    purpose: str
    stack: tuple[str, ...] = ()
    ports: tuple[tuple[int, str, str], ...] = ()  # (port, service, source)
    services: tuple[str, ...] = ()
    remote: str | None = None
    git: bool = False
    agent_docs: tuple[str, ...] = ()
    notes: str = ""


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9._-]+", "-", text.strip().lower()).strip("-.")
    return slug or "projet"


def render_entry(fields: EntryFields, body: str) -> str:
    """Entry markdown. Free text read from the project (purpose, notes, names) is
    passed through redact_secrets; `path` is the user's own folder and stays as is."""
    meta: dict[str, object] = {
        "name": fields.name,
        "path": fields.path,
        "purpose": redact_secrets(fields.purpose),
        "stack": [redact_secrets(t) for t in fields.stack],
        "ports": [
            {"port": p, "service": redact_secrets(s), "source": src}
            for p, s, src in fields.ports
        ],
        "services": [redact_secrets(n) for n in fields.services],
        "remote": redact_secrets(fields.remote) if fields.remote else None,
        "git": fields.git,
        "agent_docs": list(fields.agent_docs),
    }
    if fields.notes:
        meta["notes"] = redact_secrets(fields.notes)
    dumped = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True, width=1000)
    return f"---\n{dumped}---\n# {fields.name}\n\n{body.strip()}\n"


def _clip(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= PURPOSE_MAX else text[: PURPOSE_MAX - 1].rstrip() + "…"


def _is_prose(block: str) -> bool:
    first = block.lstrip()
    skip = ("#", "<", "![", "[![", "```", "|", "---", "===", "-", "*", ">")
    return bool(first) and not first.startswith(skip)


def first_paragraph(text: str) -> str | None:
    """The README's first prose paragraph (no headings, badges, html), ≤200 chars."""
    for block in re.split(r"\n\s*\n", text):
        cleaned = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", block)
        cleaned = cleaned.replace("**", "").replace("`", "")
        if _is_prose(cleaned):
            return _clip(cleaned)
    return None


def _inside(root: Path, path: Path) -> bool:
    """A regular file, not a symlink, that resolves inside root."""
    try:
        return (
            not path.is_symlink()
            and path.is_file()
            and path.resolve().is_relative_to(root.resolve())
        )
    except OSError:
        return False


def _read(root: Path, rel: str) -> str | None:
    """At most MAX_READ_BYTES of root/rel, or None if absent, linked or outside root."""
    path = root / rel
    if not _inside(root, path):
        return None
    try:
        with path.open("rb") as handle:
            return handle.read(MAX_READ_BYTES).decode("utf-8", errors="replace")
    except OSError:
        return None


def _manifest_description(root: Path) -> str | None:
    pyproject = _read(root, "pyproject.toml")
    if pyproject:
        try:
            desc = tomllib.loads(pyproject).get("project", {}).get("description")
        except tomllib.TOMLDecodeError:
            desc = None
        if isinstance(desc, str) and desc.strip():
            return _clip(desc)
    package = _package_json(root)
    desc = package.get("description")
    return _clip(desc) if isinstance(desc, str) and desc.strip() else None


def _package_json(root: Path) -> dict[str, object]:
    raw = _read(root, "package.json")
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def find_purpose(root: Path) -> str:
    for name in README_NAMES:
        text = _read(root, name)
        found = first_paragraph(text) if text else None
        if found:
            return found
    return _manifest_description(root) or f"Projet {root.name} (à décrire)."


def detect_stack(root: Path) -> tuple[str, ...]:
    stack: list[str] = []
    for file, tech in MANIFESTS.items():
        if (root / file).is_file() and tech not in stack:
            stack.append(tech)
    package = _package_json(root)
    deps: dict[str, object] = {}
    for key in ("dependencies", "devDependencies"):
        section = package.get(key)
        if isinstance(section, dict):
            deps = {**deps, **section}
    stack += [
        fw for dep, fw in NODE_FRAMEWORKS.items() if dep in deps and fw not in stack
    ]
    if _compose_file(root) and "docker" not in stack:
        stack.append("docker")
    return tuple(stack)


def _compose_file(root: Path) -> str | None:
    return next((n for n in COMPOSE_NAMES if _inside(root, root / n)), None)


def _host_port(spec: object) -> int | None:
    if isinstance(spec, int):
        return spec
    if isinstance(spec, dict):
        published = spec.get("published")
        return int(published) if str(published).isdigit() else None
    if not isinstance(spec, str):
        return None
    parts = spec.split("/")[0].split(":")
    host = parts[-2] if len(parts) >= 2 else parts[0]
    return int(host) if host.isdigit() else None


def _compose(root: Path) -> tuple[tuple[tuple[int, str, str], ...], tuple[str, ...]]:
    file = _compose_file(root)
    raw = _read(root, file) if file else None
    try:
        data = yaml.safe_load(raw) if raw else None
    except yaml.YAMLError:
        data = None
    services = data.get("services") if isinstance(data, dict) else None
    if not isinstance(services, dict):
        return (), ()
    ports = []
    for name, spec in services.items():
        entries = spec.get("ports", []) if isinstance(spec, dict) else []
        for entry in entries if isinstance(entries, list) else []:
            port = _host_port(entry)
            if port is not None and MIN_PORT <= port <= MAX_PORT:
                ports.append((port, str(name), "config"))
    return tuple(ports), tuple(str(n) for n in services)


def _env_example_ports(root: Path) -> tuple[tuple[int, str, str], ...]:
    raw = _read(root, ".env.example")
    found = []
    for line in (raw or "").splitlines():
        match = ENV_PORT_RE.match(line)
        if match and MIN_PORT <= int(match.group(2)) <= MAX_PORT:
            found.append((int(match.group(2)), match.group(1), "config"))
    return tuple(found)


SCP_RE = re.compile(r"^(?:[^@/]+@)?([\w.-]+):(?!//)(.+)$")


def clean_remote(url: str) -> str:
    """Remote URL without scheme, credentials, port or .git: github.com/owner/repo.

    Userinfo is everything up to the last '@' of the authority, so a password
    holding '/' or ':' (https://u:p/w@host/x) never survives.
    """
    url = url.strip()
    if "://" not in url:
        scp = SCP_RE.match(url)
        url = f"//{scp.group(1)}/{scp.group(2)}" if scp else f"//{url}"
    rest = urlsplit(url)
    raw = rest.netloc + rest.path  # re-joined: a '/' in a password splits early
    authority, _, path = raw.rpartition("@")[2].partition("/")
    host = urlsplit(f"//{authority}").hostname or authority
    cleaned = f"{host}/{path}" if path else host
    return cleaned.removesuffix("/").removesuffix(".git")


def run_git(cmd: list[str]) -> str | None:
    try:
        done = subprocess.run(
            cmd, capture_output=True, text=True, timeout=GIT_TIMEOUT_S, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return done.stdout.strip() if done.returncode == 0 and done.stdout.strip() else None


def _agent_docs(root: Path) -> tuple[str, ...]:
    docs = [n for n in AGENT_DOC_NAMES if _inside(root, root / n)]
    docs_dir = root / "docs"
    if docs_dir.is_dir() and not docs_dir.is_symlink():
        docs += sorted(
            f"docs/{p.name}" for p in docs_dir.glob("*.md") if _inside(root, p)
        )
    return tuple(docs[:MAX_DOCS])


def describe_local_project(path: Path, git_run: GitRun = run_git) -> str:
    """Registry markdown for a local folder (frontmatter + short body)."""
    root = path.expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"dossier introuvable : {root}")
    compose_ports, services = _compose(root)
    has_git = (root / ".git").exists()
    remote = (
        git_run(["git", "-C", str(root), "remote", "get-url", "origin"])
        if has_git
        else None
    )
    fields = EntryFields(
        name=slugify(root.name),
        path=str(root),
        purpose=find_purpose(root),
        stack=detect_stack(root),
        ports=tuple(dict.fromkeys(compose_ports + _env_example_ports(root))),
        services=services,
        remote=clean_remote(remote) if remote else None,
        git=has_git,
        agent_docs=_agent_docs(root),
    )
    body = "Fiche créée par `omniscient init` à partir des fichiers du projet. Relis-la et complète-la."
    return render_entry(fields, body)


def _is_candidate(folder: Path) -> bool:
    if folder.is_symlink() or not folder.is_dir() or folder.name.startswith("."):
        return False
    return (folder / ".git").exists() or any((folder / m).is_file() for m in MANIFESTS)


def scan_projects_root(root: Path) -> tuple[Path, ...]:
    """Project folders directly under root (a .git or a manifest), sorted by name."""
    base = root.expanduser()
    if not base.is_dir():
        return ()
    return tuple(
        sorted((p for p in base.iterdir() if _is_candidate(p)), key=lambda p: p.name)
    )
