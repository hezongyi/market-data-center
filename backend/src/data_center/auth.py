"""Single-operator credentials and revocable sessions shared by API workers.

Argon2 verifies passwords; SQLite transactions serialize changes. No credential
or session cache is kept in the API process. The browser holds a random bearer
token and the database stores only its SHA-256 digest.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError


class AuthError(ValueError):
    def __init__(self, message: str, status: int = 401):
        super().__init__(message)
        self.status = status


class AuthStore:
    def __init__(self, path: Path, *, username: str = "admin", seed_hash: str | None = None,
                 legacy_path: Path | None = None, ttl_seconds: int = 86400):
        if ttl_seconds <= 0:
            raise ValueError("session lifetime must be positive")
        self.path, self.username, self.ttl_seconds = path, username, ttl_seconds
        self.hasher = PasswordHasher()
        path.parent.mkdir(parents=True, exist_ok=True)
        # Create with restrictive permissions before writing any credential.
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(descriptor)
        path.chmod(0o600)
        with self._connection(write=True) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS auth_users ("
                         "singleton INTEGER PRIMARY KEY CHECK(singleton=1), "
                         "username TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL, updated_at REAL NOT NULL)")
            conn.execute("CREATE TABLE IF NOT EXISTS auth_sessions ("
                         "token_hash TEXT PRIMARY KEY, username TEXT NOT NULL, "
                         "created_at REAL NOT NULL, expires_at REAL NOT NULL)")
            conn.execute("CREATE TABLE IF NOT EXISTS auth_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            if not conn.execute("SELECT 1 FROM auth_metadata WHERE key='bootstrap'").fetchone():
                encoded = seed_hash
                if legacy_path and legacy_path.exists():
                    # Malformed state aborts startup: never silently reopen setup.
                    state = json.loads(legacy_path.read_text(encoding="utf-8"))
                    encoded = state["password_hash"]
                    if not isinstance(encoded, str) or not encoded.startswith("$argon2"):
                        raise ValueError("invalid legacy authentication state")
                if encoded:
                    conn.execute("INSERT OR IGNORE INTO auth_users VALUES (1,?,?,?)",
                                 (username, encoded, time.time()))
                # Legacy sessions intentionally expire on migration. Retaining
                # them would retain the old store's possibly lost revocations.
                conn.execute("INSERT INTO auth_metadata VALUES ('bootstrap','1')")

    @contextmanager
    def _connection(self, *, write: bool = False):
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        try:
            if write:
                conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _digest(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def _verify(self, password: str, encoded: str) -> bool:
        try:
            return self.hasher.verify(encoded, password)
        except (VerificationError, InvalidHashError):
            return False

    def initialized(self) -> bool:
        with self._connection() as conn:
            return conn.execute("SELECT 1 FROM auth_users").fetchone() is not None

    def initialize(self, username: str, password: str) -> None:
        if username != self.username or len(password) < 12:
            raise AuthError("username or password does not meet requirements", 422)
        encoded = self.hasher.hash(password)
        with self._connection(write=True) as conn:
            if conn.execute("SELECT 1 FROM auth_users").fetchone():
                raise AuthError("authentication is already initialized", 409)
            conn.execute("INSERT INTO auth_users VALUES (1,?,?,?)", (username, encoded, time.time()))

    def login(self, username: str, password: str) -> str:
        with self._connection() as conn:
            user = conn.execute("SELECT * FROM auth_users WHERE username=?", (username,)).fetchone()
        if not user or not self._verify(password, user["password_hash"]):
            raise AuthError("invalid credentials")
        token = secrets.token_urlsafe(32)
        with self._connection(write=True) as conn:
            current = conn.execute("SELECT password_hash FROM auth_users WHERE username=?", (username,)).fetchone()
            # Verification outside the lock is safe only if the credential
            # checked is still current when the session is inserted.
            if not current or current[0] != user["password_hash"]:
                raise AuthError("invalid credentials")
            now = time.time()
            conn.execute("DELETE FROM auth_sessions WHERE expires_at<=?", (now,))
            conn.execute("INSERT INTO auth_sessions VALUES (?,?,?,?)",
                         (self._digest(token), username, now, now + self.ttl_seconds))
        return token

    def session(self, token: str | None) -> dict | None:
        if not token:
            return None
        with self._connection() as conn:
            row = conn.execute("SELECT username,expires_at FROM auth_sessions WHERE token_hash=? AND expires_at>?",
                               (self._digest(token), time.time())).fetchone()
        return dict(row) if row else None

    def logout(self, token: str | None) -> None:
        if token:
            with self._connection(write=True) as conn:
                conn.execute("DELETE FROM auth_sessions WHERE token_hash=?", (self._digest(token),))

    def change_password(self, token: str | None, current_password: str, new_password: str) -> None:
        session = self.session(token)
        if not session:
            raise AuthError("not authenticated")
        with self._connection() as conn:
            user = conn.execute("SELECT * FROM auth_users WHERE username=?", (session["username"],)).fetchone()
        if len(new_password) < 12 or not user or not self._verify(current_password, user["password_hash"]):
            raise AuthError("invalid password change", 422)
        encoded = self.hasher.hash(new_password)
        with self._connection(write=True) as conn:
            active = conn.execute("SELECT 1 FROM auth_sessions WHERE token_hash=? AND expires_at>?",
                                  (self._digest(token), time.time())).fetchone()
            if not active:
                raise AuthError("not authenticated")
            updated = conn.execute("UPDATE auth_users SET password_hash=?,updated_at=? "
                                   "WHERE username=? AND password_hash=?",
                                   (encoded, time.time(), user["username"], user["password_hash"]))
            if updated.rowcount != 1:
                raise AuthError("invalid password change", 422)
            conn.execute("DELETE FROM auth_sessions WHERE username=?", (user["username"],))
