"""Persistent monitoring history built only from approved MarketSnapshot data."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from threading import RLock
from typing import Any, Callable, Iterable
from zoneinfo import ZoneInfo

from .market_data import MarketSnapshot
from .models import FUND_FLOW_METRICS, FUND_FLOW_PERIODS, MetricKind
from .parsed_values import DirectReadOutcome, empty_metric_values
from .research_replay import calculate_macd


DEFAULT_MACD_SETTINGS: dict[str, int | float] = {
    "short": 10,
    "long": 20,
    "signal": 5,
    "marker_threshold": 0.00001,
}

DEFAULT_PUSH_CONFIG: dict[str, Any] = {
    "enabled": False,
    "premium_push_enabled": True,
    "bark_groups": [],
    "sc3_bot": {
        "enabled": False,
        "base_url": "https://bot-go.apijia.cn",
        "token": "",
        "chat_id": "",
        "parse_mode": "markdown",
        "silent": False,
    },
    "wecom": {
        "enabled": False,
        "api_base_url": "https://qyapi.weixin.qq.com",
        "news_base_url": "",
        "corp_id": "",
        "corp_secret": "",
        "agent_id": 0,
        "to_user": "@all",
        "to_party": "",
        "to_tag": "",
    },
    "rules": [],
}


class SQLiteResearchStore:
    """Store replayable minute history without owning any data transport."""

    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._migrate()
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def _migrate(self) -> None:
        with self._lock, self._connection:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS research_days (
                    symbol TEXT NOT NULL,
                    trade_date TEXT NOT NULL,
                    name TEXT,
                    market TEXT NOT NULL,
                    source TEXT,
                    pre_close TEXT,
                    quote_price TEXT,
                    quote_change_percent TEXT,
                    quote_open TEXT,
                    quote_high TEXT,
                    quote_low TEXT,
                    quote_turnover_rate TEXT,
                    quote_volume TEXT,
                    quote_amount TEXT,
                    collected_at TEXT NOT NULL,
                    PRIMARY KEY(symbol, trade_date)
                );
                CREATE TABLE IF NOT EXISTS research_points (
                    symbol TEXT NOT NULL,
                    trade_date TEXT NOT NULL,
                    minute TEXT NOT NULL,
                    price TEXT,
                    average_price TEXT,
                    volume TEXT,
                    large_order_net TEXT,
                    large_order_amount TEXT,
                    retail_count TEXT,
                    macdfs TEXT,
                    captured_at TEXT NOT NULL,
                    PRIMARY KEY(symbol, trade_date, minute)
                );
                CREATE INDEX IF NOT EXISTS research_points_symbol_date
                    ON research_points(symbol, trade_date, minute);
                CREATE TABLE IF NOT EXISTS research_monitor_status (
                    symbol TEXT PRIMARY KEY,
                    latest_trade_date TEXT,
                    latest_time TEXT,
                    last_sync_at TEXT,
                    last_error TEXT
                );
                CREATE TABLE IF NOT EXISTS research_settings (
                    key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS research_push_events (
                    event_key TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS fund_flow_history_points (
                    symbol TEXT NOT NULL,
                    trade_date TEXT NOT NULL,
                    minute TEXT NOT NULL,
                    period TEXT NOT NULL,
                    unit TEXT,
                    main_net_inflow TEXT,
                    main_visible_inflow TEXT,
                    main_hidden_inflow TEXT,
                    retail_inflow TEXT,
                    captured_at TEXT NOT NULL,
                    PRIMARY KEY(symbol, trade_date, minute, period)
                );
                CREATE INDEX IF NOT EXISTS fund_flow_history_symbol_date
                    ON fund_flow_history_points(symbol, trade_date, period, minute);
                CREATE TABLE IF NOT EXISTS fund_flow_history_status (
                    symbol TEXT PRIMARY KEY,
                    latest_trade_date TEXT,
                    latest_time TEXT,
                    last_sync_at TEXT,
                    last_error TEXT
                );
                """
            )
            existing_columns = {
                str(row[1])
                for row in self._connection.execute(
                    "PRAGMA table_info(research_days)"
                ).fetchall()
            }
            for column in (
                "quote_price",
                "quote_change_percent",
                "quote_open",
                "quote_high",
                "quote_low",
                "quote_turnover_rate",
                "quote_volume",
                "quote_amount",
            ):
                if column not in existing_columns:
                    self._connection.execute(
                        f"ALTER TABLE research_days ADD COLUMN {column} TEXT"
                    )

    @staticmethod
    def _trade_date(snapshot: MarketSnapshot) -> str:
        source_digits = "".join(character for character in (snapshot.source_time or "") if character.isdigit())
        if len(source_digits) >= 8:
            return source_digits[:8]
        local = snapshot.collected_at.astimezone(ZoneInfo("Asia/Shanghai"))
        return local.strftime("%Y%m%d")

    @staticmethod
    def _series_lookup(snapshot: MarketSnapshot, key: str) -> dict[str, str | None]:
        series = snapshot.intraday_series.get(key) or {}
        return {
            str(point.get("time")): (
                None if point.get("value") is None else str(point.get("value"))
            )
            for point in series.get("points", [])
            if isinstance(point, dict) and point.get("time")
        }

    def record_snapshot(self, snapshot: MarketSnapshot) -> list[str]:
        """Upsert one approved snapshot and return only source-changed minutes."""
        trade_date = self._trade_date(snapshot)
        captured_at = snapshot.collected_at.isoformat()
        series = {
            key: self._series_lookup(snapshot, key)
            for key in ("large_order_net", "large_order_amount", "retail_count", "macdfs")
        }
        changed: list[str] = []
        quote = snapshot.quote
        previous_close = quote.get("previous_close")
        if previous_close in (None, ""):
            previous_close = quote.get("pre_close")
        with self._lock, self._connection:
            self._connection.execute(
                """INSERT INTO research_days(
                       symbol,trade_date,name,market,source,pre_close,
                       quote_price,quote_change_percent,quote_open,quote_high,
                       quote_low,quote_turnover_rate,quote_volume,quote_amount,
                       collected_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(symbol,trade_date) DO UPDATE SET
                       name=excluded.name,market=excluded.market,
                       source=CASE
                           WHEN excluded.source='MARKET_DATABASE'
                                AND research_days.source IS NOT NULL
                           THEN research_days.source
                           ELSE excluded.source
                       END,
                       pre_close=COALESCE(excluded.pre_close,research_days.pre_close),
                       quote_price=COALESCE(excluded.quote_price,research_days.quote_price),
                       quote_change_percent=COALESCE(
                           excluded.quote_change_percent,research_days.quote_change_percent
                       ),
                       quote_open=COALESCE(excluded.quote_open,research_days.quote_open),
                       quote_high=COALESCE(excluded.quote_high,research_days.quote_high),
                       quote_low=COALESCE(excluded.quote_low,research_days.quote_low),
                       quote_turnover_rate=COALESCE(
                           excluded.quote_turnover_rate,research_days.quote_turnover_rate
                       ),
                       quote_volume=COALESCE(excluded.quote_volume,research_days.quote_volume),
                       quote_amount=COALESCE(excluded.quote_amount,research_days.quote_amount),
                       collected_at=excluded.collected_at""",
                (
                    snapshot.symbol,
                    trade_date,
                    snapshot.name,
                    snapshot.market,
                    snapshot.source,
                    previous_close,
                    quote.get("price"),
                    quote.get("change_percent"),
                    quote.get("open"),
                    quote.get("high"),
                    quote.get("low"),
                    quote.get("turnover_rate"),
                    quote.get("volume"),
                    quote.get("amount"),
                    captured_at,
                ),
            )
            for point in snapshot.timeshare:
                incoming = {
                    "price": point.price,
                    "average_price": point.average_price,
                    "volume": point.volume,
                    "large_order_net": series["large_order_net"].get(point.time),
                    "large_order_amount": series["large_order_amount"].get(point.time),
                    "retail_count": series["retail_count"].get(point.time),
                    "macdfs": series["macdfs"].get(point.time),
                }
                existing = self._connection.execute(
                    """SELECT price,average_price,volume,large_order_net,
                              large_order_amount,retail_count,macdfs
                       FROM research_points
                       WHERE symbol=? AND trade_date=? AND minute=?""",
                    (snapshot.symbol, trade_date, point.time),
                ).fetchone()
                merged = {
                    key: value if value is not None else (existing[key] if existing else None)
                    for key, value in incoming.items()
                }
                if existing is None or any(existing[key] != merged[key] for key in merged):
                    changed.append(point.time)
                self._connection.execute(
                    """INSERT INTO research_points(
                           symbol,trade_date,minute,price,average_price,volume,
                           large_order_net,large_order_amount,retail_count,macdfs,captured_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(symbol,trade_date,minute) DO UPDATE SET
                           price=excluded.price,average_price=excluded.average_price,
                           volume=excluded.volume,large_order_net=excluded.large_order_net,
                           large_order_amount=excluded.large_order_amount,
                           retail_count=excluded.retail_count,macdfs=excluded.macdfs,
                           captured_at=excluded.captured_at""",
                    (
                        snapshot.symbol,
                        trade_date,
                        point.time,
                        *(merged[key] for key in (
                            "price", "average_price", "volume", "large_order_net",
                            "large_order_amount", "retail_count", "macdfs",
                        )),
                        captured_at,
                    ),
                )
            latest_time = snapshot.timeshare[-1].time if snapshot.timeshare else None
            self._connection.execute(
                """INSERT INTO research_monitor_status(
                       symbol,latest_trade_date,latest_time,last_sync_at,last_error
                   ) VALUES(?,?,?,?,NULL)
                   ON CONFLICT(symbol) DO UPDATE SET
                       latest_trade_date=excluded.latest_trade_date,
                       latest_time=excluded.latest_time,last_sync_at=excluded.last_sync_at,
                       last_error=NULL""",
                (snapshot.symbol, trade_date, latest_time, captured_at),
            )
        return sorted(set(changed))

    def record_error(self, symbol: str, error_code: str) -> None:
        now = datetime.now().astimezone().isoformat()
        with self._lock, self._connection:
            self._connection.execute(
                """INSERT INTO research_monitor_status(symbol,last_sync_at,last_error)
                   VALUES(?,?,?) ON CONFLICT(symbol) DO UPDATE SET
                   last_sync_at=excluded.last_sync_at,last_error=excluded.last_error""",
                (symbol, now, error_code),
            )

    def monitoring_status(self, symbols: Iterable[str]) -> list[dict[str, Any]]:
        result = []
        with self._lock:
            for symbol in sorted(set(symbols)):
                row = self._connection.execute(
                    "SELECT * FROM research_monitor_status WHERE symbol=?",
                    (symbol,),
                ).fetchone()
                result.append(
                    {
                        "symbol": symbol,
                        "latest_trade_date": row["latest_trade_date"] if row else None,
                        "latest_time": row["latest_time"] if row else None,
                        "last_sync_at": row["last_sync_at"] if row else None,
                        "last_error": row["last_error"] if row else None,
                    }
                )
        return result

    def save_macd_settings(self, settings: dict[str, int | float]) -> dict[str, int | float]:
        normalized = {
            "short": int(settings["short"]),
            "long": int(settings["long"]),
            "signal": int(settings["signal"]),
            "marker_threshold": float(settings.get("marker_threshold", 0)),
        }
        if not 1 <= normalized["short"] < normalized["long"] <= 200:
            raise ValueError("invalid MACD short/long settings")
        if not 1 <= normalized["signal"] <= 100:
            raise ValueError("invalid MACD signal setting")
        if not 0 <= normalized["marker_threshold"] <= 1:
            raise ValueError("invalid MACD marker threshold")
        now = datetime.now().astimezone().isoformat()
        with self._lock, self._connection:
            self._connection.execute(
                """INSERT INTO research_settings(key,value_json,updated_at)
                   VALUES('macd',?,?) ON CONFLICT(key) DO UPDATE SET
                   value_json=excluded.value_json,updated_at=excluded.updated_at""",
                (json.dumps(normalized, separators=(",", ":")), now),
            )
        return normalized

    def macd_settings(self) -> dict[str, int | float]:
        with self._lock:
            row = self._connection.execute(
                "SELECT value_json FROM research_settings WHERE key='macd'"
            ).fetchone()
        if row is None:
            return dict(DEFAULT_MACD_SETTINGS)
        return json.loads(str(row["value_json"]))

    @staticmethod
    def _copy(value: Any) -> Any:
        return json.loads(json.dumps(value, ensure_ascii=False))

    def push_config(self, *, public: bool = True) -> dict[str, Any]:
        with self._lock:
            row = self._connection.execute(
                "SELECT value_json FROM research_settings WHERE key='push'"
            ).fetchone()
        config = (
            self._copy(DEFAULT_PUSH_CONFIG)
            if row is None
            else json.loads(str(row["value_json"]))
        )
        if not public:
            return config
        for group in config.get("bark_groups", []):
            secret = str(group.get("device_key") or "")
            group["device_key"] = ""
            group["device_key_configured"] = bool(secret)
        sc3 = config.setdefault("sc3_bot", {})
        token = str(sc3.get("token") or "")
        sc3["token"] = ""
        sc3["token_configured"] = bool(token)
        wecom = config.setdefault("wecom", {})
        corp_secret = str(wecom.get("corp_secret") or "")
        wecom["corp_secret"] = ""
        wecom["corp_secret_configured"] = bool(corp_secret)
        return config

    def save_push_config(self, incoming: dict[str, Any]) -> dict[str, Any]:
        current = self.push_config(public=False)
        current_groups = {
            str(group.get("id")): group
            for group in current.get("bark_groups", [])
            if group.get("id")
        }
        groups = []
        for index, raw_group in enumerate(incoming.get("bark_groups", current.get("bark_groups", []))):
            group = dict(raw_group)
            group_id = str(group.get("id") or f"bark-{index + 1}")
            previous = current_groups.get(group_id, {})
            secret = str(group.get("device_key") or previous.get("device_key") or "")
            groups.append(
                {
                    "id": group_id,
                    "name": str(group.get("name") or group_id),
                    "base_url": str(group.get("base_url") or "https://api.day.app").rstrip("/"),
                    "device_key": secret,
                }
            )
        raw_sc3 = {**current.get("sc3_bot", {}), **incoming.get("sc3_bot", {})}
        raw_wecom = {**current.get("wecom", {}), **incoming.get("wecom", {})}
        if not raw_sc3.get("token"):
            raw_sc3["token"] = current.get("sc3_bot", {}).get("token", "")
        if not raw_wecom.get("corp_secret"):
            raw_wecom["corp_secret"] = current.get("wecom", {}).get("corp_secret", "")
        config = {
            "enabled": bool(incoming.get("enabled", current.get("enabled", False))),
            "premium_push_enabled": bool(
                incoming.get(
                    "premium_push_enabled",
                    current.get("premium_push_enabled", True),
                )
            ),
            "bark_groups": groups,
            "sc3_bot": {
                "enabled": bool(raw_sc3.get("enabled", False)),
                "base_url": str(raw_sc3.get("base_url") or "https://bot-go.apijia.cn").rstrip("/"),
                "token": str(raw_sc3.get("token") or ""),
                "chat_id": str(raw_sc3.get("chat_id") or ""),
                "parse_mode": str(raw_sc3.get("parse_mode") or "markdown"),
                "silent": bool(raw_sc3.get("silent", False)),
            },
            "wecom": {
                "enabled": bool(raw_wecom.get("enabled", False)),
                "api_base_url": str(raw_wecom.get("api_base_url") or "https://qyapi.weixin.qq.com").rstrip("/"),
                "news_base_url": str(raw_wecom.get("news_base_url") or "").rstrip("/"),
                "corp_id": str(raw_wecom.get("corp_id") or ""),
                "corp_secret": str(raw_wecom.get("corp_secret") or ""),
                "agent_id": int(raw_wecom.get("agent_id") or 0),
                "to_user": str(raw_wecom.get("to_user") or "@all"),
                "to_party": str(raw_wecom.get("to_party") or ""),
                "to_tag": str(raw_wecom.get("to_tag") or ""),
            },
            "rules": self._copy(incoming.get("rules", current.get("rules", []))),
        }
        now = datetime.now().astimezone().isoformat()
        with self._lock, self._connection:
            self._connection.execute(
                """INSERT INTO research_settings(key,value_json,updated_at)
                   VALUES('push',?,?) ON CONFLICT(key) DO UPDATE SET
                   value_json=excluded.value_json,updated_at=excluded.updated_at""",
                (json.dumps(config, ensure_ascii=False, separators=(",", ":")), now),
            )
        return self.push_config(public=True)

    def claim_push_event(self, event_key: str) -> bool:
        now = datetime.now().astimezone().isoformat()
        with self._lock, self._connection:
            cursor = self._connection.execute(
                "INSERT OR IGNORE INTO research_push_events(event_key,created_at) VALUES(?,?)",
                (event_key, now),
            )
        return cursor.rowcount == 1

    def save_premium_snapshot(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        now = datetime.now().astimezone().isoformat()
        with self._lock, self._connection:
            self._connection.execute(
                """INSERT INTO research_settings(key,value_json,updated_at)
                   VALUES('premium_snapshot',?,?) ON CONFLICT(key) DO UPDATE SET
                   value_json=excluded.value_json,updated_at=excluded.updated_at""",
                (json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")), now),
            )
        return snapshot

    def premium_snapshot(self) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT value_json FROM research_settings WHERE key='premium_snapshot'"
            ).fetchone()
        return None if row is None else json.loads(str(row["value_json"]))

    def available_dates(self, symbol: str) -> list[str]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT trade_date FROM research_days WHERE symbol=? ORDER BY trade_date DESC",
                (symbol,),
            ).fetchall()
        return [str(row["trade_date"]) for row in rows]

    @staticmethod
    def _fund_flow_fields() -> tuple[str, ...]:
        return (
            "unit",
            "main_net_inflow",
            "main_visible_inflow",
            "main_hidden_inflow",
            "retail_inflow",
        )

    def record_fund_flow(
        self,
        symbol: str,
        *,
        trade_date: str,
        minute: str,
        periods: dict[str, dict[str, str | None]],
        captured_at: datetime,
    ) -> bool:
        """Upsert one shared fund-flow sample and preserve prior non-null values."""
        fields = self._fund_flow_fields()
        captured = captured_at.isoformat()
        changed = False
        recorded = False
        with self._lock, self._connection:
            for period, raw_values in periods.items():
                if period not in {"today", "three_day", "five_day"}:
                    continue
                incoming = {
                    field: (
                        None
                        if raw_values.get(field) is None
                        else str(raw_values.get(field))
                    )
                    for field in fields
                }
                if not any(incoming[field] is not None for field in fields):
                    continue
                recorded = True
                existing = self._connection.execute(
                    """SELECT unit,main_net_inflow,main_visible_inflow,
                              main_hidden_inflow,retail_inflow
                       FROM fund_flow_history_points
                       WHERE symbol=? AND trade_date=? AND minute=? AND period=?""",
                    (symbol, trade_date, minute, period),
                ).fetchone()
                merged = {
                    field: incoming[field]
                    if incoming[field] is not None
                    else (existing[field] if existing is not None else None)
                    for field in fields
                }
                if existing is None or any(existing[field] != merged[field] for field in fields):
                    changed = True
                self._connection.execute(
                    """INSERT INTO fund_flow_history_points(
                           symbol,trade_date,minute,period,unit,
                           main_net_inflow,main_visible_inflow,main_hidden_inflow,
                           retail_inflow,captured_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(symbol,trade_date,minute,period) DO UPDATE SET
                           unit=excluded.unit,
                           main_net_inflow=excluded.main_net_inflow,
                           main_visible_inflow=excluded.main_visible_inflow,
                           main_hidden_inflow=excluded.main_hidden_inflow,
                           retail_inflow=excluded.retail_inflow,
                           captured_at=excluded.captured_at""",
                    (
                        symbol,
                        trade_date,
                        minute,
                        period,
                        *(merged[field] for field in fields),
                        captured,
                    ),
                )
            if recorded:
                self._connection.execute(
                    """INSERT INTO fund_flow_history_status(
                           symbol,latest_trade_date,latest_time,last_sync_at,last_error
                       ) VALUES(?,?,?,?,NULL)
                       ON CONFLICT(symbol) DO UPDATE SET
                           latest_trade_date=excluded.latest_trade_date,
                           latest_time=excluded.latest_time,
                           last_sync_at=excluded.last_sync_at,
                           last_error=NULL""",
                    (symbol, trade_date, minute, captured),
                )
        return changed

    def record_fund_flow_error(self, symbol: str, error_code: str) -> None:
        now = datetime.now().astimezone().isoformat()
        with self._lock, self._connection:
            self._connection.execute(
                """INSERT INTO fund_flow_history_status(symbol,last_sync_at,last_error)
                   VALUES(?,?,?) ON CONFLICT(symbol) DO UPDATE SET
                   last_sync_at=excluded.last_sync_at,last_error=excluded.last_error""",
                (symbol, now, error_code),
            )

    def latest_fund_flow_snapshot(
        self,
        symbol: str,
        *,
        now: datetime | None = None,
        max_age_seconds: float | None = None,
        trade_date: str | None = None,
    ) -> dict[str, Any] | None:
        """Return the newest complete three-period fund-flow sample."""
        available_dates = self.available_fund_flow_dates(symbol)
        selected_date = trade_date or (available_dates[0] if available_dates else None)
        if selected_date is None:
            return None
        with self._lock:
            rows = self._connection.execute(
                """SELECT * FROM fund_flow_history_points
                   WHERE symbol=? AND trade_date=?
                   ORDER BY REPLACE(minute,':','') DESC, period""",
                (symbol, selected_date),
            ).fetchall()
        grouped: dict[str, dict[str, sqlite3.Row]] = {}
        for row in rows:
            if now is not None and selected_date == now.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d"):
                if str(row["minute"]).replace(":", "") > now.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%H%M"):
                    continue
            grouped.setdefault(str(row["minute"]), {})[str(row["period"])] = row
        fields = ("unit", "main_net_inflow", "main_visible_inflow", "main_hidden_inflow", "retail_inflow")
        for minute in sorted(grouped, reverse=True):
            by_period = grouped[minute]
            if set(by_period) != {"today", "three_day", "five_day"}:
                continue
            if any(any(by_period[period][field] is None for field in fields) for period in by_period):
                continue
            captured_at = max(str(by_period[period]["captured_at"]) for period in by_period)
            if now is not None and max_age_seconds is not None:
                try:
                    age = max(0.0, (now - datetime.fromisoformat(captured_at)).total_seconds())
                except (TypeError, ValueError):
                    continue
                if age > max_age_seconds:
                    return None
            return {
                "symbol": symbol,
                "trade_date": selected_date,
                "minute": minute,
                "captured_at": captured_at,
                "periods": {
                    period: {field: by_period[period][field] for field in fields}
                    for period in ("today", "three_day", "five_day")
                },
            }
        return None

    def latest_market_enrichment(self, symbol: str) -> DirectReadOutcome | None:
        """Reconstruct the last stored L2/fund values for a closed Market view."""
        fund = self.latest_fund_flow_snapshot(symbol)
        with self._lock:
            row = self._connection.execute(
                """SELECT * FROM research_points
                   WHERE symbol=? ORDER BY trade_date DESC,REPLACE(minute,':','') DESC
                   LIMIT 1""",
                (symbol,),
            ).fetchone()
        if row is None and fund is None:
            return None
        values = empty_metric_values()
        if row is not None:
            values.update(
                {
                    MetricKind.LARGE_ORDER_NET: row["large_order_net"],
                    MetricKind.LARGE_ORDER_AMOUNT: row["large_order_amount"],
                    MetricKind.RETAIL_COUNT: row["retail_count"],
                    MetricKind.MACDFS: row["macdfs"],
                }
            )
        if fund is not None:
            for period, _label, unit_kind in FUND_FLOW_PERIODS:
                period_values = fund["periods"][period]
                values[unit_kind] = period_values["unit"]
                for name, kind in FUND_FLOW_METRICS[period].items():
                    values[kind] = period_values[name]
        trade_date = str(row["trade_date"]) if row is not None else str(fund["trade_date"])
        with self._lock:
            rows = self._connection.execute(
                """SELECT minute,large_order_net,large_order_amount,retail_count,macdfs
                   FROM research_points WHERE symbol=? AND trade_date=?
                   ORDER BY REPLACE(minute,':','')""",
                (symbol, trade_date),
            ).fetchall()
        series_specs = (
            (MetricKind.LARGE_ORDER_NET, "large_order_net", None),
            (MetricKind.LARGE_ORDER_AMOUNT, "large_order_amount", "万"),
            (MetricKind.RETAIL_COUNT, "retail_count", None),
            (MetricKind.MACDFS, "macdfs", None),
        )
        intraday_series = {
            kind: {
                "unit": unit,
                "points": [
                    {"time": str(item["minute"]), "value": item[column]}
                    for item in rows
                    if item[column] is not None
                ],
            }
            for kind, column, unit in series_specs
        }
        return DirectReadOutcome(
            values=values,
            source_errors={"core_metrics": None, "main_fund_flow": None},
            intraday_series=intraday_series,
            stored_trade_dates={
                "core_metrics": str(row["trade_date"]) if row is not None else None,
                "main_fund_flow": str(fund["trade_date"]) if fund is not None else None,
            },
        )

    def latest_market_snapshot(self, symbol: str) -> MarketSnapshot | None:
        """Reconstruct the last public quote stored for a monitored symbol."""
        with self._lock:
            day = self._connection.execute(
                """SELECT * FROM research_days WHERE symbol=?
                   ORDER BY trade_date DESC LIMIT 1""",
                (symbol,),
            ).fetchone()
            if day is None:
                return None
            point = self._connection.execute(
                """SELECT * FROM research_points WHERE symbol=? AND trade_date=?
                   ORDER BY REPLACE(minute,':','') DESC LIMIT 1""",
                (symbol, day["trade_date"]),
            ).fetchone()
        if point is None:
            return None
        collected_at = datetime.fromisoformat(str(day["collected_at"]))
        quote = {
            "price": day["quote_price"] or point["price"],
            "change_percent": day["quote_change_percent"],
            "previous_close": day["pre_close"],
            "open": day["quote_open"],
            "high": day["quote_high"],
            "low": day["quote_low"],
            "turnover_rate": day["quote_turnover_rate"],
            "volume": day["quote_volume"] or point["volume"],
            "amount": day["quote_amount"],
        }
        return MarketSnapshot(
            symbol=symbol,
            name=day["name"],
            market=day["market"],
            sequence=0,
            source_time=f"{day['trade_date']} {point['minute']}:00",
            collected_at=collected_at,
            quote=quote,
            source="MARKET_DATABASE",
            stored_trade_dates={"core_metrics": str(day["trade_date"])},
        )

    def available_fund_flow_dates(self, symbol: str) -> list[str]:
        with self._lock:
            rows = self._connection.execute(
                """SELECT DISTINCT trade_date FROM fund_flow_history_points
                   WHERE symbol=? ORDER BY trade_date DESC""",
                (symbol,),
            ).fetchall()
        return [str(row["trade_date"]) for row in rows]

    def read_fund_flow_history(
        self,
        symbol: str,
        trade_date: str | None = None,
    ) -> dict[str, Any]:
        available_dates = self.available_fund_flow_dates(symbol)
        with self._lock:
            status_row = self._connection.execute(
                "SELECT * FROM fund_flow_history_status WHERE symbol=?",
                (symbol,),
            ).fetchone()
        selected_date = trade_date or (available_dates[0] if available_dates else None)
        if selected_date is None:
            return {
                "symbol": symbol,
                "trade_date": None,
                "available_dates": [],
                "latest_sync_at": None,
                "last_error": status_row["last_error"] if status_row else None,
                "periods": {
                    "today": {"points": []},
                    "three_day": {"points": []},
                    "five_day": {"points": []},
                },
            }
        with self._lock:
            rows = self._connection.execute(
                """SELECT * FROM fund_flow_history_points
                   WHERE symbol=? AND trade_date=?
                   ORDER BY period,REPLACE(minute,':','')""",
                (symbol, selected_date),
            ).fetchall()
            status = self._connection.execute(
                "SELECT * FROM fund_flow_history_status WHERE symbol=?",
                (symbol,),
            ).fetchone()
        if not rows:
            raise LookupError("fund flow history not found")
        periods = {"today": {"points": []}, "three_day": {"points": []}, "five_day": {"points": []}}
        for row in rows:
            periods[str(row["period"])] ["points"].append(
                {
                    "time": str(row["minute"]),
                    "unit": row["unit"],
                    "main_net_inflow": row["main_net_inflow"],
                    "main_visible_inflow": row["main_visible_inflow"],
                    "main_hidden_inflow": row["main_hidden_inflow"],
                    "retail_inflow": row["retail_inflow"],
                }
            )
        return {
            "symbol": symbol,
            "trade_date": selected_date,
            "available_dates": available_dates,
            "latest_sync_at": status["last_sync_at"] if status else None,
            "last_error": status["last_error"] if status else None,
            "periods": periods,
        }

    def read_fund_flow_daily(
        self,
        symbol: str,
        limit: int = 30,
        previous_trading_date: Callable[[str], str] | None = None,
    ) -> dict[str, Any]:
        """Return one closing `today` fund-flow point per recent trade date."""
        if not 1 <= limit <= 60:
            raise ValueError("fund flow daily limit must be between 1 and 60")
        with self._lock:
            date_rows = self._connection.execute(
                """SELECT DISTINCT trade_date FROM fund_flow_history_points
                   WHERE symbol=? AND period='today'
                   ORDER BY trade_date DESC LIMIT ?""",
                (symbol, limit),
            ).fetchall()
            dates = [str(row["trade_date"]) for row in date_rows]
            if not dates:
                return {"symbol": symbol, "limit": limit, "baseline_trade_date": None, "points": []}
            placeholders = ",".join("?" for _ in dates)
            rows = self._connection.execute(
                f"""SELECT * FROM fund_flow_history_points
                    WHERE symbol=? AND trade_date IN ({placeholders})
                    ORDER BY trade_date,period,REPLACE(minute,':','')""",
                (symbol, *dates),
            ).fetchall()
        latest_by_date: dict[str, dict[str, sqlite3.Row]] = {}
        for row in rows:
            latest_by_date.setdefault(str(row["trade_date"]), {})[str(row["period"])] = row

        def period_values(row: sqlite3.Row | None) -> dict[str, str | None] | None:
            if row is None:
                return None
            return {
                "unit": row["unit"],
                "main_net_inflow": row["main_net_inflow"],
                "main_visible_inflow": row["main_visible_inflow"],
                "main_hidden_inflow": row["main_hidden_inflow"],
                "retail_inflow": row["retail_inflow"],
            }

        points = []
        for trade_date in sorted(latest_by_date):
            rows_by_period = latest_by_date[trade_date]
            row = rows_by_period.get("today")
            if row is None:
                continue
            points.append(
                {
                    "trade_date": trade_date,
                    "time": str(row["minute"]),
                    "unit": row["unit"],
                    "main_net_inflow": row["main_net_inflow"],
                    "main_visible_inflow": row["main_visible_inflow"],
                    "main_hidden_inflow": row["main_hidden_inflow"],
                    "retail_inflow": row["retail_inflow"],
                    "periods": {
                        "today": period_values(rows_by_period.get("today")),
                        "three_day": period_values(rows_by_period.get("three_day")),
                        "five_day": period_values(rows_by_period.get("five_day")),
                    },
                }
            )
        baseline_date = None
        if points:
            baseline_date = (
                previous_trading_date(points[0]["trade_date"])
                if previous_trading_date is not None
                else points[0]["trade_date"]
            )
            points.insert(0, {
                "trade_date": baseline_date,
                "time": "00:00",
                "unit": points[0]["unit"],
                "main_net_inflow": "0",
                "main_visible_inflow": "0",
                "main_hidden_inflow": "0",
                "retail_inflow": "0",
                "periods": None,
            })
        return {"symbol": symbol, "limit": limit, "baseline_trade_date": baseline_date, "points": points}

    def cleanup_fund_flow_history(self, before_trade_date: str) -> int:
        with self._lock, self._connection:
            cursor = self._connection.execute(
                "DELETE FROM fund_flow_history_points WHERE trade_date < ?",
                (before_trade_date,),
            )
            self._connection.execute(
                """DELETE FROM fund_flow_history_status
                   WHERE symbol NOT IN (SELECT DISTINCT symbol FROM fund_flow_history_points)"""
            )
        return int(cursor.rowcount)

    def read_series(self, symbol: str, trade_date: str | None = None) -> dict[str, Any]:
        with self._lock:
            selected_date = trade_date
            if selected_date is None:
                row = self._connection.execute(
                    "SELECT trade_date FROM research_days WHERE symbol=? ORDER BY trade_date DESC LIMIT 1",
                    (symbol,),
                ).fetchone()
                selected_date = str(row["trade_date"]) if row else None
            if selected_date is None:
                raise LookupError("research history not found")
            day = self._connection.execute(
                "SELECT * FROM research_days WHERE symbol=? AND trade_date=?",
                (symbol, selected_date),
            ).fetchone()
            rows = self._connection.execute(
                """SELECT * FROM research_points WHERE symbol=? AND trade_date=?
                   ORDER BY REPLACE(minute,':','')""",
                (symbol, selected_date),
            ).fetchall()
        if day is None or not rows:
            raise LookupError("research history not found")
        settings = self.macd_settings()
        prices = [float(row["price"]) if row["price"] is not None else 0.0 for row in rows]
        macd = calculate_macd(prices, settings)
        points = []
        for index, row in enumerate(rows):
            points.append(
                {
                    "time": row["minute"],
                    "price": row["price"],
                    "average_price": row["average_price"],
                    "volume": row["volume"],
                    "large_order_net": row["large_order_net"],
                    "large_order_amount": row["large_order_amount"],
                    "retail_count": row["retail_count"],
                    "macdfs": row["macdfs"],
                    **macd[index],
                }
            )
        return {
            "symbol": symbol,
            "name": day["name"],
            "market": day["market"],
            "trade_date": selected_date,
            "source": day["source"],
            "collected_at": day["collected_at"],
            "pre_close": day["pre_close"],
            "available_dates": self.available_dates(symbol),
            "macd_settings": settings,
            "points": points,
        }

    def close(self) -> None:
        with self._lock:
            self._connection.close()
