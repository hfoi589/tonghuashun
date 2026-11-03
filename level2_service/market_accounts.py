"""Persistent market users, grouped watchlists, and revocable browser sessions."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import RLock
from typing import Callable
from zoneinfo import ZoneInfo

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from argon2.low_level import Type

from .parsed_values import SymbolLookup


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


_SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


class DuplicateUserError(ValueError):
    """The normalized username is already assigned."""


class MonitorLimitError(ValueError):
    """The site-wide distinct watchlist symbol limit has been reached."""


MONITOR_LIMIT_MESSAGE = "监控数量达到上限，请联系管理员"

EMAIL_PATTERN = re.compile(r"[^@\s<>\"',;:]{1,64}@[A-Za-z0-9.-]{1,190}\.[A-Za-z]{2,24}")


class EmailCodeRateLimited(RuntimeError):
    """A verification code was requested too soon or too often."""

    def __init__(self, retry_after: int) -> None:
        super().__init__("verification code requested too frequently")
        self.retry_after = retry_after


@dataclass(frozen=True)
class MarketUser:
    id: int
    username: str
    enabled: bool
    must_change_password: bool
    role: str
    created_at: datetime
    email: str | None = None


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

    def __init__(
        self,
        path: Path,
        *,
        max_symbols_per_user: int = 50,
        max_total_symbols: int = 50,
    ) -> None:
        self.path = path.expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.max_symbols_per_user = max_symbols_per_user
        self.max_total_symbols = max_total_symbols
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
                    role TEXT NOT NULL DEFAULT 'user',
                    vibe_watchlist_synced INTEGER NOT NULL DEFAULT 0,
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
                    monitoring_enabled INTEGER NOT NULL DEFAULT 1,
                    monitoring_enabled_at TEXT,
                    PRIMARY KEY(group_id, symbol)
                );
                CREATE INDEX IF NOT EXISTS watchlist_groups_user_order
                    ON watchlist_groups(user_id, sort_order);
                CREATE INDEX IF NOT EXISTS watchlist_items_group_order
                    ON watchlist_items(group_id, sort_order);
                CREATE TABLE IF NOT EXISTS app_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS email_codes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    purpose TEXT NOT NULL,
                    email TEXT NOT NULL,
                    username TEXT,
                    salt TEXT NOT NULL,
                    code_hash TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    used INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS email_codes_lookup
                    ON email_codes(purpose, email, id);
                CREATE TABLE IF NOT EXISTS email_tickets (
                    token_hash TEXT PRIMARY KEY,
                    purpose TEXT NOT NULL,
                    email TEXT NOT NULL,
                    username TEXT,
                    expires_at TEXT NOT NULL
                );
                """
            )
            user_columns = {
                str(row["name"])
                for row in self._connection.execute(
                    "PRAGMA table_info(market_users)"
                ).fetchall()
            }
            if "role" not in user_columns:
                self._connection.execute(
                    "ALTER TABLE market_users ADD COLUMN role TEXT NOT NULL DEFAULT 'user'"
                )
            if "email" not in user_columns:
                self._connection.execute("ALTER TABLE market_users ADD COLUMN email TEXT")
            self._connection.execute(
                """CREATE UNIQUE INDEX IF NOT EXISTS market_users_email
                   ON market_users(email COLLATE NOCASE) WHERE email IS NOT NULL"""
            )
            if "vibe_watchlist_synced" not in user_columns:
                self._connection.execute(
                    "ALTER TABLE market_users ADD COLUMN vibe_watchlist_synced INTEGER NOT NULL DEFAULT 0"
                )
            # Wilson is the single default administrator for the integrated workspace.
            # User creation never accepts a role from the request and always uses the
            # database default (ordinary user).
            self._connection.execute(
                "UPDATE market_users SET role='user' WHERE lower(username)<>'wilson'"
            )
            self._connection.execute(
                "UPDATE market_users SET role='admin' WHERE lower(username)='wilson'"
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
            item_columns = {
                str(row["name"])
                for row in self._connection.execute(
                    "PRAGMA table_info(watchlist_items)"
                ).fetchall()
            }
            if "monitoring_enabled" not in item_columns:
                self._connection.execute(
                    "ALTER TABLE watchlist_items ADD COLUMN monitoring_enabled INTEGER NOT NULL DEFAULT 1"
                )
            if "monitoring_enabled_at" not in item_columns:
                self._connection.execute(
                    "ALTER TABLE watchlist_items ADD COLUMN monitoring_enabled_at TEXT"
                )
            self._connection.execute(
                """UPDATE watchlist_items
                   SET monitoring_enabled_at=(
                       SELECT u.created_at FROM watchlist_groups g
                       JOIN market_users u ON u.id=g.user_id
                       WHERE g.id=watchlist_items.group_id
                   )
                   WHERE monitoring_enabled=1 AND monitoring_enabled_at IS NULL"""
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
        if not value or len(value) > 256:
            raise ValueError("password must contain 1-256 characters")
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
            role=str(row["role"]),
            created_at=datetime.fromisoformat(str(row["created_at"])),
            email=str(row["email"]) if row["email"] is not None else None,
        )

    def total_symbol_count(self) -> int:
        """Distinct symbols across every user's watchlists."""
        with self._lock:
            return int(
                self._connection.execute(
                    "SELECT COUNT(DISTINCT symbol) FROM watchlist_items"
                ).fetchone()[0]
            )

    def _symbol_tracked_by_anyone(self, symbol: str) -> bool:
        return (
            self._connection.execute(
                "SELECT 1 FROM watchlist_items WHERE symbol=? LIMIT 1", (symbol,)
            ).fetchone()
            is not None
        )

    def create_user(self, username: str, temporary_password: str) -> MarketUser:
        normalized = self._username(username)
        password = self._password(temporary_password)
        password_hash = self._hasher.hash(password)
        created_at = _utc_now().isoformat()
        try:
            with self._lock, self._connection:
                cursor = self._connection.execute(
                    "INSERT INTO market_users(username,password_hash,created_at,role) VALUES(?,?,?,?)",
                    (normalized, password_hash, created_at, "user"),
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
                "SELECT id,username,enabled,must_change_password,role,created_at,email FROM market_users WHERE id=?",
                (user_id,),
            ).fetchone()
        if row is None:
            raise LookupError("market user not found")
        return self._user(row)

    def list_users(self) -> list[MarketUser]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT id,username,enabled,must_change_password,role,created_at,email FROM market_users ORDER BY id"
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
        password_hash = self._hasher.hash(password)
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
                    (password_hash, user_id),
                )
        return self.get_user(user_id)

    def reset_password(self, user_id: int, temporary_password: str) -> MarketUser:
        password = self._password(temporary_password)
        password_hash = self._hasher.hash(password)
        with self._lock, self._connection:
            cursor = self._connection.execute(
                "UPDATE market_users SET password_hash=?,must_change_password=1 WHERE id=?",
                (password_hash, user_id),
            )
            if cursor.rowcount != 1:
                raise LookupError("market user not found")
        return self.get_user(user_id)

    # ---- self-service registration, password reset, and site settings ----

    CODE_TTL = timedelta(minutes=10)
    TICKET_TTL = timedelta(minutes=15)
    CODE_RESEND_SECONDS = 60
    CODE_MAX_ATTEMPTS = 5
    CODE_DAILY_LIMIT = 10

    @staticmethod
    def normalize_email(value: str) -> str:
        normalized = value.strip().lower()
        if len(normalized) > 254 or not EMAIL_PATTERN.fullmatch(normalized):
            raise ValueError("邮箱格式不正确")
        return normalized

    @staticmethod
    def check_new_password(value: str) -> str:
        if len(value) < 8 or len(value) > 256:
            raise ValueError("密码长度需为 8-256 个字符")
        return value

    def get_setting(self, key: str) -> str | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT value FROM app_settings WHERE key=?", (key,)
            ).fetchone()
        return str(row["value"]) if row is not None else None

    def set_setting(self, key: str, value: str) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO app_settings(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def find_user_by_email(self, email: str) -> MarketUser | None:
        normalized = self.normalize_email(email)
        with self._lock:
            row = self._connection.execute(
                "SELECT id,username,enabled,must_change_password,role,created_at,email "
                "FROM market_users WHERE email=? COLLATE NOCASE",
                (normalized,),
            ).fetchone()
        return self._user(row) if row is not None else None

    def username_taken(self, username: str) -> bool:
        normalized = self._username(username)
        with self._lock:
            return (
                self._connection.execute(
                    "SELECT 1 FROM market_users WHERE username=?", (normalized,)
                ).fetchone()
                is not None
            )

    def create_registered_user(self, username: str, email: str, password: str) -> MarketUser:
        normalized = self._username(username)
        address = self.normalize_email(email)
        password_hash = self._hasher.hash(self.check_new_password(password))
        try:
            with self._lock, self._connection:
                cursor = self._connection.execute(
                    "INSERT INTO market_users(username,password_hash,created_at,role,email,must_change_password) "
                    "VALUES(?,?,?,?,?,0)",
                    (normalized, password_hash, _utc_now().isoformat(), "user", address),
                )
                user_id = int(cursor.lastrowid)
                self._connection.execute(
                    "INSERT INTO watchlist_groups(user_id,name,sort_order,is_primary) VALUES(?,?,0,1)",
                    (user_id, "自选"),
                )
        except sqlite3.IntegrityError as error:
            raise DuplicateUserError(normalized) from error
        return self.get_user(user_id)

    def set_password(self, user_id: int, new_password: str) -> MarketUser:
        password_hash = self._hasher.hash(self.check_new_password(new_password))
        with self._lock, self._connection:
            cursor = self._connection.execute(
                "UPDATE market_users SET password_hash=?,must_change_password=0 WHERE id=?",
                (password_hash, user_id),
            )
            if cursor.rowcount != 1:
                raise LookupError("market user not found")
        return self.get_user(user_id)

    @staticmethod
    def _code_hash(salt: str, code: str) -> str:
        return hashlib.sha256(f"{salt}:{code}".encode()).hexdigest()

    def issue_email_code(
        self, purpose: str, email: str, username: str | None = None
    ) -> str:
        """Create a six-digit code, invalidating older ones for the same target."""
        address = self.normalize_email(email)
        now = _utc_now()
        with self._lock, self._connection:
            last = self._connection.execute(
                "SELECT created_at FROM email_codes WHERE purpose=? AND email=? ORDER BY id DESC LIMIT 1",
                (purpose, address),
            ).fetchone()
            if last is not None:
                elapsed = (now - datetime.fromisoformat(str(last["created_at"]))).total_seconds()
                if elapsed < self.CODE_RESEND_SECONDS:
                    raise EmailCodeRateLimited(int(self.CODE_RESEND_SECONDS - elapsed) + 1)
            today = self._connection.execute(
                "SELECT COUNT(*) FROM email_codes WHERE email=? AND created_at>?",
                (address, (now - timedelta(days=1)).isoformat()),
            ).fetchone()[0]
            if today >= self.CODE_DAILY_LIMIT:
                raise EmailCodeRateLimited(3600)
            self._connection.execute(
                "UPDATE email_codes SET used=1 WHERE purpose=? AND email=?",
                (purpose, address),
            )
            code = f"{secrets.randbelow(1_000_000):06d}"
            salt = secrets.token_hex(8)
            self._connection.execute(
                "INSERT INTO email_codes(purpose,email,username,salt,code_hash,created_at,expires_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (
                    purpose,
                    address,
                    username,
                    salt,
                    self._code_hash(salt, code),
                    now.isoformat(),
                    (now + self.CODE_TTL).isoformat(),
                ),
            )
            self._connection.execute(
                "DELETE FROM email_codes WHERE expires_at<?",
                ((now - timedelta(days=2)).isoformat(),),
            )
        return code

    def discard_email_codes(self, purpose: str, email: str) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "DELETE FROM email_codes WHERE id=(SELECT MAX(id) FROM email_codes WHERE purpose=? AND email=?)",
                (purpose, self.normalize_email(email)),
            )

    def verify_email_code(self, purpose: str, email: str, code: str) -> str | None:
        """Return a one-time ticket for a correct code; None for any failure."""
        address = self.normalize_email(email)
        now = _utc_now()
        with self._lock, self._connection:
            row = self._connection.execute(
                "SELECT * FROM email_codes WHERE purpose=? AND email=? AND used=0 ORDER BY id DESC LIMIT 1",
                (purpose, address),
            ).fetchone()
            if row is None or datetime.fromisoformat(str(row["expires_at"])) <= now:
                return None
            if int(row["attempts"]) >= self.CODE_MAX_ATTEMPTS:
                return None
            self._connection.execute(
                "UPDATE email_codes SET attempts=attempts+1 WHERE id=?", (row["id"],)
            )
            expected = str(row["code_hash"])
            candidate = self._code_hash(str(row["salt"]), code.strip())
            if not hmac.compare_digest(expected, candidate):
                return None
            self._connection.execute("UPDATE email_codes SET used=1 WHERE id=?", (row["id"],))
            token = secrets.token_urlsafe(32)
            self._connection.execute(
                "INSERT INTO email_tickets(token_hash,purpose,email,username,expires_at) VALUES(?,?,?,?,?)",
                (
                    hashlib.sha256(token.encode()).hexdigest(),
                    purpose,
                    address,
                    row["username"],
                    (now + self.TICKET_TTL).isoformat(),
                ),
            )
        return token

    def consume_ticket(self, purpose: str, ticket: str) -> tuple[str, str | None] | None:
        """Spend a ticket exactly once; returns (email, username) or None."""
        token_hash = hashlib.sha256(ticket.encode()).hexdigest()
        with self._lock, self._connection:
            row = self._connection.execute(
                "SELECT * FROM email_tickets WHERE token_hash=? AND purpose=?",
                (token_hash, purpose),
            ).fetchone()
            if row is None:
                return None
            self._connection.execute("DELETE FROM email_tickets WHERE token_hash=?", (token_hash,))
            if datetime.fromisoformat(str(row["expires_at"])) <= _utc_now():
                return None
            return str(row["email"]), (str(row["username"]) if row["username"] else None)

    def peek_ticket(self, purpose: str, ticket: str) -> bool:
        with self._lock:
            row = self._connection.execute(
                "SELECT expires_at FROM email_tickets WHERE token_hash=? AND purpose=?",
                (hashlib.sha256(ticket.encode()).hexdigest(), purpose),
            ).fetchone()
        return row is not None and datetime.fromisoformat(str(row["expires_at"])) > _utc_now()

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
            rows = self._connection.execute(
                """SELECT g.id,g.name,g.sort_order,g.is_primary,
                          i.symbol,i.name AS item_name,i.market,i.sort_order AS item_order
                   FROM watchlist_groups g
                   LEFT JOIN watchlist_items i ON i.group_id=g.id
                   WHERE g.user_id=?
                   ORDER BY g.sort_order,g.id,i.sort_order,i.symbol""",
                (user_id,),
            ).fetchall()
        groups: dict[int, dict[str, object]] = {}
        for row in rows:
            group_id = int(row["id"])
            group = groups.setdefault(
                group_id,
                {
                    "name": str(row["name"]),
                    "sort_order": int(row["sort_order"]),
                    "is_primary": bool(row["is_primary"]),
                    "items": [],
                },
            )
            if row["symbol"] is not None:
                group["items"].append(
                    WatchlistItem(
                        symbol=str(row["symbol"]),
                        name=str(row["item_name"]),
                        market=str(row["market"]),
                    )
                )
        return [
            WatchlistGroup(
                id=group_id,
                name=str(value["name"]),
                sort_order=int(value["sort_order"]),
                is_primary=bool(value["is_primary"]),
                items=tuple(value["items"]),
            )
            for group_id, value in groups.items()
        ]

    def vibe_watchlist_synced(self, user_id: int) -> bool:
        with self._lock:
            row = self._connection.execute(
                "SELECT vibe_watchlist_synced FROM market_users WHERE id=?",
                (user_id,),
            ).fetchone()
        if row is None:
            raise LookupError("market user not found")
        return bool(row["vibe_watchlist_synced"])

    def mark_vibe_watchlist_synced(self, user_id: int) -> None:
        with self._lock, self._connection:
            cursor = self._connection.execute(
                "UPDATE market_users SET vibe_watchlist_synced=1 WHERE id=?",
                (user_id,),
            )
            if cursor.rowcount != 1:
                raise LookupError("market user not found")

    def merge_primary_symbols(self, user_id: int, symbols: list[SymbolLookup]) -> None:
        """Import legacy Vibe favorites into this user's primary Market list."""
        with self._lock, self._connection:
            user = self._connection.execute(
                "SELECT id FROM market_users WHERE id=?", (user_id,)
            ).fetchone()
            if user is None:
                raise LookupError("market user not found")
            primary = self._connection.execute(
                "SELECT id FROM watchlist_groups WHERE user_id=? AND is_primary=1 LIMIT 1",
                (user_id,),
            ).fetchone()
            if primary is None:
                raise LookupError("primary watchlist group not found")
            group_id = int(primary["id"])
            rows = self._connection.execute(
                """SELECT DISTINCT i.symbol FROM watchlist_items i
                   JOIN watchlist_groups g ON g.id=i.group_id WHERE g.user_id=?""",
                (user_id,),
            ).fetchall()
            existing = {str(row["symbol"]) for row in rows}
            count = len(existing)
            for symbol in symbols:
                if symbol.symbol in existing:
                    continue
                if count >= self.max_symbols_per_user:
                    raise ValueError("watchlist symbol limit reached during Vibe sync")
                if (
                    not self._symbol_tracked_by_anyone(symbol.symbol)
                    and self.total_symbol_count() >= self.max_total_symbols
                ):
                    raise MonitorLimitError(MONITOR_LIMIT_MESSAGE)
                self._connection.execute(
                    """INSERT INTO watchlist_items(
                           group_id,symbol,name,market,sort_order,monitoring_enabled_at
                       ) VALUES(?,?,?,?,COALESCE((SELECT MAX(sort_order)+1 FROM watchlist_items WHERE group_id=?),0),?)""",
                    (
                        group_id,
                        symbol.symbol,
                        symbol.name,
                        symbol.market,
                        group_id,
                        _utc_now().isoformat(),
                    ),
                )
                existing.add(symbol.symbol)
                count += 1

    def has_watchlist_symbol(self, user_id: int, symbol: str) -> bool:
        with self._lock:
            row = self._connection.execute(
                """SELECT 1 FROM watchlist_items i JOIN watchlist_groups g ON g.id=i.group_id
                   WHERE g.user_id=? AND i.symbol=? LIMIT 1""",
                (user_id, symbol),
            ).fetchone()
        return row is not None

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
            existing_in_group = self._connection.execute(
                "SELECT symbol,name,market FROM watchlist_items WHERE group_id=? AND symbol=?",
                (group_id, symbol.symbol),
            ).fetchone()
            if existing_in_group is not None:
                return WatchlistItem(
                    symbol=str(existing_in_group["symbol"]),
                    name=str(existing_in_group["name"]),
                    market=str(existing_in_group["market"]),
                )
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
            if (
                not self._symbol_tracked_by_anyone(symbol.symbol)
                and self.total_symbol_count() >= self.max_total_symbols
            ):
                raise MonitorLimitError(MONITOR_LIMIT_MESSAGE)
            try:
                monitoring_enabled_at = _utc_now().isoformat()
                self._connection.execute(
                    """INSERT INTO watchlist_items(
                           group_id,symbol,name,market,sort_order,monitoring_enabled_at
                       ) VALUES(?,?,?,?,COALESCE((SELECT MAX(sort_order)+1 FROM watchlist_items WHERE group_id=?),0),?)""",
                    (group_id, symbol.symbol, symbol.name, symbol.market, group_id, monitoring_enabled_at),
                )
                if group_id != primary_group_id:
                    self._connection.execute(
                        """INSERT INTO watchlist_items(
                               group_id,symbol,name,market,sort_order,monitoring_enabled_at
                           ) VALUES(?,?,?,?,COALESCE((SELECT MAX(sort_order)+1 FROM watchlist_items WHERE group_id=?),0),?)
                           ON CONFLICT(group_id,symbol) DO UPDATE SET
                               name=excluded.name,
                               market=excluded.market,
                               monitoring_enabled_at=excluded.monitoring_enabled_at""",
                        (
                            primary_group_id,
                            symbol.symbol,
                            symbol.name,
                            symbol.market,
                            primary_group_id,
                            monitoring_enabled_at,
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

    def remove_symbol_everywhere(self, user_id: int, symbol: str) -> None:
        """Remove one symbol from the primary watchlist and every custom group."""
        with self._lock, self._connection:
            group_rows = self._connection.execute(
                """SELECT DISTINCT g.id FROM watchlist_groups g
                   JOIN watchlist_items i ON i.group_id=g.id
                   WHERE g.user_id=? AND i.symbol=?""",
                (user_id, symbol),
            ).fetchall()
            if not group_rows:
                raise LookupError("watchlist symbol not found")
            group_ids = [int(row["id"]) for row in group_rows]
            self._connection.execute(
                """DELETE FROM watchlist_items
                   WHERE symbol=? AND group_id IN (
                       SELECT id FROM watchlist_groups WHERE user_id=?
                   )""",
                (symbol, user_id),
            )
            for group_id in group_ids:
                self._normalize_item_order(group_id)

    def set_monitoring_enabled(
        self,
        user_id: int,
        symbol: str,
        enabled: bool,
    ) -> bool:
        """Toggle collection for one user's symbol without removing it."""
        with self._lock, self._connection:
            if enabled:
                cursor = self._connection.execute(
                    """UPDATE watchlist_items
                       SET monitoring_enabled=1,monitoring_enabled_at=?
                       WHERE symbol=? AND group_id IN (
                           SELECT id FROM watchlist_groups WHERE user_id=?
                       )""",
                    (_utc_now().isoformat(), symbol, user_id),
                )
            else:
                cursor = self._connection.execute(
                    """UPDATE watchlist_items SET monitoring_enabled=0
                   WHERE symbol=? AND group_id IN (
                       SELECT id FROM watchlist_groups WHERE user_id=?
                   )""",
                    (symbol, user_id),
                )
            if cursor.rowcount < 1:
                raise LookupError("watchlist symbol not found")
        return enabled

    def monitoring_state(self, user_id: int) -> dict[str, bool]:
        with self._lock:
            rows = self._connection.execute(
                """SELECT i.symbol,MAX(i.monitoring_enabled) AS enabled
                   FROM watchlist_items i
                   JOIN watchlist_groups g ON g.id=i.group_id
                   WHERE g.user_id=? GROUP BY i.symbol ORDER BY i.symbol""",
                (user_id,),
            ).fetchall()
        return {str(row["symbol"]): bool(row["enabled"]) for row in rows}

    def list_monitored_symbols(self) -> list[str]:
        """Return the de-duplicated union enabled by any active market user."""
        with self._lock:
            rows = self._connection.execute(
                """SELECT DISTINCT i.symbol
                   FROM watchlist_items i
                   JOIN watchlist_groups g ON g.id=i.group_id
                   JOIN market_users u ON u.id=g.user_id
                   WHERE u.enabled=1 AND i.monitoring_enabled=1
                   ORDER BY i.symbol"""
            ).fetchall()
        return [str(row["symbol"]) for row in rows]

    def list_admin_monitored_symbols(self) -> list[dict[str, object]]:
        """Return globally monitored symbols with their active users and dates."""
        with self._lock:
            rows = self._connection.execute(
                """SELECT i.symbol,i.name,i.monitoring_enabled_at,u.username
                   FROM watchlist_items i
                   JOIN watchlist_groups g ON g.id=i.group_id
                   JOIN market_users u ON u.id=g.user_id
                   WHERE u.enabled=1 AND i.monitoring_enabled=1
                   ORDER BY i.symbol,i.monitoring_enabled_at,u.id"""
            ).fetchall()
        result: dict[str, dict[str, object]] = {}
        for row in rows:
            symbol = str(row["symbol"])
            entry = result.setdefault(
                symbol,
                {
                    "symbol": symbol,
                    "stock_name": str(row["name"]),
                    "monitoring_date": None,
                    "monitoring_users": [],
                },
            )
            timestamp = row["monitoring_enabled_at"]
            date_value = None
            if timestamp:
                parsed = datetime.fromisoformat(str(timestamp))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                date_value = parsed.astimezone(_SHANGHAI_TZ).date().isoformat()
            current_date = entry["monitoring_date"]
            if date_value is not None and (current_date is None or date_value < current_date):
                entry["monitoring_date"] = date_value
            users = entry["monitoring_users"]
            username = str(row["username"])
            if username not in users:
                users.append(username)
        return list(result.values())

    def list_watchlist_symbols(self) -> list[str]:
        """Return every symbol held by an enabled market user, de-duplicated globally."""
        with self._lock:
            rows = self._connection.execute(
                """SELECT DISTINCT i.symbol
                   FROM watchlist_items i
                   JOIN watchlist_groups g ON g.id=i.group_id
                   JOIN market_users u ON u.id=g.user_id
                   WHERE u.enabled=1
                   ORDER BY i.symbol"""
            ).fetchall()
        return [str(row["symbol"]) for row in rows]

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
                "SELECT name,market,monitoring_enabled,monitoring_enabled_at FROM watchlist_items WHERE group_id=? AND symbol=?",
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
                """INSERT INTO watchlist_items(
                       group_id,symbol,name,market,sort_order,monitoring_enabled,monitoring_enabled_at
                   ) VALUES(?,?,?,?,?,?,?)""",
                (
                    target_group_id,
                    symbol,
                    row["name"],
                    row["market"],
                    index,
                    row["monitoring_enabled"],
                    row["monitoring_enabled_at"],
                ),
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

    _REVOKE_USER_SCRIPT = """
local session_ids = redis.call('SMEMBERS', KEYS[1])
for _, session_id in ipairs(session_ids) do
  redis.call('DEL', ARGV[1] .. session_id)
end
return redis.call('DEL', KEYS[1])
"""

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
        payload = json.dumps(
            {
                "user_id": user_id,
                "csrf_token": session.csrf_token,
                "expires_at": session.expires_at.isoformat(),
            }
        )
        pipeline_factory = getattr(self.client, "pipeline", None)
        if callable(pipeline_factory):
            with pipeline_factory(transaction=True) as pipeline:
                pipeline.setex(self._key(session.session_id), seconds, payload)
                pipeline.sadd(self._user_key(user_id), session.session_id)
                pipeline.execute()
        else:
            self.client.setex(self._key(session.session_id), seconds, payload)
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
            # Expiry is handled inline instead of calling revoke(), which would
            # re-enter get() and recurse forever for an already-expired payload.
            self._delete_session(session.session_id, session.user_id)
            return None
        return session

    def revoke(self, session_id: str | None) -> None:
        if not session_id:
            return
        raw = self.client.get(self._key(session_id))
        user_id = None
        if raw is not None:
            try:
                user_id = int(json.loads(self._text(raw))["user_id"])
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                pass
        self._delete_session(session_id, user_id)

    def _delete_session(self, session_id: str, user_id: int | None) -> None:
        """Delete payload and reverse-index membership as one logical operation."""
        pipeline_factory = getattr(self.client, "pipeline", None)
        if callable(pipeline_factory):
            with pipeline_factory(transaction=True) as pipeline:
                pipeline.delete(self._key(session_id))
                if user_id is not None:
                    pipeline.srem(self._user_key(user_id), session_id)
                pipeline.execute()
            return
        self.client.delete(self._key(session_id))
        if user_id is not None:
            self.client.srem(self._user_key(user_id), session_id)

    def revoke_user(self, user_id: int) -> None:
        key = self._user_key(user_id)
        evaluator = getattr(self.client, "eval", None)
        if callable(evaluator):
            evaluator(self._REVOKE_USER_SCRIPT, 1, key, "ths:market:sessions:")
            return
        session_ids = [self._text(raw) for raw in self.client.smembers(key)]
        pipeline_factory = getattr(self.client, "pipeline", None)
        if callable(pipeline_factory):
            with pipeline_factory(transaction=True) as pipeline:
                for session_id in session_ids:
                    pipeline.delete(self._key(session_id))
                pipeline.delete(key)
                pipeline.execute()
            return
        for session_id in session_ids:
            self.client.delete(self._key(session_id))
        self.client.delete(key)
