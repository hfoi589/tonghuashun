"""Persistent market users, grouped watchlists, and revocable browser sessions."""

from __future__ import annotations

import json
import re
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import RLock
from typing import Callable

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from argon2.low_level import Type

from .parsed_values import SymbolLookup


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class DuplicateUserError(ValueError):
    """The normalized username is already assigned."""


@dataclass(frozen=True)
class MarketUser:
    id: int
    username: str
    enabled: bool
    must_change_password: bool
    created_at: datetime


@dataclass(frozen=True)
class WatchlistItem:
    symbol: str
    name: str
    market: str


@dataclass(frozen=True)
class WatchlistGroup:
    id: int
    name: str
    sort_order: int
    is_primary: bool
    items: tuple[WatchlistItem, ...]


@dataclass(frozen=True)
class MarketSession:
    session_id: str
    user_id: int
    csrf_token: str
    expires_at: datetime


class SQLiteMarketAccountStore:
    """Small relational store suitable for the single API process deployment."""

    def __init__(self, path: Path, *, max_symbols_per_user: int = 50) -> None:
        self.path = path.expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.max_symbols_per_user = max_symbols_per_user
        self._lock = RLock()
        self._hasher = PasswordHasher(type=Type.ID)
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._migrate()

    def _migrate(self) -> None:
        with self._lock, self._connection:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS market_users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL COLLATE NOCASE UNIQUE,
                    password_hash TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    must_change_password INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS watchlist_groups (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES market_users(id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    sort_order INTEGER NOT NULL,
                    is_primary INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(user_id, name)
                );
                CREATE TABLE IF NOT EXISTS watchlist_items (
                    group_id INTEGER NOT NULL REFERENCES watchlist_groups(id) ON DELETE CASCADE,
                    symbol TEXT NOT NULL,
                    name TEXT NOT NULL,
                    market TEXT NOT NULL,
                    sort_order INTEGER NOT NULL,
                    PRIMARY KEY(group_id, symbol)
                );
                CREATE INDEX IF NOT EXISTS watchlist_groups_user_order
                    ON watchlist_groups(user_id, sort_order);
                CREATE INDEX IF NOT EXISTS watchlist_items_group_order
                    ON watchlist_items(group_id, sort_order);
                """
            )
            columns = {
                str(row["name"])
                for row in self._connection.execute(
                    "PRAGMA table_info(watchlist_groups)"
                ).fetchall()
            }
            if "is_primary" not in columns:
                self._connection.execute(
                    "ALTER TABLE watchlist_groups ADD COLUMN is_primary INTEGER NOT NULL DEFAULT 0"
                )
            user_ids = self._connection.execute(
                "SELECT DISTINCT user_id FROM watchlist_groups"
            ).fetchall()
            for row in user_ids:
                user_id = int(row["user_id"])
                primary = self._connection.execute(
                    "SELECT id FROM watchlist_groups WHERE user_id=? AND is_primary=1 LIMIT 1",
                    (user_id,),
                ).fetchone()
                if primary is None:
                    primary = self._connection.execute(
                        """SELECT id FROM watchlist_groups WHERE user_id=?
                           ORDER BY CASE WHEN name='自选' THEN 0 ELSE 1 END,sort_order,id LIMIT 1""",
                        (user_id,),
                    ).fetchone()
                if primary is None:
                    continue
                primary_group_id = int(primary["id"])
                self._connection.execute(
                    "UPDATE watchlist_groups SET is_primary=1 WHERE id=?",
                    (primary_group_id,),
                )
                existing_items = self._connection.execute(
                    """SELECT i.symbol,i.name,i.market
                       FROM watchlist_items i
                       JOIN watchlist_groups g ON g.id=i.group_id
                       WHERE g.user_id=? AND g.id<>?
                       ORDER BY g.sort_order,g.id,i.sort_order,i.symbol""",
                    (user_id, primary_group_id),
                ).fetchall()
                for item in existing_items:
                    self._connection.execute(
                        """INSERT OR IGNORE INTO watchlist_items(group_id,symbol,name,market,sort_order)
                           VALUES(?,?,?,?,COALESCE((SELECT MAX(sort_order)+1 FROM watchlist_items WHERE group_id=?),0))""",
                        (
                            primary_group_id,
                            item["symbol"],
                            item["name"],
                            item["market"],
                            primary_group_id,
                        ),
                    )
            self._connection.execute(
                """CREATE UNIQUE INDEX IF NOT EXISTS watchlist_groups_one_primary
                   ON watchlist_groups(user_id) WHERE is_primary=1"""
            )

    @staticmethod
    def _username(value: str) -> str:
        normalized = value.strip().lower()
        if not re.fullmatch(r"[a-z0-9._-]{3,32}", normalized):
            raise ValueError("username must be 3-32 letters, digits, dots, underscores, or hyphens")
        return normalized

    @staticmethod
    def _password(value: str) -> str:
        if len(value) < 8 or len(value) > 256:
            raise ValueError("password must contain 8-256 characters")
        return value

    @staticmethod
    def _group_name(value: str) -> str:
        normalized = value.strip()
        if not normalized or len(normalized) > 24:
            raise ValueError("watchlist group name must contain 1-24 characters")
        return normalized

    @staticmethod
    def _user(row: sqlite3.Row) -> MarketUser:
        return MarketUser(
            id=int(row["id"]),
            username=str(row["username"]),
            enabled=bool(row["enabled"]),
            must_change_password=bool(row["must_change_password"]),
            created_at=datetime.fromisoformat(str(row["created_at"])),
        )

    def create_user(self, username: str, temporary_password: str) -> MarketUser:
        normalized = self._username(username)
        password = self._password(temporary_password)
        created_at = _utc_now().isoformat()
        try:
            with self._lock, self._connection:
                cursor = self._connection.execute(
                    "INSERT INTO market_users(username,password_hash,created_at) VALUES(?,?,?)",
                    (normalized, self._hasher.hash(password), created_at),
                )
                user_id = int(cursor.lastrowid)
                self._connection.execute(
                    "INSERT INTO watchlist_groups(user_id,name,sort_order,is_primary) VALUES(?,?,0,1)",
                    (user_id, "自选"),
                )
        except sqlite3.IntegrityError as error:
            raise DuplicateUserError(normalized) from error
        return self.get_user(user_id)

    def get_user(self, user_id: int) -> MarketUser:
        with self._lock:
            row = self._connection.execute(
                "SELECT id,username,enabled,must_change_password,created_at FROM market_users WHERE id=?",
                (user_id,),
            ).fetchone()
        if row is None:
            raise LookupError("market user not found")
        return self._user(row)

    def list_users(self) -> list[MarketUser]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT id,username,enabled,must_change_password,created_at FROM market_users ORDER BY id"
            ).fetchall()
        return [self._user(row) for row in rows]

    def authenticate(self, username: str, password: str) -> MarketUser | None:
        try:
            normalized = self._username(username)
        except ValueError:
            return None
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM market_users WHERE username=?",
                (normalized,),
            ).fetchone()
        if row is None or not bool(row["enabled"]):
            return None
        try:
            if not self._hasher.verify(str(row["password_hash"]), password):
                return None
        except (InvalidHashError, VerifyMismatchError):
            return None
        return self._user(row)

    def change_password(self, user_id: int, current_password: str, new_password: str) -> MarketUser:
        password = self._password(new_password)
        with self._lock:
            row = self._connection.execute(
                "SELECT password_hash FROM market_users WHERE id=? AND enabled=1",
                (user_id,),
            ).fetchone()
            if row is None:
                raise LookupError("market user not found")
            try:
                valid = self._hasher.verify(str(row["password_hash"]), current_password)
            except (InvalidHashError, VerifyMismatchError):
                valid = False
            if not valid:
                raise PermissionError("invalid current password")
            with self._connection:
                self._connection.execute(
                    "UPDATE market_users SET password_hash=?,must_change_password=0 WHERE id=?",
                    (self._hasher.hash(password), user_id),
                )
        return self.get_user(user_id)

    def reset_password(self, user_id: int, temporary_password: str) -> MarketUser:
        password = self._password(temporary_password)
        with self._lock, self._connection:
            cursor = self._connection.execute(
                "UPDATE market_users SET password_hash=?,must_change_password=1 WHERE id=?",
                (self._hasher.hash(password), user_id),
            )
            if cursor.rowcount != 1:
                raise LookupError("market user not found")
        return self.get_user(user_id)

    def set_user_enabled(self, user_id: int, enabled: bool) -> MarketUser:
        with self._lock, self._connection:
            cursor = self._connection.execute(
                "UPDATE market_users SET enabled=? WHERE id=?",
                (int(enabled), user_id),
            )
            if cursor.rowcount != 1:
                raise LookupError("market user not found")
        return self.get_user(user_id)

    def list_watchlists(self, user_id: int) -> list[WatchlistGroup]:
        self.get_user(user_id)
        with self._lock:
            groups = self._connection.execute(
                "SELECT id,name,sort_order,is_primary FROM watchlist_groups WHERE user_id=? ORDER BY sort_order,id",
                (user_id,),
            ).fetchall()
            result: list[WatchlistGroup] = []
            for group in groups:
                rows = self._connection.execute(
                    "SELECT symbol,name,market FROM watchlist_items WHERE group_id=? ORDER BY sort_order,symbol",
                    (group["id"],),
                ).fetchall()
                result.append(
                    WatchlistGroup(
                        id=int(group["id"]),
                        name=str(group["name"]),
                        sort_order=int(group["sort_order"]),
                        is_primary=bool(group["is_primary"]),
                        items=tuple(
                            WatchlistItem(
                                symbol=str(row["symbol"]),
                                name=str(row["name"]),
                                market=str(row["market"]),
                            )
                            for row in rows
                        ),
                    )
                )
        return result

    def _owned_group(self, user_id: int, group_id: int) -> sqlite3.Row:
        row = self._connection.execute(
            "SELECT id,name,sort_order,is_primary FROM watchlist_groups WHERE id=? AND user_id=?",
            (group_id, user_id),
        ).fetchone()
        if row is None:
            raise LookupError("watchlist group not found")
        return row

    def create_group(self, user_id: int, name: str) -> WatchlistGroup:
        normalized = self._group_name(name)
        self.get_user(user_id)
        try:
            with self._lock, self._connection:
                cursor = self._connection.execute(
                    """INSERT INTO watchlist_groups(user_id,name,sort_order)
                       VALUES(?,?,COALESCE((SELECT MAX(sort_order)+1 FROM watchlist_groups WHERE user_id=?),0))""",
                    (user_id, normalized, user_id),
                )
                group_id = int(cursor.lastrowid)
        except sqlite3.IntegrityError as error:
            raise ValueError("watchlist group already exists") from error
        return next(group for group in self.list_watchlists(user_id) if group.id == group_id)

    def rename_group(self, user_id: int, group_id: int, name: str) -> WatchlistGroup:
        normalized = self._group_name(name)
        try:
            with self._lock, self._connection:
                self._owned_group(user_id, group_id)
                self._connection.execute(
                    "UPDATE watchlist_groups SET name=? WHERE id=?",
                    (normalized, group_id),
                )
        except sqlite3.IntegrityError as error:
            raise ValueError("watchlist group already exists") from error
        return next(group for group in self.list_watchlists(user_id) if group.id == group_id)

    def delete_group(self, user_id: int, group_id: int) -> None:
        with self._lock, self._connection:
            group = self._owned_group(user_id, group_id)
            if bool(group["is_primary"]):
                raise ValueError("primary watchlist group cannot be deleted")
            count = self._connection.execute(
                "SELECT COUNT(*) FROM watchlist_groups WHERE user_id=?",
                (user_id,),
            ).fetchone()[0]
            if count <= 1:
                raise ValueError("at least one watchlist group is required")
            self._connection.execute("DELETE FROM watchlist_groups WHERE id=?", (group_id,))
            self._normalize_group_order(user_id)

    def reorder_groups(self, user_id: int, group_ids: list[int]) -> None:
        with self._lock, self._connection:
            current = [group.id for group in self.list_watchlists(user_id)]
            if len(group_ids) != len(set(group_ids)) or set(group_ids) != set(current):
                raise ValueError("group order must contain every group exactly once")
            for index, group_id in enumerate(group_ids):
                self._connection.execute(
                    "UPDATE watchlist_groups SET sort_order=? WHERE id=? AND user_id=?",
                    (index, group_id, user_id),
                )

    def add_symbol(self, user_id: int, group_id: int, symbol: SymbolLookup) -> WatchlistItem:
        with self._lock, self._connection:
            self._owned_group(user_id, group_id)
            primary_group = self._connection.execute(
                "SELECT id FROM watchlist_groups WHERE user_id=? AND is_primary=1 LIMIT 1",
                (user_id,),
            ).fetchone()
            if primary_group is None:
                raise LookupError("watchlist group not found")
            primary_group_id = int(primary_group["id"])
            known = self._connection.execute(
                """SELECT COUNT(DISTINCT i.symbol) FROM watchlist_items i
                   JOIN watchlist_groups g ON g.id=i.group_id WHERE g.user_id=?""",
                (user_id,),
            ).fetchone()[0]
            already_known = self._connection.execute(
                """SELECT 1 FROM watchlist_items i JOIN watchlist_groups g ON g.id=i.group_id
                   WHERE g.user_id=? AND i.symbol=? LIMIT 1""",
                (user_id, symbol.symbol),
            ).fetchone()
            if known >= self.max_symbols_per_user and already_known is None:
                raise ValueError("watchlist symbol limit reached")
            try:
                self._connection.execute(
                    """INSERT INTO watchlist_items(group_id,symbol,name,market,sort_order)
                       VALUES(?,?,?,?,COALESCE((SELECT MAX(sort_order)+1 FROM watchlist_items WHERE group_id=?),0))""",
                    (group_id, symbol.symbol, symbol.name, symbol.market, group_id),
                )
                if group_id != primary_group_id:
                    self._connection.execute(
                        """INSERT INTO watchlist_items(group_id,symbol,name,market,sort_order)
                           VALUES(?,?,?,?,COALESCE((SELECT MAX(sort_order)+1 FROM watchlist_items WHERE group_id=?),0))
                           ON CONFLICT(group_id,symbol) DO UPDATE SET
                               name=excluded.name,
                               market=excluded.market""",
                        (
                            primary_group_id,
                            symbol.symbol,
                            symbol.name,
                            symbol.market,
                            primary_group_id,
                        ),
                    )
            except sqlite3.IntegrityError as error:
                raise ValueError("symbol already exists in this group") from error
        return WatchlistItem(symbol=symbol.symbol, name=symbol.name, market=symbol.market)

    def remove_symbol(self, user_id: int, group_id: int, symbol: str) -> None:
        with self._lock, self._connection:
            self._owned_group(user_id, group_id)
            cursor = self._connection.execute(
                "DELETE FROM watchlist_items WHERE group_id=? AND symbol=?",
                (group_id, symbol),
            )
            if cursor.rowcount != 1:
                raise LookupError("watchlist symbol not found")
            self._normalize_item_order(group_id)

    def reorder_symbols(self, user_id: int, group_id: int, symbols: list[str]) -> None:
        with self._lock, self._connection:
            self._owned_group(user_id, group_id)
            current = [
                str(row[0])
                for row in self._connection.execute(
                    "SELECT symbol FROM watchlist_items WHERE group_id=? ORDER BY sort_order,symbol",
                    (group_id,),
                ).fetchall()
            ]
            if len(symbols) != len(set(symbols)) or set(symbols) != set(current):
                raise ValueError("symbol order must contain every group symbol exactly once")
            for index, symbol in enumerate(symbols):
                self._connection.execute(
                    "UPDATE watchlist_items SET sort_order=? WHERE group_id=? AND symbol=?",
                    (index, group_id, symbol),
                )

    def move_symbol(
        self,
        user_id: int,
        source_group_id: int,
        target_group_id: int,
        symbol: str,
        target_index: int,
    ) -> None:
        with self._lock, self._connection:
            self._owned_group(user_id, source_group_id)
            self._owned_group(user_id, target_group_id)
            row = self._connection.execute(
                "SELECT name,market FROM watchlist_items WHERE group_id=? AND symbol=?",
                (source_group_id, symbol),
            ).fetchone()
            if row is None:
                raise LookupError("watchlist symbol not found")
            if source_group_id == target_group_id:
                group = next(
                    item for item in self.list_watchlists(user_id) if item.id == source_group_id
                )
                ordered = [item.symbol for item in group.items if item.symbol != symbol]
                ordered.insert(max(0, min(target_index, len(ordered))), symbol)
                self.reorder_symbols(user_id, source_group_id, ordered)
                return
            if self._connection.execute(
                "SELECT 1 FROM watchlist_items WHERE group_id=? AND symbol=?",
                (target_group_id, symbol),
            ).fetchone() is not None:
                raise ValueError("symbol already exists in target group")
            self._connection.execute(
                "DELETE FROM watchlist_items WHERE group_id=? AND symbol=?",
                (source_group_id, symbol),
            )
            self._normalize_item_order(source_group_id)
            target_symbols = [
                str(item[0])
                for item in self._connection.execute(
                    "SELECT symbol FROM watchlist_items WHERE group_id=? ORDER BY sort_order,symbol",
                    (target_group_id,),
                ).fetchall()
            ]
            index = max(0, min(target_index, len(target_symbols)))
            self._connection.execute(
                "UPDATE watchlist_items SET sort_order=sort_order+1 WHERE group_id=? AND sort_order>=?",
                (target_group_id, index),
            )
            self._connection.execute(
                "INSERT INTO watchlist_items(group_id,symbol,name,market,sort_order) VALUES(?,?,?,?,?)",
                (target_group_id, symbol, row["name"], row["market"], index),
            )

    def _normalize_group_order(self, user_id: int) -> None:
        rows = self._connection.execute(
            "SELECT id FROM watchlist_groups WHERE user_id=? ORDER BY sort_order,id",
            (user_id,),
        ).fetchall()
        for index, row in enumerate(rows):
            self._connection.execute(
                "UPDATE watchlist_groups SET sort_order=? WHERE id=?",
                (index, row[0]),
            )

    def _normalize_item_order(self, group_id: int) -> None:
        rows = self._connection.execute(
            "SELECT symbol FROM watchlist_items WHERE group_id=? ORDER BY sort_order,symbol",
            (group_id,),
        ).fetchall()
        for index, row in enumerate(rows):
            self._connection.execute(
                "UPDATE watchlist_items SET sort_order=? WHERE group_id=? AND symbol=?",
                (index, group_id, row[0]),
            )


class InMemoryMarketSessionStore:
    def __init__(
        self,
        *,
        ttl: timedelta = timedelta(days=7),
        now: Callable[[], datetime] = _utc_now,
    ) -> None:
        self.ttl = ttl
        self._now = now
        self._sessions: dict[str, MarketSession] = {}
        self._lock = RLock()

    def create(self, user_id: int) -> MarketSession:
        session = MarketSession(
            session_id=secrets.token_urlsafe(32),
            user_id=user_id,
            csrf_token=secrets.token_urlsafe(32),
            expires_at=self._now() + self.ttl,
        )
        with self._lock:
            self._sessions[session.session_id] = session
        return session

    def get(self, session_id: str | None) -> MarketSession | None:
        if not session_id:
            return None
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None or session.expires_at <= self._now():
                self._sessions.pop(session_id, None)
                return None
            return session

    def revoke(self, session_id: str | None) -> None:
        if session_id:
            with self._lock:
                self._sessions.pop(session_id, None)

    def revoke_user(self, user_id: int) -> None:
        with self._lock:
            for session_id in [
                key for key, session in self._sessions.items() if session.user_id == user_id
            ]:
                self._sessions.pop(session_id, None)


class RedisMarketSessionStore:
    """Redis-backed sessions with a reverse index for administrator revocation."""

    def __init__(self, client: object, *, ttl: timedelta = timedelta(days=7)) -> None:
        for method in ("delete", "get", "sadd", "setex", "smembers", "srem"):
            if not callable(getattr(client, method, None)):
                raise TypeError(f"RedisMarketSessionStore requires redis client method: {method}")
        self.client = client
        self.ttl = ttl

    @staticmethod
    def _key(session_id: str) -> str:
        return f"ths:market:sessions:{session_id}"

    @staticmethod
    def _user_key(user_id: int) -> str:
        return f"ths:market:user-sessions:{user_id}"

    def create(self, user_id: int) -> MarketSession:
        session = MarketSession(
            session_id=secrets.token_urlsafe(32),
            user_id=user_id,
            csrf_token=secrets.token_urlsafe(32),
            expires_at=_utc_now() + self.ttl,
        )
        seconds = max(1, round(self.ttl.total_seconds()))
        self.client.setex(
            self._key(session.session_id),
            seconds,
            json.dumps(
                {
                    "user_id": user_id,
                    "csrf_token": session.csrf_token,
                    "expires_at": session.expires_at.isoformat(),
                }
            ),
        )
        self.client.sadd(self._user_key(user_id), session.session_id)
        return session

    @staticmethod
    def _text(value: object) -> str:
        return value.decode("utf-8") if isinstance(value, bytes) else str(value)

    def get(self, session_id: str | None) -> MarketSession | None:
        if not session_id:
            return None
        raw = self.client.get(self._key(session_id))
        if raw is None:
            return None
        try:
            value = json.loads(self._text(raw))
            session = MarketSession(
                session_id=session_id,
                user_id=int(value["user_id"]),
                csrf_token=str(value["csrf_token"]),
                expires_at=datetime.fromisoformat(str(value["expires_at"])),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            self.client.delete(self._key(session_id))
            return None
        if session.expires_at <= _utc_now():
            self.revoke(session_id)
            return None
        return session

    def revoke(self, session_id: str | None) -> None:
        session = self.get(session_id) if session_id else None
        if session_id:
            self.client.delete(self._key(session_id))
        if session is not None:
            self.client.srem(self._user_key(session.user_id), session.session_id)

    def revoke_user(self, user_id: int) -> None:
        key = self._user_key(user_id)
        for raw in self.client.smembers(key):
            self.client.delete(self._key(self._text(raw)))
        self.client.delete(key)
