"""Export the public engine from a private Omniscient checkout, then scan it for leaks.

    uv run python scripts/export_public.py SRC DEST [--marker TEXT]... [--allow FILE:TEXT]... [--dry-run]

Copies an ALLOWLIST of engine files from SRC into DEST (which must be absent or empty) and
never copies the owner's data (vault/, repos/, pocs/, config/, .env, agentrun/, ...).
The exported tree is then scanned for secret-looking strings, the --marker strings
(owner identifiers: pass them on the command line, never hardcode them here), absolute
/home/<user>/ paths and e-mail addresses. Any hit → report + exit code 1.
--allow FILE:TEXT tolerates TEXT inside one exported file (e.g. the LICENSE copyright line).
--dry-run plans and scans without writing anything. This script never creates
repositories and never pushes.
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from omniscient.build.publish import SECRET_PATTERNS

# Top-level entries copied as-is (folders recursively, minus EXCLUDED_PARTS).
ALLOWED_TOP = (
    "src",
    "tests",
    "pyproject.toml",
    "uv.lock",
    "README.md",
    "LICENSE",
    "SECURITY.md",
    "CONTRIBUTING.md",
    ".github",
    ".gitignore",
    ".python-version",
    ".env.example",
    "CLAUDE.md",
    "docs/agents",
    "scripts/export_public.py",
    "scripts/agentrun-up.sh",
    "scripts/install-docker-wsl.sh",
    "slack/manifest.yaml",  # generic Slack app manifest (no ids, no tokens)
)
# Source path → exported path (the public AGENTS.md replaces the owner's one).
RENAMED = {"docs/AGENTS.public.md": "AGENTS.md"}
# Never exported, wherever they appear inside an allowed folder.
EXCLUDED_PARTS = frozenset(
    {"__pycache__", ".venv", ".pytest_cache", ".ruff_cache", ".mypy_cache", ".coverage"}
)
EXCLUDED_SUFFIXES = (".pyc", ".pyo")
ENV_EXAMPLE = ".env.example"

_EMAIL = re.compile(
    r"\b([A-Za-z0-9._%+-]+)@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,})\b"
)
_ALLOWED_EMAIL_LOCAL = ("noreply", "no-reply", "git")  # git@github.com is an SSH remote
_ALLOWED_EMAIL_DOMAINS = ("example.com", "example.org", "example.net", "users.noreply.github.com")
_HOME_PATH = re.compile(r"/home/[a-z_][a-z0-9_-]*/")


class ExportError(Exception):
    """The export cannot run (bad arguments, unsafe destination)."""


@dataclass(frozen=True)
class Hit:
    path: str
    line: int
    kind: str

    def render(self) -> str:
        return f"{self.path}:{self.line}: {self.kind}"


@dataclass(frozen=True)
class Allowance:
    path: str
    text: str


def parse_allowance(raw: str) -> Allowance:
    path, sep, text = raw.partition(":")
    if not sep or not path.strip() or not text:
        raise ExportError(f"--allow attend FILE:TEXT, reçu {raw!r}")
    return Allowance(path.strip(), text)


def _excluded(rel: Path) -> bool:
    if any(part in EXCLUDED_PARTS for part in rel.parts):
        return True
    if rel.name.startswith(".env") and rel.name != ENV_EXAMPLE:
        return True
    return rel.suffix in EXCLUDED_SUFFIXES


def _walk(src: Path, entry: Path) -> Iterable[Path]:
    if entry.is_symlink():
        return
    if entry.is_file():
        yield entry
        return
    for path in sorted(entry.rglob("*")):
        if path.is_file() and not path.is_symlink():
            yield path


def plan_export(src: Path) -> list[tuple[Path, str]]:
    """Return (source file, exported relative path) pairs for every allowed file."""
    plan: list[tuple[Path, str]] = []
    for top in ALLOWED_TOP:
        entry = src / top
        if not entry.exists():
            continue
        for path in _walk(src, entry):
            rel = path.relative_to(src)
            if not _excluded(rel):
                plan.append((path, rel.as_posix()))
    for source, target in RENAMED.items():
        if (src / source).is_file():
            plan = [(p, r) for p, r in plan if r != target]
            plan.append((src / source, target))
    return sorted(plan, key=lambda pair: pair[1])


def _email_allowed(local: str, domain: str) -> bool:
    if local.lower() in _ALLOWED_EMAIL_LOCAL:
        return True
    return any(
        domain.lower() == d or domain.lower().endswith("." + d)
        for d in _ALLOWED_EMAIL_DOMAINS
    )


def _line_hits(line: str, markers: Sequence[str]) -> list[str]:
    kinds = [f"secret {p.pattern[:24]}" for p in SECRET_PATTERNS if p.search(line)]
    if _HOME_PATH.search(line) and not any(k.startswith("secret /home") for k in kinds):
        kinds.append("absolute /home/<user>/ path")
    lowered = line.lower()
    kinds += [f"marker {m!r}" for m in markers if m.lower() in lowered]
    kinds += [
        "e-mail address"
        for m in _EMAIL.finditer(line)
        if not _email_allowed(m.group(1), m.group(2))
    ][:1]
    return kinds


def scan_text(
    rel: str, text: str, markers: Sequence[str], allow: Sequence[Allowance] = ()
) -> list[Hit]:
    """Every leak in one file's text; allowed TEXT for this file is ignored."""
    for allowance in allow:
        if allowance.path == rel:
            text = text.replace(allowance.text, "")
    hits: list[Hit] = []
    for number, line in enumerate(text.splitlines(), start=1):
        hits += [Hit(rel, number, kind) for kind in _line_hits(line, markers)]
    return hits


def scan_plan(
    plan: Sequence[tuple[Path, str]],
    markers: Sequence[str],
    allow: Sequence[Allowance] = (),
) -> list[Hit]:
    """Scan each planned file (its path name is scanned too: a file name can leak)."""
    hits: list[Hit] = []
    for path, rel in plan:
        hits += [Hit(rel, 0, kind) for kind in _line_hits(rel, markers)]
        text = path.read_bytes().decode("utf-8", errors="ignore")
        hits += scan_text(rel, text, markers, allow)
    return hits


def _check_destination(src: Path, dest: Path) -> None:
    if dest == src or src in dest.parents:
        raise ExportError("DEST ne doit pas être dans SRC.")
    if dest.exists() and (not dest.is_dir() or any(dest.iterdir())):
        raise ExportError(f"DEST existe et n'est pas vide : {dest}")


def copy_plan(plan: Sequence[tuple[Path, str]], dest: Path) -> None:
    for path, rel in plan:
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)


def export(
    src: Path,
    dest: Path,
    markers: Sequence[str],
    allow: Sequence[Allowance] = (),
    dry_run: bool = False,
) -> tuple[list[tuple[Path, str]], list[Hit]]:
    """Copy the allowlist into dest (unless dry_run) and scan what was (or would be) exported."""
    src, dest = src.resolve(), dest.resolve()
    if not (src / "pyproject.toml").is_file():
        raise ExportError(f"SRC n'est pas un checkout Omniscient : {src}")
    _check_destination(src, dest)
    plan = plan_export(src)
    if not dry_run:
        copy_plan(plan, dest)
        plan = [(dest / rel, rel) for _, rel in plan]
    return plan, scan_plan(plan, markers, allow)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Export the public Omniscient engine.")
    parser.add_argument("src", type=Path)
    parser.add_argument("dest", type=Path)
    parser.add_argument(
        "--marker", action="append", default=[], help="owner identifier to forbid"
    )
    parser.add_argument(
        "--allow", action="append", default=[], help="FILE:TEXT tolerated in FILE"
    )
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        markers = [m for m in args.marker if m.strip()]
        allow = [parse_allowance(raw) for raw in args.allow]
        plan, hits = export(args.src, args.dest, markers, allow, args.dry_run)
    except ExportError as exc:
        print(f"Erreur : {exc}", file=sys.stderr)
        return 2
    verb = "à exporter" if args.dry_run else "exportés"
    print(f"{len(plan)} fichiers {verb}.")
    for hit in hits:
        print(hit.render())
    if hits:
        print(
            f"{len(hits)} fuite(s) possible(s) : export NON publiable.", file=sys.stderr
        )
        return 1
    print("Scan propre.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
