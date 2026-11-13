"""Publish a prototype's web demo to the public GitHub Pages site.

Each prototype ships `web/index.html` (self-contained, simulated data). We copy that
folder into a local clone of the site repository under `<slug>/`, refuse anything that
looks like a secret or a local path, rebuild the site's index, commit and push.
GitHub Pages then serves it at `<base_url><slug>/` (about a minute after the push).
"""

from __future__ import annotations

import html
import re
import shutil
import subprocess
from pathlib import Path

import frontmatter

from omniscient.redact import REDACTED, SECRET_PATTERNS, redact_secrets

WEB_DIR = "web"
WEB_ENTRY = "index.html"
MAX_SITE_BYTES = 5 * 1024 * 1024
_TITLE = re.compile(r"<title>(.*?)</title>", re.IGNORECASE | re.DOTALL)


__all__ = ["REDACTED", "SECRET_PATTERNS", "redact_secrets"]  # re-exported


class PublishError(Exception):
    """The prototype has no web demo, or it is unsafe or could not be pushed."""


def find_secrets(root: Path) -> list[str]:
    """Return 'file: pattern' for every secret-looking string under root."""
    hits = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for pattern in SECRET_PATTERNS:
            if pattern.search(text):
                hits.append(f"{path.relative_to(root)}: {pattern.pattern[:24]}")
    return hits


def _web_files(web: Path) -> list[Path]:
    return [p for p in sorted(web.rglob("*")) if p.is_file() and not p.is_symlink()]


def check_web_demo(poc_dir: Path) -> Path:
    """Return the prototype's web folder after checking it is safe to publish."""
    web = poc_dir / WEB_DIR
    if not (web / WEB_ENTRY).is_file():
        raise PublishError(f"no {WEB_DIR}/{WEB_ENTRY} in {poc_dir.name}")
    files = _web_files(web)
    size = sum(p.stat().st_size for p in files)
    if size > MAX_SITE_BYTES:
        raise PublishError(f"web demo is {size} bytes, over {MAX_SITE_BYTES}")
    hits = find_secrets(web)
    if hits:
        raise PublishError(
            "refusing to publish, secret-like content: " + "; ".join(hits)
        )
    return web


def render_index(site: Path) -> str:
    """The site's home page: one link per published prototype, newest name first."""
    items = []
    for entry in sorted(site.iterdir(), reverse=True):
        page = entry / WEB_ENTRY
        if entry.name.startswith(".") or not page.is_file():
            continue
        match = _TITLE.search(page.read_text(encoding="utf-8", errors="ignore"))
        title = html.unescape(match.group(1).strip()) if match else entry.name
        items.append(
            f'<li><a href="{html.escape(entry.name)}/">{html.escape(title)}</a></li>'
        )
    return (
        '<!doctype html><html lang="fr"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        "<title>Prototypes Omniscient</title></head>"
        '<body style="font-family:system-ui;max-width:720px;margin:2rem auto;padding:0 16px">'
        "<h1>Prototypes Omniscient</h1>"
        "<p>Prototypes jetables construits automatiquement. Données simulées.</p>"
        f"<ul>{''.join(items)}</ul></body></html>\n"
    )


def _git(site: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=site, check=True, capture_output=True)


def publish_web_demo(poc_dir: Path, slug: str, site: Path, base_url: str) -> str:
    """Copy poc_dir/web into the site clone at <slug>/, push, return the public URL."""
    web = check_web_demo(poc_dir)
    if not (site / ".git").is_dir():
        raise PublishError(f"no site clone at {site}")
    _git(site, "pull", "-q", "--ff-only")
    dest = site / slug
    shutil.rmtree(dest, ignore_errors=True)
    for src in _web_files(web):
        target = dest / src.relative_to(web)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target)
    (site / WEB_ENTRY).write_text(render_index(site), encoding="utf-8")
    _git(site, "add", "-A")
    _git(
        site,
        "-c",
        "user.name=Omniscient",
        "-c",
        "user.email=omniscient@localhost",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        f"publish: {slug}",
    )
    try:
        _git(site, "push", "-q")
    except subprocess.CalledProcessError as exc:
        raise PublishError(
            f"push failed: {exc.stderr.decode(errors='ignore')[:200]}"
        ) from exc
    return f"{base_url.rstrip('/')}/{slug}/"


def record_url(idea_path: Path, url: str) -> None:
    """Remember the public demo URL on the idea note (frontmatter + Prototype section)."""
    post = frontmatter.load(idea_path)
    post["poc_url"] = url
    line = f"**Essayer :** {url}"
    if line not in post.content:
        post.content = post.content.replace(
            "## Prototype\n", f"## Prototype\n{line}\n", 1
        )
    idea_path.write_text(frontmatter.dumps(post) + "\n", encoding="utf-8")
