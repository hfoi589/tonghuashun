"""Password-hash verification and server-side administrator sessions."""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

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

    def __init__(self, password_hash: str | None, session_ttl: timedelta = timedelta(hours=8)) -> None:
        self.password_hash = password_hash
        self.session_ttl = session_ttl
        self._hasher = PasswordHasher(type=Type.ID)
        self._sessions: dict[str, AdminSession] = {}

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
            session_id=secrets.token_urlsafe(32),
            csrf_token=secrets.token_urlsafe(32),
            expires_at=_now() + self.session_ttl,
        )
        self._sessions[session.session_id] = session
        return session

    def valid_session(self, session_id: str | None) -> AdminSession | None:
        if not session_id:
            return None
        session = self._sessions.get(session_id)
        if session is None or session.expires_at <= _now():
            self._sessions.pop(session_id, None)
            return None
        return session

    def revoke(self, session_id: str | None) -> None:
        if session_id:
            self._sessions.pop(session_id, None)
