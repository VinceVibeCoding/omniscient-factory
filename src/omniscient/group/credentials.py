"""omnigroup credentials: the hook token file, widget tokens (hashes only) and file modes.

Two credentials, two powers (see docs/omnigroup.md, « Modèle de menace »):
- the **hook credential** (`<home>/.omniscient/group.token`, 0600) lets the agents' hook
  post events and wait for the pending its own call opened (with a wait ticket);
- a **widget token** answers, prompts and reads everything. It is minted by pairing,
  returned once, and only its SHA-256 is kept (table `widget_tokens` of group.db).

Files: the state folder is 0700, the token file and group.db (with its -wal/-shm/-journal
companions) 0600. The token file is written atomically (mkstemp in the folder + replace).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import secrets
import sqlite3
import stat
import tempfile
import threading
from datetime import datetime
from pathlib import Path

from omniscient.group.models import iso, utcnow
from omniscient.home import Home

log = logging.getLogger(__name__)

TOKEN_FILE = "group.token"
DB_FILE = "group.db"
TOKEN_MIN_LEN = 32
DIR_MODE = 0o700
FILE_MODE = 0o600
DB_COMPANIONS = ("-wal", "-shm", "-journal")
MAX_WIDGETS = 8  # pairing more widgets forgets the oldest
NONCE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
_SCHEMA = """
CREATE TABLE IF NOT EXISTS widget_tokens (
    hash TEXT PRIMARY KEY, client_name TEXT NOT NULL, created_at TEXT NOT NULL);
"""


# ---- modes ----------------------------------------------------------------------------


def secure_dir(path: Path) -> Path:
    """Create `path` (and parents) and make it private to the user (0700)."""
    path.mkdir(parents=True, exist_ok=True, mode=DIR_MODE)
    if stat.S_IMODE(path.stat().st_mode) != DIR_MODE:
        path.chmod(DIR_MODE)
    return path


def secure_file(path: Path) -> None:
    """Make an existing file 0600 (a missing one is fine)."""
    try:
        if stat.S_IMODE(path.stat().st_mode) != FILE_MODE:
            path.chmod(FILE_MODE)
    except FileNotFoundError:
        return


def secure_db_files(db: Path) -> None:
    """Pre-create the database 0600 (SQLite gives its journals the same mode) and
    tighten any companion file left by an older run."""
    if str(db) == ":memory:":
        return
    secure_dir(db.parent)
    fd = os.open(db, os.O_RDWR | os.O_CREAT, FILE_MODE)
    os.close(fd)
    for suffix in ("", *DB_COMPANIONS):
        secure_file(db.with_name(db.name + suffix))


# ---- hook credential --------------------------------------------------------------------


def hook_token_path(home: Home) -> Path:
    return home.state / TOKEN_FILE


def db_path(home: Home) -> Path:
    return home.state / DB_FILE


def read_hook_token(home: Home) -> str | None:
    """The hook token, or None when the daemon never ran in this home."""
    try:
        token = hook_token_path(home).read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    return token or None


def write_atomic(path: Path, text: str) -> None:
    """Write `text` to `path` through a 0600 temp file in the same folder, then replace."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def load_or_create_hook_token(home: Home) -> str:
    """The hook token (created 0600 on first use); refuses a short or empty file."""
    path = hook_token_path(home)
    secure_dir(path.parent)
    token = read_hook_token(home)
    if token is None and not path.exists():
        write_atomic(path, secrets.token_urlsafe(32) + "\n")
        token = read_hook_token(home)
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode != FILE_MODE:
        log.warning("omnigroup: token file was mode %o, tightening to 600", mode)
        path.chmod(FILE_MODE)
    if token is None or len(token) < TOKEN_MIN_LEN:
        raise RuntimeError(f"jeton omnigroup invalide : supprime {path} et relance")
    return token


def same_secret(given: str, expected: str) -> bool:
    return hmac.compare_digest(given.encode(), expected.encode())


# ---- widget tokens -----------------------------------------------------------------------


def token_hash(token: str) -> str:
    """SHA-256 of a token, hex (what group.db keeps)."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def proof(stored_hash: str, nonce: str) -> str:
    """HMAC-SHA256(key = the raw token hash, message = nonce), hex: the daemon proves it
    knows the widget's token hash without the widget sending its token."""
    key = bytes.fromhex(stored_hash)
    return hmac.new(key, nonce.encode("utf-8"), hashlib.sha256).hexdigest()


class WidgetTokens:
    """Widget token hashes in group.db (its own connection; thread-safe)."""

    def __init__(self, path: Path | str) -> None:
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._db.executescript(_SCHEMA)
            self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def mint(self, client_name: str, now: datetime | None = None) -> str:
        """A new widget token: its hash is stored, the plaintext is returned once."""
        token = secrets.token_urlsafe(32)
        stamp = iso(now or utcnow())
        with self._lock:
            self._db.execute(
                "INSERT INTO widget_tokens (hash, client_name, created_at) "
                "VALUES (?, ?, ?)",
                (token_hash(token), client_name, stamp),
            )
            self._db.execute(
                "DELETE FROM widget_tokens WHERE hash NOT IN (SELECT hash FROM "
                "widget_tokens ORDER BY rowid DESC LIMIT ?)",
                (MAX_WIDGETS,),
            )
            self._db.commit()
        return token

    def is_valid(self, token: str) -> bool:
        if not token:
            return False
        with self._lock:
            row = self._db.execute(
                "SELECT 1 FROM widget_tokens WHERE hash = ?", (token_hash(token),)
            ).fetchone()
        return row is not None

    def hashes(self) -> list[str]:
        with self._lock:
            rows = self._db.execute(
                "SELECT hash FROM widget_tokens ORDER BY rowid DESC"
            ).fetchall()
        return [r[0] for r in rows]

    def revoke_all(self) -> int:
        """Forget every widget token; returns how many were dropped."""
        with self._lock:
            count = self._db.execute("DELETE FROM widget_tokens").rowcount
            self._db.commit()
        return count

    def proofs(self, nonce: str) -> list[str]:
        """One proof per paired widget for GET /health?nonce=…"""
        return [proof(h, nonce) for h in self.hashes()]
