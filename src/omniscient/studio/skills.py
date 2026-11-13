"""The skills library: find Claude Code skills on this machine and copy them into the repo.

Sources, in display order:
  projet                 repo/.claude/skills/<dir>/SKILL.md
  personnel              ~/.claude/skills/<dir>/SKILL.md (and ~/.claude/skills/learned/<dir>/)
  plugin:anthropic-skills  ~/.claude/skills/synced/<account>/<dir>/SKILL.md (claude.ai sync)
  plugin:<name>          ~/.claude/plugins/cache/<marketplace>/<plugin>/<version>/ — the
                         installed version (installed_plugins.json) or else the newest by
                         mtime; skill dirs from plugin.json "skills" plus the default skills/.
Only the YAML frontmatter is parsed during a scan (fast for hundreds of skills); a bad
SKILL.md is skipped and reported in SkillScan.problems, never raised.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import yaml

from omniscient.studio.ideas import StudioError

SKILL_FILE = "SKILL.md"
SOURCE_PROJECT = "projet"
SOURCE_PERSONAL = "personnel"
SOURCE_SYNCED = "plugin:anthropic-skills"
PLUGIN_PREFIX = "plugin:"
SKIP_DIRS = frozenset({".git", "node_modules", "__pycache__"})
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
FRONTMATTER_MAX_BYTES = 64 * 1024
_FENCE = "---"
_LOADER: Any = getattr(yaml, "CSafeLoader", yaml.SafeLoader)


@dataclass(frozen=True)
class SkillInfo:
    """One skill as the library shows it."""

    name: str
    description: str
    source: str  # "projet" | "personnel" | "plugin:<name>"
    path: Path  # the SKILL.md
    installed_in_project: bool


@dataclass(frozen=True)
class SkillScan:
    """A scan's skills plus the SKILL.md files that could not be read."""

    skills: tuple[SkillInfo, ...]
    problems: tuple[str, ...]


# ---------- parsing ----------


def _frontmatter_text(path: Path) -> str:
    with path.open("r", encoding="utf-8", errors="strict") as fh:
        head = fh.read(FRONTMATTER_MAX_BYTES)
    lines = head.splitlines()
    if not lines or lines[0].strip() != _FENCE:
        raise ValueError("no frontmatter")
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == _FENCE:
            return "\n".join(lines[1:index])
    raise ValueError("unterminated frontmatter")


def read_meta(path: Path) -> tuple[str, str]:
    """(name, description) from a SKILL.md; the name falls back to the directory name."""
    data = yaml.load(_frontmatter_text(path), Loader=_LOADER)
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise TypeError("frontmatter is not a mapping")
    name = str(data.get("name") or path.parent.name).strip()
    description = " ".join(str(data.get("description") or "").split())
    return name, description


def read_body(path: Path) -> str:
    """The SKILL.md without its frontmatter (for the preview)."""
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    if lines and lines[0].strip() == _FENCE:
        for index, line in enumerate(lines[1:], start=1):
            if line.strip() == _FENCE:
                return "\n".join(lines[index + 1 :]).strip()
    return text.strip()


def install_dir_name(name: str) -> str:
    """The project directory a skill installs into (':' becomes '-')."""
    return name.replace(":", "-")


# ---------- discovery ----------


def _skill_files(folder: Path) -> Iterator[Path]:
    """folder/*/SKILL.md, skipping hidden directories; a missing folder yields nothing."""
    try:
        entries = sorted(folder.iterdir())
    except OSError:
        return
    for entry in entries:
        if entry.name.startswith(".") or not entry.is_dir():
            continue
        candidate = entry / SKILL_FILE
        if candidate.is_file():
            yield candidate


def _load_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _installed_paths(plugins: Path) -> frozenset[Path]:
    data = _load_json(plugins / "installed_plugins.json").get("plugins")
    found: set[Path] = set()
    if isinstance(data, dict):
        for entries in data.values():
            for entry in entries if isinstance(entries, list) else []:
                path = entry.get("installPath") if isinstance(entry, dict) else None
                if isinstance(path, str):
                    found.add(Path(path).resolve())
    return frozenset(found)


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _plugin_versions(cache: Path, installed: frozenset[Path]) -> dict[str, Path]:
    """Plugin name -> the version directory to read (installed first, else newest)."""
    best: dict[str, tuple[bool, float, Path]] = {}
    for version in cache.glob("*/*/*/"):
        if not version.is_dir() or version.name.startswith("."):
            continue
        rank = (version.resolve() in installed, _mtime(version), version)
        name = version.parent.name
        if name not in best or rank[:2] > best[name][:2]:
            best[name] = rank
    return {name: rank[2] for name, rank in best.items()}


def _plugin_skill_files(root: Path) -> Iterator[Path]:
    """SKILL.md files a plugin version declares (plugin.json "skills" + default skills/)."""
    declared = _load_json(root / ".claude-plugin" / "plugin.json").get("skills")
    entries = declared if isinstance(declared, list) else [declared]
    folders = ["skills", *(e for e in entries if isinstance(e, str))]
    base = root.resolve()
    seen: set[Path] = set()
    for folder in folders:
        target = (root / folder).resolve()
        if target != base and base not in target.parents:
            continue  # refuse paths that leave the plugin
        direct = target / SKILL_FILE
        for path in [direct] if direct.is_file() else _skill_files(target):
            if path not in seen:
                seen.add(path)
                yield path


def _plugin_name(root: Path) -> str:
    name = _load_json(root / ".claude-plugin" / "plugin.json").get("name")
    return name if isinstance(name, str) and name.strip() else root.parent.name


def _sources(repo: Path, home: Path) -> Iterator[tuple[str, Path]]:
    """(source, SKILL.md) for every candidate skill on this machine."""
    for path in _skill_files(repo / ".claude" / "skills"):
        yield SOURCE_PROJECT, path
    personal = home / ".claude" / "skills"
    for folder in (personal, personal / "learned"):
        for path in _skill_files(folder):
            yield SOURCE_PERSONAL, path
    for account in sorted((personal / "synced").glob("*/")):
        if not account.name.startswith("."):
            for path in _skill_files(account):
                yield SOURCE_SYNCED, path
    plugins = home / ".claude" / "plugins"
    versions = _plugin_versions(plugins / "cache", _installed_paths(plugins))
    for _, root in sorted(versions.items()):
        source = PLUGIN_PREFIX + _plugin_name(root)
        for path in _plugin_skill_files(root):
            yield source, path


def _sort_key(skill: SkillInfo) -> tuple[int, str, str]:
    rank = {SOURCE_PROJECT: 0, SOURCE_PERSONAL: 1}.get(skill.source, 2)
    return rank, skill.source, skill.name.casefold()


def _project_names(repo: Path) -> frozenset[str]:
    return frozenset(p.parent.name for p in _skill_files(repo / ".claude" / "skills"))


def _build(
    found: Iterable[tuple[str, Path]], project: frozenset[str]
) -> tuple[list[SkillInfo], list[str]]:
    skills: list[SkillInfo] = []
    problems: list[str] = []
    for source, path in found:
        try:
            name, description = read_meta(path)
        except (OSError, ValueError, TypeError, yaml.YAMLError) as exc:
            problems.append(f"{path}: {exc}")
            continue
        installed = source == SOURCE_PROJECT or install_dir_name(name) in project
        skills.append(SkillInfo(name, description, source, path, installed))
    return skills, problems


def scan_report(repo: Path, home: Path | None = None) -> SkillScan:
    """Every skill on this machine plus the files that could not be parsed."""
    home = Path.home() if home is None else home
    skills, problems = _build(_sources(repo, home), _project_names(repo))
    return SkillScan(tuple(sorted(skills, key=_sort_key)), tuple(problems))


def scan_skills(repo: Path, home: Path | None = None) -> tuple[SkillInfo, ...]:
    """Every skill, sorted by source then name (bad files skipped; see scan_report)."""
    return scan_report(repo, home).skills


def refresh_installed(
    skills: tuple[SkillInfo, ...], repo: Path
) -> tuple[SkillInfo, ...]:
    """Re-read only the project skills after an install/remove (no full rescan)."""
    others = [s for s in skills if s.source != SOURCE_PROJECT]
    project = _project_names(repo)
    fresh, _ = _build(
        ((SOURCE_PROJECT, p) for p in _skill_files(repo / ".claude" / "skills")),
        project,
    )
    flagged = [
        replace(s, installed_in_project=install_dir_name(s.name) in project)
        for s in others
    ]
    return tuple(sorted([*fresh, *flagged], key=_sort_key))


# ---------- install / remove ----------


def _check_name(name: str) -> str:
    if not NAME_RE.fullmatch(name):
        raise StudioError(
            f"nom de skill invalide « {name} » (attendu : minuscules, chiffres, - ou _)"
        )
    return name


def _copy_tree(src: Path, dst: Path, root: Path) -> None:
    """Copy src into dst; symlinks are followed only when they stay inside root."""
    dst.mkdir(parents=True, exist_ok=True)
    for entry in sorted(src.iterdir()):
        if entry.name in SKIP_DIRS:
            continue
        real = entry.resolve()
        if entry.is_symlink() and real != root and root not in real.parents:
            continue  # never pull files from outside the skill
        if real.is_dir():
            if entry.is_symlink():
                continue  # avoid loops through in-tree directory links
            _copy_tree(entry, dst / entry.name, root)
        elif real.is_file():
            shutil.copy2(real, dst / entry.name)


def install_skill(skill: SkillInfo, repo: Path) -> Path:
    """Copy the skill's directory into repo/.claude/skills/<name>/ and return that path."""
    dirname = _check_name(install_dir_name(skill.name))
    source = skill.path.parent
    if not skill.path.is_file():
        raise StudioError(f"le skill « {skill.name} » est introuvable ({skill.path})")
    skills_dir = repo / ".claude" / "skills"
    target = skills_dir / dirname
    if target.exists() or target.is_symlink():
        raise StudioError(f"le skill « {dirname} » existe déjà dans le projet")
    skills_dir.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{dirname}-", dir=skills_dir))
    try:
        _copy_tree(source, staging, source.resolve())
        os.rename(staging, target)  # atomic: never a half-copied skill
    except OSError as exc:
        shutil.rmtree(staging, ignore_errors=True)
        raise StudioError(f"copie du skill « {dirname} » impossible : {exc}") from exc
    return target


def remove_skill(name: str, repo: Path) -> None:
    """Delete repo/.claude/skills/<name>/ (project skills only)."""
    dirname = _check_name(install_dir_name(name))
    skills_dir = (repo / ".claude" / "skills").resolve()
    target = repo / ".claude" / "skills" / dirname
    if target.is_symlink() or not (target / SKILL_FILE).is_file():
        raise StudioError(f"aucun skill « {dirname} » dans le projet")
    if target.resolve().parent != skills_dir:
        raise StudioError(f"aucun skill « {dirname} » dans le projet")
    try:
        shutil.rmtree(target)
    except OSError as exc:
        raise StudioError(f"suppression de « {dirname} » impossible : {exc}") from exc
