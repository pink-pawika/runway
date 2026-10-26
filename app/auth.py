"""Accounts and sessions. Spending data lives in one database file per user."""

from __future__ import annotations

import hashlib
import re
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.db import connect, init_db

SESSION_DAYS = 30
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,
    salt TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    expires_at TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS auth_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class AccountExists(Exception):
    """The email is already registered."""


def normalize_email(value: str) -> str:
    email = value.strip().lower()
    if not _EMAIL.fullmatch(email) or len(email) > 254:
        raise ValueError("Enter a valid email address.")
    return email


def hash_password(password: str, salt: bytes | None = None) -> tuple[str, str]:
    if salt is None:
        salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 210_000)
    return salt.hex(), digest.hex()


def password_matches(password: str, salt_hex: str, hash_hex: str) -> bool:
    _salt, digest = hash_password(password, bytes.fromhex(salt_hex))
    return secrets.compare_digest(digest, hash_hex)


def init_auth(auth_dir: Path) -> Path:
    path = auth_dir / "auth.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(path)
    try:
        conn.executescript(_SCHEMA)
        conn.commit()
    finally:
        conn.close()
    return path


def user_db_path(auth_dir: Path, user_id: int) -> Path:
    return auth_dir / "users" / f"{int(user_id)}.db"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _copy_ledger(source: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    origin = sqlite3.connect(source)
    target = sqlite3.connect(dest)
    try:
        origin.backup(target)
    finally:
        origin.close()
        target.close()


def _prepare_ledger(auth_dir: Path, user_id: int, legacy_db: Path | None) -> Path:
    dest = user_db_path(auth_dir, user_id)
    if dest.exists():
        return init_db(dest)
    auth_path = init_auth(auth_dir)
    conn = connect(auth_path)
    won = False
    try:
        conn.execute("BEGIN IMMEDIATE")
        claimed = conn.execute(
            "SELECT value FROM auth_meta WHERE key = 'legacy_claimed'"
        ).fetchone()
        if claimed is None and legacy_db is not None and legacy_db.exists():
            conn.execute(
                "INSERT INTO auth_meta (key, value) VALUES ('legacy_claimed', ?)",
                (str(user_id),),
            )
            won = True
        conn.commit()
    finally:
        conn.close()
    if won:
        try:
            _copy_ledger(legacy_db, dest)
        except OSError:
            if dest.exists():
                dest.unlink()
    return init_db(dest)


def create_user(auth_dir: Path, email: str, password: str, legacy_db: Path | None) -> dict[str, object]:
    auth_path = init_auth(auth_dir)
    salt, digest = hash_password(password)
    conn = connect(auth_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        taken = conn.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone()
        if taken is not None:
            conn.rollback()
            raise AccountExists
        cursor = conn.execute(
            """
            INSERT INTO users (email, salt, password_hash, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (email, salt, digest, _now().isoformat()),
        )
        user_id = int(cursor.lastrowid)
        conn.commit()
    finally:
        conn.close()
    _prepare_ledger(auth_dir, user_id, legacy_db)
    return {"id": user_id, "email": email}


def authenticate(auth_dir: Path, email: str, password: str) -> dict[str, object] | None:
    auth_path = init_auth(auth_dir)
    conn = connect(auth_path)
    try:
        row = conn.execute(
            "SELECT id, email, salt, password_hash FROM users WHERE email = ?",
            (email,),
        ).fetchone()
    finally:
        conn.close()
    if row is None or not password_matches(password, str(row["salt"]), str(row["password_hash"])):
        return None
    return {"id": int(row["id"]), "email": str(row["email"])}


def start_session(auth_dir: Path, user_id: int) -> str:
    auth_path = init_auth(auth_dir)
    token = secrets.token_urlsafe(32)
    expires = (_now() + timedelta(days=SESSION_DAYS)).isoformat()
    conn = connect(auth_path)
    try:
        conn.execute(
            "INSERT INTO sessions (token, user_id, expires_at) VALUES (?, ?, ?)",
            (token, user_id, expires),
        )
        conn.commit()
    finally:
        conn.close()
    return token


def user_for_token(auth_dir: Path, token: str) -> dict[str, object] | None:
    if not token:
        return None
    auth_path = init_auth(auth_dir)
    conn = connect(auth_path)
    try:
        row = conn.execute(
            """
            SELECT users.id, users.email, sessions.expires_at
            FROM sessions
            JOIN users ON users.id = sessions.user_id
            WHERE sessions.token = ?
            """,
            (token,),
        ).fetchone()
        if row is None:
            return None
        if str(row["expires_at"]) <= _now().isoformat():
            conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
            conn.commit()
            return None
    finally:
        conn.close()
    return {"id": int(row["id"]), "email": str(row["email"])}


def end_session(auth_dir: Path, token: str) -> None:
    if not token:
        return
    auth_path = init_auth(auth_dir)
    conn = connect(auth_path)
    try:
        conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
        conn.commit()
    finally:
        conn.close()


def open_user_db(auth_dir: Path, user_id: int, legacy_db: Path | None) -> Path:
    """Return this user's ledger, creating it if a session outlived the file."""
    path = user_db_path(auth_dir, user_id)
    if path.exists():
        return init_db(path)
    return _prepare_ledger(auth_dir, user_id, legacy_db)
