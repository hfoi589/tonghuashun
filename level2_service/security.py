"""Password-hash verification and server-side administrator sessions."""

from __future__ import annotations

import secrets
import hashlib
import hmac
import os
import tempfile
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import RLock
from typing import Callable

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from argon2.low_level import Type


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class AdminSession:
    session_id: str
    csrf_token: str
    expires_at: datetime


class AdminSessionManager:
    """Keeps random session and CSRF tokens in memory; a Redis version may replace it."""

    def __init__(
        self,
        password_hash: str | None,
        session_ttl: timedelta = timedelta(hours=8),
        session_secret: str | None = None,
        persist_password_hash: Callable[[str], None] | None = None,
    ) -> None:
        self.password_hash = password_hash
        self.session_ttl = session_ttl
        self._session_secret = session_secret.encode("utf-8") if session_secret else None
        self._hasher = PasswordHasher(type=Type.ID)
        self._persist_password_hash = persist_password_hash
        self._sessions: dict[str, AdminSession] = {}
        self._login_failures: dict[str, deque[datetime]] = {}
        self._lock = RLock()
        self._max_sessions = 2048
        self.login_failure_limit = 5
        self.login_failure_window = timedelta(minutes=1)

    @property
    def configured(self) -> bool:
        return self.password_hash is not None

    def authenticate(self, password: str) -> AdminSession | None:
        if self.password_hash is None:
            return None
        try:
            if not self._hasher.verify(self.password_hash, password):
                return None
        except (InvalidHashError, VerifyMismatchError):
            return None
        session = AdminSession(
            session_id=self._new_session_id(),
            csrf_token=secrets.token_urlsafe(32),
            expires_at=_now() + self.session_ttl,
        )
        with self._lock:
            self._prune_locked()
            self._sessions[session.session_id] = session
            if len(self._sessions) > self._max_sessions:
                oldest = min(self._sessions.values(), key=lambda item: item.expires_at)
                self._sessions.pop(oldest.session_id, None)
        return session

    def change_password(self, current_password: str, new_password: str) -> bool:
        """Verify and atomically rotate the password used by future sessions."""
        if self.password_hash is None:
            return False
        try:
            if not self._hasher.verify(self.password_hash, current_password):
                return False
        except (InvalidHashError, VerifyMismatchError):
            return False
        replacement = self._hasher.hash(new_password)
        if self._persist_password_hash is not None:
            self._persist_password_hash(replacement)
        self.password_hash = replacement
        with self._lock:
            self._sessions.clear()
            self._login_failures.clear()
        return True

    def login_allowed(self, identifier: str) -> bool:
        now = _now()
        with self._lock:
            self._prune_locked(now)
            failures = self._login_failures.get(identifier)
            if failures is None:
                return True
            now = _now()
            cutoff = now - self.login_failure_window
            while failures and failures[0] <= cutoff:
                failures.popleft()
            if not failures:
                self._login_failures.pop(identifier, None)
                return True
            return len(failures) < self.login_failure_limit

    def record_login_failure(self, identifier: str) -> None:
        self.login_allowed(identifier)
        with self._lock:
            self._login_failures.setdefault(identifier, deque()).append(_now())

    def record_login_success(self, identifier: str) -> None:
        with self._lock:
            self._login_failures.pop(identifier, None)

    def valid_session(self, session_id: str | None) -> AdminSession | None:
        if not session_id:
            return None
        with self._lock:
            self._prune_locked()
            session = self._sessions.get(session_id)
            if session is None or not self._valid_session_id(session_id) or session.expires_at <= _now():
                self._sessions.pop(session_id, None)
                return None
            return session

    def revoke(self, session_id: str | None) -> None:
        if session_id:
            with self._lock:
                self._sessions.pop(session_id, None)

    def _new_session_id(self) -> str:
        nonce = secrets.token_urlsafe(32)
        if self._session_secret is None:
            return nonce
        signature = hmac.new(self._session_secret, nonce.encode("ascii"), hashlib.sha256).hexdigest()
        return f"{nonce}.{signature}"

    def _prune_locked(self, now: datetime | None = None) -> None:
        current = now or _now()
        expired = [
            session_id
            for session_id, session in self._sessions.items()
            if session.expires_at <= current
        ]
        for session_id in expired:
            self._sessions.pop(session_id, None)
        cutoff = current - self.login_failure_window
        stale_identifiers = []
        for identifier, failures in self._login_failures.items():
            while failures and failures[0] <= cutoff:
                failures.popleft()
            if not failures:
                stale_identifiers.append(identifier)
        for identifier in stale_identifiers:
            self._login_failures.pop(identifier, None)

    def _valid_session_id(self, session_id: str) -> bool:
        if self._session_secret is None:
            return True
        try:
            nonce, signature = session_id.rsplit(".", 1)
        except ValueError:
            return False
        expected = hmac.new(self._session_secret, nonce.encode("ascii"), hashlib.sha256).hexdigest()
        return hmac.compare_digest(signature, expected)


def persist_password_hash(path: Path, password_hash: str) -> None:
    """Atomically persist an Argon2id hash with owner-only permissions."""
    if not password_hash.startswith("$argon2id$"):
        raise ValueError("password hash must be Argon2id")
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent, text=True)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as temporary:
            descriptor = -1
            temporary.write(password_hash)
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, path)
        os.chmod(path, 0o600)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
