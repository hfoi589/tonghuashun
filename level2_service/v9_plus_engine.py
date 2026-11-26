"""V9+ Aggressive / Balanced BRN0Y + FX-Adjusted Premium & 3-State Quant Engine.

Implements causal feature alignment (FX-adjusted IOPV & premium, BRN0Y sp_fx,
10-day cointegration, 2-year Brent range quantile, MA5/MA15 regime, MACD(10,20,5)
crosses and 12m wave extrema), 3-tier conviction lot sizing, stateful intraday
exit logic, and 14:53 3-state overnight rebalancing across both historical
backtesting (capital_sim / base_position_t0) and live AutoTradeEngine polling.
"""

from __future__ import annotations

from bisect import bisect_right
from datetime import datetime, timedelta, timezone
import json
import math
import os
import re
import sqlite3
import statistics
import time
from typing import Any, Callable, Mapping, Sequence
import urllib.request
from zoneinfo import ZoneInfo

from .research_replay import calculate_macd

SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
NY_TZ = ZoneInfo("America/New_York")
_DATE_RE = re.compile(r"^[0-9]{8}\Z")
_W_OIL = 0.867
_BETA = 0.68
_W_XOP = 0.94
_BETA_DAYTIME_BRN = 0.45
_BETA_DAYTIME_NG = 0.06
_BETA_DAYTIME_ES = 0.28
_LOT_SIZE = 100
_DEFAULT_FX = 6.70

_MEMORY_MACRO_CACHE: dict[tuple[str, str], tuple[float, Any]] = {}
_MEMORY_BRN_SERIES_CACHE: dict[tuple[str, str, str], tuple[float, list[int], list[float]]] = {}


def _resolve_cross_market_anchor(pos_sizing: Mapping[str, Any] | None) -> str:
    raw = str((pos_sizing or {}).get("cross_market_anchor") or "brent").strip().lower()
    return "xop_us_ep" if raw == "xop_us_ep" else "brent"


def _is_xop_anchor(pos_sizing: Mapping[str, Any] | None) -> bool:
    return _resolve_cross_market_anchor(pos_sizing) == "xop_us_ep"


def _parse_sh(trade_date: str, hhmm: str) -> datetime:
    clean_t = hhmm.strip()
    if ":" not in clean_t and len(clean_t) == 4:
        clean_t = f"{clean_t[:2]}:{clean_t[2:]}"
    return datetime(
        int(trade_date[:4]),
        int(trade_date[4:6]),
        int(trade_date[6:8]),
        int(clean_t[:2]),
        int(clean_t[3:5]),
        tzinfo=SHANGHAI_TZ,
    )


def _val_at(ts_sorted: Sequence[int], px_sorted: Sequence[float], dt_sh: datetime) -> float | None:
    if not ts_sorted:
        return None
    idx = bisect_right(ts_sorted, int(dt_sh.timestamp())) - 1
    return float(px_sorted[idx]) if idx >= 0 else None


def _val_at_with_ts(ts_sorted: Sequence[int], px_sorted: Sequence[float], dt_sh: datetime) -> tuple[float, int] | None:
    if not ts_sorted:
        return None
    idx = bisect_right(ts_sorted, int(dt_sh.timestamp())) - 1
    if idx < 0:
        return None
    return float(px_sorted[idx]), int(ts_sorted[idx])


def _http_get_text(
    url: str,
    *,
    referer: str = "https://finance.sina.com.cn/",
    encoding: str = "utf-8",
    timeout: float = 8.0,
) -> str:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
            ),
            "Accept": "*/*",
            "Referer": referer,
            "Connection": "close",
        },
    )
    with opener.open(req, timeout=timeout) as resp:
        return resp.read().decode(encoding, errors="replace")


def _parse_nav_payload(text: str) -> dict[str, float]:
    m = re.search(r"Data_netWorthTrend\s*=\s*(\[.*?\]);", text, re.S)
    if not m:
        raise ValueError("Data_netWorthTrend missing")
    arr = json.loads(m.group(1))
    out: dict[str, float] = {}
    for item in arr:
        if not isinstance(item, Mapping):
            continue
        x_val = item.get("x")
        y_val = item.get("y")
        if x_val is None or y_val is None:
            continue
        try:
            td = datetime.fromtimestamp(float(x_val) / 1000.0, tz=SHANGHAI_TZ).strftime("%Y%m%d")
            val = float(y_val)
            if val > 0:
                out[td] = val
        except (TypeError, ValueError):
            continue
    return out


def _parse_oil_daily_payload(text: str) -> list[list[Any]]:
    s_idx = text.find("[")
    e_idx = text.rfind("]")
    if s_idx == -1 or e_idx <= s_idx:
        raise ValueError("Sina OIL daily payload missing")
    arr = json.loads(text[s_idx : e_idx + 1])
    rows: list[list[Any]] = []
    for item in arr:
        if not isinstance(item, Mapping):
            continue
        d_raw = str(item.get("date") or "").replace("-", "").strip()
        if not _DATE_RE.fullmatch(d_raw):
            continue
        try:
            c_val = float(item.get("close") or 0.0)
        except (TypeError, ValueError):
            continue
        if c_val <= 0:
            continue
        # Shift daily close causally to next calendar day 06:00 CST so day T never peeks day T's close
        causal_dt = datetime.strptime(d_raw, "%Y%m%d").replace(tzinfo=SHANGHAI_TZ) + timedelta(days=1, hours=6)
        rows.append([d_raw, int(causal_dt.timestamp()), round(c_val, 4)])
    rows.sort(key=lambda r: (r[1], r[0]))
    return rows


def _parse_fx_series_payload(daily_text: str, min_text: str | None) -> dict[str, list[Any]]:
    events: dict[int, float] = {}
    m = re.search(r'"([^"]+)"', daily_text or "")
    if m:
        for seg in m.group(1).split("|"):
            parts = seg.split(",")
            if len(parts) < 5:
                continue
            d_raw = parts[0].strip().replace("-", "")
            if not _DATE_RE.fullmatch(d_raw) or d_raw < "20260101":
                continue
            try:
                o_px = float(parts[1])
                c_px = float(parts[4])
            except (TypeError, ValueError):
                continue
            if o_px > 0:
                t0 = int(datetime.strptime(d_raw, "%Y%m%d").replace(hour=9, minute=0, tzinfo=SHANGHAI_TZ).timestamp())
                events[t0] = o_px
            if c_px > 0:
                t1 = int(datetime.strptime(d_raw, "%Y%m%d").replace(hour=23, minute=0, tzinfo=SHANGHAI_TZ).timestamp())
                events[t1] = c_px
    if min_text:
        s_idx = min_text.find("[")
        e_idx = min_text.rfind("]")
        if s_idx != -1 and e_idx > s_idx:
            try:
                arr = json.loads(min_text[s_idx : e_idx + 1])
                for item in arr:
                    if not isinstance(item, Mapping):
                        continue
                    ds = str(item.get("d") or "").strip()
                    try:
                        c_px = float(item.get("c") or 0.0)
                        ts = int(datetime.strptime(ds, "%Y-%m-%d %H:%M:%S").replace(tzinfo=SHANGHAI_TZ).timestamp())
                    except (TypeError, ValueError):
                        continue
                    if c_px > 0:
                        events[ts] = c_px
            except Exception:
                pass
    ts_sorted = sorted(events)
    return {"ts": ts_sorted, "px": [round(events[t], 5) for t in ts_sorted]}


def _parse_us_daily_payload(text: str) -> list[list[Any]]:
    """Parse Sina US daily K-line JSONP and shift US close causally to next calendar day 06:00 CST."""
    s_idx = text.find("[")
    e_idx = text.rfind("]")
    if s_idx == -1 or e_idx <= s_idx:
        raise ValueError("Sina US daily payload missing")
    arr = json.loads(text[s_idx : e_idx + 1])
    rows_by_d: dict[str, list[Any]] = {}
    for item in arr:
        if not isinstance(item, Mapping):
            continue
        d_raw = str(item.get("d") or item.get("date") or "").replace("-", "").strip()
        if not _DATE_RE.fullmatch(d_raw):
            continue
        try:
            c_val = float(item.get("c") or item.get("close") or 0.0)
        except (TypeError, ValueError):
            continue
        if c_val <= 0:
            continue
        causal_dt = datetime.strptime(d_raw, "%Y%m%d").replace(tzinfo=SHANGHAI_TZ) + timedelta(days=1, hours=6)
        rows_by_d[d_raw] = [d_raw, int(causal_dt.timestamp()), round(c_val, 4)]
    if not rows_by_d:
        raise ValueError("Sina US daily payload empty")
    return sorted(rows_by_d.values(), key=lambda r: (r[1], r[0]))


def _parse_yahoo_daily_payload(text: str) -> list[list[Any]]:
    """Parse Yahoo v8 chart daily closes and shift US close causally to next day 06:00 CST."""
    decoded = json.loads(text)
    result = (((decoded or {}).get("chart") or {}).get("result") or [None])[0]
    if not isinstance(result, Mapping):
        raise ValueError("Yahoo daily chart missing")
    timestamps = result.get("timestamp") or []
    closes = (((result.get("indicators") or {}).get("quote") or [{}])[0].get("close") or [])
    rows_by_d: dict[str, list[Any]] = {}
    for ts_raw, c_raw in zip(timestamps, closes):
        if ts_raw is None or c_raw is None:
            continue
        try:
            c_val = float(c_raw)
            d_raw = datetime.fromtimestamp(float(ts_raw), tz=NY_TZ).strftime("%Y%m%d")
        except (TypeError, ValueError):
            continue
        if c_val <= 0 or not _DATE_RE.fullmatch(d_raw):
            continue
        causal_dt = datetime.strptime(d_raw, "%Y%m%d").replace(tzinfo=SHANGHAI_TZ) + timedelta(days=1, hours=6)
        rows_by_d[d_raw] = [d_raw, int(causal_dt.timestamp()), round(c_val, 4)]
    if not rows_by_d:
        raise ValueError("Yahoo daily chart empty")
    return sorted(rows_by_d.values(), key=lambda r: (r[1], r[0]))


def _parse_sina_mink_payload(text: str) -> dict[str, list[Any]]:
    """Parse Sina GlobalService.getMink JSONP into sorted timestamp/price arrays."""
    s_idx = text.find("[")
    e_idx = text.rfind("]")
    if s_idx == -1 or e_idx <= s_idx:
        raise ValueError("Sina mink payload missing")
    arr = json.loads(text[s_idx : e_idx + 1])
    events: dict[int, float] = {}
    for item in arr:
        if not isinstance(item, Mapping):
            continue
        ds = str(item.get("d") or "").strip()
        if not ds:
            continue
        try:
            c_px = float(item.get("c") or 0.0)
            fmt = "%Y-%m-%d %H:%M:%S" if len(ds) >= 19 else "%Y-%m-%d %H:%M"
            ts = int(datetime.strptime(ds[:19], fmt).replace(tzinfo=SHANGHAI_TZ).timestamp())
        except (TypeError, ValueError):
            continue
        if c_px > 0:
            events[ts] = c_px
    ts_sorted = sorted(events)
    return {"ts": ts_sorted, "px": [round(events[t], 4) for t in ts_sorted]}


def _parse_us_reg_date(label: str, ref_year: int) -> str | None:
    """Parse Sina US quote regular close date label like 'Nov 25 04:00PM EST' into YYYYMMDD."""
    m = re.match(r"^\s*([A-Za-z]{3})\s+(\d{1,2})\b", str(label or ""))
    if not m:
        return None
    try:
        parsed = datetime.strptime(f"{ref_year} {m.group(1)} {int(m.group(2)):02d}", "%Y %b %d")
        return parsed.strftime("%Y%m%d")
    except ValueError:
        return None


class V9MacroDataStore:
    """SQLite-backed cache for official NAV, 2y Brent daily range, and USD/CNY series."""

    def __init__(
        self,
        research_store: Any,
        *,
        allow_network: bool | None = None,
        fetch_text: Callable[..., str] | None = None,
    ) -> None:
        self.store = research_store
        env_offline = os.environ.get("THS_QUANT_OFFLINE", "").strip().lower() in {"1", "true", "yes"}
        self.allow_network = (not env_offline) if allow_network is None else bool(allow_network)
        self.fetch_text = fetch_text or _http_get_text
        self._ensure_table()

    def _ensure_table(self) -> None:
        conn = getattr(self.store, "_connection", None)
        lock = getattr(self.store, "_lock", None)
        if conn is None or lock is None:
            return
        with lock, conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS quant_macro_cache (
                    cache_key TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )

    def _read_db_cache(self, key: str) -> tuple[float, Any] | None:
        conn = getattr(self.store, "_connection", None)
        lock = getattr(self.store, "_lock", None)
        if conn is None or lock is None:
            return None
        try:
            with lock:
                row = conn.execute(
                    "SELECT payload_json, updated_at FROM quant_macro_cache WHERE cache_key=?",
                    (key,),
                ).fetchone()
            if row is None:
                return None
            return float(row[1]), json.loads(str(row[0]))
        except Exception:
            return None

    def _write_db_cache(self, key: str, payload: Any, ts: float) -> None:
        conn = getattr(self.store, "_connection", None)
        lock = getattr(self.store, "_lock", None)
        if conn is None or lock is None:
            return
        try:
            encoded = json.dumps(payload, ensure_ascii=False)
            with lock, conn:
                conn.execute(
                    """
                    INSERT INTO quant_macro_cache(cache_key, payload_json, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(cache_key) DO UPDATE SET
                        payload_json=excluded.payload_json,
                        updated_at=excluded.updated_at
                    """,
                    (key, encoded, ts),
                )
        except Exception:
            pass

    def _store_id(self) -> str:
        return str(getattr(self.store, "path", id(self.store)))

    def _get_cached_or_fetch(
        self,
        cache_key: str,
        ttl_seconds: float,
        fetcher: Callable[[], Any],
    ) -> Any | None:
        now = time.time()
        m_key = (self._store_id(), cache_key)
        mem = _MEMORY_MACRO_CACHE.get(m_key)
        if mem is not None and (now - mem[0]) < ttl_seconds:
            return mem[1]

        db_val = self._read_db_cache(cache_key)
        if db_val is not None:
            db_ts, db_payload = db_val
            if (now - db_ts) < ttl_seconds or not self.allow_network:
                _MEMORY_MACRO_CACHE[m_key] = (db_ts, db_payload)
                return db_payload

        if self.allow_network:
            try:
                fresh = fetcher()
                _MEMORY_MACRO_CACHE[m_key] = (now, fresh)
                self._write_db_cache(cache_key, fresh, now)
                return fresh
            except Exception:
                pass

        if db_val is not None:
            _MEMORY_MACRO_CACHE[m_key] = db_val
            return db_val[1]
        if mem is not None:
            return mem[1]
        return None

    def load_nav_map(self, symbol: str) -> dict[str, float]:
        key = f"nav_{symbol}"

        def _fetch() -> dict[str, float]:
            txt = self.fetch_text(
                f"https://fund.eastmoney.com/pingzhongdata/{symbol}.js",
                referer=f"https://fund.eastmoney.com/{symbol}.html",
                encoding="utf-8",
            )
            parsed = _parse_nav_payload(txt)
            if not parsed:
                raise ValueError("empty NAV payload")
            return parsed

        data = self._get_cached_or_fetch(key, 3600.0, _fetch)
        return {str(k): float(v) for k, v in data.items()} if isinstance(data, Mapping) else {}

    def load_oil_daily_rows(self) -> list[list[Any]]:
        def _fetch() -> list[list[Any]]:
            txt = self.fetch_text(
                "https://stock2.finance.sina.com.cn/futures/api/jsonp.php/var%20x=/"
                "GlobalFuturesService.getGlobalFuturesDailyKLine?symbol=OIL",
                referer="https://finance.sina.com.cn/",
                encoding="utf-8",
            )
            rows = _parse_oil_daily_payload(txt)
            if not rows:
                raise ValueError("empty OIL daily rows")
            return rows

        data = self._get_cached_or_fetch("oil_daily_2y", 3600.0, _fetch)
        return list(data) if isinstance(data, Sequence) else []

    def load_fx_series(self) -> tuple[list[int], list[float]]:
        def _fetch() -> dict[str, list[Any]]:
            daily_txt = self.fetch_text(
                "https://vip.stock.finance.sina.com.cn/forex/api/jsonp.php/var%20x=/"
                "NewForexService.getDayKLine?symbol=fx_susdcny",
                referer="https://finance.sina.com.cn/",
                encoding="utf-8",
            )
            min_txt: str | None = None
            try:
                min_txt = self.fetch_text(
                    "https://vip.stock.finance.sina.com.cn/forex/api/jsonp.php/var%20x=/"
                    "NewForexService.getMinKline?symbol=fx_susdcny&scale=60&datalen=1200",
                    referer="https://finance.sina.com.cn/",
                    encoding="utf-8",
                )
            except Exception:
                min_txt = None
            parsed = _parse_fx_series_payload(daily_txt, min_txt)
            if not parsed["ts"]:
                raise ValueError("empty fx series")
            return parsed

        data = self._get_cached_or_fetch("fx_usdcny_series", 600.0, _fetch)
        if isinstance(data, Mapping) and data.get("ts"):
            return [int(x) for x in data["ts"]], [float(x) for x in data["px"]]
        return [], []

    def fetch_live_spot(self) -> tuple[float | None, float | None]:
        """Fetch live BRN0Y (hf_OIL) & USD/CNY spot quotes (8s memory cache)."""
        if not self.allow_network:
            return None, None
        now = time.time()
        spot_key = ("global", "live_spot_oil_fx")
        cached = _MEMORY_MACRO_CACHE.get(spot_key)
        if cached is not None and (now - cached[0]) < 8.0:
            return cached[1]
        brn_px: float | None = None
        fx_px: float | None = None
        try:
            raw = self.fetch_text(
                "https://hq.sinajs.cn/list=USDCNY,hf_OIL",
                referer="https://finance.sina.com.cn/",
                encoding="gbk",
                timeout=4.0,
            )
            m_fx = re.search(r'hq_str_USDCNY="([^"]+)"', raw)
            m_oil = re.search(r'hq_str_hf_OIL="([^"]+)"', raw)
            if m_fx:
                val = float(m_fx.group(1).split(",")[1])
                if val > 0:
                    fx_px = val
            if m_oil:
                val = float(m_oil.group(1).split(",")[0])
                if val > 0:
                    brn_px = val
        except Exception:
            pass
        res = (brn_px, fx_px)
        _MEMORY_MACRO_CACHE[spot_key] = (now, res)
        return res

    def _load_us_etf_daily_rows(self, sina_sym: str, yahoo_sym: str, cache_key: str) -> list[list[Any]]:
        def _fetch() -> list[list[Any]]:
            try:
                txt = self.fetch_text(
                    f"https://stock.finance.sina.com.cn/usstock/api/jsonp_v2.php/var%20x=/US_MinKService.getDailyK?symbol={sina_sym}",
                    referer="https://finance.sina.com.cn/",
                    encoding="utf-8",
                )
                return _parse_us_daily_payload(txt)
            except Exception:
                last_err: Exception | None = None
                for host in ("https://query1.finance.yahoo.com", "https://query2.finance.yahoo.com"):
                    try:
                        y_txt = self.fetch_text(
                            f"{host}/v8/finance/chart/{yahoo_sym}?range=2y&interval=1d",
                            referer="https://finance.yahoo.com/",
                            encoding="utf-8",
                        )
                        return _parse_yahoo_daily_payload(y_txt)
                    except Exception as err:
                        last_err = err
                if last_err is not None:
                    raise last_err
                raise ValueError(f"empty {yahoo_sym} daily rows")

        data = self._get_cached_or_fetch(cache_key, 3600.0, _fetch)
        return list(data) if isinstance(data, Sequence) else []

    def load_xop_daily_rows(self) -> list[list[Any]]:
        return self._load_us_etf_daily_rows("xop", "XOP", "xop_daily_2y")

    def load_spy_daily_rows(self) -> list[list[Any]]:
        return self._load_us_etf_daily_rows("spy", "SPY", "spy_daily_2y")

    def load_ng_daily_rows(self) -> list[list[Any]]:
        def _fetch() -> list[list[Any]]:
            txt = self.fetch_text(
                "https://stock2.finance.sina.com.cn/futures/api/jsonp.php/var%20x=/"
                "GlobalFuturesService.getGlobalFuturesDailyKLine?symbol=NG",
                referer="https://finance.sina.com.cn/",
                encoding="utf-8",
            )
            rows = _parse_oil_daily_payload(txt)
            if not rows:
                raise ValueError("empty NG daily rows")
            return rows

        data = self._get_cached_or_fetch("ng_daily_2y", 3600.0, _fetch)
        return list(data) if isinstance(data, Sequence) else []

    def load_fut_1m_series(self, fut_symbol: str) -> tuple[list[int], list[float]]:
        sym_up = str(fut_symbol or "").strip().upper()
        if not sym_up:
            return [], []

        def _fetch() -> dict[str, list[Any]]:
            txt = self.fetch_text(
                f"https://gu.sina.cn/ft/api/jsonp.php/var%20x=/GlobalService.getMink?symbol={sym_up}&type=1",
                referer="https://finance.sina.cn/",
                encoding="utf-8",
            )
            parsed = _parse_sina_mink_payload(txt)
            if not parsed["ts"]:
                raise ValueError(f"empty {sym_up} 1m series")
            return parsed

        data = self._get_cached_or_fetch(f"fut_1m_{sym_up.lower()}", 300.0, _fetch)
        if isinstance(data, Mapping) and data.get("ts"):
            return [int(x) for x in data["ts"]], [float(x) for x in data["px"]]
        return [], []

    def fetch_live_xop_spot(self) -> dict[str, Any] | None:
        """Fetch live USDCNY, hf_OIL, hf_NG, hf_ES, and gb_xop quotes (8s memory cache)."""
        if not self.allow_network:
            return None
        now = time.time()
        spot_key = ("global", "live_spot_xop_bundle")
        cached = _MEMORY_MACRO_CACHE.get(spot_key)
        if cached is not None and (now - cached[0]) < 8.0:
            return dict(cached[1]) if isinstance(cached[1], Mapping) else None

        brn_px: float | None = None
        fx_px: float | None = None
        ng_px: float | None = None
        es_px: float | None = None
        xop_close: float | None = None
        xop_prev_close: float | None = None
        xop_chg_pct: float | None = None
        xop_ext_chg_pct: float | None = None
        xop_quote_date: str | None = None
        xop_reg_date: str | None = None
        try:
            raw = self.fetch_text(
                "https://hq.sinajs.cn/list=USDCNY,hf_OIL,hf_NG,hf_ES,gb_xop",
                referer="https://finance.sina.com.cn/",
                encoding="gbk",
                timeout=4.0,
            )
            m_fx = re.search(r'hq_str_USDCNY="([^"]+)"', raw)
            m_oil = re.search(r'hq_str_hf_OIL="([^"]+)"', raw)
            m_ng = re.search(r'hq_str_hf_NG="([^"]+)"', raw)
            m_es = re.search(r'hq_str_hf_ES="([^"]+)"', raw)
            m_xop = re.search(r'hq_str_gb_xop="([^"]+)"', raw)
            if m_fx:
                val = float(m_fx.group(1).split(",")[1])
                if val > 0:
                    fx_px = val
            if m_oil:
                val = float(m_oil.group(1).split(",")[0])
                if val > 0:
                    brn_px = val
            if m_ng:
                val = float(m_ng.group(1).split(",")[0])
                if val > 0:
                    ng_px = val
            if m_es:
                val = float(m_es.group(1).split(",")[0])
                if val > 0:
                    es_px = val
            if m_xop:
                parts = m_xop.group(1).split(",")
                if len(parts) > 3:
                    val = float(parts[1] or 0.0)
                    if val > 0:
                        xop_close = val
                    try:
                        xop_chg_pct = float(parts[2])
                    except (TypeError, ValueError):
                        xop_chg_pct = None
                    qd = str(parts[3] or "").strip()[:10].replace("-", "")
                    if _DATE_RE.fullmatch(qd):
                        xop_quote_date = qd
                if len(parts) > 26:
                    try:
                        prev_c = float(parts[26] or 0.0)
                        if prev_c > 0:
                            xop_prev_close = prev_c
                    except (TypeError, ValueError):
                        pass
                if len(parts) > 25:
                    ref_yr = int(xop_quote_date[:4]) if xop_quote_date else datetime.now(SHANGHAI_TZ).year
                    xop_reg_date = _parse_us_reg_date(parts[25], ref_yr)
                    if xop_reg_date and xop_quote_date and xop_reg_date > xop_quote_date:
                        xop_reg_date = _parse_us_reg_date(parts[25], ref_yr - 1)
                if len(parts) > 22 and xop_close and xop_close > 0:
                    try:
                        ext_px = float(parts[21] or 0.0)
                        if ext_px > 0:
                            ext_pct = (ext_px / xop_close - 1.0) * 100.0
                            if abs(ext_pct) <= 8.0:
                                xop_ext_chg_pct = round(ext_pct, 4)
                    except (TypeError, ValueError):
                        pass
        except Exception:
            pass

        bundle = {
            "brn_px": brn_px,
            "fx_px": fx_px,
            "ng_px": ng_px,
            "es_px": es_px,
            "xop_close": xop_close,
            "xop_prev_close": xop_prev_close,
            "xop_chg_pct": xop_chg_pct,
            "xop_ext_chg_pct": xop_ext_chg_pct,
            "xop_quote_date": xop_quote_date,
            "xop_reg_date": xop_reg_date,
        }
        _MEMORY_MACRO_CACHE[spot_key] = (now, bundle)
        return dict(bundle)


def _brent_range_position(px: float, lo_h: float, hi_h: float) -> float:
    return max(0.0, min(1.0, (px - lo_h) / max(1.0, hi_h - lo_h)))


def _causal_macd_fields(pts: Sequence[Mapping[str, Any]], i: int) -> dict[str, Any]:
    mc = float(pts[i].get("macd") or 0.0)
    pmc = float(pts[i - 1].get("macd") or mc) if i >= 1 else mc
    dmc10 = mc - pmc
    w12 = [float(pts[j].get("macd") or 0.0) for j in range(max(0, i - 12), i)] or [mc]
    return {
        "diff10": float(pts[i].get("diff") or 0.0),
        "dea10": float(pts[i].get("dea") or 0.0),
        "macd10": mc,
        "dmc10": dmc10,
        "max_red12": max(w12),
        "min_grn12": min(w12),
        "gc10": i >= 1 and pmc <= 0.0 < mc,
        "dc10": i >= 1 and pmc >= 0.0 > mc,
    }


def is_discount_uptrend_sell_guarded(b: Mapping[str, Any], sc: float = 1.0) -> tuple[bool, str]:
    """Block premature reverse-T sells in IOPV discount zone unless overshoot is extreme or MACD meaningfully dead-crosses."""
    pm = float(b["prem"])
    sp = float(b["sp_fx"])
    spi = float(b["sp_intra"])
    brn_chg = float(b.get("brn_chg", 0.0))
    brn_m5 = float(b.get("brn_m5", 0.0))
    real_dc = bool(b.get("dc10", False)) and float(b.get("max_red12", 0.0)) >= 0.0004 * sc
    if pm < -0.30 and not real_dc and sp < 0.58 * sc:
        if spi < 0.32 * sc:
            return True, f"折价区({pm:+.2f}%)日内真实超涨不足(sp_intra {spi:+.2f}%<{0.32 * sc:+.2f}%)"
        if brn_chg >= 2.0 and brn_m5 >= 0.06:
            return True, f"折价区({pm:+.2f}%)且布油强攻(当日{brn_chg:+.2f}%, 5m{brn_m5:+.2f}%)，待sp_fx≥{0.58 * sc:+.2f}%或有效死叉"
    return False, ""


def signals_v8_regime_macd(day: Mapping[str, Any], b: Mapping[str, Any], sc: float = 1.0) -> tuple[bool, bool]:
    """V8/V9 regime-aware intraday entry signals (returns (sell_sig, buy_sig))."""
    sp = float(b["sp_fx"])
    spi = float(b["sp_intra"])
    pm = float(b["prem"])
    mc = float(b["macd10"])
    dmc = float(b["dmc10"])
    below_ma5 = float(b["price"]) < float(day["ma5"])

    dc_cond = (
        bool(b["dc10"])
        and pm > -0.80
        and (
            (float(b["max_red12"]) >= 0.0015 * sc and sp >= 0.0)
            or (float(b["max_red12"]) >= 0.0018 * sc and (pm >= -0.10 or spi >= 0.25 * sc))
            or (float(b["diff10"]) >= 0.0020 * sc and abs(dmc) >= 0.0004 * sc and sp >= -0.05)
            or (abs(mc) >= 0.0005 * sc and pm >= 0.80 and sp >= 0.0)
        )
    )
    sell_sig = (pm > -1.0) and (
        (sp >= 0.42 * sc and float(b["coint10"]) >= 0.20 * sc)
        or (float(b["m3"]) >= 0.20 * sc and float(b["m1"]) >= 0.05 and mc >= 0.0018 * sc and sp >= 0.05)
        or dc_cond
    )
    if sell_sig:
        sell_guarded, _ = is_discount_uptrend_sell_guarded(b, sc)
        if sell_guarded:
            sell_sig = False

    buy_base = (pm <= 0.50) and (
        (pm <= -0.50 * sc and sp <= -0.25 * sc and float(b["m3"]) <= -0.10)
        or (sp <= -0.42 * sc and float(b["coint10"]) <= -0.20 * sc)
        or (float(b["m5"]) <= -0.32 * sc and float(b["m1"]) <= -0.05 and mc <= -0.0020 * sc and sp <= -0.10)
    )
    if dmc <= -0.0006:
        buy_base = False
    if pm > -0.20 and spi >= 0.35:
        buy_base = False
    if below_ma5 and sp > -0.75 * sc and (float(b["iopv_intra"]) <= -0.05 or dmc < 0.0):
        buy_base = False

    buy_gc_res = (pm <= 0.20) and (
        (bool(b["gc10"]) and sp <= -0.15 * sc and (dmc >= 0.0004 * sc or float(b["min_grn12"]) <= -0.0012 * sc))
        or (float(b["diff10"]) <= -0.0020 * sc and dmc >= 0.0005 * sc and sp <= -0.18 * sc)
    )
    if below_ma5 and float(b["iopv_intra"]) <= -0.08:
        buy_gc_res = False

    return bool(sell_sig), bool(buy_base or buy_gc_res)


def _suppress_waterfall_short_entry(b: Mapping[str, Any], close_wt: float, sc: float = 1.0) -> bool:
    """Avoid opening a new reverse-T short leg in wt<=0 regime on a mild-spread crash bar that contradicts cover."""
    return (
        float(close_wt) <= 0.0
        and float(b["m5"]) <= -0.28
        and float(b["macd10"]) <= -0.0018
        and float(b["sp_fx"]) < 0.25 * sc
    )


def should_exit_short(
    b: Mapping[str, Any],
    entry_sp: float,
    adaptive_exits: bool = True,
    *,
    close_wt: float | None = None,
    entry_px: float | None = None,
) -> bool:
    """Cover an open reverse-T short leg on spread normalization, genuine dip-momentum, or deep discount compression."""
    sp = float(b["sp_fx"])
    if sp <= -0.08 and float(b["coint10"]) <= 0.0:
        return True
    if float(b["m5"]) <= -0.28 and float(b["macd10"]) <= -0.0018:
        if close_wt is not None and float(close_wt) <= 0.0:
            if (float(entry_sp) - sp) < 0.15:
                return False
            if entry_px is not None and float(entry_px) > 0 and float(b["price"]) >= float(entry_px):
                return False
        return True
    if adaptive_exits and entry_sp >= 1.20 and (entry_sp - sp) >= 0.85 and float(b["dmc10"]) >= 0.0:
        return True
    if (
        adaptive_exits
        and float(b["prem"]) <= -0.75
        and (entry_sp - sp) >= 0.28
        and float(b["dmc10"]) > 0.0
    ):
        return True
    return False


def should_add_short_tranche(
    b: Mapping[str, Any],
    *,
    entry_px: float,
    entry_sp: float,
    s_sig: bool,
) -> bool:
    """Allow a second-tranche reverse-T add-on when spread widens further and upward momentum decelerates."""
    return bool(
        s_sig
        and float(entry_px) > 0
        and float(b["sp_fx"]) >= float(entry_sp) + 0.15
        and float(b["price"]) >= float(entry_px) * 1.004
        and float(b.get("dmc10", 0.0)) <= 0.0002
    )


def _is_early_unclosed_live_bar(
    bar_hhmm: str,
    now_dt: datetime | None,
    min_elapsed_seconds: int = 15,
) -> bool:
    """Return True while the latest live minute bar is still within its first `min_elapsed_seconds`."""
    if now_dt is None:
        return False
    clean = str(bar_hhmm or "").strip()
    if ":" not in clean:
        return False
    try:
        hh, mm = clean.split(":", 1)
        bar_m = int(hh) * 60 + int(mm)
    except ValueError:
        return False
    now_m = int(now_dt.hour) * 60 + int(now_dt.minute)
    sec = int(now_dt.second)
    if bar_m == now_m + 1:
        return sec < min_elapsed_seconds
    if bar_m == now_m and sec > 0:
        return sec < min_elapsed_seconds
    return False


def should_exit_long_strict(b: Mapping[str, Any], adaptive_exits: bool = True) -> bool:
    pm = float(b["prem"])
    if adaptive_exits and pm <= -1.00 and float(b["iopv_intra"]) >= 0.20:
        tgt = 0.28
    else:
        tgt = 0.10 if pm < 0.0 else -0.02
    return float(b["sp_fx"]) >= tgt or (
        float(b["m3"]) >= 0.18 and float(b["macd10"]) >= 0.0015
    )


def clean_buy_tier(day: Mapping[str, Any], b: Mapping[str, Any]) -> tuple[str, float]:
    """3-tier upward conviction share fraction (50% / 80% / 100%)."""
    pm = float(b["prem"])
    sp = float(b["sp_fx"])
    above_ma5 = float(b["price"]) >= float(day["ma5"])
    if (pm <= -0.60 and (sp <= -0.45 or float(b["iopv_intra"]) >= 0.10 or bool(b["gc10"]))) or (
        sp <= -0.65 and above_ma5
    ):
        return "A档·极高确定性(100%)", 1.00
    if pm <= -0.15 or above_ma5:
        return "B档·高确定性(80%)", 0.80
    return "C档·标准确定性(50%)", 0.50


def clean_sell_tier(_day: Mapping[str, Any], b: Mapping[str, Any]) -> tuple[str, float]:
    """3-tier downward conviction share fraction (50% / 60% / 80% / 100%)."""
    pm = float(b["prem"])
    sp = float(b["sp_fx"])
    if pm < -0.40 and sp < 0.75 and float(b.get("brn_chg", 0.0)) >= 2.5 and float(b.get("dmc10", 0.0)) > 0.0002:
        return "B档·折价强油分批高抛(60%)", 0.60
    if pm >= 0.60 or float(b.get("max_red12", 0.0)) >= 0.0018 or sp >= 0.60:
        return "A档·极高确定性(100%)", 1.00
    if pm >= -0.20 or sp >= 0.45:
        return "B档·高确定性(80%)", 0.80
    return "C档·标准确定性(50%)", 0.50


def check_v9_tuning_sell_guard(
    b: Mapping[str, Any],
    v9_tuning: Mapping[str, Any] | None,
) -> tuple[bool, str]:
    """Block premature reverse-T sell when carryover premium masks non-positive/surging Brent spread."""
    tuning = v9_tuning if isinstance(v9_tuning, Mapping) else {}
    if not bool(tuning.get("bull_oil_surge_guard")):
        return False, ""
    sp = float(b["sp_fx"])
    if sp < 0.05:
        return True, f"跨市场价差未转正(sp_fx {sp:+.2f}%<+0.05%)，高溢价下禁做假死叉反T"
    spi = float(b["sp_intra"])
    brn_chg = float(b.get("brn_chg", 0.0))
    if spi < 0.05 and brn_chg >= 2.0 and not bool(b["dc10"]):
        return True, f"布油强攻(当日{brn_chg:+.2f}%)且日内未超涨(sp_intra {spi:+.2f}%<+0.05%)，防开盘卖飞"
    return False, ""


def apply_v9_sell_tier_tuning(
    day: Mapping[str, Any],
    b: Mapping[str, Any],
    t_lbl: str,
    frac: float,
    v9_tuning: Mapping[str, Any] | None,
) -> tuple[str, float]:
    """Cap reverse-T sell tier fraction during strong Brent rallies unless intraday stretch is extreme."""
    tuning = v9_tuning if isinstance(v9_tuning, Mapping) else {}
    raw_cap = tuning.get("bull_oil_surge_tier_cap_pct")
    if raw_cap is None:
        return t_lbl, frac
    cap_frac = float(raw_cap) / 100.0
    if (
        float(b.get("brn_chg", 0.0)) >= 2.5
        and float(b["price"]) >= float(day["ma5"])
        and float(b["sp_intra"]) < 0.80
        and frac > cap_frac
    ):
        return f"强油价半仓保护({cap_frac * 100:.0f}%)", cap_frac
    return t_lbl, frac


def filter_bear_regime_buy(
    day: Mapping[str, Any],
    b: Mapping[str, Any],
    b_sig: bool,
    close_wt: float,
    sc: float = 1.0,
) -> bool:
    """Prevent chasing morning gap-ups/above-VWAP spikes when daily trend is bearish and non-overnight."""
    if not b_sig:
        return False
    bear_trend = (float(day["ma5"]) < float(day["ma15"])) and (float(b["price"]) < float(day["ma5"]))
    if bear_trend and close_wt <= 0.0:
        macd_stabilizing = bool(b["gc10"]) or (float(b["dmc10"]) >= 0.00010 * sc and float(b["m1"]) >= 0.0)
        if (
            float(b.get("vdev", 0.0)) > -0.05
            or float(b.get("lof_chg", 0.0)) > 0.20
            or not macd_stabilizing
        ) and float(b["sp_fx"]) > -1.20 * sc:
            return False
    return True


def apply_xop_overnight_intraday_overlays(
    day: Mapping[str, Any],
    b: Mapping[str, Any],
    *,
    s_sig: bool,
    b_sig: bool,
    scalp_ok: bool,
    close_wt: float,
    sc: float = 1.0,
) -> tuple[bool, bool, bool, dict[str, Any]]:
    """Apply US XOP / Natural Gas / SPY overnight lead-lag overlays on intraday signals."""
    xop_ov = float(day.get("xop_ov") or 0.0)
    spy_ov = float(day.get("spy_ov") or 0.0)
    ng_ov = float(day.get("ng_ov") or 0.0)
    xop_alpha = float(day.get("xop_vs_oil_alpha") or 0.0)

    xop_strong_ov = (xop_ov >= 1.20) or (xop_alpha >= 0.80) or (ng_ov >= 3.0 and xop_ov >= 0.20)
    xop_weak_ov = (xop_ov <= -1.40) or (spy_ov <= -1.00 and xop_ov <= -0.60)

    pm = float(b["prem"])
    sp = float(b["sp_fx"])
    t = str(b["time"])
    dmc = float(b["dmc10"])
    m1 = float(b["m1"])
    dc10 = bool(b["dc10"])

    sell_guarded_xop = bool(s_sig and xop_strong_ov and pm < -0.15 and not dc10 and sp < 0.60 * sc)
    if sell_guarded_xop:
        s_sig = False

    sell_weak_ov_triggered = bool(
        (not s_sig) and (not sell_guarded_xop) and xop_weak_ov and pm >= 0.35 and sp >= 0.28 * sc and dc10
    )
    if sell_weak_ov_triggered:
        s_sig = True

    buy_knife_blocked_xop = bool(b_sig and xop_weak_ov and t < "10:15" and (pm > -0.65 or dmc < 0.0))
    if buy_knife_blocked_xop:
        b_sig = False

    scalp_blocked_xop = bool(scalp_ok and xop_weak_ov and pm > 0.20)
    if scalp_blocked_xop:
        scalp_ok = False

    buy_catchup_xop = bool(
        (not b_sig)
        and (not buy_knife_blocked_xop)
        and xop_strong_ov
        and ("09:33" <= t <= "10:45")
        and close_wt > 0.0
        and pm <= -0.35
        and sp <= -0.22 * sc
        and dmc >= 0.0
        and m1 >= -0.02
    )
    if buy_catchup_xop:
        b_sig = True

    return s_sig, b_sig, scalp_ok, {
        "xop_strong_ov": xop_strong_ov,
        "xop_weak_ov": xop_weak_ov,
        "sell_guarded_xop": sell_guarded_xop,
        "sell_weak_ov_triggered": sell_weak_ov_triggered,
        "buy_knife_blocked_xop": buy_knife_blocked_xop,
        "scalp_blocked_xop": scalp_blocked_xop,
        "buy_catchup_xop": buy_catchup_xop,
        "xop_ov": xop_ov,
        "spy_ov": spy_ov,
        "ng_ov": ng_ov,
        "xop_alpha": xop_alpha,
    }


def _apply_v9_macd_anchor(
    days: Sequence[dict[str, Any]],
    v9_tuning: Mapping[str, Any] | None,
) -> None:
    """Rescale absolute MACD-amplitude fields per bar to the calibrated price anchor."""
    tuning = v9_tuning if isinstance(v9_tuning, Mapping) else {}
    raw_anchor = tuning.get("macd_price_anchor")
    if raw_anchor is None:
        return
    try:
        anchor = float(raw_anchor)
    except (TypeError, ValueError):
        return
    if anchor <= 0.0:
        return
    macd_keys = ("diff10", "dea10", "macd10", "dmc10", "max_red12", "min_grn12")
    for day in days:
        bars = day.get("bars") or []
        for b in bars:
            raw_px = b.get("price") or day.get("pre") or 0.5
            k = anchor / max(0.5, float(raw_px))
            if abs(k - 1.0) <= 1e-12:
                continue
            for mk in macd_keys:
                if mk in b and b[mk] is not None:
                    b[mk] = float(b[mk]) * k


def _shift_buy_bar_prem(
    b: Mapping[str, Any],
    v9_tuning: Mapping[str, Any] | None,
) -> Mapping[str, Any]:
    """Subtract structural QDII buy-side premium offset so dip-buy gates remain comparable across LOFs."""
    tuning = v9_tuning if isinstance(v9_tuning, Mapping) else {}
    raw_off = tuning.get("buy_prem_offset_pct")
    if raw_off is None:
        return b
    try:
        offset = float(raw_off)
    except (TypeError, ValueError):
        return b
    if abs(offset) <= 1e-12:
        return b
    return {**b, "prem": float(b["prem"]) - offset}


def signals_v8_with_tuning(
    day: Mapping[str, Any],
    b: Mapping[str, Any],
    sc: float = 1.0,
    v9_tuning: Mapping[str, Any] | None = None,
) -> tuple[bool, bool]:
    """Run V8/V9 regime signals with optional sell-guard and buy-side premium offset tuning."""
    s_sig, raw_b_sig = signals_v8_regime_macd(day, b, sc)
    if s_sig and check_v9_tuning_sell_guard(b, v9_tuning)[0]:
        s_sig = False
    b_buy = _shift_buy_bar_prem(b, v9_tuning)
    if b_buy is not b:
        _, raw_b_sig = signals_v8_regime_macd(day, b_buy, sc)
    return s_sig, raw_b_sig


def should_enter_intraday_dip_scalp(
    b: Mapping[str, Any],
    sc: float = 1.0,
    *,
    day: Mapping[str, Any] | None = None,
    strict_mode: bool = False,
    v9_tuning: Mapping[str, Any] | None = None,
    cur_close_wt: float = 1.0,
) -> tuple[bool, str, float]:
    """Unlock causal intraday relative-dip T+0 inventory building even in high-premium or flat regimes."""
    tuning = v9_tuning if isinstance(v9_tuning, Mapping) else {}
    strict_dip = bool(tuning.get("strict_dip_scalp"))
    t = str(b["time"])
    max_time = "13:45" if strict_mode else "14:15"
    if not ("09:40" <= t <= max_time):
        return False, "", 0.0
    min_vdev = -0.50 if strict_mode else -0.35
    macd_turn_up = bool(b["gc10"]) or (
        float(b["macd10"]) < 0.0
        and float(b["dmc10"]) >= 0.00022 * sc
        and float(b["min_grn12"]) <= -0.0008 * sc
    )
    triggered = (
        float(b.get("vdev", 0.0)) <= min_vdev
        and float(b.get("px_dd_hi", 0.0)) >= 0.38
        and float(b.get("sp_dd_hi", 0.0)) >= 0.22
        and float(b["prem"]) <= 2.60
        and macd_turn_up
        and float(b.get("brn_m5", 0.0)) >= -0.08
        and float(b.get("m1", 0.0)) >= -0.05
    )
    if triggered and strict_mode:
        sp = float(b["sp_fx"])
        spi = float(b["sp_intra"])
        if sp > -0.10 and spi > -0.30:
            triggered = False
        elif (
            day is not None
            and float(day["ma5"]) < float(day["ma15"])
            and float(b["price"]) < float(day["ma5"])
            and sp > -0.45
        ):
            triggered = False
    if triggered and strict_dip:
        if (
            float(cur_close_wt) <= 0.0
            or float(b.get("brn_pos", 0.0)) >= 0.85
            or t > "13:50"
            or float(b.get("vdev", 0.0)) > -0.45
            or float(b.get("px_dd_hi", 0.0)) < 0.48
        ):
            triggered = False
    if not triggered:
        return False, "", 0.0
    if float(b["prem"]) > 0.50:
        return True, "高溢价日内做T档(70%·收盘不隔夜)", 0.70
    return True, "超跌波段做T档(80%)", 0.80


def should_exit_long_v9(
    day: Mapping[str, Any],
    b: Mapping[str, Any],
    *,
    entry_px: float,
    entry_sp: float,
    s_sig: bool,
    close_wt: float,
    adaptive_exits: bool = True,
) -> tuple[bool, str]:
    """Exit standard +T long on strict spread repair or bear-trend relative take-profit / protective stop."""
    if should_exit_long_strict(b, adaptive_exits):
        return True, "正T止盈卖出"
    bear_trend = (float(day["ma5"]) < float(day["ma15"])) and (float(b["price"]) < float(day["ma5"]))
    if bear_trend and entry_px > 0:
        gain_pct = (float(b["price"]) - entry_px) / entry_px * 100.0
        sp_gain = float(b["sp_fx"]) - entry_sp
        if (gain_pct >= 0.45 and (s_sig or float(b["dmc10"]) <= 0.0)) or (
            sp_gain >= 0.35 and gain_pct >= 0.25 and float(b["dmc10"]) <= 0.0
        ):
            return True, f"弱势正T波段止盈({gain_pct:+.2f}%)"
        if close_wt <= 0.0 and gain_pct <= -0.55 and float(b["dmc10"]) < 0.0:
            return True, f"空头不隔夜保护止损({gain_pct:+.2f}%)"
    return False, ""


def should_exit_scalp_long(
    b: Mapping[str, Any],
    *,
    entry_px: float,
    entry_sp: float,
    s_sig: bool,
) -> tuple[bool, str]:
    """Exit intraday dip-scalp long on sell-peak resonance, relative profit stall, or tight breakdown stop."""
    gain_pct = (float(b["price"]) - entry_px) / entry_px * 100.0 if entry_px > 0 else 0.0
    sp_gain = float(b["sp_fx"]) - entry_sp
    if s_sig and gain_pct >= 0.20:
        return True, f"日内冲高卖点止盈({gain_pct:+.2f}%)"
    if (gain_pct >= 0.48 and float(b["dmc10"]) <= 0.0) or (
        sp_gain >= 0.30 and gain_pct >= 0.30 and float(b["dmc10"]) <= 0.0
    ):
        return True, f"日内波段目标止盈({gain_pct:+.2f}%)"
    if gain_pct <= -0.65 and float(b["dmc10"]) < 0.0:
        return True, f"日内做T破位止损({gain_pct:+.2f}%)"
    return False, ""


def resolve_effective_close_weight(
    overnight_fn: Callable[[Mapping[str, Any], Mapping[str, Any], int], tuple[float, str]],
    day: Mapping[str, Any],
    b_close: Mapping[str, Any],
    pos_mode: int | str = 0,
    entry_prem: float = 0.0,
    v9_tuning: Mapping[str, Any] | None = None,
) -> tuple[float, str]:
    """Enforce 14:53 non-overnight exit for high-premium intraday dip-scalp legs below MA5."""
    wt, wt_lbl = overnight_fn(day, b_close, 0)
    tuning = v9_tuning if isinstance(v9_tuning, Mapping) else {}
    prem_offset = float(tuning.get("buy_prem_offset_pct") or 0.0)
    eff_entry_prem = float(entry_prem) - prem_offset
    eff_close_prem = float(b_close["prem"]) - prem_offset
    if (
        (pos_mode == 2 or pos_mode == "scalp")
        and eff_entry_prem > 0.50
        and eff_close_prem >= 1.20
        and float(b_close["price"]) < float(day["ma5"])
        and wt > 0.0
    ):
        return 0.0, f"高溢价日内做T仓(溢价{float(b_close['prem']):+.2f}%)破MA5收盘清零不隔夜(0%)"
    return wt, wt_lbl


def v9_pro_close_weight(day: Mapping[str, Any], b1453: Mapping[str, Any], _pos: int = 0) -> tuple[float, str]:
    """V9+ Aggressive 3-state overnight position weight & human-readable rationale."""
    bp = float(b1453["brn_pos"])
    pm = float(b1453["prem"])
    pr = float(b1453["price"])
    above_ma5 = pr >= float(day["ma5"])
    non_bear_ma15 = float(day["ma5"]) >= float(day["ma15"]) or pr >= float(day["ma15"])

    if bp >= 0.90:
        return 0.0, f"布伦特2年极高分位({bp * 100:.1f}%>=90%)收盘全平避险(0%)"
    if bp >= 0.72 and not above_ma5:
        return 0.0, f"布伦特高位({bp * 100:.1f}%>=72%)且跌破MA5，收盘空仓避险(0%)"
    if float(day["max_bpos_20d"]) >= 0.72 and not non_bear_ma15:
        return 0.0, f"高油价回落确认空头趋势(MA5<MA15)，收盘全平(0%)"
    if (not non_bear_ma15) and (not above_ma5) and bp > 0.45 and pm > -1.50:
        return 0.0, f"空头趋势下无深折价保护(溢价{pm:+.2f}%)，收盘避险空仓(0%)"
    if bp >= 0.72:
        w = 0.45 if pm <= -0.30 else 0.35
        return w, f"布伦特偏高但维持MA5上方({bp * 100:.1f}%)，轻仓留底({w * 100:.0f}%)"
    if bp <= 0.42:
        w = 1.00 if (pm <= 1.20 or above_ma5) else 0.85
        return w, f"布伦特2年低位区({bp * 100:.1f}%<=42%)，高仓位留底过夜({w * 100:.0f}%)"
    if pm <= -0.30 and non_bear_ma15:
        w = 1.00 if above_ma5 else 0.85
        return w, f"负溢价趋势安全区(溢价{pm:+.2f}%且非空头)，重仓留底({w * 100:.0f}%)"
    w = 0.80 if above_ma5 else 0.55
    if pm >= 3.00 and not above_ma5:
        w = 0.30
    return w, f"中枢常态区间(BRN分位{bp * 100:.1f}%, 溢价{pm:+.2f}%)，均衡底仓({w * 100:.0f}%)"


def brent_v10_overnight(day: Mapping[str, Any], b1453: Mapping[str, Any], _pos: int = 0) -> tuple[float, str]:
    """V9+ Balanced 3-state overnight position weight."""
    bp = float(b1453["brn_pos"])
    pm = float(b1453["prem"])
    pr = float(b1453["price"])
    above_ma5 = pr >= float(day["ma5"])
    non_bear_ma15 = float(day["ma5"]) >= float(day["ma15"]) or pr >= float(day["ma15"])

    if (bp >= 0.75 and not above_ma5) or (float(day["max_bpos_20d"]) >= 0.75 and not non_bear_ma15):
        return 0.0, f"稳健风控清仓(BRN分位{bp * 100:.1f}%)"
    if bp >= 0.90:
        return 0.0, "布伦特极高分位稳健清仓(0%)"
    if bp >= 0.75:
        w = 0.45 if pm <= -0.50 else 0.30
        return w, f"高位多头轻仓({w * 100:.0f}%)"
    if bp <= 0.40:
        w = 1.00 if pm <= 1.0 else 0.90
        return w, f"低位强持有({w * 100:.0f}%)"
    w = 0.75 - (bp - 0.40) / 0.35 * 0.25
    if pm <= -0.30:
        w = min(1.0, w + 0.20)
    elif pm >= 2.50:
        w = max(0.30, w - 0.20)
    return w, f"稳健中枢动态底仓({w * 100:.0f}%)"


def v9_xop_close_weight(day: Mapping[str, Any], b1453: Mapping[str, Any], _pos: int = 0) -> tuple[float, str]:
    """162411 XOP-anchored 14:53 overnight position weight combining commodity cycle, US XOP trend, premium, and daytime lead-lag."""
    bp = float(b1453["brn_pos"])
    xp = float(b1453.get("xop_pos", bp))
    pm = float(b1453["prem"])
    pr = float(b1453["price"])
    day_lead = float(b1453.get("daytime_lead_pct", b1453.get("daytime_brn_pct", 0.0)))
    above_ma5 = pr >= float(day["ma5"])
    non_bear_ma15 = float(day["ma5"]) >= float(day["ma15"]) or pr >= float(day["ma15"])
    xop_bull = bool(day.get("xop_bull", True))
    xop_above_ma5 = bool(day.get("xop_above_ma5", above_ma5))
    max_bp_20d = float(day.get("max_bpos_20d", bp))
    max_xp_20d = float(day.get("max_xpos_20d", xp))

    if bp >= 0.90:
        return 0.0, f"布油2年极高分位({bp * 100:.1f}%>=90%)收盘全平避险(0%)"
    if bp >= 0.72 and not above_ma5:
        return 0.0, f"布油高位({bp * 100:.1f}%>=72%)且跌破MA5，收盘空仓避险(0%)"
    if max_bp_20d >= 0.72 and not non_bear_ma15:
        return 0.0, "高油价回落确认空头趋势(MA5<MA15)，收盘全平避险(0%)"
    if max_xp_20d >= 0.80 and (not non_bear_ma15) and (not xop_bull):
        return 0.0, "美股XOP高位回落且中美油气双空头，收盘全平避险(0%)"
    if (not non_bear_ma15) and (not above_ma5) and (not xop_bull) and pm > -1.00:
        return 0.0, f"XOP与场内双空头且无深折价保护(溢价{pm:+.2f}%)，收盘空仓(0%)"
    if (not non_bear_ma15) and (not above_ma5) and bp > 0.45 and pm > -1.20:
        return 0.0, f"场内空头破MA5且无深折价保护(溢价{pm:+.2f}%)，收盘空仓(0%)"
    if pm >= 0.80:
        return 0.0, f"XOP汇调溢价过高({pm:+.2f}%>=+0.80%)防隔夜杀溢价清仓(0%)"
    if pm >= 0.40 and not above_ma5:
        return 0.0, f"XOP汇调溢价偏高({pm:+.2f}%)且位于MA5下方，收盘避险(0%)"
    if day_lead <= -0.45 and pm >= -0.05 and (not xop_above_ma5 or not above_ma5):
        return 0.0, f"亚欧盘油气较美股收盘走弱({day_lead:+.2f}%)且无折价垫({pm:+.2f}%)，防今夜XOP补跌(0%)"

    if bp >= 0.72:
        w = 0.50 if (xop_bull and pm <= -0.25) else 0.35
        return w, f"布油偏高但守住MA5({bp * 100:.1f}%)，轻仓留底({w * 100:.0f}%)"
    if day_lead <= -0.45 and pm >= 0.0:
        return 0.35, f"亚欧盘油气偏弱({day_lead:+.2f}%)降仓防御今夜XOP波动(35%)"
    if bp <= 0.42 or xp <= 0.38:
        w = 1.00 if (pm <= 0.35 or above_ma5) else 0.85
        return w, f"油气周期低位区(BRN {bp * 100:.1f}%/XOP {xp * 100:.1f}%)，重仓留底({w * 100:.0f}%)"
    if day_lead >= 0.40 and pm <= 0.25 and non_bear_ma15:
        w = 1.00 if (above_ma5 or xop_bull or pm <= -0.25) else 0.85
        return w, f"亚欧盘油气较美股收盘领涨({day_lead:+.2f}%)且估值安全({pm:+.2f}%)，重仓博今夜XOP补涨({w * 100:.0f}%)"
    if pm <= -0.25 and (non_bear_ma15 or xop_bull):
        w = 1.00 if (above_ma5 and xop_bull) else 0.85
        return w, f"XOP汇调折价安全区({pm:+.2f}%)且多头未坏，高仓位留底({w * 100:.0f}%)"
    w = 0.85 if (above_ma5 and xop_bull) else (0.75 if above_ma5 else (0.55 if xop_bull else 0.35))
    return w, f"XOP常态区间(XOP分位{xp * 100:.1f}%/BRN {bp * 100:.1f}%, 溢价{pm:+.2f}%)，动态底仓({w * 100:.0f}%)"


def resolve_close_weight_fn(
    engine_profile: str,
    cross_market_anchor: str = "brent",
) -> Callable[[Mapping[str, Any], Mapping[str, Any], int], tuple[float, str]]:
    if engine_profile == "v9_plus_balanced":
        return brent_v10_overnight
    if cross_market_anchor == "xop_us_ep":
        return v9_xop_close_weight
    return v9_pro_close_weight


def _v9_pro_tuned_close_weight(
    day: Mapping[str, Any],
    b1453: Mapping[str, Any],
    tuning: Mapping[str, Any],
) -> tuple[float, str]:
    """Evaluate 14:53 overnight weight with calibrated LOF structural premium, bubble cap, and bull-pullback floor."""
    bp = float(b1453["brn_pos"])
    pm = float(b1453["prem"])
    pr = float(b1453["price"])
    ma5 = float(day["ma5"])
    ma15 = float(day["ma15"])
    above_ma5 = pr >= ma5
    bull_ma15 = ma5 >= ma15
    non_bear_ma15 = bull_ma15 or pr >= ma15
    prem_shift = float(tuning.get("buy_prem_offset_pct") or 0.0)

    if bp >= 0.90:
        return 0.0, f"布伦特2年极高分位({bp * 100:.1f}%>=90%)收盘全平避险(0%)"
    if bp >= 0.72 and not above_ma5:
        return 0.0, f"布伦特高位({bp * 100:.1f}%>=72%)且跌破MA5，收盘空仓避险(0%)"
    if float(day["max_bpos_20d"]) >= 0.72 and not non_bear_ma15:
        return 0.0, "高油价回落确认空头趋势(MA5<MA15)，收盘全平(0%)"
    if (not non_bear_ma15) and (not above_ma5) and bp > 0.45 and pm > (-1.50 + prem_shift):
        return 0.0, f"空头趋势下无深折价保护(溢价{pm:+.2f}%)，收盘避险空仓(0%)"

    raw_bcap = tuning.get("bubble_prem_cap_pct")
    if raw_bcap is not None:
        bcap = float(raw_bcap)
        if pm >= bcap:
            if (not above_ma5) or (not bull_ma15) or float(b1453.get("sp_fx", 0.0)) <= -1.50:
                return 0.0, f"极端高溢价泡沫({pm:+.2f}%>={bcap:.1f}%)破位或深贴水清仓避险(0%)"
            raw_bw = tuning.get("bubble_overnight_weight_pct")
            b_w = max(0.0, min(1.0, (float(raw_bw) if raw_bw is not None else 45.0) / 100.0))
            return b_w, f"极端高溢价泡沫({pm:+.2f}%>={bcap:.1f}%)多头降仓封顶({b_w * 100:.0f}%)"

    raw_pb = tuning.get("bull_pullback_weight_pct")
    if raw_pb is not None and pm >= 2.50 and (not above_ma5) and not (bull_ma15 and pr >= ma15):
        return 0.0, f"高溢价({pm:+.2f}%>=+2.5%)且均线趋势走弱，防杀溢价清仓(0%)"

    if bp >= 0.72:
        w = 0.45 if pm <= (-0.30 + prem_shift) else 0.35
        return w, f"布伦特偏高但维持MA5上方({bp * 100:.1f}%)，轻仓留底({w * 100:.0f}%)"
    if bp <= 0.42:
        w = 1.00 if (pm <= (1.20 + prem_shift) or above_ma5) else 0.85
        return w, f"布伦特2年低位区({bp * 100:.1f}%<=42%)，高仓位留底过夜({w * 100:.0f}%)"
    if pm <= (-0.30 + prem_shift) and non_bear_ma15:
        w = 1.00 if above_ma5 else 0.85
        return w, f"负溢价趋势安全区(溢价{pm:+.2f}%且非空头)，重仓留底({w * 100:.0f}%)"

    if raw_pb is not None:
        pb_w = max(0.0, min(1.0, float(raw_pb) / 100.0))
        if above_ma5:
            return 0.85, f"中枢多头区间(BRN分位{bp * 100:.1f}%, 溢价{pm:+.2f}%)，重仓留底(85%)"
        if bull_ma15 and pr >= ma15:
            return pb_w, f"多头趋势回踩MA5但守稳MA15(溢价{pm:+.2f}%)，保留T+0底仓({pb_w * 100:.0f}%)"
        return 0.50, f"中枢弱势区间(BRN分位{bp * 100:.1f}%, 溢价{pm:+.2f}%)，半仓防御(50%)"

    w = 0.80 if above_ma5 else 0.55
    if pm >= 3.00 and not above_ma5:
        w = 0.30
    return w, f"中枢常态区间(BRN分位{bp * 100:.1f}%, 溢价{pm:+.2f}%)，均衡底仓({w * 100:.0f}%)"


def apply_v9_close_weight_tuning(
    overnight_fn: Callable[[Mapping[str, Any], Mapping[str, Any], int], tuple[float, str]],
    v9_tuning: Mapping[str, Any] | None,
) -> Callable[[Mapping[str, Any], Mapping[str, Any], int], tuple[float, str]]:
    """Wrap 14:53 close-weight logic with optional LOF bubble/pullback/offset tuning and high-premium bear guard."""
    tuning = v9_tuning if isinstance(v9_tuning, Mapping) else {}
    base_fn = overnight_fn
    if base_fn is v9_pro_close_weight and any(
        tuning.get(k) is not None
        for k in ("bubble_prem_cap_pct", "bull_pullback_weight_pct", "buy_prem_offset_pct")
    ):
        frozen_tuning = dict(tuning)

        def _tuned_pro(day: Mapping[str, Any], b1453: Mapping[str, Any], _pos: int = 0) -> tuple[float, str]:
            return _v9_pro_tuned_close_weight(day, b1453, frozen_tuning)

        base_fn = _tuned_pro

    raw_cut = tuning.get("bear_high_prem_flat_pct")
    if raw_cut is None:
        return base_fn
    cut = float(raw_cut)

    def _wrapped(day: Mapping[str, Any], b1453: Mapping[str, Any], pos: int = 0) -> tuple[float, str]:
        wt, lbl = base_fn(day, b1453, pos)
        pm = float(b1453["prem"])
        pr = float(b1453["price"])
        above_ma5 = pr >= float(day["ma5"])
        non_bear_ma15 = float(day["ma5"]) >= float(day["ma15"]) or pr >= float(day["ma15"])
        if (not non_bear_ma15) and (not above_ma5) and pm >= cut:
            return 0.0, f"空头破位且高溢价({pm:+.2f}%>={cut:+.1f}%)防杀溢价清仓(0%)"
        return wt, lbl

    return _wrapped


def _load_brn_series_for_window(store: Any, lookback_start: str, cleaned_end: str | None) -> tuple[list[int], list[float]]:
    with store._lock:
        max_row = store._connection.execute(
            "SELECT MAX(trade_date || minute), COUNT(*) FROM research_points WHERE symbol='BRN0Y' AND trade_date>=?",
            (lookback_start,),
        ).fetchone()
    head_key = f"{max_row[0] or ''}:{max_row[1] or 0}" if max_row else "0"
    store_key = str(getattr(store, "path", id(store)))
    cache_key = (store_key, f"{lookback_start}:{cleaned_end or 'open'}", head_key)
    cached = _MEMORY_BRN_SERIES_CACHE.get(cache_key)
    if cached is not None:
        return cached[1], cached[2]

    sql = "SELECT trade_date, minute, price FROM research_points WHERE symbol='BRN0Y' AND trade_date>=?"
    params: list[Any] = [lookback_start]
    if cleaned_end:
        sql += " AND trade_date<=?"
        params.append(cleaned_end)
    sql += " ORDER BY trade_date ASC, REPLACE(minute, ':', '') ASC"
    with store._lock:
        rows = store._connection.execute(sql, tuple(params)).fetchall()

    ts_list: list[int] = []
    px_list: list[float] = []
    for r in rows:
        td = str(r["trade_date"])
        mm = str(r["minute"]).replace(":", "").zfill(4)
        try:
            px = float(r["price"] or 0.0)
        except (TypeError, ValueError):
            continue
        if px <= 0 or len(mm) != 4 or not mm.isdigit() or not _DATE_RE.fullmatch(td):
            continue
        hh_i, mm_i = int(mm[:2]), int(mm[2:])
        if not (0 <= hh_i <= 23 and 0 <= mm_i <= 59):
            continue
        dt_pt = _parse_sh(td, f"{mm[:2]}:{mm[2:]}")
        ts_list.append(int(dt_pt.timestamp()))
        px_list.append(px)

    if len(_MEMORY_BRN_SERIES_CACHE) > 8:
        _MEMORY_BRN_SERIES_CACHE.clear()
    _MEMORY_BRN_SERIES_CACHE[cache_key] = (time.time(), ts_list, px_list)
    return ts_list, px_list


def _day_oil_range(
    oil_rows: Sequence[Sequence[Any]],
    brn_px: Sequence[float],
    td: str,
    cutoff_ts: int,
) -> tuple[float, float]:
    cutoff_start = (datetime.strptime(td, "%Y%m%d") - timedelta(days=730)).strftime("%Y%m%d")
    vals = [float(r[2]) for r in oil_rows if int(r[1]) <= cutoff_ts and str(r[0]) >= cutoff_start]
    if vals:
        return min(vals), max(vals)
    if brn_px:
        return min(brn_px), max(brn_px)
    return 60.0, 90.0


def _day_xop_range(
    xop_rows: Sequence[Sequence[Any]],
    td: str,
    cutoff_ts: int,
) -> tuple[float, float]:
    cutoff_start = (datetime.strptime(td, "%Y%m%d") - timedelta(days=730)).strftime("%Y%m%d")
    vals = [float(r[2]) for r in xop_rows if int(r[1]) <= cutoff_ts and str(r[0]) >= cutoff_start]
    if vals:
        return min(vals), max(vals)
    if xop_rows:
        all_vals = [float(r[2]) for r in xop_rows]
        return min(all_vals), max(all_vals)
    return 120.0, 200.0


def _merge_live_xop_rows(
    xop_rows: Sequence[Sequence[Any]],
    live_xop_spot: Mapping[str, Any] | None,
) -> list[list[Any]]:
    merged: dict[str, list[Any]] = {str(r[0]): [str(r[0]), int(r[1]), float(r[2])] for r in xop_rows}
    if live_xop_spot:
        reg_d = str(live_xop_spot.get("xop_reg_date") or "").strip()
        close_v = live_xop_spot.get("xop_close")
        if _DATE_RE.fullmatch(reg_d) and close_v is not None and float(close_v) > 0:
            causal_dt = datetime.strptime(reg_d, "%Y%m%d").replace(tzinfo=SHANGHAI_TZ) + timedelta(days=1, hours=6)
            merged[reg_d] = [reg_d, int(causal_dt.timestamp()), round(float(close_v), 4)]
    return sorted(merged.values(), key=lambda r: (r[1], r[0]))


def _build_xop_days_timeline(
    *,
    day_rows: Sequence[Any],
    pts_by_day: Mapping[str, list[dict[str, Any]]],
    clean_start: str,
    end_date: str | None,
    close_cutoff_time: str,
    eff_macd: Mapping[str, Any],
    brn_ts: Sequence[int],
    brn_px: Sequence[float],
    fx_ts: Sequence[int],
    fx_px: Sequence[float],
    oil_rows: Sequence[Sequence[Any]],
    nav_map: Mapping[str, float],
    xop_rows: Sequence[Sequence[Any]],
    spy_rows: Sequence[Sequence[Any]],
    ng_rows: Sequence[Sequence[Any]],
    ng_1m_ts: Sequence[int],
    ng_1m_px: Sequence[float],
    es_1m_ts: Sequence[int],
    es_1m_px: Sequence[float],
    live_xop_spot: Mapping[str, Any] | None,
) -> tuple[list[dict[str, Any]], int]:
    """Build causal feature timeline anchored to US XOP closes + intraday BRN0Y/NG/ES/FX."""
    xop_by_d = {str(r[0]): float(r[2]) for r in xop_rows}
    xop_ts = [int(r[1]) for r in xop_rows]
    spy_ts = [int(r[1]) for r in spy_rows]
    ng_ts = [int(r[1]) for r in ng_rows]
    nav_dates = sorted(nav_map)

    ratio_hist: list[float] = []
    close_hist: list[float] = []
    recent_bpos: list[float] = []
    recent_xpos: list[float] = []
    days_out: list[dict[str, Any]] = []
    candidate_days = sum(1 for d in day_rows if str(d["trade_date"]) >= clean_start)

    for idx, day_row in enumerate(day_rows):
        td = str(day_row["trade_date"])
        raw_pts = pts_by_day.get(td, [])
        if len(raw_pts) < 2:
            continue
        prices = [float(p["price"]) for p in raw_pts]
        macd_rows = calculate_macd(prices, eff_macd)
        pts = [{**p, **m} for p, m in zip(raw_pts, macd_rows)]
        p1500 = next((p for p in reversed(prices) if p > 0), 0.0)

        has_prev_td = idx >= 1
        prev_td = str(day_rows[idx - 1]["trade_date"]) if has_prev_td else td
        td_m2 = str(day_rows[idx - 2]["trade_date"]) if idx >= 2 else prev_td
        t0930_dt = _parse_sh(td, "09:30")
        t0930_ts = int(t0930_dt.timestamp())
        prev0930_ts = int(_parse_sh(prev_td, "09:30").timestamp())

        xi = max(0, bisect_right(xop_ts, t0930_ts) - 1)
        xi_prev = max(0, bisect_right(xop_ts, prev0930_ts) - 1) if has_prev_td else max(0, xi - 1)
        x_cur = float(xop_rows[xi][2])
        x_prev_us = float(xop_rows[xi_prev][2])
        xop_ov = (x_cur / x_prev_us - 1.0) * 100.0 if x_prev_us > 0 else 0.0

        si = bisect_right(spy_ts, t0930_ts) - 1
        si_prev = (bisect_right(spy_ts, prev0930_ts) - 1) if has_prev_td else (si - 1)
        spy_ov = (
            (float(spy_rows[si][2]) / float(spy_rows[si_prev][2]) - 1.0) * 100.0
            if (si >= 0 and si_prev >= 0 and float(spy_rows[si_prev][2]) > 0)
            else 0.0
        )

        ni = bisect_right(ng_ts, t0930_ts) - 1
        ni_prev = (bisect_right(ng_ts, prev0930_ts) - 1) if has_prev_td else (ni - 1)
        ng_ov = (
            (float(ng_rows[ni][2]) / float(ng_rows[ni_prev][2]) - 1.0) * 100.0
            if (ni >= 0 and ni_prev >= 0 and float(ng_rows[ni_prev][2]) > 0)
            else 0.0
        )

        x_ma5 = statistics.mean(float(r[2]) for r in xop_rows[max(0, xi - 4) : xi + 1])
        x_ma15 = statistics.mean(float(r[2]) for r in xop_rows[max(0, xi - 14) : xi + 1])
        x_lo, x_hi = _day_xop_range(xop_rows, td, t0930_ts)
        xop_pos = _brent_range_position(x_cur, x_lo, x_hi)

        dt_us_close = datetime.fromtimestamp(int(xop_rows[xi][1]) - 3600, tz=SHANGHAI_TZ)
        b_us_close = _val_at(brn_ts, brn_px, dt_us_close) or _val_at(brn_ts, brn_px, t0930_dt) or 70.0
        f_us_close = _val_at(fx_ts, fx_px, _parse_sh(str(xop_rows[xi][0]), "23:00")) or _DEFAULT_FX

        dt_prev_us_close = datetime.fromtimestamp(int(xop_rows[xi_prev][1]) - 3600, tz=SHANGHAI_TZ)
        b_prev_us_close = _val_at(brn_ts, brn_px, dt_prev_us_close) or b_us_close
        b1500 = _val_at(brn_ts, brn_px, _parse_sh(td, "15:00")) or b_us_close
        f1500 = _val_at(fx_ts, fx_px, _parse_sh(td, "15:00")) or f_us_close

        x_syn_1500 = x_cur * (1.0 + _BETA_DAYTIME_BRN * ((b1500 / b_us_close - 1.0) if b_us_close > 0 else 0.0))
        cur_r = p1500 / ((x_syn_1500 ** _W_XOP) * f1500) if (x_syn_1500 > 0 and f1500 > 0 and p1500 > 0) else None
        lo_h, hi_h = _day_oil_range(oil_rows, brn_px, td, t0930_ts)

        in_range = td >= clean_start and (not end_date or td <= end_date)
        if in_range:
            pre_raw = day_row["pre_close"]
            pre = float(pre_raw) if pre_raw is not None and float(pre_raw) > 0 else (close_hist[-1] if close_hist else prices[0])
            b_open_td = _val_at(brn_ts, brn_px, t0930_dt)
            f_open_td = _val_at(fx_ts, fx_px, t0930_dt) or _DEFAULT_FX
            b_p15 = (_val_at(brn_ts, brn_px, _parse_sh(prev_td, "15:00")) if has_prev_td else b_open_td) or b_prev_us_close
            f_p15 = (_val_at(fx_ts, fx_px, _parse_sh(prev_td, "15:00")) if has_prev_td else f_open_td) or f_us_close
            x_syn_p15 = (
                x_prev_us * (1.0 + _BETA_DAYTIME_BRN * ((b_p15 / b_prev_us_close - 1.0) if b_prev_us_close > 0 else 0.0))
                if has_prev_td
                else x_cur
            )

            nav_td = next((k for k in reversed(nav_dates) if k <= td_m2), None) if has_prev_td else None
            if nav_td is not None:
                nav_base = float(nav_map[nav_td])
                nav_xop_td = max((d for d in xop_by_d if d <= nav_td), default=None)
                x_nav0 = xop_by_d[nav_xop_td] if nav_xop_td is not None else x_cur
                f_nav0 = _val_at(fx_ts, fx_px, _parse_sh(nav_td, "23:00")) or f_us_close
                nav_est_t1 = (
                    nav_base * (1.0 + _W_XOP * (x_cur / x_nav0 - 1.0)) * (f_us_close / f_nav0)
                    if x_nav0 > 0 and f_nav0 > 0
                    else pre
                )
            else:
                nav_est_t1 = pre
                f_us_close = f_p15

            brn_ov = ((b_open_td or b_p15) / b_p15 - 1.0) * 100.0 if b_p15 > 0 else 0.0
            xop_vs_oil_alpha = xop_ov - 0.55 * brn_ov
            eq10 = (
                statistics.mean(ratio_hist[-10:])
                if ratio_hist
                else (pre / ((x_syn_p15 ** _W_XOP) * f_p15) if x_syn_p15 > 0 and f_p15 > 0 else None)
            )
            ma5 = statistics.mean(close_hist[-5:]) if close_hist else pre
            ma15 = statistics.mean(close_hist[-15:]) if close_hist else pre

            ng_base_pair = _val_at_with_ts(ng_1m_ts, ng_1m_px, dt_us_close)
            ng_us_close, ng_base_ts = (
                ng_base_pair
                if (ng_base_pair is not None and abs(dt_us_close.timestamp() - ng_base_pair[1]) <= 12 * 3600)
                else (None, 0)
            )
            es_base_pair = _val_at_with_ts(es_1m_ts, es_1m_px, dt_us_close)
            es_us_close, es_base_ts = (
                es_base_pair
                if (es_base_pair is not None and abs(dt_us_close.timestamp() - es_base_pair[1]) <= 12 * 3600)
                else (None, 0)
            )

            live_ext_r = 0.0
            if (
                live_xop_spot
                and str(live_xop_spot.get("xop_quote_date") or "") == td
                and live_xop_spot.get("xop_ext_chg_pct") is not None
            ):
                ext_val = float(live_xop_spot["xop_ext_chg_pct"]) / 100.0
                if abs(ext_val) <= 0.08:
                    live_ext_r = ext_val

            bars: list[dict[str, Any]] = []
            sp0: float | None = None
            b0_iopv: float | None = None
            cum_pv = 0.0
            cum_v = 0.0
            hi_px = 0.0
            hi_sp = -999.0
            px_scale = max(1.0, min(2.2, 2.0 / max(0.30, pre)))

            for i, pt in enumerate(pts):
                pr = float(pt["price"])
                if pr <= 0:
                    continue
                tm = pt["time"]
                t_dt = _parse_sh(td, tm)
                bn = _val_at(brn_ts, brn_px, t_dt) or b_us_close
                fn = _val_at(fx_ts, fx_px, t_dt) or f_us_close

                daytime_brn_r = max(-0.15, min(0.15, (bn / b_us_close - 1.0) if b_us_close > 0 else 0.0))
                daytime_ng_r = 0.0
                if ng_us_close and ng_us_close > 0:
                    ng_now_pair = _val_at_with_ts(ng_1m_ts, ng_1m_px, t_dt)
                    if ng_now_pair is not None and ng_now_pair[1] > ng_base_ts:
                        daytime_ng_r = max(-0.15, min(0.15, ng_now_pair[0] / ng_us_close - 1.0))
                daytime_es_r = 0.0
                if es_us_close and es_us_close > 0:
                    es_now_pair = _val_at_with_ts(es_1m_ts, es_1m_px, t_dt)
                    if es_now_pair is not None and es_now_pair[1] > es_base_ts:
                        daytime_es_r = max(-0.10, min(0.10, es_now_pair[0] / es_us_close - 1.0))

                ext_r_bar = live_ext_r if tm >= "09:00" else 0.0
                daytime_xop_r = (
                    ext_r_bar
                    + _BETA_DAYTIME_BRN * daytime_brn_r
                    + _BETA_DAYTIME_NG * daytime_ng_r
                    + _BETA_DAYTIME_ES * daytime_es_r
                )
                x_syn = x_cur * (1.0 + daytime_xop_r)
                iopv = (
                    nav_est_t1 * (1.0 + _W_XOP * daytime_xop_r) * (fn / f_us_close)
                    if f_us_close > 0
                    else pre
                )
                prem = (pr / iopv - 1.0) * 100.0 if iopv > 0 else 0.0
                brn_chg = (bn / b_p15 - 1.0) * 100.0 if b_p15 > 0 else 0.0
                fx_chg = (fn / f_p15 - 1.0) * 100.0 if f_p15 > 0 else 0.0
                lof_chg = (pr - pre) / pre * 100.0 if pre > 0 else 0.0
                xop_syn_chg = (x_syn / x_syn_p15 - 1.0) * 100.0 if x_syn_p15 > 0 else 0.0
                sp_fx = lof_chg - (_W_XOP * xop_syn_chg + fx_chg)
                if sp0 is None:
                    sp0 = sp_fx
                    b0_iopv = iopv if iopv > 0 else pr
                coint10 = (
                    (pr / (eq10 * (x_syn ** _W_XOP) * fn) - 1.0) * 100.0
                    if eq10 and x_syn > 0 and fn > 0
                    else prem
                )
                vol_f = float(pt.get("volume") or 0.0)
                if vol_f > 0:
                    cum_pv += pr * vol_f
                    cum_v += vol_f
                avg_raw = pt.get("average_price")
                vwap_intra = (
                    float(avg_raw)
                    if (avg_raw is not None and float(avg_raw) > 0)
                    else (cum_pv / cum_v if cum_v > 0 else pr)
                )
                hi_px = max(hi_px, pr)
                hi_sp = max(hi_sp, sp_fx)
                vdev = (pr / vwap_intra - 1.0) * 100.0 if vwap_intra > 0 else 0.0
                px_dd_hi = (hi_px - pr) / hi_px * 100.0 if hi_px > 0 else 0.0
                sp_dd_hi = hi_sp - sp_fx
                prev5_brn = float(bars[len(bars) - 5]["brn"]) if len(bars) >= 5 else bn
                brn_m5 = (bn / prev5_brn - 1.0) * 100.0 if len(bars) >= 5 and prev5_brn > 0 else 0.0
                p1 = float(pts[i - 1]["price"]) if i >= 1 and float(pts[i - 1]["price"]) > 0 else pr
                p3 = float(pts[i - 3]["price"]) if i >= 3 and float(pts[i - 3]["price"]) > 0 else pr
                p5 = float(pts[i - 5]["price"]) if i >= 5 and float(pts[i - 5]["price"]) > 0 else pr
                m1 = (pr - p1) / p1 * 100.0 if i >= 1 else 0.0
                m3 = (pr - p3) / p3 * 100.0 if i >= 3 else 0.0
                m5 = (pr - p5) / p5 * 100.0 if i >= 5 else 0.0
                macd_raw = _causal_macd_fields(pts, i)
                macd_f = {
                    "diff10": macd_raw["diff10"] * px_scale,
                    "dea10": macd_raw["dea10"] * px_scale,
                    "macd10": macd_raw["macd10"] * px_scale,
                    "dmc10": macd_raw["dmc10"] * px_scale,
                    "max_red12": macd_raw["max_red12"] * px_scale,
                    "min_grn12": macd_raw["min_grn12"] * px_scale,
                    "gc10": macd_raw["gc10"],
                    "dc10": macd_raw["dc10"],
                }
                daytime_lead_pct = (daytime_brn_r + 0.15 * daytime_ng_r + 0.30 * daytime_es_r) * 100.0
                bars.append(
                    {
                        "i": len(bars),
                        "time": tm,
                        "price": pr,
                        "average_price": pt.get("average_price"),
                        "volume": pt.get("volume"),
                        "iopv": iopv,
                        "prem": prem,
                        "lof_chg": lof_chg,
                        "vwap_intra": vwap_intra,
                        "vdev": vdev,
                        "px_dd_hi": px_dd_hi,
                        "sp_dd_hi": sp_dd_hi,
                        "brn_m5": brn_m5,
                        "sp_fx": sp_fx,
                        "sp_intra": sp_fx - sp0,
                        "iopv_intra": ((iopv / b0_iopv - 1.0) * 100.0) if b0_iopv else 0.0,
                        "coint10": coint10,
                        "brn": bn,
                        "brn_chg": brn_chg,
                        "brn_pos": _brent_range_position(bn, lo_h, hi_h),
                        "daytime_brn_pct": daytime_brn_r * 100.0,
                        "daytime_lead_pct": daytime_lead_pct,
                        "xop_pos": _brent_range_position(x_syn, x_lo, x_hi),
                        "fx": fn,
                        **macd_f,
                        "m1": m1,
                        "m3": m3,
                        "m5": m5,
                    }
                )

            if bars:
                b_close_cut = next((b for b in reversed(bars) if b["time"] <= close_cutoff_time), bars[-1])
                max_bpos_20d = max(recent_bpos[-20:] + [b_close_cut["brn_pos"]])
                max_xpos_20d = max(recent_xpos[-20:] + [xop_pos])
                recent_bpos.append(b_close_cut["brn_pos"])
                recent_xpos.append(xop_pos)
                days_out.append(
                    {
                        "td": td,
                        "ym": f"{td[:4]}-{td[4:6]}",
                        "pre": pre,
                        "ma5": ma5,
                        "ma15": ma15,
                        "p1500": bars[-1]["price"],
                        "lo_hist": lo_h,
                        "hi_hist": hi_h,
                        "max_bpos_20d": max_bpos_20d,
                        "max_xpos_20d": max_xpos_20d,
                        "xop_close": x_cur,
                        "xop_ov": xop_ov,
                        "spy_ov": spy_ov,
                        "ng_ov": ng_ov,
                        "brn_ov": brn_ov,
                        "xop_vs_oil_alpha": xop_vs_oil_alpha,
                        "xop_ma5": x_ma5,
                        "xop_ma15": x_ma15,
                        "xop_bull": (x_ma5 >= x_ma15) or (x_cur >= x_ma15),
                        "xop_above_ma5": x_cur >= x_ma5,
                        "cross_market_anchor": "xop_us_ep",
                        "bars": bars,
                    }
                )
        else:
            valid_pts = [pt for pt in pts if float(pt["price"]) > 0]
            if valid_pts:
                pt_cut = next((pt for pt in reversed(valid_pts) if pt["time"] <= close_cutoff_time), valid_pts[-1])
                b_open_td = _val_at(brn_ts, brn_px, t0930_dt)
                b_p15 = (_val_at(brn_ts, brn_px, _parse_sh(prev_td, "15:00")) if has_prev_td else b_open_td) or b_us_close
                bn_cut = _val_at(brn_ts, brn_px, _parse_sh(td, pt_cut["time"])) or b_p15
                recent_bpos.append(_brent_range_position(bn_cut, lo_h, hi_h))
                recent_xpos.append(xop_pos)

        if cur_r is not None:
            ratio_hist.append(cur_r)
        if p1500 > 0:
            close_hist.append(p1500)

    return days_out, candidate_days


def build_v9_plus_days(
    store: Any,
    *,
    symbol: str = "160723",
    start_date: str = "20260701",
    end_date: str | None = None,
    macd_settings: Mapping[str, Any] | None = None,
    allow_network: bool | None = None,
    live_spot: tuple[float | None, float | None] | None = None,
    close_cutoff_time: str = "14:53",
    warmup_days: int = 55,
    cross_market_anchor: str = "brent",
    live_xop_spot: Mapping[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """Build enriched per-day feature timelines using strictly causal aligned inputs."""
    clean_start = str(start_date or "20260701").replace("-", "").strip()
    try:
        lookback_start = (datetime.strptime(clean_start, "%Y%m%d") - timedelta(days=warmup_days)).strftime("%Y%m%d")
    except ValueError:
        lookback_start = clean_start

    with store._lock:
        day_sql = "SELECT trade_date, pre_close FROM research_days WHERE symbol=? AND trade_date>=?"
        day_params: list[Any] = [symbol, lookback_start]
        if end_date:
            day_sql += " AND trade_date<=?"
            day_params.append(end_date)
        day_sql += " ORDER BY trade_date ASC"
        day_rows = store._connection.execute(day_sql, tuple(day_params)).fetchall()

        pt_sql = (
            "SELECT trade_date, minute, price, average_price, volume FROM research_points "
            "WHERE symbol=? AND trade_date>=?"
            + (" AND trade_date<=?" if end_date else "")
            + " ORDER BY trade_date ASC, REPLACE(minute, ':', '') ASC"
        )
        pts_by_day: dict[str, list[dict[str, Any]]] = {}
        for r in store._connection.execute(pt_sql, tuple(day_params)).fetchall():
            pts_by_day.setdefault(str(r["trade_date"]), []).append(
                {
                    "time": str(r["minute"]),
                    "price": float(r["price"]) if r["price"] is not None else 0.0,
                    "average_price": float(r["average_price"]) if r["average_price"] is not None else None,
                    "volume": float(r["volume"]) if r["volume"] is not None else None,
                }
            )

    brn_ts, brn_px = _load_brn_series_for_window(store, lookback_start, end_date)
    macro = V9MacroDataStore(store, allow_network=allow_network)
    nav_map = macro.load_nav_map(symbol)
    oil_rows = macro.load_oil_daily_rows()
    fx_ts, fx_px = macro.load_fx_series()

    if live_spot is not None:
        spot_brn, spot_fx = live_spot
        now_ts = int(datetime.now(SHANGHAI_TZ).timestamp())
        if spot_brn is not None and spot_brn > 0 and (not brn_ts or now_ts > brn_ts[-1]):
            brn_ts = [*brn_ts, now_ts]
            brn_px = [*brn_px, float(spot_brn)]
        if spot_fx is not None and spot_fx > 0 and (not fx_ts or now_ts > fx_ts[-1]):
            fx_ts = [*fx_ts, now_ts]
            fx_px = [*fx_px, float(spot_fx)]

    eff_macd = macd_settings or {"short": 10, "long": 20, "signal": 5}
    if cross_market_anchor == "xop_us_ep":
        xop_rows = _merge_live_xop_rows(macro.load_xop_daily_rows(), live_xop_spot)
        if xop_rows:
            spy_rows = macro.load_spy_daily_rows()
            ng_rows = macro.load_ng_daily_rows()
            ng_1m_ts, ng_1m_px = macro.load_fut_1m_series("NG")
            es_1m_ts, es_1m_px = macro.load_fut_1m_series("ES")
            if live_xop_spot is not None:
                now_ts = int(datetime.now(SHANGHAI_TZ).timestamp())
                live_ng = live_xop_spot.get("ng_px")
                live_es = live_xop_spot.get("es_px")
                if live_ng is not None and float(live_ng) > 0 and (not ng_1m_ts or now_ts > ng_1m_ts[-1]):
                    ng_1m_ts = [*ng_1m_ts, now_ts]
                    ng_1m_px = [*ng_1m_px, float(live_ng)]
                if live_es is not None and float(live_es) > 0 and (not es_1m_ts or now_ts > es_1m_ts[-1]):
                    es_1m_ts = [*es_1m_ts, now_ts]
                    es_1m_px = [*es_1m_px, float(live_es)]
            return _build_xop_days_timeline(
                day_rows=day_rows,
                pts_by_day=pts_by_day,
                clean_start=clean_start,
                end_date=end_date,
                close_cutoff_time=close_cutoff_time,
                eff_macd=eff_macd,
                brn_ts=brn_ts,
                brn_px=brn_px,
                fx_ts=fx_ts,
                fx_px=fx_px,
                oil_rows=oil_rows,
                nav_map=nav_map,
                xop_rows=xop_rows,
                spy_rows=spy_rows,
                ng_rows=ng_rows,
                ng_1m_ts=ng_1m_ts,
                ng_1m_px=ng_1m_px,
                es_1m_ts=es_1m_ts,
                es_1m_px=es_1m_px,
                live_xop_spot=live_xop_spot,
            )

    ratio_hist: list[float] = []
    close_hist: list[float] = []
    recent_bpos: list[float] = []
    days_out: list[dict[str, Any]] = []
    candidate_days = sum(1 for d in day_rows if str(d["trade_date"]) >= clean_start)
    nav_dates = sorted(nav_map)

    for idx, day_row in enumerate(day_rows):
        td = str(day_row["trade_date"])
        raw_pts = pts_by_day.get(td, [])
        if len(raw_pts) < 2:
            continue
        prices = [float(p["price"]) for p in raw_pts]
        macd_rows = calculate_macd(prices, eff_macd)
        pts = [{**p, **m} for p, m in zip(raw_pts, macd_rows)]
        p1500 = next((p for p in reversed(prices) if p > 0), 0.0)
        b1500 = _val_at(brn_ts, brn_px, _parse_sh(td, "15:00"))
        f1500 = _val_at(fx_ts, fx_px, _parse_sh(td, "15:00")) or _DEFAULT_FX
        cur_r = p1500 / ((b1500 ** _BETA) * f1500) if (b1500 and f1500 and p1500 > 0) else None

        in_range = td >= clean_start and (not end_date or td <= end_date)
        if in_range:
            pre_raw = day_row["pre_close"]
            pre = float(pre_raw) if pre_raw is not None and float(pre_raw) > 0 else (close_hist[-1] if close_hist else prices[0])
            has_prev_td = idx >= 1
            prev_td = str(day_rows[idx - 1]["trade_date"]) if has_prev_td else td
            td_m2 = str(day_rows[idx - 2]["trade_date"]) if idx >= 2 else prev_td
            b_open_td = _val_at(brn_ts, brn_px, _parse_sh(td, "09:30"))
            f_open_td = _val_at(fx_ts, fx_px, _parse_sh(td, "09:30")) or _DEFAULT_FX
            nav_td = next((k for k in reversed(nav_dates) if k <= td_m2), None) if has_prev_td else None
            if nav_td is not None:
                nav_base = float(nav_map[nav_td])
                dt_nav = _parse_sh(nav_td, "23:59") + timedelta(hours=5)
                b_nav0 = _val_at(brn_ts, brn_px, dt_nav) or b_open_td or b1500 or 1.0
                f_nav0 = _val_at(fx_ts, fx_px, dt_nav) or f_open_td or _DEFAULT_FX
            else:
                nav_base = pre
                b_nav0 = (_val_at(brn_ts, brn_px, _parse_sh(prev_td, "15:00")) if has_prev_td else b_open_td) or b1500 or 1.0
                f_nav0 = (_val_at(fx_ts, fx_px, _parse_sh(prev_td, "15:00")) if has_prev_td else f_open_td) or _DEFAULT_FX

            b_p15 = (_val_at(brn_ts, brn_px, _parse_sh(prev_td, "15:00")) if has_prev_td else b_open_td) or b_nav0
            f_p15 = (_val_at(fx_ts, fx_px, _parse_sh(prev_td, "15:00")) if has_prev_td else f_open_td) or f_nav0
            eq10 = (
                statistics.mean(ratio_hist[-10:])
                if ratio_hist
                else (pre / ((b_p15 ** _BETA) * f_p15) if b_p15 > 0 and f_p15 > 0 else None)
            )
            ma5 = statistics.mean(close_hist[-5:]) if close_hist else pre
            ma15 = statistics.mean(close_hist[-15:]) if close_hist else pre
            lo_h, hi_h = _day_oil_range(oil_rows, brn_px, td, int(_parse_sh(td, "09:30").timestamp()))

            bars: list[dict[str, Any]] = []
            sp0: float | None = None
            b0_iopv: float | None = None
            cum_pv = 0.0
            cum_v = 0.0
            hi_px = 0.0
            hi_sp = -999.0
            for i, pt in enumerate(pts):
                pr = float(pt["price"])
                if pr <= 0:
                    continue
                t_dt = _parse_sh(td, pt["time"])
                bn = _val_at(brn_ts, brn_px, t_dt) or b_p15
                fn = _val_at(fx_ts, fx_px, t_dt) or f_p15
                iopv = nav_base * (1.0 + _W_OIL * (bn / b_nav0 - 1.0)) * (fn / f_nav0) if b_nav0 > 0 and f_nav0 > 0 else pre
                prem = (pr / iopv - 1.0) * 100.0 if iopv > 0 else 0.0
                brn_chg = (bn / b_p15 - 1.0) * 100.0 if b_p15 > 0 else 0.0
                fx_chg = (fn / f_p15 - 1.0) * 100.0 if f_p15 > 0 else 0.0
                lof_chg = (pr - pre) / pre * 100.0 if pre > 0 else 0.0
                sp_fx = lof_chg - (_BETA * brn_chg + fx_chg)
                if sp0 is None:
                    sp0 = sp_fx
                    b0_iopv = iopv if iopv > 0 else pr
                coint10 = (
                    (pr / (eq10 * (bn ** _BETA) * fn) - 1.0) * 100.0
                    if eq10 and bn > 0 and fn > 0
                    else 0.0
                )
                vol_f = float(pt.get("volume") or 0.0)
                if vol_f > 0:
                    cum_pv += pr * vol_f
                    cum_v += vol_f
                avg_raw = pt.get("average_price")
                vwap_intra = (
                    float(avg_raw)
                    if (avg_raw is not None and float(avg_raw) > 0)
                    else (cum_pv / cum_v if cum_v > 0 else pr)
                )
                hi_px = max(hi_px, pr)
                hi_sp = max(hi_sp, sp_fx)
                vdev = (pr / vwap_intra - 1.0) * 100.0 if vwap_intra > 0 else 0.0
                px_dd_hi = (hi_px - pr) / hi_px * 100.0 if hi_px > 0 else 0.0
                sp_dd_hi = hi_sp - sp_fx
                prev5_brn = float(bars[len(bars) - 5]["brn"]) if len(bars) >= 5 else bn
                brn_m5 = (bn / prev5_brn - 1.0) * 100.0 if len(bars) >= 5 and prev5_brn > 0 else 0.0
                p1 = float(pts[i - 1]["price"]) if i >= 1 and float(pts[i - 1]["price"]) > 0 else pr
                p3 = float(pts[i - 3]["price"]) if i >= 3 and float(pts[i - 3]["price"]) > 0 else pr
                p5 = float(pts[i - 5]["price"]) if i >= 5 and float(pts[i - 5]["price"]) > 0 else pr
                m1 = (pr - p1) / p1 * 100.0 if i >= 1 else 0.0
                m3 = (pr - p3) / p3 * 100.0 if i >= 3 else 0.0
                m5 = (pr - p5) / p5 * 100.0 if i >= 5 else 0.0
                macd_f = _causal_macd_fields(pts, i)
                bars.append(
                    {
                        "i": len(bars),
                        "time": pt["time"],
                        "price": pr,
                        "average_price": pt.get("average_price"),
                        "volume": pt.get("volume"),
                        "iopv": iopv,
                        "prem": prem,
                        "lof_chg": lof_chg,
                        "vwap_intra": vwap_intra,
                        "vdev": vdev,
                        "px_dd_hi": px_dd_hi,
                        "sp_dd_hi": sp_dd_hi,
                        "brn_m5": brn_m5,
                        "sp_fx": sp_fx,
                        "sp_intra": sp_fx - sp0,
                        "iopv_intra": ((iopv / b0_iopv - 1.0) * 100.0) if b0_iopv else 0.0,
                        "coint10": coint10,
                        "brn": bn,
                        "brn_chg": brn_chg,
                        "brn_pos": _brent_range_position(bn, lo_h, hi_h),
                        "fx": fn,
                        **macd_f,
                        "m1": m1,
                        "m3": m3,
                        "m5": m5,
                    }
                )
            if bars:
                b_close_cut = next((b for b in reversed(bars) if b["time"] <= close_cutoff_time), bars[-1])
                max_bpos_20d = max(recent_bpos[-20:] + [b_close_cut["brn_pos"]])
                recent_bpos.append(b_close_cut["brn_pos"])
                days_out.append(
                    {
                        "td": td,
                        "ym": f"{td[:4]}-{td[4:6]}",
                        "pre": pre,
                        "ma5": ma5,
                        "ma15": ma15,
                        "p1500": bars[-1]["price"],
                        "lo_hist": lo_h,
                        "hi_hist": hi_h,
                        "max_bpos_20d": max_bpos_20d,
                        "bars": bars,
                    }
                )
        else:
            valid_pts = [pt for pt in pts if float(pt["price"]) > 0]
            if valid_pts:
                pt_cut = next((pt for pt in reversed(valid_pts) if pt["time"] <= close_cutoff_time), valid_pts[-1])
                has_prev_td = idx >= 1
                prev_td = str(day_rows[idx - 1]["trade_date"]) if has_prev_td else td
                b_open_td = _val_at(brn_ts, brn_px, _parse_sh(td, "09:30"))
                b_p15 = (_val_at(brn_ts, brn_px, _parse_sh(prev_td, "15:00")) if has_prev_td else b_open_td) or b1500 or 1.0
                bn_cut = _val_at(brn_ts, brn_px, _parse_sh(td, pt_cut["time"])) or b_p15
                lo_h, hi_h = _day_oil_range(oil_rows, brn_px, td, int(_parse_sh(td, "09:30").timestamp()))
                recent_bpos.append(_brent_range_position(bn_cut, lo_h, hi_h))
        if cur_r is not None:
            ratio_hist.append(cur_r)
        if p1500 > 0:
            close_hist.append(p1500)

    return days_out, candidate_days


def _horizon_stats(items: Sequence[Mapping[str, Any]], *, side: str, field: str, min_move_pct: float) -> dict[str, Any]:
    vals = [float(x[field]) for x in items if x.get(field) is not None]
    total = len(vals)
    insufficient = len(items) - total
    if not total:
        return {
            "total": 0,
            "insufficient_tail": insufficient,
            "wins": 0,
            "win_rate_pct": None,
            "avg_price_change_pct": None,
            "median_price_change_pct": None,
            "avg_favorable_pct": None,
            "median_favorable_pct": None,
        }
    favorable = vals if side == "buy" else [-v for v in vals]
    wins = sum(1 for f in favorable if (f > 0 if min_move_pct <= 0 else f >= min_move_pct))
    return {
        "total": total,
        "insufficient_tail": insufficient,
        "wins": wins,
        "win_rate_pct": round(wins / total * 100.0, 2),
        "avg_price_change_pct": round(statistics.mean(vals), 4),
        "median_price_change_pct": round(statistics.median(vals), 4),
        "avg_favorable_pct": round(statistics.mean(favorable), 4),
        "median_favorable_pct": round(statistics.median(favorable), 4),
    }


def _v9_bucket_session(hhmm: str) -> str:
    compact = hhmm.replace(":", "")
    if compact < "1000":
        return "09:30-10:00 开盘脉冲"
    if compact <= "1130":
        return "10:00-11:30 上午盘"
    if compact < "1400":
        return "13:00-14:00 午后盘"
    return "14:00-15:00 尾盘"


def evaluate_v9_plus_backtest(
    store: Any,
    rule_spec: Mapping[str, Any],
    criteria: Mapping[str, Any],
    pos_sizing: Mapping[str, Any],
    *,
    symbol: str,
    cleaned_end: str | None,
    signal_limit: int = 200,
    allow_network: bool | None = None,
) -> dict[str, Any]:
    """Run causal V9+ Aggressive/Balanced fixed-capital simulation matching QuantEvaluationResult."""
    macd_settings = rule_spec.get("macd_settings") or {"short": 10, "long": 20, "signal": 5, "marker_threshold": 0.0002}
    close_hhmm = str(pos_sizing.get("close_flatten_time") or "14:53")
    engine_profile = str(pos_sizing.get("engine_profile") or "v9_plus_aggressive")
    cross_market_anchor = _resolve_cross_market_anchor(pos_sizing)
    v9_tuning = dict(pos_sizing.get("v9_tuning") or {})
    strict_scalp = bool(v9_tuning.get("scalp_strict_mode"))
    overnight_fn = apply_v9_close_weight_tuning(
        resolve_close_weight_fn(engine_profile, cross_market_anchor), v9_tuning
    )
    sc = float(pos_sizing.get("scale_factor") or 1.0)
    cooldown = max(1, int(pos_sizing.get("cooldown_minutes") or 4))
    max_legs = max(1, int(pos_sizing.get("max_daily_entries") or criteria.get("max_daily_trades") or 8))
    fee_pct = float(criteria.get("round_trip_fee_pct") or 0.01)
    single_fee = (fee_pct / 2.0) / 100.0
    cap_limit = float(criteria.get("capital_cap") or 100000.0)
    primary_h = int(criteria.get("primary_horizon_minutes") or 5)
    min_move = float(criteria.get("min_move_pct") or 0.0)
    rule_id = str(rule_spec.get("id") or "ai-160723-v9-plus-aggressive")

    days, candidate_days = build_v9_plus_days(
        store,
        symbol=symbol,
        start_date=str(criteria.get("start_date") or "20260701"),
        end_date=cleaned_end,
        macd_settings=macd_settings,
        allow_network=allow_network,
        close_cutoff_time=close_hhmm,
        cross_market_anchor=cross_market_anchor,
    )
    if days:
        _apply_v9_macd_anchor(days, v9_tuning)

    now_iso = datetime.now(SHANGHAI_TZ).isoformat(timespec="seconds")
    if not days:
        return {
            "symbol": symbol,
            "evaluated_at": now_iso,
            "criteria": dict(criteria),
            "macd_settings": dict(macd_settings),
            "position_sizing": dict(pos_sizing),
            "requires_flow": False,
            "effective_scope": "all",
            "coverage": {
                "total_candidate_days": candidate_days,
                "evaluated_days": 0,
                "skipped_no_flow_days": 0,
                "first_date": None,
                "last_date": None,
                "skipped_dates_sample": [],
            },
            "capital_sim": {
                "mode": str(pos_sizing.get("mode") or "cash_pool_rolling"),
                "capital_cap": round(cap_limit, 2),
                "ending_equity": round(cap_limit, 2),
                "net_profit_amount": 0.0,
                "net_return_pct": 0.0,
                "max_drawdown_pct": 0.0,
                "max_drawdown_amount": 0.0,
                "sharpe_ratio": None,
                "evaluated_days": 0,
                "traded_days": 0,
                "total_orders": 0,
                "buy_orders": 0,
                "sell_orders": 0,
                "avg_order_shares": 0,
                "min_order_shares": 0,
                "max_order_shares": 0,
                "total_fees_amount": 0.0,
                "ending_shares": 0,
                "same_day_turnaround": True,
                "close_flatten_time": close_hhmm,
                "daily_ledger": [],
            },
            "summary": {
                "total_signals": 0,
                "primary_valid_signals": 0,
                "primary_wins": 0,
                "overall_win_rate_pct": None,
                "buy_signals": 0,
                "buy_valid": 0,
                "buy_wins": 0,
                "buy_win_rate_pct": None,
                "buy_avg_pct": None,
                "buy_median_pct": None,
                "buy_avg_mfe_pct": None,
                "buy_avg_mae_pct": None,
                "sell_signals": 0,
                "sell_valid": 0,
                "sell_wins": 0,
                "sell_win_rate_pct": None,
                "sell_avg_price_change_pct": None,
                "sell_avg_drop_pct": None,
                "sell_median_drop_pct": None,
                "sell_avg_mfe_drop_pct": None,
                "sell_avg_mae_pct": None,
            },
            "horizons": {
                k: {"minutes": int(k[:-1]) if k != "close" else None, "total": 0, "wins": 0, "overall_win_rate_pct": None,
                    "buy": _horizon_stats([], side="buy", field="to_close_pct", min_move_pct=min_move),
                    "sell": _horizon_stats([], side="sell", field="to_close_pct", min_move_pct=min_move)}
                for k in ("1m", "3m", "5m", "10m", "15m", "30m", "close")
            },
            "trade_sim": {
                "paired_trades": 0,
                "paired_win_rate_pct": None,
                "expectancy_pct": None,
                "profit_factor": None,
                "cumulative_return_pct": 0.0,
                "max_consecutive_losses": 0,
            },
            "attribution": {k: [] for k in (
                "by_session", "by_vwap_zone", "by_macd_zero", "by_macd_hist_strength",
                "by_volume_ratio", "by_day_open_gap", "by_brent_spread"
            )},
            "daily_equity_curve": [],
            "signals": [],
            "total_signals_returned": 0,
        }

    cash = cap_limit
    shares = 0
    b00 = days[0]["bars"][0]
    w0, _ = overnight_fn(days[0], b00, 0)
    sh0 = int((cash * w0) // (b00["price"] * _LOT_SIZE * (1.0 + single_fee))) * _LOT_SIZE
    init_fee = 0.0
    init_orders: list[dict[str, Any]] = []
    if sh0 >= _LOT_SIZE:
        amt0 = sh0 * b00["price"]
        init_fee = amt0 * single_fee
        cash -= amt0 + init_fee
        shares += sh0
        init_orders.append(
            {
                "time": b00["time"],
                "side": "buy",
                "price": round(b00["price"], 3),
                "shares": sh0,
                "tier": f"首日三态底仓初始化({w0 * 100:.0f}%)",
            }
        )

    eq_prev = cap_limit
    peak_eq = cap_limit
    max_dd_pct = 0.0
    max_dd_amt = 0.0
    total_fees = init_fee
    buy_orders = 1 if sh0 >= _LOT_SIZE else 0
    sell_orders = 0
    cap_traded_days = 0
    share_sizes: list[int] = [sh0] if sh0 >= _LOT_SIZE else []
    cap_daily_rets: list[float] = []
    cap_daily_ledger: list[dict[str, Any]] = []

    all_signals: list[dict[str, Any]] = []
    paired_returns: list[float] = []
    max_consec_losses = 0
    cur_consec_losses = 0
    cum_paired_ret = 0.0
    daily_equity_curve: list[dict[str, Any]] = []

    base_hold_nav = 1.0
    t0_compound_nav = 1.0
    with_base_nav = 1.0
    combined_50_nav = 1.0
    t0_peak_nav = 1.0
    t0_max_dd = 0.0
    t0_cum_gross = 0.0
    t0_cum_net = 0.0
    t0_buy_net = 0.0
    t0_sell_net = 0.0
    t0_buy_days = 0
    t0_sell_days = 0
    t0_traded_days = 0
    t0_hold_days = 0
    t0_daily_rets: list[float] = []
    t0_traded_rets: list[float] = []
    t0_monthly_map: dict[str, dict[str, Any]] = {}
    t0_daily_curve: list[dict[str, Any]] = []

    for day_idx, day in enumerate(days):
        td = day["td"]
        bars = day["bars"]
        d_start_eq = eq_prev
        day_orders: list[dict[str, Any]] = list(init_orders) if day_idx == 0 else []
        day_buy_sh = sh0 if day_idx == 0 else 0
        day_sell_sh = 0
        day_t0_legs: list[tuple[str, float, float, float]] = []

        pos = 0
        leg_q = 0
        leg_frac = 0.0
        entry_px = 0.0
        entry_sp = 0.0
        entry_prem = 0.0
        entry_idx = -1
        last_exit_idx = -99
        legs_d = 0
        allow_addon = False
        addon_done = False
        rebalanced = False

        for b in bars:
            pr = b["price"]
            t = b["time"]
            eq_now = cash + shares * pr
            peak_eq = max(peak_eq, eq_now)
            dd_amt = max(0.0, peak_eq - eq_now)
            dd_pct = (dd_amt / peak_eq * 100.0) if peak_eq > 0 else 0.0
            max_dd_amt = max(max_dd_amt, dd_amt)
            max_dd_pct = max(max_dd_pct, dd_pct)

            if t >= close_hhmm:
                if pos != 0 and entry_px > 0:
                    f_side = "buy" if pos > 0 else "sell"
                    day_t0_legs.append((f_side, entry_px, pr, leg_frac))
                    g_rt = ((pr - entry_px) / entry_px * 100.0) if pos > 0 else ((entry_px - pr) / entry_px * 100.0)
                    rt_net = g_rt - fee_pct
                    paired_returns.append(rt_net)
                    cum_paired_ret += rt_net
                    if rt_net <= 0:
                        cur_consec_losses += 1
                        max_consec_losses = max(max_consec_losses, cur_consec_losses)
                    else:
                        cur_consec_losses = 0
                    close_sig_title = "V9+ 日内超跌波段做T" if pos == 2 else "V9+ 进取尾盘平T"
                    _append_v9_signal(all_signals, day, bars, entry_idx, f_side, rule_id, close_sig_title, leg_q, leg_frac * 100.0, "尾盘收盘清算", pr, min_move)
                if not rebalanced:
                    wt, wt_lbl = resolve_effective_close_weight(overnight_fn, day, b, pos, entry_prem, v9_tuning=v9_tuning)
                    tgt_sh = int((eq_now * wt) // (pr * _LOT_SIZE * (1.0 + single_fee))) * _LOT_SIZE
                    diff_sh = tgt_sh - shares
                    if diff_sh >= _LOT_SIZE:
                        q = min(diff_sh, int(cash // (pr * _LOT_SIZE * (1.0 + single_fee))) * _LOT_SIZE)
                        if q >= _LOT_SIZE:
                            amt = q * pr
                            fee = amt * single_fee
                            cash -= amt + fee
                            shares += q
                            total_fees += fee
                            buy_orders += 1
                            day_buy_sh += q
                            share_sizes.append(q)
                            day_orders.append({"time": t, "side": "buy", "price": round(pr, 3), "shares": q, "tier": wt_lbl})
                    elif diff_sh <= -_LOT_SIZE:
                        q = min((-diff_sh // _LOT_SIZE) * _LOT_SIZE, shares)
                        if q >= _LOT_SIZE:
                            amt = q * pr
                            fee = amt * single_fee
                            cash += amt - fee
                            shares -= q
                            total_fees += fee
                            sell_orders += 1
                            day_sell_sh += q
                            share_sizes.append(q)
                            day_orders.append({"time": t, "side": "sell", "price": round(pr, 3), "shares": q, "tier": wt_lbl})
                    pos = 0
                    leg_q = 0
                    allow_addon = False
                    addon_done = False
                    rebalanced = True
                continue

            in_win = ("09:32" <= t <= "11:29") or ("13:02" <= t <= "14:30")
            s_sig, raw_b_sig = signals_v8_with_tuning(day, b, sc, v9_tuning)
            cur_wt, _ = overnight_fn(day, b, 0)
            b_buy = _shift_buy_bar_prem(b, v9_tuning)
            b_sig = filter_bear_regime_buy(day, b_buy, raw_b_sig, cur_wt, sc)
            scalp_ok, s_lbl, s_frac = should_enter_intraday_dip_scalp(
                b, sc, day=day, strict_mode=strict_scalp, v9_tuning=v9_tuning, cur_close_wt=cur_wt
            )
            if day.get("cross_market_anchor") == "xop_us_ep":
                s_sig, b_sig, scalp_ok, _ = apply_xop_overnight_intraday_overlays(
                    day,
                    b,
                    s_sig=s_sig,
                    b_sig=b_sig,
                    scalp_ok=scalp_ok,
                    close_wt=cur_wt,
                    sc=sc,
                )
            s_sig_short = False if (s_sig and _suppress_waterfall_short_entry(b, cur_wt, sc)) else s_sig
            if pos == 0 and in_win and (b["i"] - last_exit_idx) >= cooldown and legs_d < max_legs:
                if s_sig_short and shares >= _LOT_SIZE:
                    t_lbl, frac = apply_v9_sell_tier_tuning(day, b, *clean_sell_tier(day, b), v9_tuning)
                    q = min(shares, max(_LOT_SIZE, int((shares * frac) // _LOT_SIZE) * _LOT_SIZE))
                    amt = q * pr
                    fee = amt * single_fee
                    cash += amt - fee
                    shares -= q
                    total_fees += fee
                    sell_orders += 1
                    day_sell_sh += q
                    share_sizes.append(q)
                    pos, leg_q, leg_frac, entry_px, entry_sp, entry_prem, entry_idx = (
                        -1, q, frac, pr, b["sp_fx"], float(b["prem"]), b["i"]
                    )
                    allow_addon = ("分批高抛" in t_lbl)
                    addon_done = False
                    legs_d += 1
                    day_orders.append({"time": t, "side": "sell", "price": round(pr, 3), "shares": q, "tier": f"反T高抛 · {t_lbl}"})
                elif b_sig and cash >= pr * _LOT_SIZE * (1.0 + single_fee):
                    t_lbl, frac = clean_buy_tier(day, b_buy)
                    q = max(_LOT_SIZE, int((cash * frac) // (pr * _LOT_SIZE * (1.0 + single_fee))) * _LOT_SIZE)
                    amt = q * pr
                    fee = amt * single_fee
                    if cash >= amt + fee:
                        cash -= amt + fee
                        shares += q
                        total_fees += fee
                        buy_orders += 1
                        day_buy_sh += q
                        share_sizes.append(q)
                        pos, leg_q, leg_frac, entry_px, entry_sp, entry_prem, entry_idx = (
                            1, q, frac, pr, b["sp_fx"], float(b["prem"]), b["i"]
                        )
                        legs_d += 1
                        day_orders.append({"time": t, "side": "buy", "price": round(pr, 3), "shares": q, "tier": f"正T低吸 · {t_lbl}"})
                elif scalp_ok and cash >= pr * _LOT_SIZE * (1.0 + single_fee):
                    q = max(_LOT_SIZE, int((cash * s_frac) // (pr * _LOT_SIZE * (1.0 + single_fee))) * _LOT_SIZE)
                    amt = q * pr
                    fee = amt * single_fee
                    if cash >= amt + fee:
                        cash -= amt + fee
                        shares += q
                        total_fees += fee
                        buy_orders += 1
                        day_buy_sh += q
                        share_sizes.append(q)
                        pos, leg_q, leg_frac, entry_px, entry_sp, entry_prem, entry_idx = (
                            2, q, s_frac, pr, b["sp_fx"], float(b["prem"]), b["i"]
                        )
                        legs_d += 1
                        day_orders.append({"time": t, "side": "buy", "price": round(pr, 3), "shares": q, "tier": f"日内超跌做T · {s_lbl}"})
            elif pos == -1:
                if (
                    allow_addon
                    and (not addon_done)
                    and in_win
                    and shares >= _LOT_SIZE
                    and should_add_short_tranche(b, entry_px=entry_px, entry_sp=entry_sp, s_sig=s_sig_short)
                ):
                    q_add = shares
                    amt = q_add * pr
                    fee = amt * single_fee
                    cash += amt - fee
                    shares -= q_add
                    total_fees += fee
                    sell_orders += 1
                    day_sell_sh += q_add
                    share_sizes.append(q_add)
                    tot_q = leg_q + q_add
                    if tot_q > 0:
                        entry_px = (entry_px * leg_q + pr * q_add) / tot_q
                        entry_sp = (entry_sp * leg_q + float(b["sp_fx"]) * q_add) / tot_q
                        entry_prem = (entry_prem * leg_q + float(b["prem"]) * q_add) / tot_q
                    leg_q = tot_q
                    leg_frac = 1.0
                    allow_addon = False
                    addon_done = True
                    day_orders.append({"time": t, "side": "sell", "price": round(pr, 3), "shares": q_add, "tier": "反T二段补抛 · 余仓兑现"})
                elif should_exit_short(b, entry_sp, True, close_wt=cur_wt, entry_px=entry_px):
                    q = min(leg_q, int(cash // (pr * _LOT_SIZE * (1.0 + single_fee))) * _LOT_SIZE)
                    if q >= _LOT_SIZE:
                        amt = q * pr
                        fee = amt * single_fee
                        cash -= amt + fee
                        shares += q
                        total_fees += fee
                        buy_orders += 1
                        day_buy_sh += q
                        share_sizes.append(q)
                        day_orders.append({"time": t, "side": "buy", "price": round(pr, 3), "shares": q, "tier": "反T止盈回补"})
                    day_t0_legs.append(("sell", entry_px, pr, leg_frac))
                    g_rt = (entry_px - pr) / entry_px * 100.0
                    rt_net = g_rt - fee_pct
                    paired_returns.append(rt_net)
                    cum_paired_ret += rt_net
                    if rt_net <= 0:
                        cur_consec_losses += 1
                        max_consec_losses = max(max_consec_losses, cur_consec_losses)
                    else:
                        cur_consec_losses = 0
                    _append_v9_signal(all_signals, day, bars, entry_idx, "sell", rule_id, "V9+ 进取反T高抛", q or leg_q, leg_frac * 100.0, day_orders[-1]["tier"], pr, min_move)
                    pos, leg_q, last_exit_idx, allow_addon, addon_done = 0, 0, b["i"], False, False
            elif pos == 1:
                exit_1, exit_1_lbl = should_exit_long_v9(
                    day,
                    b_buy,
                    entry_px=entry_px,
                    entry_sp=entry_sp,
                    s_sig=s_sig,
                    close_wt=cur_wt,
                    adaptive_exits=True,
                )
                if exit_1:
                    q = min(leg_q, shares)
                    if q >= _LOT_SIZE:
                        amt = q * pr
                        fee = amt * single_fee
                        cash += amt - fee
                        shares -= q
                        total_fees += fee
                        sell_orders += 1
                        day_sell_sh += q
                        share_sizes.append(q)
                        day_orders.append({"time": t, "side": "sell", "price": round(pr, 3), "shares": q, "tier": exit_1_lbl})
                    day_t0_legs.append(("buy", entry_px, pr, leg_frac))
                    g_rt = (pr - entry_px) / entry_px * 100.0
                    rt_net = g_rt - fee_pct
                    paired_returns.append(rt_net)
                    cum_paired_ret += rt_net
                    if rt_net <= 0:
                        cur_consec_losses += 1
                        max_consec_losses = max(max_consec_losses, cur_consec_losses)
                    else:
                        cur_consec_losses = 0
                    _append_v9_signal(all_signals, day, bars, entry_idx, "buy", rule_id, "V9+ 进取正T低吸", q or leg_q, leg_frac * 100.0, day_orders[-1]["tier"], pr, min_move)
                    pos, leg_q, last_exit_idx = 0, 0, b["i"]
            elif pos == 2:
                exit_2, exit_2_lbl = should_exit_scalp_long(
                    b,
                    entry_px=entry_px,
                    entry_sp=entry_sp,
                    s_sig=s_sig,
                )
                if exit_2:
                    q = min(leg_q, shares)
                    if q >= _LOT_SIZE:
                        amt = q * pr
                        fee = amt * single_fee
                        cash += amt - fee
                        shares -= q
                        total_fees += fee
                        sell_orders += 1
                        day_sell_sh += q
                        share_sizes.append(q)
                        day_orders.append({"time": t, "side": "sell", "price": round(pr, 3), "shares": q, "tier": exit_2_lbl})
                    day_t0_legs.append(("buy", entry_px, pr, leg_frac))
                    g_rt = (pr - entry_px) / entry_px * 100.0
                    rt_net = g_rt - fee_pct
                    paired_returns.append(rt_net)
                    cum_paired_ret += rt_net
                    if rt_net <= 0:
                        cur_consec_losses += 1
                        max_consec_losses = max(max_consec_losses, cur_consec_losses)
                    else:
                        cur_consec_losses = 0
                    _append_v9_signal(all_signals, day, bars, entry_idx, "buy", rule_id, "V9+ 日内超跌波段做T", q or leg_q, leg_frac * 100.0, day_orders[-1]["tier"], pr, min_move)
                    pos, leg_q, last_exit_idx = 0, 0, b["i"]

        if day_orders:
            cap_traded_days += 1

        eq_end = cash + shares * day["p1500"]
        day_pnl = eq_end - d_start_eq
        day_cap_ret = (day_pnl / d_start_eq * 100.0) if d_start_eq > 0 else 0.0
        cap_daily_rets.append(day_cap_ret)
        eq_prev = eq_end
        cap_daily_ledger.append(
            {
                "trade_date": td,
                "buy_shares": day_buy_sh,
                "sell_shares": day_sell_sh,
                "ending_shares": shares,
                "day_pnl_amount": round(day_pnl, 2),
                "cum_pnl_amount": round(eq_end - cap_limit, 2),
                "equity": round(eq_end, 2),
                "orders": day_orders,
            }
        )

        # Accumulate base_position_t0 & monthly table
        base_day_ret = (day["p1500"] - day["pre"]) / day["pre"] * 100.0 if day["pre"] > 0 else 0.0
        base_hold_nav *= 1.0 + base_day_ret / 100.0
        day_gross_t0 = 0.0
        day_net_t0 = 0.0
        day_buy_t0 = 0.0
        day_sell_t0 = 0.0
        for s_side, ep_v, ex_v, w_frac in day_t0_legs:
            g_v = ((ex_v - ep_v) / ep_v * 100.0) if s_side == "buy" else ((ep_v - ex_v) / ep_v * 100.0)
            n_v = (g_v - fee_pct) * w_frac
            day_gross_t0 += g_v * w_frac
            day_net_t0 += n_v
            if s_side == "buy":
                day_buy_t0 += n_v
            else:
                day_sell_t0 += n_v

        if day_t0_legs:
            t0_traded_days += 1
            t0_traded_rets.append(day_net_t0)
            if any(x[0] == "buy" for x in day_t0_legs):
                t0_buy_days += 1
                t0_buy_net += day_buy_t0
            if any(x[0] == "sell" for x in day_t0_legs):
                t0_sell_days += 1
                t0_sell_net += day_sell_t0
        else:
            t0_hold_days += 1

        t0_cum_gross += day_gross_t0
        t0_cum_net += day_net_t0
        t0_compound_nav *= 1.0 + day_cap_ret / 100.0
        with_base_nav *= 1.0 + (base_day_ret + day_net_t0) / 100.0
        combined_50_nav *= 1.0 + (0.5 * base_day_ret + 0.5 * day_net_t0) / 100.0
        t0_peak_nav = max(t0_peak_nav, t0_compound_nav)
        t0_max_dd = max(t0_max_dd, (t0_peak_nav - t0_compound_nav) / t0_peak_nav * 100.0 if t0_peak_nav > 0 else 0.0)
        t0_daily_rets.append(day_cap_ret)

        m_blk = t0_monthly_map.setdefault(
            day["ym"],
            {
                "month": day["ym"],
                "days": 0,
                "traded_days": 0,
                "buy_t_days": 0,
                "sell_t_days": 0,
                "gross_return_pct": 0.0,
                "net_return_pct": 0.0,
                "nav_factor": 1.0,
                "base_nav": 1.0,
            },
        )
        m_blk["days"] += 1
        if day_t0_legs or day_orders:
            m_blk["traded_days"] += 1
        if any(x[0] == "buy" for x in day_t0_legs):
            m_blk["buy_t_days"] += 1
        if any(x[0] == "sell" for x in day_t0_legs):
            m_blk["sell_t_days"] += 1
        m_blk["gross_return_pct"] += day_gross_t0
        m_blk["net_return_pct"] += day_cap_ret
        m_blk["nav_factor"] *= 1.0 + day_cap_ret / 100.0
        m_blk["base_nav"] *= 1.0 + base_day_ret / 100.0

        t0_daily_curve.append(
            {
                "trade_date": td,
                "side": "both" if any(x[0] == "buy" for x in day_t0_legs) and any(x[0] == "sell" for x in day_t0_legs) else (day_t0_legs[0][0] if day_t0_legs else None),
                "entry_time": day_orders[0]["time"] if day_orders else None,
                "entry_price": day_orders[0]["price"] if day_orders else None,
                "close_price": round(day["p1500"], 3),
                "brn_overnight_pct": None,
                "brn_spread_pct": round(bars[0]["sp_fx"], 4),
                "gross_ret_pct": round(day_gross_t0, 4),
                "net_ret_pct": round(day_cap_ret, 4),
                "cum_net_ret_pct": round((eq_end / cap_limit - 1.0) * 100.0, 4),
                "base_day_ret_pct": round(base_day_ret, 4),
                "base_cum_pct": round((base_hold_nav - 1.0) * 100.0, 4),
            }
        )
        daily_equity_curve.append(
            {
                "trade_date": td,
                "signal_count": len(day_t0_legs),
                "buy_count": sum(1 for x in day_t0_legs if x[0] == "buy"),
                "sell_count": sum(1 for x in day_t0_legs if x[0] == "sell"),
                "return_pct": round(day_cap_ret, 4),
                "cumulative_return_pct": round((eq_end / cap_limit - 1.0) * 100.0, 4),
            }
        )

    buy_signals = [s for s in all_signals if s["side"] == "buy"]
    sell_signals = [s for s in all_signals if s["side"] == "sell"]
    horizons_summary: dict[str, Any] = {}
    for h in (1, 3, 5, 10, 15, 30):
        field = f"ret_{h}m_pct"
        bs = _horizon_stats(buy_signals, side="buy", field=field, min_move_pct=min_move)
        ss = _horizon_stats(sell_signals, side="sell", field=field, min_move_pct=min_move)
        tot_n = bs["total"] + ss["total"]
        tot_w = bs["wins"] + ss["wins"]
        horizons_summary[f"{h}m"] = {
            "minutes": h,
            "total": tot_n,
            "wins": tot_w,
            "overall_win_rate_pct": round(tot_w / tot_n * 100.0, 2) if tot_n else None,
            "buy": bs,
            "sell": ss,
        }
    b_cls = _horizon_stats(buy_signals, side="buy", field="to_close_pct", min_move_pct=min_move)
    s_cls = _horizon_stats(sell_signals, side="sell", field="to_close_pct", min_move_pct=min_move)
    c_n = b_cls["total"] + s_cls["total"]
    c_w = b_cls["wins"] + s_cls["wins"]
    horizons_summary["close"] = {
        "minutes": None,
        "total": c_n,
        "wins": c_w,
        "overall_win_rate_pct": round(c_w / c_n * 100.0, 2) if c_n else None,
        "buy": b_cls,
        "sell": s_cls,
    }
    prim_block = horizons_summary.get(f"{primary_h}m", horizons_summary["5m"])

    pos_trips = [r for r in paired_returns if r > 0]
    neg_trips = [abs(r) for r in paired_returns if r < 0]
    pf_val = round(sum(pos_trips) / sum(neg_trips), 3) if neg_trips and sum(neg_trips) > 0 else (999.0 if pos_trips else None)

    attribution = _build_v9_attribution(all_signals, f"ret_{primary_h}m_pct", min_move)

    cap_net_profit = round(eq_prev - cap_limit, 2)
    cap_net_ret = round((cap_net_profit / cap_limit * 100.0) if cap_limit > 0 else 0.0, 2)
    cap_sharpe: float | None = None
    if len(cap_daily_rets) >= 5:
        c_std = statistics.pstdev(cap_daily_rets)
        if c_std > 1e-9:
            cap_sharpe = round((statistics.mean(cap_daily_rets) / c_std) * math.sqrt(252.0), 3)

    base_hold_pct = (base_hold_nav - 1.0) * 100.0
    pos_t0 = [r for r in t0_daily_rets if r > 0]
    neg_t0 = [abs(r) for r in t0_daily_rets if r < 0]
    t0_pf = round(sum(pos_t0) / sum(neg_t0), 3) if neg_t0 and sum(neg_t0) > 0 else (999.0 if pos_t0 else None)
    eval_n = len(days)

    monthly_returns = [
        {
            "month": item["month"],
            "days": item["days"],
            "traded_days": item["traded_days"],
            "buy_t_days": item["buy_t_days"],
            "sell_t_days": item["sell_t_days"],
            "gross_return_pct": round(item["gross_return_pct"], 4),
            "net_return_pct": round((item["nav_factor"] - 1.0) * 100.0, 4),
            "base_hold_pct": round((item["base_nav"] - 1.0) * 100.0, 4),
        }
        for item in sorted(t0_monthly_map.values(), key=lambda x: x["month"])
    ]

    capped_signals = list(reversed(all_signals))[: max(1, min(5000, int(signal_limit)))]
    buy_mfe = [float(s["mfe_primary_pct"]) for s in buy_signals if s.get("mfe_primary_pct") is not None]
    buy_mae = [float(s["mae_primary_pct"]) for s in buy_signals if s.get("mae_primary_pct") is not None]
    sell_mfe = [float(s["mfe_primary_pct"]) for s in sell_signals if s.get("mfe_primary_pct") is not None]
    sell_mae = [float(s["mae_primary_pct"]) for s in sell_signals if s.get("mae_primary_pct") is not None]

    return {
        "symbol": symbol,
        "evaluated_at": now_iso,
        "criteria": dict(criteria),
        "macd_settings": dict(macd_settings),
        "position_sizing": dict(pos_sizing),
        "requires_flow": False,
        "effective_scope": "all",
        "coverage": {
            "total_candidate_days": candidate_days,
            "evaluated_days": eval_n,
            "skipped_no_flow_days": 0,
            "first_date": days[0]["td"],
            "last_date": days[-1]["td"],
            "skipped_dates_sample": [],
        },
        "base_position_t0": {
            "start_date": days[0]["td"],
            "end_date": days[-1]["td"],
            "morning_window": "09:32-14:30",
            "max_daily_trades": max_legs,
            "round_trip_fee_pct": fee_pct,
            "evaluated_days": eval_n,
            "traded_days": t0_traded_days,
            "buy_t_days": t0_buy_days,
            "sell_t_days": t0_sell_days,
            "hold_days": t0_hold_days,
            "daily_win_rate_pct": round(len(pos_trips) / len(paired_returns) * 100.0, 2) if paired_returns else None,
            "avg_trade_net_pct": round(statistics.mean(paired_returns), 4) if paired_returns else None,
            "gross_cum_return_pct": round(t0_cum_gross, 4),
            "net_cum_return_pct": round(cap_net_ret, 4),
            "compound_net_return_pct": round(cap_net_ret, 4),
            "buy_t_net_return_pct": round(t0_buy_net, 4),
            "sell_t_net_return_pct": round(t0_sell_net, 4),
            "base_hold_return_pct": round(base_hold_pct, 4),
            "with_base_total_return_pct": round(cap_net_ret, 4),
            "combined_50_50_return_pct": round((combined_50_nav - 1.0) * 100.0, 4),
            "alpha_vs_hold_pct": round(cap_net_ret - base_hold_pct, 4),
            "annualized_net_return_pct": round(((eq_prev / cap_limit) ** (252.0 / eval_n) - 1.0) * 100.0, 2) if eval_n > 0 and eq_prev > 0 else None,
            "max_drawdown_pct": round(max_dd_pct, 4),
            "sharpe_ratio": cap_sharpe,
            "profit_factor": t0_pf,
            "monthly_returns": monthly_returns,
            "daily_curve": t0_daily_curve[-240:],
        },
        "capital_sim": {
            "mode": str(pos_sizing.get("mode") or "cash_pool_rolling"),
            "capital_cap": round(cap_limit, 2),
            "ending_equity": round(eq_prev, 2),
            "net_profit_amount": cap_net_profit,
            "net_return_pct": cap_net_ret,
            "max_drawdown_pct": round(max_dd_pct, 2),
            "max_drawdown_amount": round(max_dd_amt, 2),
            "sharpe_ratio": cap_sharpe,
            "evaluated_days": eval_n,
            "traded_days": cap_traded_days,
            "total_orders": buy_orders + sell_orders,
            "buy_orders": buy_orders,
            "sell_orders": sell_orders,
            "avg_order_shares": round(statistics.mean(share_sizes)) if share_sizes else 0,
            "min_order_shares": min(share_sizes) if share_sizes else 0,
            "max_order_shares": max(share_sizes) if share_sizes else 0,
            "total_fees_amount": round(total_fees, 2),
            "ending_shares": shares,
            "same_day_turnaround": True,
            "close_flatten_time": close_hhmm,
            "daily_ledger": cap_daily_ledger[-120:],
        },
        "summary": {
            "total_signals": len(all_signals),
            "primary_valid_signals": prim_block["total"],
            "primary_wins": prim_block["wins"],
            "overall_win_rate_pct": prim_block["overall_win_rate_pct"],
            "buy_signals": len(buy_signals),
            "buy_valid": prim_block["buy"]["total"],
            "buy_wins": prim_block["buy"]["wins"],
            "buy_win_rate_pct": prim_block["buy"]["win_rate_pct"],
            "buy_avg_pct": prim_block["buy"]["avg_price_change_pct"],
            "buy_median_pct": prim_block["buy"]["median_price_change_pct"],
            "buy_avg_mfe_pct": round(statistics.mean(buy_mfe), 4) if buy_mfe else None,
            "buy_avg_mae_pct": round(statistics.mean(buy_mae), 4) if buy_mae else None,
            "sell_signals": len(sell_signals),
            "sell_valid": prim_block["sell"]["total"],
            "sell_wins": prim_block["sell"]["wins"],
            "sell_win_rate_pct": prim_block["sell"]["win_rate_pct"],
            "sell_avg_price_change_pct": prim_block["sell"]["avg_price_change_pct"],
            "sell_avg_drop_pct": prim_block["sell"]["avg_favorable_pct"],
            "sell_median_drop_pct": prim_block["sell"]["median_favorable_pct"],
            "sell_avg_mfe_drop_pct": round(statistics.mean(sell_mfe), 4) if sell_mfe else None,
            "sell_avg_mae_pct": round(statistics.mean(sell_mae), 4) if sell_mae else None,
        },
        "horizons": horizons_summary,
        "trade_sim": {
            "paired_trades": len(paired_returns),
            "paired_win_rate_pct": round(len(pos_trips) / len(paired_returns) * 100.0, 2) if paired_returns else None,
            "expectancy_pct": round(statistics.mean(paired_returns), 4) if paired_returns else None,
            "profit_factor": pf_val,
            "cumulative_return_pct": round(cum_paired_ret, 4),
            "max_consecutive_losses": max_consec_losses,
        },
        "attribution": attribution,
        "daily_equity_curve": daily_equity_curve[-180:],
        "signals": capped_signals,
        "total_signals_returned": len(capped_signals),
    }


def _append_v9_signal(
    out: list[dict[str, Any]],
    day: Mapping[str, Any],
    bars: Sequence[Mapping[str, Any]],
    idx: int,
    side: str,
    rule_id: str,
    rule_title: str,
    shares: int,
    weight_pct: float,
    tier_lbl: str,
    exit_px: float,
    min_move: float,
) -> None:
    if idx < 0 or idx >= len(bars):
        return
    b = bars[idx]
    ep = float(b["price"])
    rets = {}
    for h in (1, 3, 5, 10, 15, 30):
        target_b = bars[idx + h] if (idx + h) < len(bars) else None
        rets[f"ret_{h}m_pct"] = round((float(target_b["price"]) - ep) / ep * 100.0, 4) if target_b else None
    to_cls = round((float(day["p1500"]) - ep) / ep * 100.0, 4) if ep > 0 else None
    rt_ret = round(((exit_px - ep) / ep * 100.0) if side == "buy" else ((ep - exit_px) / ep * 100.0), 4)
    prim = rets["ret_5m_pct"]
    fav = None if prim is None else (prim if side == "buy" else round(-prim, 4))
    succ = None if fav is None else ((fav > 0) if min_move <= 0 else (fav >= min_move))
    avg_p = b.get("average_price")
    vwap_dev = round((ep - float(avg_p)) / float(avg_p) * 100.0, 4) if avg_p and float(avg_p) > 0 else None
    out.append(
        {
            "trade_date": day["td"],
            "time": str(b["time"]),
            "side": side,
            "rule_id": rule_id,
            "rule_title": rule_title,
            "price": ep,
            "average_price": round(float(avg_p), 4) if avg_p is not None else None,
            "vwap_dev_pct": vwap_dev,
            "diff": round(float(b["diff10"]), 6),
            "dea": round(float(b["dea10"]), 6),
            "macd": round(float(b["macd10"]), 6),
            "volume_ratio": None,
            "flow_value": None,
            "open_gap_pct": round((float(bars[0]["price"]) - float(day["pre"])) / float(day["pre"]) * 100.0, 4) if day["pre"] > 0 else None,
            "brn_price": round(float(b["brn"]), 3),
            "brn_overnight_pct": None,
            "brn_from_prev_close_pct": round(float(b["brn_chg"]), 4),
            "brn_spread_pct": round(float(b["sp_fx"]), 4),
            "planned_shares": shares,
            "planned_amount": round(shares * ep, 2),
            "weight_pct": round(weight_pct, 2),
            "sizing_tier": tier_lbl,
            **rets,
            "to_close_pct": to_cls,
            "t0_close_net_pct": rt_ret,
            "favorable_primary_pct": fav,
            "mfe_primary_pct": fav,
            "mae_primary_pct": fav,
            "success": succ,
        }
    )


def _build_v9_attribution(all_signals: Sequence[Mapping[str, Any]], prim_field: str, min_move: float) -> dict[str, list[dict[str, Any]]]:
    def _brent_bkt(sp: Any) -> str:
        if sp is None:
            return "无布伦特基线"
        v = float(sp)
        if v <= -0.35:
            return "汇调折价滞涨 (sp_fx≤-0.35%)"
        if v >= 0.35:
            return "汇调溢价超涨 (sp_fx≥+0.35%)"
        return "跨市场同步 (-0.35%~+0.35%)"

    def _vwap_bkt(v: Any) -> str:
        if v is None:
            return "无均价基线"
        f = float(v)
        if f <= -0.10:
            return "均线下方 (≤-0.10%)"
        if f >= 0.10:
            return "均线上方 (≥+0.10%)"
        return "贴近均线 (-0.10%~+0.10%)"

    dims: dict[str, Callable[[Mapping[str, Any]], str]] = {
        "by_session": lambda s: _v9_bucket_session(str(s["time"])),
        "by_vwap_zone": lambda s: _vwap_bkt(s.get("vwap_dev_pct")),
        "by_macd_zero": lambda s: "未知" if s.get("diff") is None else ("零轴下方 (DIFF≤0)" if float(s["diff"]) <= 0 else "零轴上方 (DIFF>0)"),
        "by_macd_hist_strength": lambda s: "强柱突破" if s.get("macd") is not None and abs(float(s["macd"])) >= 0.0015 else "常规区间",
        "by_volume_ratio": lambda _s: "全量分钟样本",
        "by_day_open_gap": lambda s: "平开" if s.get("open_gap_pct") is None or -0.5 <= float(s["open_gap_pct"]) <= 0.5 else ("高开 (>+0.5%)" if float(s["open_gap_pct"]) > 0.5 else "低开 (<-0.5%)"),
        "by_brent_spread": lambda s: _brent_bkt(s.get("brn_spread_pct")),
    }
    out: dict[str, list[dict[str, Any]]] = {}
    for d_key, fn in dims.items():
        buckets: dict[str, list[Mapping[str, Any]]] = {}
        for sig in all_signals:
            buckets.setdefault(fn(sig), []).append(sig)
        rows: list[dict[str, Any]] = []
        for b_name, items in buckets.items():
            b_buy = [x for x in items if x["side"] == "buy"]
            b_sell = [x for x in items if x["side"] == "sell"]
            bs = _horizon_stats(b_buy, side="buy", field=prim_field, min_move_pct=min_move)
            ss = _horizon_stats(b_sell, side="sell", field=prim_field, min_move_pct=min_move)
            t0_vals = [float(x["t0_close_net_pct"]) for x in items if x.get("t0_close_net_pct") is not None]
            tot = bs["total"] + ss["total"]
            wins = bs["wins"] + ss["wins"]
            rows.append(
                {
                    "bucket": b_name,
                    "total": tot,
                    "wins": wins,
                    "overall_win_rate_pct": round(wins / tot * 100.0, 2) if tot else None,
                    "avg_to_close_t0_pct": round(statistics.mean(t0_vals), 4) if t0_vals else None,
                    "buy_total": bs["total"],
                    "buy_wins": bs["wins"],
                    "buy_win_rate_pct": bs["win_rate_pct"],
                    "buy_avg_pct": bs["avg_favorable_pct"],
                    "sell_total": ss["total"],
                    "sell_wins": ss["wins"],
                    "sell_win_rate_pct": ss["win_rate_pct"],
                    "sell_avg_drop_pct": ss["avg_favorable_pct"],
                }
            )
        out[d_key] = rows
    return out


def _resolve_live_avail_cash(
    cap_limit: float,
    rolling_shares: int,
    cur_px: float,
    broker_available_cash: float | None,
) -> float:
    """Intersect remaining strategy cap with fee-buffered live broker available cash."""
    strategy_rem_cash = max(0.0, float(cap_limit) - max(0, int(rolling_shares)) * float(cur_px))
    if broker_available_cash is not None:
        fee_safe_broker_cash = max(0.0, float(broker_available_cash) * 0.9995)
        return min(strategy_rem_cash, fee_safe_broker_cash)
    return strategy_rem_cash


def _prepare_v9_plus_live_state(
    store: Any,
    *,
    symbol: str,
    trade_date: str,
    cap_limit: float,
    rule_spec: Mapping[str, Any],
    pos_sizing: Mapping[str, Any],
    today_signals: Sequence[Mapping[str, Any]],
    rolling_shares: int,
    allow_network: bool | None = None,
    broker_available_cash: float | None = None,
    expected_ledger_shares: int | None = None,
) -> dict[str, Any] | None:
    macro = V9MacroDataStore(store, allow_network=allow_network)
    cross_market_anchor = _resolve_cross_market_anchor(pos_sizing)
    live_xop_spot = macro.fetch_live_xop_spot() if cross_market_anchor == "xop_us_ep" else None
    if live_xop_spot is not None:
        live_spot: tuple[float | None, float | None] = (
            live_xop_spot.get("brn_px"),
            live_xop_spot.get("fx_px"),
        )
    else:
        live_spot = macro.fetch_live_spot()
    close_hhmm = str(pos_sizing.get("close_flatten_time") or "14:53")
    macd_settings = rule_spec.get("macd_settings") or {"short": 10, "long": 20, "signal": 5}
    days, _ = build_v9_plus_days(
        store,
        symbol=symbol,
        start_date=trade_date,
        end_date=trade_date,
        macd_settings=macd_settings,
        allow_network=allow_network,
        live_spot=live_spot,
        close_cutoff_time=close_hhmm,
        cross_market_anchor=cross_market_anchor,
        live_xop_spot=live_xop_spot,
    )
    if not days:
        return None
    v9_tuning = dict(pos_sizing.get("v9_tuning") or {})
    _apply_v9_macd_anchor(days, v9_tuning)
    day = days[-1]
    bars = day["bars"]
    if not bars:
        return None

    engine_profile = str(pos_sizing.get("engine_profile") or "v9_plus_aggressive")
    actual_anchor = str(day.get("cross_market_anchor") or "brent")
    strict_scalp = bool(v9_tuning.get("scalp_strict_mode"))
    overnight_fn = apply_v9_close_weight_tuning(
        resolve_close_weight_fn(engine_profile, actual_anchor), v9_tuning
    )
    sc = float(pos_sizing.get("scale_factor") or 1.0)
    cooldown = max(1, int(pos_sizing.get("cooldown_minutes") or 4))
    max_legs = max(1, int(pos_sizing.get("max_daily_entries") or 8))
    min_hold = max(0, int(pos_sizing.get("min_retained_shares", _LOT_SIZE)))
    rule_id = str(rule_spec.get("id") or "ai-160723-v9-plus-aggressive")
    rule_name = str(rule_spec.get("name") or "V9+ 进取版")

    # Reconstruct live intraday state machine from today's persisted ledger signals
    open_leg: dict[str, Any] | None = None
    last_exit_minute: str | None = None
    entries_count = 0
    close_rebal_done = False
    for sig in today_signals:
        skey = str(sig.get("signal_key") or "")
        s_side = str(sig.get("side") or "")
        s_qty = int(sig.get("quantity") or 0)
        s_min = str(sig.get("minute") or "")
        s_px = float(sig.get("signal_price") or 0.0)
        if ":v9-entry:" in skey or ":v9-scalp:" in skey:
            entries_count += 1
            open_leg = {
                "side": s_side,
                "qty": s_qty,
                "minute": s_min,
                "last_leg_minute": s_min,
                "mode": "scalp" if ":v9-scalp:" in skey else "entry",
                "signal_price": s_px,
                "allow_addon": bool(s_side == "sell" and "分批高抛" in str(sig.get("rule_title") or "")),
                "addon_done": False,
                "tranches": [{"minute": s_min, "qty": s_qty, "signal_price": s_px}],
            }
        elif ":v9-addon:" in skey and open_leg is not None and open_leg["side"] == s_side:
            prev_q = max(0, int(open_leg["qty"]))
            add_q = max(0, s_qty)
            tot_q = prev_q + add_q
            if tot_q > 0 and open_leg["signal_price"] > 0 and s_px > 0:
                open_leg["signal_price"] = (open_leg["signal_price"] * prev_q + s_px * add_q) / tot_q
            elif s_px > 0:
                open_leg["signal_price"] = s_px
            open_leg["qty"] = tot_q
            open_leg["allow_addon"] = False
            open_leg["addon_done"] = True
            open_leg["last_leg_minute"] = s_min
            open_leg.setdefault("tranches", []).append({"minute": s_min, "qty": add_q, "signal_price": s_px})
        elif ":v9-exit:" in skey:
            open_leg = None
            last_exit_minute = s_min
        elif ":v9-close:" in skey:
            close_rebal_done = True

    if open_leg is not None and expected_ledger_shares is not None:
        ext_delta = int(rolling_shares) - int(expected_ledger_shares)
        if open_leg["side"] == "sell" and ext_delta > 0:
            rem_qty = (max(0, int(open_leg["qty"]) - ext_delta) // _LOT_SIZE) * _LOT_SIZE
            if rem_qty < _LOT_SIZE:
                open_leg = None
            else:
                open_leg["qty"] = rem_qty
        elif open_leg["side"] == "buy":
            if int(rolling_shares) <= max(_LOT_SIZE - 1, min_hold):
                open_leg = None
            elif ext_delta < 0:
                rem_qty = (max(0, int(open_leg["qty"]) + ext_delta) // _LOT_SIZE) * _LOT_SIZE
                if rem_qty < _LOT_SIZE:
                    open_leg = None
                else:
                    open_leg["qty"] = rem_qty

    last_exit_idx = -99
    if last_exit_minute:
        for b in bars:
            if b["time"] <= last_exit_minute:
                last_exit_idx = b["i"]

    entry_sp_fx = 0.0
    entry_px = 0.0
    entry_prem = 0.0
    if open_leg is not None:
        tranches = open_leg.get("tranches") or [
            {"minute": open_leg["minute"], "qty": open_leg["qty"], "signal_price": open_leg["signal_price"]}
        ]
        tot_w = 0
        w_sp = 0.0
        w_pm = 0.0
        w_px = 0.0
        for tr in tranches:
            tq = max(0, int(tr.get("qty") or 0))
            if tq <= 0:
                continue
            tr_b = next((b for b in reversed(bars) if b["time"] <= str(tr.get("minute") or "")), bars[-1])
            tr_px = float(tr.get("signal_price") or 0.0) or float(tr_b["price"])
            tot_w += tq
            w_sp += float(tr_b["sp_fx"]) * tq
            w_pm += float(tr_b["prem"]) * tq
            w_px += tr_px * tq
        if tot_w > 0:
            entry_sp_fx = w_sp / tot_w
            entry_prem = w_pm / tot_w
            entry_px = w_px / tot_w
        else:
            ref_b = next((b for b in reversed(bars) if b["time"] <= open_leg["minute"]), bars[-1])
            entry_sp_fx = float(ref_b["sp_fx"])
            entry_px = open_leg["signal_price"] if open_leg["signal_price"] > 0 else float(ref_b["price"])
            entry_prem = float(ref_b["prem"])

    return {
        "day": day,
        "bars": bars,
        "close_hhmm": close_hhmm,
        "engine_profile": engine_profile,
        "overnight_fn": overnight_fn,
        "sc": sc,
        "cooldown": cooldown,
        "max_legs": max_legs,
        "min_hold": min_hold,
        "rule_id": rule_id,
        "rule_name": rule_name,
        "open_leg": open_leg,
        "last_exit_idx": last_exit_idx,
        "entries_count": entries_count,
        "close_rebal_done": close_rebal_done,
        "entry_sp_fx": entry_sp_fx,
        "entry_px": entry_px,
        "entry_prem": entry_prem,
        "v9_tuning": v9_tuning,
        "cross_market_anchor": actual_anchor,
        "broker_available_cash": float(broker_available_cash) if broker_available_cash is not None else None,
    }


def _describe_v9_sell_entry(
    b: Mapping[str, Any],
    sc: float,
    frac: float,
    min_hold: int,
    day: Mapping[str, Any] | None = None,
) -> tuple[str, str]:
    is_xop = bool(day and day.get("cross_market_anchor") == "xop_us_ep")
    bench_lbl = "美股XOP油气股与日内原油合成估值" if is_xop else "国际原油及汇率理论涨幅"
    bench_short = "美股XOP合成基准" if is_xop else "原油"
    retain_txt = f"，已强制保留{min_hold}股底仓" if min_hold > 0 else ""
    meaning = f"先卖后买做反T，趁基金短线跑赢{bench_short}先高抛{frac * 100:.0f}%仓位，等回落再低价接回赚差价{retain_txt}"
    sp = float(b["sp_fx"])
    coint10 = float(b["coint10"])
    pm = float(b["prem"])
    if is_xop and day is not None:
        xop_ov = float(day.get("xop_ov") or 0.0)
        spy_ov = float(day.get("spy_ov") or 0.0)
        if (xop_ov <= -1.40 or (spy_ov <= -1.00 and xop_ov <= -0.60)) and pm >= 0.35 and bool(b["dc10"]):
            why = (
                f"昨晚美股XOP({xop_ov:+.2f}%)/标普({spy_ov:+.2f}%)走弱，基金早盘仍出现 {pm:+.2f}% 溢价且分时MACD死叉，"
                f"多涨 {sp:+.2f}% 后借冲高先高抛锁利"
            )
            return meaning, why
    if sp >= 0.42 * sc and coint10 >= 0.20 * sc:
        why = (
            f"基金比{bench_lbl}多涨了 {sp:+.2f}%（超涨线 +{0.42 * sc:.2f}%），"
            f"且高于10日均值均衡位 {coint10:+.2f}%，实时折溢价 {pm:+.2f}%"
        )
    elif float(b["m3"]) >= 0.20 * sc and float(b["m1"]) >= 0.05 and float(b["macd10"]) >= 0.0018 * sc and sp >= 0.05:
        why = f"基金3分钟快速拉升 {float(b['m3']):+.2f}%、分时红柱放大冲高，且比{bench_short}多涨 {sp:+.2f}%，短线脉冲见顶"
    else:
        why = f"分时上涨动能减弱出现MACD死叉信号，同时相对{bench_short}偏强（多涨 {sp:+.2f}%，实时折溢价 {pm:+.2f}%）"
    return meaning, why


def _describe_v9_buy_entry(day: Mapping[str, Any], b: Mapping[str, Any], sc: float, frac: float) -> tuple[str, str]:
    is_xop = bool(day.get("cross_market_anchor") == "xop_us_ep")
    bench_lbl = "美股XOP油气股与汇率理论涨幅" if is_xop else "原油及汇率理论涨幅"
    bench_short = "美股XOP合成基准" if is_xop else "原油"
    meaning = f"先买后卖做正T，趁基金涨幅落后于{bench_short}逢低买入{frac * 100:.0f}%可用额度，等补涨修复再卖出赚差价"
    pm = float(b["prem"])
    sp = float(b["sp_fx"])
    coint10 = float(b["coint10"])
    if is_xop:
        xop_ov = float(day.get("xop_ov") or 0.0)
        ng_ov = float(day.get("ng_ov") or 0.0)
        xop_alpha = float(day.get("xop_vs_oil_alpha") or 0.0)
        if ((xop_ov >= 1.20) or (xop_alpha >= 0.80) or (ng_ov >= 3.0 and xop_ov >= 0.20)) and pm <= -0.35 and sp <= -0.22 * sc:
            why = (
                f"昨晚美股XOP({xop_ov:+.2f}%)/天然气({ng_ov:+.2f}%)隔夜强势领涨，"
                f"基金早盘仍处于 {pm:+.2f}% 折价且落后基准 {abs(sp):.2f}%、分时企稳，逢低吸筹博补涨"
            )
            return meaning, why
    if pm <= -0.50 * sc and sp <= -0.25 * sc and float(b["m3"]) <= -0.10:
        why = f"基金处于 {pm:+.2f}% 折价区间，比{bench_short}理论涨幅少涨了 {abs(sp):.2f}% 且3分钟回调 {float(b['m3']):+.2f}% 砸出低吸坑"
    elif sp <= -0.42 * sc and coint10 <= -0.20 * sc:
        why = f"基金明显滞涨，比{bench_lbl}落后 {abs(sp):.2f}%（低于10日均衡位 {abs(coint10):.2f}%，实时折溢价 {pm:+.2f}%）"
    elif float(b["m5"]) <= -0.32 * sc and float(b["m1"]) <= -0.05 and float(b["macd10"]) <= -0.0020 * sc and sp <= -0.10:
        why = f"基金5分钟急跌 {float(b['m5']):+.2f}% 出现短线超卖，且涨幅落后{bench_short} {abs(sp):.2f}%"
    else:
        why = f"基金涨幅落后{bench_short} {abs(sp):.2f}%（折溢价 {pm:+.2f}%），同时分时下跌动能衰竭拐头企稳"
    return meaning, why


def _describe_v9_scalp_buy(
    b: Mapping[str, Any],
    s_frac: float,
    day: Mapping[str, Any] | None = None,
) -> tuple[str, str]:
    is_xop = bool(day and day.get("cross_market_anchor") == "xop_us_ep")
    meaning = f"盘中急跌波段做T（动用{s_frac * 100:.0f}%可用额度快进快出，收盘不留隔夜高溢价风险）"
    why = (
        f"基金从今日高点回撤 {float(b.get('px_dd_hi', 0.0)):.2f}%、"
        f"低于盘中成交均价 {abs(float(b.get('vdev', 0.0))):.2f}%，同时{'外盘油气' if is_xop else '国际油价'}平稳、分时指标止跌拐头向上"
    )
    return meaning, why


def _describe_v9_short_cover(
    b: Mapping[str, Any],
    entry_sp_fx: float,
    day: Mapping[str, Any] | None = None,
) -> tuple[str, str]:
    is_xop = bool(day and day.get("cross_market_anchor") == "xop_us_ep")
    bench_short = "XOP合成估值" if is_xop else "原油"
    meaning = "反T低吸接回，把前面高抛卖出的筹码买回平仓，锁定日内高抛低吸差价"
    sp = float(b["sp_fx"])
    pm = float(b["prem"])
    if sp <= -0.08 and float(b["coint10"]) <= 0.0:
        why = f"此前高抛时的超涨水分已完全消退（卖出时多涨 {entry_sp_fx:+.2f}%，现已回落至落后{bench_short} {abs(sp):.2f}%）"
    elif pm <= -0.75 and (entry_sp_fx - sp) >= 0.28 and float(b["dmc10"]) > 0.0:
        why = f"基金已跌入 {pm:+.2f}% 深折价安全区，较卖出时回吐了 {entry_sp_fx - sp:.2f}% 超涨幅度且分时止跌企稳"
    elif entry_sp_fx >= 1.20 and (entry_sp_fx - sp) >= 0.85 and float(b["dmc10"]) >= 0.0:
        why = f"极端超涨已大幅回落 {entry_sp_fx - sp:.2f}%（从 {entry_sp_fx:+.2f}% 回落至 {sp:+.2f}%）且分时止跌"
    else:
        why = f"基金5分钟快速急跌 {float(b['m5']):+.2f}% 释放短线跌幅（当前折溢价 {pm:+.2f}%），趁回调接回筹码"
    return meaning, why


def _describe_v9_long_exit(
    b: Mapping[str, Any],
    entry_px: float,
    exit_lbl: str,
    retained_note: bool,
    min_hold: int,
) -> tuple[str, str]:
    gain_pct = ((float(b["price"]) - entry_px) / entry_px * 100.0) if entry_px > 0 else 0.0
    ret_txt = f"（已强制保留{min_hold}股底仓）" if retained_note and min_hold > 0 else ""
    if "止损" in exit_lbl:
        meaning = f"日内做T保护止损，短线走弱先平掉今日加买筹码控制回撤{ret_txt}"
        why = f"现价较买入价 {entry_px:.3f} 回撤 {gain_pct:+.2f}% 且分时动能转弱，触发保护离场"
    else:
        meaning = f"日内做T止盈卖出，把盘中低吸买入的筹码逢高兑现落袋{ret_txt}"
        why = f"基金滞涨坑已补涨修复（现价较买入价 {entry_px:.3f} 变动 {gain_pct:+.2f}%，相对原油价差回升至 {float(b['sp_fx']):+.2f}%，折溢价 {float(b['prem']):+.2f}%）"
    return meaning, why


def _describe_v9_close_rebalance(
    day: Mapping[str, Any],
    b: Mapping[str, Any],
    *,
    wt: float,
    side: str,
    rolling_shares: int,
    tgt_sh: int,
    min_hold: int,
) -> tuple[str, str]:
    bp_pct = float(b["brn_pos"]) * 100.0
    max20_pct = float(day["max_bpos_20d"]) * 100.0
    pm = float(b["prem"])
    pr = float(b["price"])
    ma5 = float(day["ma5"])
    ma15 = float(day["ma15"])
    brn = float(b["brn"])
    is_xop = bool(day.get("cross_market_anchor") == "xop_us_ep")
    xp_pct = float(b.get("xop_pos", b["brn_pos"])) * 100.0
    day_lead = float(b.get("daytime_lead_pct", b.get("daytime_brn_pct", 0.0)))
    xop_bull = bool(day.get("xop_bull", True))
    if wt <= 0.0:
        if bp_pct >= 90.0:
            plain_wt_why = f"布油处于近2年 {bp_pct:.1f}% 极高风险区（${brn:.2f}），不宜重仓过夜"
        elif is_xop and pm >= 0.80:
            plain_wt_why = f"按美股XOP汇调估值，基金溢价已达 {pm:+.2f}%（>=+0.80%），需防今晚开盘杀溢价清仓避险"
        elif is_xop and pm >= 0.40 and pr < ma5:
            plain_wt_why = f"按美股XOP汇调估值溢价偏高（{pm:+.2f}%）且跌破5日均线（{ma5:.3f}），不宜留仓过夜"
        elif is_xop and day_lead <= -0.45 and pm >= -0.05:
            plain_wt_why = f"亚欧盘原油等外盘指标较今晨美股收盘走弱（{day_lead:+.2f}%）且无折价保护，防今晚美股XOP补跌"
        elif bp_pct >= 72.0 and pr < ma5:
            plain_wt_why = f"布油处于近2年 {bp_pct:.1f}% 偏高位且基金跌破5日均线（{ma5:.3f}），需避险过夜"
        elif max20_pct >= 72.0 and ma5 < ma15 and pr < ma15:
            plain_wt_why = f"近20日布油刚从 {max20_pct:.1f}% 高位回落，且基金现价 {pr:.3f} 尚未收复15日均线（{ma15:.3f}），空头保护生效不宜重仓过夜"
        elif is_xop and (not xop_bull) and ma5 < ma15:
            plain_wt_why = f"美股XOP与场内基金处于双空头趋势（XOP2年分位 {xp_pct:.1f}%，折溢价 {pm:+.2f}%），空仓避险"
        elif pm >= 15.0:
            plain_wt_why = f"场内溢价高达 {pm:+.2f}% 已处于极端泡沫区间且趋势走弱，清仓防范隔夜杀溢价"
        elif ma5 < ma15 and pr < ma15 and pm >= 2.0:
            plain_wt_why = f"基金处于均线空头破位区间且仍带 {pm:+.2f}% 溢价泡沫，需防范杀溢价补跌"
        else:
            plain_wt_why = f"日线均线处于弱势区间且缺乏深折价保护（当前折溢价 {pm:+.2f}%），需避险控仓"
        if side == "buy":
            meaning = f"尾盘保底买回{min_hold}股，空头避险期不留重仓过夜，仅留{min_hold}股让同花顺连续统计累计收益与摊薄成本"
            why = f"{plain_wt_why}；因当前持仓为0股，按强制留底规则买回{min_hold}股"
        else:
            meaning = f"尾盘避险清仓，防范隔夜外盘波动风险，仅保留{min_hold}股延续同花顺累计收益统计"
            why = f"{plain_wt_why}，将持仓从 {rolling_shares}股减至保底 {tgt_sh}股"
        return meaning, why

    if bp_pct <= 42.0 or (is_xop and xp_pct <= 38.0):
        plain_wt_why = (
            f"油气板块处于近2年低位价值区（布油 {bp_pct:.1f}% / XOP {xp_pct:.1f}%），下行风险较小"
            if is_xop
            else f"布油处于近2年 {bp_pct:.1f}% 低位区（${brn:.2f}），下行风险较小"
        )
    elif is_xop and day_lead >= 0.40 and pm <= 0.25:
        plain_wt_why = f"亚欧盘原油等外盘指标较美股收盘领涨 {day_lead:+.2f}% 且XOP汇调估值安全（{pm:+.2f}%），博弈今晚美股XOP补涨"
    elif pm <= -0.30 and (ma5 >= ma15 or pr >= ma15 or (is_xop and xop_bull)):
        plain_wt_why = f"基金站稳均线非空头区且享有 {pm:+.2f}% 折价安全垫"
    elif bp_pct >= 72.0:
        plain_wt_why = f"布油处于近2年 {bp_pct:.1f}% 偏高位但基金仍守住5日均线（{ma5:.3f}），维持轻仓"
    elif pm >= 15.0:
        plain_wt_why = f"场内溢价已达 {pm:+.2f}% 进入高溢价泡沫区，按 {wt * 100:.0f}% 封顶控仓留底，防范隔夜杀溢价风险"
    elif is_xop:
        plain_wt_why = f"美股XOP处于近2年 {xp_pct:.1f}% 常态区间（布油 {bp_pct:.1f}%），基金位于5日均线{'上方' if pr >= ma5 else '下方'}（折溢价 {pm:+.2f}%）"
    else:
        plain_wt_why = f"布油处于近2年 {bp_pct:.1f}% 常态区间（${brn:.2f}），基金位于5日均线{'上方' if pr >= ma5 else '下方'}（折溢价 {pm:+.2f}%）"

    if side == "buy":
        meaning = f"尾盘回补过夜底仓至 {wt * 100:.0f}% 水位，备好底仓供次日盘中继续高抛低吸做T"
        why = f"{plain_wt_why}，持仓从 {rolling_shares}股补至目标 {tgt_sh}股"
    else:
        meaning = f"尾盘减仓调平至 {wt * 100:.0f}% 过夜目标水位，控制隔夜敞口"
        why = f"{plain_wt_why}，持仓从 {rolling_shares}股降至目标 {tgt_sh}股"
    return meaning, why


def _describe_v9_sell_addon(
    b: Mapping[str, Any],
    entry_px: float,
    entry_sp: float,
    min_hold: int,
    day: Mapping[str, Any] | None = None,
) -> tuple[str, str]:
    is_xop = bool(day and day.get("cross_market_anchor") == "xop_us_ep")
    bench_short = "美股XOP合成基准" if is_xop else "原油"
    retain_txt = f"（已强制保留{min_hold}股底仓）" if min_hold > 0 else ""
    meaning = f"反T二段高抛兑现，趁价差继续拉开且冲高动能放缓，将剩余可卖仓位逢高卖出锁定高位均价{retain_txt}"
    sp = float(b["sp_fx"])
    px = float(b["price"])
    pm = float(b["prem"])
    why = (
        f"基金已较首笔卖价 {entry_px:.3f} 再涨至 {px:.3f}，相对{bench_short}多涨幅度从 {entry_sp:+.2f}% 扩至 {sp:+.2f}%，"
        f"同时分时红柱动能放缓（实时折溢价 {pm:+.2f}%）"
    )
    return meaning, why


def evaluate_v9_plus_live_decision(
    store: Any,
    *,
    symbol: str,
    trade_date: str,
    now_hhmm: str,
    now_dt: datetime | None = None,
    cap_limit: float,
    rule_spec: Mapping[str, Any],
    pos_sizing: Mapping[str, Any],
    today_signals: Sequence[Mapping[str, Any]],
    rolling_shares: int,
    allow_network: bool | None = None,
    broker_available_cash: float | None = None,
    expected_ledger_shares: int | None = None,
) -> dict[str, Any]:
    """Evaluate live V9+ intraday entry/exit & 14:53 3-state close rebalance."""
    ctx = _prepare_v9_plus_live_state(
        store,
        symbol=symbol,
        trade_date=trade_date,
        cap_limit=cap_limit,
        rule_spec=rule_spec,
        pos_sizing=pos_sizing,
        today_signals=today_signals,
        rolling_shares=rolling_shares,
        allow_network=allow_network,
        broker_available_cash=broker_available_cash,
        expected_ledger_shares=expected_ledger_shares,
    )
    if ctx is None or len(ctx["bars"]) < 2:
        return {"intraday": [], "close_rebalance": None, "diagnostics": None}

    day = ctx["day"]
    bars = ctx["bars"]
    close_hhmm = ctx["close_hhmm"]
    overnight_fn = ctx["overnight_fn"]
    sc = ctx["sc"]
    cooldown = ctx["cooldown"]
    max_legs = ctx["max_legs"]
    min_hold = int(ctx["min_hold"])
    rule_id = ctx["rule_id"]
    rule_name = ctx["rule_name"]
    open_leg = ctx["open_leg"]
    last_exit_idx = ctx["last_exit_idx"]
    entries_count = ctx["entries_count"]
    close_rebal_done = ctx["close_rebal_done"]
    entry_sp_fx = ctx["entry_sp_fx"]
    entry_px = ctx["entry_px"]
    entry_prem = ctx["entry_prem"]
    v9_tuning = ctx.get("v9_tuning") or {}
    strict_scalp = bool(v9_tuning.get("scalp_strict_mode"))
    is_xop = bool(day.get("cross_market_anchor") == "xop_us_ep")

    b_last = bars[-1]
    cur_px = float(b_last["price"])
    avail_cash = _resolve_live_avail_cash(
        cap_limit,
        rolling_shares,
        cur_px,
        ctx["broker_available_cash"],
    )
    sellable_above_floor = max(0, int(rolling_shares) - min_hold)
    candidate_bars = [b for b in bars[-2:]]
    intraday_candidates: list[dict[str, Any]] = []

    if open_leg is None:
        for b in candidate_bars:
            t = str(b["time"])
            in_win = ("09:32" <= t <= "11:29") or ("13:02" <= t <= "14:30")
            if not in_win or (b["i"] - last_exit_idx) < cooldown or entries_count >= max_legs:
                continue
            if b is b_last and _is_early_unclosed_live_bar(t, now_dt, 15) and abs(float(b["sp_fx"])) < 0.65 * sc:
                continue
            s_sig, raw_b_sig = signals_v8_with_tuning(day, b, sc, v9_tuning)
            cur_wt, _ = overnight_fn(day, b, 0)
            b_buy = _shift_buy_bar_prem(b, v9_tuning)
            b_sig = filter_bear_regime_buy(day, b_buy, raw_b_sig, cur_wt, sc)
            scalp_ok, s_lbl, s_frac = should_enter_intraday_dip_scalp(
                b, sc, day=day, strict_mode=strict_scalp, v9_tuning=v9_tuning, cur_close_wt=cur_wt
            )
            if is_xop:
                s_sig, b_sig, scalp_ok, _ = apply_xop_overnight_intraday_overlays(
                    day,
                    b,
                    s_sig=s_sig,
                    b_sig=b_sig,
                    scalp_ok=scalp_ok,
                    close_wt=cur_wt,
                    sc=sc,
                )
            s_sig_short = False if (s_sig and _suppress_waterfall_short_entry(b, cur_wt, sc)) else s_sig
            if s_sig_short and sellable_above_floor >= _LOT_SIZE:
                t_lbl, frac = apply_v9_sell_tier_tuning(day, b, *clean_sell_tier(day, b), v9_tuning)
                q = (
                    sellable_above_floor
                    if frac >= 0.999
                    else min(sellable_above_floor, max(_LOT_SIZE, int((rolling_shares * frac) // _LOT_SIZE) * _LOT_SIZE))
                )
                push_meaning, push_why = _describe_v9_sell_entry(b, sc, frac, min_hold, day=day)
                intraday_candidates.append(
                    {
                        "rule_id": rule_id,
                        "rule_title": f"{rule_name} · 反T高抛({t_lbl})",
                        "rule_summary": f"汇调溢价{b['prem']:+.2f}% / 价差{b['sp_fx']:+.2f}% / MACD{b['macd10']:+.4f}；自主卖{q}股"
                        + (f"(保留{min_hold}股底仓)" if min_hold > 0 else ""),
                        "push_meaning": push_meaning,
                        "push_why": push_why,
                        "side": "sell",
                        "minute": t,
                        "price": f"{b['price']:.3f}",
                        "qty_override": q,
                        "weight_pct": round(frac * 100.0, 1),
                        "sizing_tier": t_lbl,
                        "signal_tag": "v9-entry",
                        "t0_net_cap_mode": True,
                    }
                )
                break
            if b_sig and avail_cash >= b["price"] * _LOT_SIZE:
                t_lbl, frac = clean_buy_tier(day, b_buy)
                q = int((avail_cash * frac) // (b["price"] * _LOT_SIZE)) * _LOT_SIZE
                if q < _LOT_SIZE and avail_cash >= b["price"] * _LOT_SIZE:
                    q = _LOT_SIZE
                if q >= _LOT_SIZE:
                    push_meaning, push_why = _describe_v9_buy_entry(day, b, sc, frac)
                    intraday_candidates.append(
                        {
                            "rule_id": rule_id,
                            "rule_title": f"{rule_name} · 正T低吸({t_lbl})",
                            "rule_summary": f"汇调溢价{b['prem']:+.2f}% / 价差{b['sp_fx']:+.2f}% / MA5={day['ma5']:.3f}；自主买{q}股",
                            "push_meaning": push_meaning,
                            "push_why": push_why,
                            "side": "buy",
                            "minute": t,
                            "price": f"{b['price']:.3f}",
                            "qty_override": q,
                            "weight_pct": round(frac * 100.0, 1),
                            "sizing_tier": t_lbl,
                            "signal_tag": "v9-entry",
                            "t0_net_cap_mode": True,
                        }
                    )
                    break
            if scalp_ok and avail_cash >= b["price"] * _LOT_SIZE:
                q = int((avail_cash * s_frac) // (b["price"] * _LOT_SIZE)) * _LOT_SIZE
                if q < _LOT_SIZE and avail_cash >= b["price"] * _LOT_SIZE:
                    q = _LOT_SIZE
                if q >= _LOT_SIZE:
                    push_meaning, push_why = _describe_v9_scalp_buy(b, s_frac, day=day)
                    intraday_candidates.append(
                        {
                            "rule_id": rule_id,
                            "rule_title": f"{rule_name} · 日内超跌做T({s_lbl})",
                            "rule_summary": f"均价偏离{b['vdev']:+.2f}% / 溢价{b['prem']:+.2f}% / 价差{b['sp_fx']:+.2f}%；日内建T买{q}股(收盘不隔夜)",
                            "push_meaning": push_meaning,
                            "push_why": push_why,
                            "side": "buy",
                            "minute": t,
                            "price": f"{b['price']:.3f}",
                            "qty_override": q,
                            "weight_pct": round(s_frac * 100.0, 1),
                            "sizing_tier": s_lbl,
                            "signal_tag": "v9-scalp",
                            "t0_net_cap_mode": True,
                        }
                    )
                    break
    else:
        last_min = str(open_leg.get("last_leg_minute") or open_leg["minute"])
        for b in candidate_bars:
            t = str(b["time"])
            if t <= last_min or t >= close_hhmm:
                continue
            if b is b_last and _is_early_unclosed_live_bar(t, now_dt, 15) and abs(float(b["sp_fx"])) < 0.65 * sc:
                continue
            if open_leg["side"] == "sell":
                cur_wt, _ = overnight_fn(day, b, 0)
                in_win = ("09:32" <= t <= "11:29") or ("13:02" <= t <= "14:30")
                s_sig_add, _ = signals_v8_with_tuning(day, b, sc, v9_tuning)
                if is_xop:
                    s_sig_add, _, _, _ = apply_xop_overnight_intraday_overlays(
                        day, b, s_sig=s_sig_add, b_sig=False, scalp_ok=False, close_wt=cur_wt, sc=sc
                    )
                if s_sig_add and _suppress_waterfall_short_entry(b, cur_wt, sc):
                    s_sig_add = False
                if (
                    bool(open_leg.get("allow_addon"))
                    and (not open_leg.get("addon_done"))
                    and in_win
                    and sellable_above_floor >= _LOT_SIZE
                    and should_add_short_tranche(b, entry_px=entry_px, entry_sp=entry_sp_fx, s_sig=s_sig_add)
                ):
                    q = sellable_above_floor
                    push_meaning, push_why = _describe_v9_sell_addon(b, entry_px, entry_sp_fx, min_hold, day=day)
                    intraday_candidates.append(
                        {
                            "rule_id": rule_id,
                            "rule_title": f"{rule_name} · 反T二段高抛(余仓兑现)",
                            "rule_summary": f"价差扩至{b['sp_fx']:+.2f}%(首卖{entry_sp_fx:+.2f}%) / 溢价{b['prem']:+.2f}%；二段卖{q}股"
                            + (f"(保留{min_hold}股底仓)" if min_hold > 0 else ""),
                            "push_meaning": push_meaning,
                            "push_why": push_why,
                            "side": "sell",
                            "minute": t,
                            "price": f"{b['price']:.3f}",
                            "qty_override": q,
                            "weight_pct": 100.0,
                            "sizing_tier": "反T二段高抛(余仓兑现)",
                            "signal_tag": "v9-addon",
                            "t0_net_cap_mode": True,
                        }
                    )
                    break
                if should_exit_short(b, entry_sp_fx, True, close_wt=cur_wt, entry_px=entry_px):
                    max_afford = int(avail_cash // (b["price"] * _LOT_SIZE)) * _LOT_SIZE
                    q = min(int(open_leg["qty"]), max_afford)
                    if q >= _LOT_SIZE:
                        push_meaning, push_why = _describe_v9_short_cover(b, entry_sp_fx, day=day)
                        intraday_candidates.append(
                            {
                                "rule_id": rule_id,
                                "rule_title": f"{rule_name} · 反T止盈回补",
                                "rule_summary": f"sp_fx回落至{b['sp_fx']:+.2f}% (入场{entry_sp_fx:+.2f}%，溢价{b['prem']:+.2f}%)，平仓回补{q}股",
                                "push_meaning": push_meaning,
                                "push_why": push_why,
                                "side": "buy",
                                "minute": t,
                                "price": f"{b['price']:.3f}",
                                "qty_override": q,
                                "weight_pct": 100.0,
                                "sizing_tier": "反T止盈回补",
                                "signal_tag": "v9-exit",
                                "t0_net_cap_mode": True,
                            }
                        )
                        break
            elif open_leg["side"] == "buy":
                s_sig, _ = signals_v8_with_tuning(day, b, sc, v9_tuning)
                cur_wt, _ = overnight_fn(day, b, 0)
                if is_xop:
                    s_sig, _, _, _ = apply_xop_overnight_intraday_overlays(
                        day, b, s_sig=s_sig, b_sig=False, scalp_ok=False, close_wt=cur_wt, sc=sc
                    )
                if open_leg.get("mode") == "scalp":
                    exit_ok, exit_lbl = should_exit_scalp_long(
                        b,
                        entry_px=entry_px,
                        entry_sp=entry_sp_fx,
                        s_sig=s_sig,
                    )
                else:
                    exit_ok, exit_lbl = should_exit_long_v9(
                        day,
                        _shift_buy_bar_prem(b, v9_tuning),
                        entry_px=entry_px,
                        entry_sp=entry_sp_fx,
                        s_sig=s_sig,
                        close_wt=cur_wt,
                        adaptive_exits=True,
                    )
                if exit_ok:
                    q = min(int(open_leg["qty"]), sellable_above_floor)
                    if q >= _LOT_SIZE:
                        retained_flag = bool(min_hold > 0 and (int(rolling_shares) - q) == min_hold)
                        push_meaning, push_why = _describe_v9_long_exit(b, entry_px, exit_lbl, retained_flag, min_hold)
                        intraday_candidates.append(
                            {
                                "rule_id": rule_id,
                                "rule_title": f"{rule_name} · {exit_lbl}",
                                "rule_summary": f"sp_fx修复至{b['sp_fx']:+.2f}% / 溢价{b['prem']:+.2f}%，止盈卖出{q}股"
                                + (f"(保留{min_hold}股底仓)" if retained_flag else ""),
                                "push_meaning": push_meaning,
                                "push_why": push_why,
                                "side": "sell",
                                "minute": t,
                                "price": f"{b['price']:.3f}",
                                "qty_override": q,
                                "weight_pct": 100.0,
                                "sizing_tier": exit_lbl,
                                "signal_tag": "v9-exit",
                                "t0_net_cap_mode": True,
                            }
                        )
                        break

    close_rebalance: dict[str, Any] | None = None
    pos_mode_lbl = open_leg.get("mode", 0) if open_leg is not None else 0
    wt, wt_reason = resolve_effective_close_weight(overnight_fn, day, b_last, pos_mode_lbl, entry_prem, v9_tuning=v9_tuning)
    raw_tgt_sh = int((cap_limit * wt) // (cur_px * _LOT_SIZE)) * _LOT_SIZE if cur_px > 0 else 0
    tgt_sh = (
        max(min_hold, raw_tgt_sh)
        if (cur_px > 0 and min_hold > 0 and cap_limit >= cur_px * min_hold)
        else raw_tgt_sh
    )
    if (not close_rebal_done) and close_hhmm <= now_hhmm <= "14:55" and cur_px > 0:
        diff_sh = tgt_sh - int(rolling_shares)
        if diff_sh >= _LOT_SIZE:
            q = min(diff_sh, int(avail_cash // (cur_px * _LOT_SIZE)) * _LOT_SIZE)
            if q >= _LOT_SIZE:
                push_meaning, push_why = _describe_v9_close_rebalance(
                    day, b_last, wt=wt, side="buy", rolling_shares=int(rolling_shares), tgt_sh=tgt_sh, min_hold=min_hold
                )
                if wt <= 0.0 and min_hold > 0 and tgt_sh == min_hold:
                    close_title = f"[尾盘三态调仓·保底留仓{min_hold}股] {rule_name}"
                    close_summary = f"{wt_reason}（强制保留{min_hold}股延续同花顺收益统计）；当前持{rolling_shares}股→目标{tgt_sh}股，买回{q}股"
                else:
                    close_title = f"[尾盘三态调仓·加仓留底] {rule_name}"
                    close_summary = f"{wt_reason}；当前持{rolling_shares}股→目标{tgt_sh}股，加仓{q}股"
                close_rebalance = {
                    "rule_id": rule_id,
                    "rule_title": close_title,
                    "rule_summary": close_summary,
                    "push_meaning": push_meaning,
                    "push_why": push_why,
                    "side": "buy",
                    "minute": now_hhmm,
                    "price": f"{cur_px:.3f}",
                    "qty_override": q,
                    "signal_tag": "v9-close",
                    "t0_net_cap_mode": True,
                }
        elif diff_sh <= -_LOT_SIZE and sellable_above_floor >= _LOT_SIZE:
            q = min((-diff_sh // _LOT_SIZE) * _LOT_SIZE, sellable_above_floor)
            if q >= _LOT_SIZE:
                retain_note = f"(保留{min_hold}股底仓)" if min_hold > 0 and tgt_sh == min_hold else ""
                push_meaning, push_why = _describe_v9_close_rebalance(
                    day, b_last, wt=wt, side="sell", rolling_shares=int(rolling_shares), tgt_sh=tgt_sh, min_hold=min_hold
                )
                close_rebalance = {
                    "rule_id": rule_id,
                    "rule_title": f"[尾盘三态调仓·减仓避险] {rule_name}",
                    "rule_summary": f"{wt_reason}{retain_note}；当前持{rolling_shares}股→目标{tgt_sh}股，卖出{q}股",
                    "push_meaning": push_meaning,
                    "push_why": push_why,
                    "side": "sell",
                    "minute": now_hhmm,
                    "price": f"{cur_px:.3f}",
                    "qty_override": q,
                    "signal_tag": "v9-close",
                    "t0_net_cap_mode": True,
                }

    buy_lbl, buy_frac = clean_buy_tier(day, _shift_buy_bar_prem(b_last, v9_tuning))
    sell_lbl, sell_frac = apply_v9_sell_tier_tuning(day, b_last, *clean_sell_tier(day, b_last), v9_tuning)
    diag_payload: dict[str, Any] = {
        "price": round(cur_px, 3),
        "prem_pct": round(float(b_last["prem"]), 3),
        "sp_fx_pct": round(float(b_last["sp_fx"]), 3),
        "brn_price": round(float(b_last["brn"]), 3),
        "brn_pos_pct": round(float(b_last["brn_pos"]) * 100.0, 1),
        "max_bpos_20d_pct": round(float(day["max_bpos_20d"]) * 100.0, 1),
        "ma5": round(float(day["ma5"]), 3),
        "ma15": round(float(day["ma15"]), 3),
        "close_target_weight_pct": round(wt * 100.0, 1),
        "close_target_shares": int(tgt_sh),
        "min_retained_shares": min_hold,
        "close_target_reason": wt_reason,
        "buy_tier": buy_lbl,
        "buy_weight_pct": round(buy_frac * 100.0, 1),
        "sell_tier": sell_lbl,
        "sell_weight_pct": round(sell_frac * 100.0, 1),
        "cross_market_anchor": str(day.get("cross_market_anchor") or "brent"),
    }
    if is_xop:
        diag_payload.update(
            {
                "xop_close": round(float(day.get("xop_close") or 0.0), 2),
                "xop_overnight_pct": round(float(day.get("xop_ov") or 0.0), 2),
                "xop_pos_pct": round(float(b_last.get("xop_pos", 0.0)) * 100.0, 1),
                "max_xpos_20d_pct": round(float(day.get("max_xpos_20d", 0.0)) * 100.0, 1),
                "ng_overnight_pct": round(float(day.get("ng_ov") or 0.0), 2),
                "spy_overnight_pct": round(float(day.get("spy_ov") or 0.0), 2),
                "daytime_lead_pct": round(float(b_last.get("daytime_lead_pct", b_last.get("daytime_brn_pct", 0.0))), 2),
                "xop_bull": bool(day.get("xop_bull")),
            }
        )
    return {
        "intraday": intraday_candidates,
        "close_rebalance": close_rebalance,
        "diagnostics": diag_payload,
    }


def evaluate_v9_plus_live_indicators(
    store: Any,
    *,
    symbol: str,
    trade_date: str,
    now_hhmm: str,
    cap_limit: float,
    rule_spec: Mapping[str, Any],
    pos_sizing: Mapping[str, Any],
    today_signals: Sequence[Mapping[str, Any]],
    rolling_shares: int,
    allow_network: bool | None = None,
    broker_available_cash: float | None = None,
    expected_ledger_shares: int | None = None,
) -> dict[str, Any] | None:
    """Return live market indicators and separated buy/sell rule checklist for V9+."""
    ctx = _prepare_v9_plus_live_state(
        store,
        symbol=symbol,
        trade_date=trade_date,
        cap_limit=cap_limit,
        rule_spec=rule_spec,
        pos_sizing=pos_sizing,
        today_signals=today_signals,
        rolling_shares=rolling_shares,
        allow_network=allow_network,
        broker_available_cash=broker_available_cash,
        expected_ledger_shares=expected_ledger_shares,
    )
    if ctx is None:
        return None

    day = ctx["day"]
    bars = ctx["bars"]
    b = bars[-1]
    t = str(b["time"])
    cur_px = float(b["price"])
    pre = float(day["pre"])
    ma5 = float(day["ma5"])
    ma15 = float(day["ma15"])
    sc = ctx["sc"]
    cooldown = ctx["cooldown"]
    max_legs = ctx["max_legs"]
    min_hold = int(ctx["min_hold"])
    open_leg = ctx["open_leg"]
    last_exit_idx = ctx["last_exit_idx"]
    entries_count = ctx["entries_count"]
    close_hhmm = ctx["close_hhmm"]
    overnight_fn = ctx["overnight_fn"]
    entry_sp_fx = ctx["entry_sp_fx"]
    entry_px = ctx["entry_px"]
    entry_prem = ctx["entry_prem"]
    v9_tuning = ctx.get("v9_tuning") or {}
    strict_scalp = bool(v9_tuning.get("scalp_strict_mode"))
    is_xop = bool(day.get("cross_market_anchor") == "xop_us_ep")

    pm = float(b["prem"])
    sp = float(b["sp_fx"])
    spi = float(b["sp_intra"])
    coint10 = float(b["coint10"])
    mc = float(b["macd10"])
    dmc = float(b["dmc10"])
    diff10 = float(b["diff10"])
    dea10 = float(b["dea10"])
    max_red12 = float(b["max_red12"])
    min_grn12 = float(b["min_grn12"])
    gc10 = bool(b["gc10"])
    dc10 = bool(b["dc10"])
    vdev = float(b.get("vdev") or 0.0)
    px_dd_hi = float(b.get("px_dd_hi") or 0.0)
    sp_dd_hi = float(b.get("sp_dd_hi") or 0.0)
    m1 = float(b["m1"])
    m3 = float(b["m3"])
    m5 = float(b["m5"])
    iopv = float(b["iopv"])
    iopv_intra = float(b["iopv_intra"])
    brn = float(b["brn"])
    brn_pos_pct = float(b["brn_pos"]) * 100.0
    brn_chg = float(b["brn_chg"])
    brn_m5 = float(b.get("brn_m5") or 0.0)
    fx = float(b["fx"])
    below_ma5 = cur_px < ma5

    s_sig, raw_b_sig = signals_v8_with_tuning(day, b, sc, v9_tuning)
    tuning_sell_guarded, tuning_sell_guard_reason = check_v9_tuning_sell_guard(b, v9_tuning)
    cur_wt_zero, _ = overnight_fn(day, b, 0)
    b_buy = _shift_buy_bar_prem(b, v9_tuning)
    b_sig = filter_bear_regime_buy(day, b_buy, raw_b_sig, cur_wt_zero, sc)
    scalp_ok, scalp_lbl, scalp_frac = should_enter_intraday_dip_scalp(
        b, sc, day=day, strict_mode=strict_scalp, v9_tuning=v9_tuning, cur_close_wt=cur_wt_zero
    )
    xop_overlay_meta: dict[str, Any] = {}
    if is_xop:
        s_sig, b_sig, scalp_ok, xop_overlay_meta = apply_xop_overnight_intraday_overlays(
            day,
            b,
            s_sig=s_sig,
            b_sig=b_sig,
            scalp_ok=scalp_ok,
            close_wt=cur_wt_zero,
            sc=sc,
        )
    pos_mode_lbl = open_leg.get("mode", 0) if open_leg is not None else 0
    wt, wt_reason = resolve_effective_close_weight(overnight_fn, day, b, pos_mode_lbl, entry_prem, v9_tuning=v9_tuning)

    broker_cash = ctx["broker_available_cash"]
    strategy_rem_cash = max(0.0, float(cap_limit) - max(0, int(rolling_shares)) * cur_px)
    avail_cash = _resolve_live_avail_cash(cap_limit, rolling_shares, cur_px, broker_cash)
    cash_cap_note = (
        f"(柜台可用 ¥{broker_cash:,.0f})"
        if broker_cash is not None and (broker_cash * 0.9995) < strategy_rem_cash
        else ""
    )
    raw_tgt_sh = int((cap_limit * wt) // (cur_px * _LOT_SIZE)) * _LOT_SIZE if cur_px > 0 else 0
    tgt_sh = (
        max(min_hold, raw_tgt_sh)
        if (cur_px > 0 and min_hold > 0 and cap_limit >= cur_px * min_hold)
        else raw_tgt_sh
    )
    sellable_above_floor = max(0, int(rolling_shares) - min_hold)
    diff_sh = tgt_sh - int(rolling_shares)

    in_win = ("09:32" <= t <= "11:29") or ("13:02" <= t <= "14:30")
    cooldown_ok = (b["i"] - last_exit_idx) >= cooldown
    legs_ok = entries_count < max_legs
    close_win = close_hhmm <= now_hhmm <= "14:55"
    has_bars = len(bars) >= 2

    # Buy decomposition
    prem_shift = float(v9_tuning.get("buy_prem_offset_pct") or 0.0)
    pm_buy = pm - prem_shift
    strict_dip_scalp = bool(v9_tuning.get("strict_dip_scalp"))
    buy_path_a = (pm_buy <= 0.50) and (pm_buy <= -0.50 * sc and sp <= -0.25 * sc and m3 <= -0.10)
    buy_path_b = (pm_buy <= 0.50) and (sp <= -0.42 * sc and coint10 <= -0.20 * sc)
    buy_path_c = (pm_buy <= 0.50) and (m5 <= -0.32 * sc and m1 <= -0.05 and mc <= -0.0020 * sc and sp <= -0.10)
    buy_base_pass = (buy_path_a or buy_path_b or buy_path_c)
    buy_gc_res = (pm_buy <= 0.20) and (
        (gc10 and sp <= -0.15 * sc and (dmc >= 0.0004 * sc or min_grn12 <= -0.0012 * sc))
        or (diff10 <= -0.0020 * sc and dmc >= 0.0005 * sc and sp <= -0.18 * sc)
    ) and not (below_ma5 and iopv_intra <= -0.08)

    veto_dmc = dmc <= -0.0006
    veto_chasing = pm_buy > -0.20 and spi >= 0.35
    veto_below_ma5 = below_ma5 and sp > -0.75 * sc and (iopv_intra <= -0.05 or dmc < 0.0)
    bear_allow = filter_bear_regime_buy(day, b_buy, True, cur_wt_zero, sc)
    buy_filter_ok = (not veto_dmc) and (not veto_chasing) and (not veto_below_ma5) and bear_allow
    veto_reasons = []
    if veto_dmc:
        veto_reasons.append(f"ΔMACD {dmc:+.4f}<=-0.0006")
    if veto_chasing:
        veto_reasons.append(f"日内sp变化 {spi:+.2f}%>=+0.35%")
    if veto_below_ma5:
        veto_reasons.append("MA5下方且IOPV/斜率偏弱")
    if not bear_allow:
        veto_reasons.append("空头不隔夜防接飞刀过滤")

    buy_tier_lbl, buy_tier_frac = clean_buy_tier(day, b_buy)
    sell_tier_lbl, sell_tier_frac = apply_v9_sell_tier_tuning(day, b, *clean_sell_tier(day, b), v9_tuning)

    buy_gate_ok = has_bars and open_leg is None and in_win and cooldown_ok and legs_ok and (avail_cash >= cur_px * _LOT_SIZE)
    buy_close_ok = (not ctx["close_rebal_done"]) and close_win and diff_sh >= _LOT_SIZE and avail_cash >= cur_px * _LOT_SIZE

    buy_conditions: list[dict[str, Any]] = [
        {
            "id": "v9-buy-gate",
            "label": "开仓窗口 / 冷却 / 资金闸门",
            "matched": buy_gate_ok,
            "current_value": f"{t} · 资金可用 ¥{avail_cash:,.0f}{cash_cap_note} · 今日{entries_count}/{max_legs}笔",
            "target_rule": f"09:32-11:29/13:02-14:30, 无未平腿, 冷却≥{cooldown}分",
            "detail": (
                "允许新开仓"
                if buy_gate_ok
                else f"暂停开仓({'持腿中' if open_leg is not None else ('非窗口' if not in_win else '资金/冷却/次数受限')})"
            ),
        },
        {
            "id": "v9-buy-a",
            "label": "正T路径A · 折价滞涨低吸",
            "matched": buy_path_a,
            "current_value": f"溢价 {pm:+.2f}%, sp_fx {sp:+.2f}%, m3 {m3:+.2f}%",
            "target_rule": f"溢价≤{-0.50 * sc + prem_shift:+.2f}% & sp_fx≤{-0.25 * sc:+.2f}% & m3≤-0.10%",
            "detail": f"汇调溢价{pm:+.2f}%, 跨市场价差{sp:+.2f}%, 3m涨跌{m3:+.2f}%",
        },
        {
            "id": "v9-buy-b",
            "label": "正T路径B · 深负价差协整买点",
            "matched": buy_path_b,
            "current_value": f"sp_fx {sp:+.2f}%, coint10 {coint10:+.2f}%, 溢价 {pm:+.2f}%",
            "target_rule": f"sp_fx≤{-0.42 * sc:+.2f}% & coint10≤{-0.20 * sc:+.2f}% & 溢价≤{0.50 + prem_shift:+.2f}%",
            "detail": f"价差{sp:+.2f}%, 10分协整{coint10:+.2f}%",
        },
        {
            "id": "v9-buy-c",
            "label": "正T路径C · 急跌负柱脉冲抄底",
            "matched": buy_path_c,
            "current_value": f"m5 {m5:+.2f}%, m1 {m1:+.2f}%, MACD {mc:+.4f}",
            "target_rule": f"m5≤{-0.32 * sc:+.2f}% & m1≤-0.05% & MACD≤{-0.0020 * sc:+.4f} & sp_fx≤-0.10%",
            "detail": f"5m动量{m5:+.2f}%, MACD柱{mc:+.4f}, sp_fx {sp:+.2f}%",
        },
        {
            "id": "v9-buy-d",
            "label": "正T路径D · 金叉/深负DIFF共振",
            "matched": buy_gc_res,
            "current_value": f"金叉={'是' if gc10 else '否'}, DIFF {diff10:+.4f}, ΔMACD {dmc:+.4f}",
            "target_rule": f"溢价≤{0.20 + prem_shift:+.2f}%, sp_fx≤-0.15% 且 金叉/深DIFF企稳",
            "detail": f"sp_fx {sp:+.2f}%, min_grn12 {min_grn12:+.4f}, IOPV变化 {iopv_intra:+.2f}%",
        },
        {
            "id": "v9-buy-filter",
            "label": "正T趋势风控滤波 (不接飞刀/不追高)",
            "matched": buy_filter_ok,
            "current_value": f"ΔMACD {dmc:+.4f}, MA5={ma5:.3f}({'上方' if not below_ma5 else '下方'}), vdev {vdev:+.2f}%",
            "target_rule": "ΔMACD>-0.0006 且 无高开滞后追高/空头逆势破位",
            "detail": "通过全部趋势风控" if buy_filter_ok else f"触发拦截：{'；'.join(veto_reasons)}",
        },
        {
            "id": "v9-buy-scalp",
            "label": (
                "日内超跌做T路径 (09:40-13:45·去泡沫严选)"
                if strict_scalp
                else (
                    "日内超跌做T路径 (09:40-13:50·防飞刀严选)"
                    if strict_dip_scalp
                    else "日内超跌做T路径 (09:40-14:15)"
                )
            ),
            "matched": scalp_ok,
            "current_value": f"均价偏离 {vdev:+.2f}%, 日高回撤 {px_dd_hi:.2f}%, sp回撤 {sp_dd_hi:+.2f}%",
            "target_rule": (
                "vdev≤-0.50% & (sp_fx≤-0.10%或sp_intra≤-0.30%) & MACD转强"
                if strict_scalp
                else (
                    "非0%避险态 & BRN分位<85% & vdev≤-0.45% & px回撤≥0.48% & MACD转强"
                    if strict_dip_scalp
                    else "vdev≤-0.35% & px回撤≥0.38% & sp回撤≥0.22% & MACD转强"
                )
            ),
            "detail": f"{scalp_lbl} · brn5m {brn_m5:+.2f}%, 溢价 {pm:+.2f}%" if scalp_ok else f"溢价{pm:+.2f}%, brn5m {brn_m5:+.2f}%, ΔMACD {dmc:+.4f}",
        },
        {
            "id": "v9-buy-close",
            "label": f"{close_hhmm} 尾盘三态调仓补仓检查",
            "matched": buy_close_ok,
            "current_value": f"现持 {rolling_shares}股 / 目标 {tgt_sh}股 ({wt * 100:.0f}%)",
            "target_rule": f"{close_hhmm}-14:55 且 目标仓位高出现持≥100股",
            "detail": wt_reason,
        },
    ]
    s_sig_short = False if (s_sig and _suppress_waterfall_short_entry(b, cur_wt_zero, sc)) else s_sig
    short_cover_signal_ok = (
        bool(should_exit_short(b, entry_sp_fx, True, close_wt=cur_wt_zero, entry_px=entry_px))
        if (open_leg is not None and open_leg["side"] == "sell")
        else False
    )
    if open_leg is not None and open_leg["side"] == "sell":
        cover_rule_txt = (
            "sp_fx≤-0.08%&coint10≤0 或 (m5≤-0.28%&价差回落≥0.15%&低价) 或 深折价回落"
            if float(cur_wt_zero) <= 0.0
            else "sp_fx≤-0.08%&coint10≤0 或 深折价(≤-0.75%)回落≥0.28%&ΔMACD>0"
        )
        buy_conditions.insert(
            1,
            {
                "id": "v9-buy-cover",
                "label": "反T空腿止盈回补条件",
                "matched": short_cover_signal_ok,
                "current_value": f"当前 sp_fx {sp:+.2f}% (入场 {entry_sp_fx:+.2f}%, 回落 {entry_sp_fx - sp:+.2f}%), 溢价 {pm:+.2f}%",
                "target_rule": cover_rule_txt,
                "detail": "达到反T平仓回补条件"
                if short_cover_signal_ok
                else ("空头期价差或价格未优于卖出点，暂不亏损回补" if float(cur_wt_zero) <= 0.0 else "继续持空腿等待价差回落"),
            },
        )
    if is_xop:
        xop_ov_v = float(day.get("xop_ov") or 0.0)
        spy_ov_v = float(day.get("spy_ov") or 0.0)
        ng_ov_v = float(day.get("ng_ov") or 0.0)
        buy_xop_blocked = bool(xop_overlay_meta.get("buy_knife_blocked_xop") or xop_overlay_meta.get("scalp_blocked_xop"))
        buy_xop_catchup = bool(xop_overlay_meta.get("buy_catchup_xop"))
        buy_conditions.insert(
            -1,
            {
                "id": "v9-buy-xop-guard",
                "label": "XOP隔夜因子共振 / 防飞刀滤波",
                "matched": buy_xop_catchup or (not buy_xop_blocked),
                "current_value": f"XOP {xop_ov_v:+.2f}%, SPY {spy_ov_v:+.2f}%, NG {ng_ov_v:+.2f}%, 溢价 {pm:+.2f}%",
                "target_rule": "强隔夜XOP/NG折价补涨优先；弱隔夜XOP/SPY早盘防接飞刀",
                "detail": (
                    "触发隔夜领涨折价补涨买点"
                    if buy_xop_catchup
                    else ("弱隔夜美股拖累，早盘拦截追接买点" if buy_xop_blocked else "通过XOP隔夜风控检查")
                ),
            },
        )

    buy_matched_now = bool(
        (buy_gate_ok and (b_sig or scalp_ok))
        or (open_leg is not None and open_leg["side"] == "sell" and short_cover_signal_ok and avail_cash >= cur_px * _LOT_SIZE)
        or buy_close_ok
    )

    # Sell decomposition
    sell_path_a = (pm > -1.0) and (sp >= 0.42 * sc and coint10 >= 0.20 * sc)
    sell_path_b = (pm > -1.0) and (m3 >= 0.20 * sc and m1 >= 0.05 and mc >= 0.0018 * sc and sp >= 0.05)
    sell_path_c = (
        dc10
        and pm > -0.80
        and (
            (max_red12 >= 0.0015 * sc and sp >= 0.0)
            or (max_red12 >= 0.0018 * sc and (pm >= -0.10 or spi >= 0.25 * sc))
            or (diff10 >= 0.0020 * sc and abs(dmc) >= 0.0004 * sc and sp >= -0.05)
            or (abs(mc) >= 0.0005 * sc and pm >= 0.80 and sp >= 0.0)
        )
    )
    base_sell_guarded, base_sell_guard_reason = is_discount_uptrend_sell_guarded(b, sc)
    sell_guarded = base_sell_guarded or tuning_sell_guarded
    sell_guard_reason = "；".join(p for p in (base_sell_guard_reason, tuning_sell_guard_reason) if p)
    if open_leg is not None and open_leg["side"] == "buy":
        if open_leg.get("mode") == "scalp":
            exit_long_ok, exit_long_lbl = should_exit_scalp_long(b, entry_px=entry_px, entry_sp=entry_sp_fx, s_sig=s_sig)
        else:
            exit_long_ok, exit_long_lbl = should_exit_long_v9(
                day,
                b_buy,
                entry_px=entry_px,
                entry_sp=entry_sp_fx,
                s_sig=s_sig,
                close_wt=cur_wt_zero,
                adaptive_exits=True,
            )
        exit_long_target = "价差修复达标 / 浮盈止盈 / 破位保护"
    else:
        tgt_sp = 0.28 if (pm_buy <= -1.00 and iopv_intra >= 0.20) else (0.10 if pm_buy < 0.0 else -0.02)
        exit_long_ok = should_exit_long_strict(b_buy, True)
        exit_long_lbl = f"严格价差修复(目标≥{tgt_sp:+.2f}%)"
        exit_long_target = f"sp_fx≥{tgt_sp:+.2f}% 或 (m3≥+0.18% & MACD≥+0.0015)"

    sell_gate_ok_legacy = has_bars and (sellable_above_floor >= _LOT_SIZE) and (in_win or (open_leg is not None and open_leg["side"] == "buy"))
    short_entry_gate_ok = bool(
        has_bars
        and sellable_above_floor >= _LOT_SIZE
        and open_leg is None
        and in_win
        and cooldown_ok
        and legs_ok
    )
    short_addon_gate_ok = bool(
        has_bars
        and open_leg is not None
        and open_leg["side"] == "sell"
        and bool(open_leg.get("allow_addon"))
        and (not open_leg.get("addon_done"))
        and in_win
        and sellable_above_floor >= _LOT_SIZE
    )
    short_addon_ok = bool(
        short_addon_gate_ok
        and should_add_short_tranche(b, entry_px=entry_px, entry_sp=entry_sp_fx, s_sig=s_sig_short)
    )
    long_exit_gate_ok = bool(
        has_bars
        and open_leg is not None
        and open_leg["side"] == "buy"
        and sellable_above_floor >= _LOT_SIZE
    )
    short_cover_gate_ok = bool(
        has_bars
        and open_leg is not None
        and open_leg["side"] == "sell"
        and avail_cash >= cur_px * _LOT_SIZE
    )

    sell_gate_ok = bool(short_entry_gate_ok or short_addon_gate_ok or long_exit_gate_ok)
    sell_close_ok = (not ctx["close_rebal_done"]) and close_win and diff_sh <= -_LOT_SIZE and sellable_above_floor >= _LOT_SIZE

    if open_leg is not None and open_leg["side"] == "sell":
        if short_addon_gate_ok:
            sell_gate_current = f"{t} · 剩余 {rolling_shares}股（可卖 {sellable_above_floor}股） · 持反T空腿 {int(open_leg['qty'])}股"
            sell_gate_detail = f"首笔分批已卖 {int(open_leg['qty'])}股，尚余 {sellable_above_floor}股可待高点二段加抛"
        else:
            sell_gate_current = f"{t} · 剩余 {rolling_shares}股 · 持反T空腿 {int(open_leg['qty'])}股中"
            sell_gate_detail = "持反T空腿中，暂停再开反T空腿"
    elif open_leg is not None and open_leg["side"] == "buy":
        sell_gate_current = f"{t} · 当前持仓 {rolling_shares}股（可卖 {sellable_above_floor}股） · 持正T多腿"
        sell_gate_detail = (
            "满足卖出持仓前置条件"
            if sell_gate_ok
            else f"扣除保底 {min_hold} 股后可卖股数不足 100 股"
        )
    else:
        sell_gate_current = f"{t} · 当前持仓 {rolling_shares}股（可卖 {sellable_above_floor}股） · 底仓状态"
        if 0 < int(rolling_shares) <= min_hold:
            sell_gate_detail = f"当前仅剩保底 {min_hold} 股（强制保留延续同花顺收益统计），暂停卖出"
        else:
            sell_gate_detail = (
                "满足卖出持仓前置条件"
                if (sell_gate_ok and sell_gate_ok_legacy)
                else f"扣除保底 {min_hold} 股后可卖不足 100 股或非盘中卖出时段（或冷却/笔数受限）"
            )

    sell_conditions: list[dict[str, Any]] = [
        {
            "id": "v9-sell-gate",
            "label": "可卖底仓 / 窗口 / 冷却闸门",
            "matched": sell_gate_ok,
            "current_value": sell_gate_current,
            "target_rule": f"扣除保底{min_hold}股后可卖≥100股、无未平反T空腿且处于盘中时段（或分批二段加抛/正T止盈）",
            "detail": sell_gate_detail,
        },
        {
            "id": "v9-sell-a",
            "label": "反T路径A · 溢价超涨协整高抛",
            "matched": sell_path_a,
            "current_value": f"sp_fx {sp:+.2f}%, coint10 {coint10:+.2f}%, 溢价 {pm:+.2f}%",
            "target_rule": f"sp_fx≥{0.42 * sc:+.2f}% & coint10≥{0.20 * sc:+.2f}% & 溢价>-1.0%",
            "detail": f"价差{sp:+.2f}%, 10分协整{coint10:+.2f}%",
        },
        {
            "id": "v9-sell-b",
            "label": "反T路径B · 短线冲高强红柱",
            "matched": sell_path_b,
            "current_value": f"m3 {m3:+.2f}%, m1 {m1:+.2f}%, MACD {mc:+.4f}",
            "target_rule": f"m3≥{0.20 * sc:+.2f}% & m1≥+0.05% & MACD≥{0.0018 * sc:+.4f} & sp_fx≥+0.05%",
            "detail": f"3m脉冲{m3:+.2f}%, sp_fx {sp:+.2f}%",
        },
        {
            "id": "v9-sell-c",
            "label": "反T路径C · 溢价区MACD死叉钝化",
            "matched": sell_path_c,
            "current_value": f"死叉={'是' if dc10 else '否'}, max_red12 {max_red12:+.4f}, 溢价 {pm:+.2f}%",
            "target_rule": "MACD死叉 & 溢价>-0.80% & 强红柱/高位DIFF/高溢价共振",
            "detail": f"DIFF {diff10:+.4f}, 日内价差变化 {spi:+.2f}%",
        },
        {
            "id": "v9-sell-brent-guard",
            "label": "反T防卖飞风控滤波 (折价+强布油不摸顶)",
            "matched": not sell_guarded,
            "current_value": f"溢价 {pm:+.2f}%, sp_intra {spi:+.2f}%, 布油 {brn_chg:+.2f}%(5m {brn_m5:+.2f}%)",
            "target_rule": (
                f"非(溢价<-0.30%&未死叉&sp_fx<{0.58 * sc:+.2f}%) 且 sp_fx≥+0.05% 且 非(布油≥+2%&sp_intra<+0.05%&未死叉)"
                if bool(v9_tuning.get("bull_oil_surge_guard"))
                else f"非(溢价<-0.30%&有效死叉未触发&sp_fx<{0.58 * sc:+.2f}%) 或 (sp_intra≥{0.32 * sc:+.2f}%且非布油强攻)"
            ),
            "detail": "通过折价强攻防早卖检查" if not sell_guarded else f"触发防卖飞拦截：{sell_guard_reason}",
        },
        {
            "id": "v9-sell-exit",
            "label": "多腿止盈 / 价差修复平仓线",
            "matched": exit_long_ok,
            "current_value": f"sp_fx {sp:+.2f}%, 溢价 {pm:+.2f}%, 均价偏离 {vdev:+.2f}%",
            "target_rule": exit_long_target,
            "detail": exit_long_lbl or f"未触发平仓（sp_fx {sp:+.2f}%）",
        },
        {
            "id": "v9-sell-close",
            "label": f"{close_hhmm} 尾盘三态调仓减仓检查",
            "matched": sell_close_ok,
            "current_value": f"现持 {rolling_shares}股 / 目标 {tgt_sh}股 ({wt * 100:.0f}%)",
            "target_rule": f"{close_hhmm}-14:55 且 需减持≥100股避险",
            "detail": wt_reason,
        },
    ]
    if is_xop:
        xop_ov_v = float(day.get("xop_ov") or 0.0)
        spy_ov_v = float(day.get("spy_ov") or 0.0)
        sell_guarded_xop = bool(xop_overlay_meta.get("sell_guarded_xop"))
        sell_weak_ov = bool(xop_overlay_meta.get("sell_weak_ov_triggered"))
        sell_conditions.insert(
            -1,
            {
                "id": "v9-sell-xop-guard",
                "label": "XOP隔夜因子共振 / 领涨防卖飞滤波",
                "matched": not sell_guarded_xop,
                "current_value": f"XOP {xop_ov_v:+.2f}%, SPY {spy_ov_v:+.2f}%, 溢价 {pm:+.2f}%, sp_fx {sp:+.2f}%",
                "target_rule": "强隔夜XOP/NG且折价未破位时防反T早卖；弱隔夜高溢价加速高抛",
                "detail": (
                    "触发弱隔夜XOP/SPY高溢价高抛信号"
                    if sell_weak_ov
                    else ("强隔夜XOP/NG领涨，折价区拦截反T早卖" if sell_guarded_xop else "通过XOP隔夜卖出风控检查")
                ),
            },
        )

    sell_matched_now = bool(
        (sellable_above_floor >= _LOT_SIZE and open_leg is None and in_win and cooldown_ok and legs_ok and s_sig_short)
        or short_addon_ok
        or (open_leg is not None and open_leg["side"] == "buy" and exit_long_ok and sellable_above_floor >= _LOT_SIZE)
        or sell_close_ok
    )

    chg_pct = ((cur_px - pre) / pre * 100.0) if pre > 0 else float(b.get("lof_chg") or 0.0)
    market_indicators = [
        {
            "key": "price",
            "label": "最新价 / 均线状态",
            "value": f"{cur_px:.3f} ({chg_pct:+.2f}%)",
            "raw": round(cur_px, 4),
            "hint": f"MA5 {ma5:.3f} · MA15 {ma15:.3f} · {'站上MA5' if not below_ma5 else '位于MA5下方'}",
        },
        {
            "key": "prem",
            "label": "XOP汇调实时溢价率" if is_xop else "汇调实时溢价率",
            "value": f"{pm:+.2f}%",
            "raw": round(pm, 3),
            "hint": f"IOPV {iopv:.4f} (日内 {iopv_intra:+.2f}%) · 昨收 {pre:.3f}",
        },
        {
            "key": "sp_fx",
            "label": "跨市场汇调价差(XOP)" if is_xop else "跨市场汇调价差 sp_fx",
            "value": f"{sp:+.2f}%",
            "raw": round(sp, 3),
            "hint": f"日内变化 {spi:+.2f}% · 10m协整 {coint10:+.2f}% · 价差回撤 {sp_dd_hi:+.2f}%",
        },
        {
            "key": "brn",
            "label": "布伦特 / 2年分位",
            "value": f"${brn:.2f} ({brn_pos_pct:.1f}%)",
            "raw": round(brn, 3),
            "hint": f"20日峰值分位 {float(day['max_bpos_20d']) * 100.0:.1f}% · 较前收 {brn_chg:+.2f}% · 5m {brn_m5:+.2f}% · USD/CNY {fx:.4f}",
        },
        {
            "key": "macd",
            "label": "MACD(10,20,5)",
            "value": f"{mc:+.4f}",
            "raw": round(mc, 5),
            "hint": f"DIFF {diff10:+.4f} · DEA {dea10:+.4f} · ΔMACD {dmc:+.4f} ({'金叉' if gc10 else ('死叉' if dc10 else '延续')})",
        },
        {
            "key": "vdev",
            "label": "均价偏离 / 短线动量",
            "value": f"{vdev:+.2f}%",
            "raw": round(vdev, 3),
            "hint": f"日高回撤 {px_dd_hi:.2f}% · 1m {m1:+.2f}% · 3m {m3:+.2f}% · 5m {m5:+.2f}%",
        },
        {
            "key": "close_wt",
            "label": "14:53 尾盘三态目标仓位",
            "value": (
                f"0% (保底{min_hold}股)"
                if (wt <= 0.0 and min_hold > 0 and tgt_sh == min_hold)
                else f"{wt * 100:.0f}% ({tgt_sh}股)"
            ),
            "raw": round(wt * 100.0, 1),
            "hint": (
                f"{wt_reason}（清仓强制保留{min_hold}股，当前策略底仓 {rolling_shares}股）"
                if (wt <= 0.0 and min_hold > 0 and tgt_sh == min_hold)
                else f"{wt_reason}（当前策略底仓 {rolling_shares}股）"
            ),
        },
    ]
    if is_xop:
        xop_close_v = float(day.get("xop_close") or 0.0)
        xop_pos_pct = float(b.get("xop_pos", 0.0)) * 100.0
        max_xpos_20d_pct = float(day.get("max_xpos_20d", 0.0)) * 100.0
        xop_ov_v = float(day.get("xop_ov") or 0.0)
        ng_ov_v = float(day.get("ng_ov") or 0.0)
        spy_ov_v = float(day.get("spy_ov") or 0.0)
        day_lead = float(b.get("daytime_lead_pct", b.get("daytime_brn_pct", 0.0)))
        market_indicators.insert(
            3,
            {
                "key": "xop",
                "label": "美股XOP / 2年分位",
                "value": f"${xop_close_v:.2f} ({xop_pos_pct:.1f}%)",
                "raw": round(xop_close_v, 3),
                "hint": f"20日峰值分位 {max_xpos_20d_pct:.1f}% · 昨夜XOP {xop_ov_v:+.2f}% · NG {ng_ov_v:+.2f}% · SPY {spy_ov_v:+.2f}% · 日内合成分项 {day_lead:+.2f}%",
            },
        )

    buy_hit_cnt = sum(1 for item in buy_conditions if item["matched"])
    sell_hit_cnt = sum(1 for item in sell_conditions if item["matched"])
    diag_out: dict[str, Any] = {
        "price": round(cur_px, 3),
        "prem_pct": round(pm, 3),
        "sp_fx_pct": round(sp, 3),
        "brn_price": round(brn, 3),
        "brn_pos_pct": round(brn_pos_pct, 1),
        "max_bpos_20d_pct": round(float(day["max_bpos_20d"]) * 100.0, 1),
        "ma5": round(ma5, 3),
        "ma15": round(ma15, 3),
        "close_target_weight_pct": round(wt * 100.0, 1),
        "close_target_reason": wt_reason,
        "buy_tier": buy_tier_lbl,
        "buy_weight_pct": round(buy_tier_frac * 100.0, 1),
        "sell_tier": sell_tier_lbl,
        "sell_weight_pct": round(sell_tier_frac * 100.0, 1),
        "rolling_shares": int(rolling_shares),
        "avail_cash": round(avail_cash, 2),
        "effective_avail_cash": round(avail_cash, 2),
        "broker_available_cash": round(float(broker_cash), 2) if broker_cash is not None else None,
        "open_leg_side": open_leg["side"] if open_leg is not None else None,
        "open_leg_qty": int(open_leg["qty"]) if open_leg is not None else 0,
        "open_leg_minute": str(open_leg["minute"]) if open_leg is not None else None,
        "in_intraday_window": bool(in_win),
        "cooldown_ok": bool(cooldown_ok),
        "legs_ok": bool(legs_ok),
        "entries_count": int(entries_count),
        "max_legs": int(max_legs),
        "close_hhmm": str(close_hhmm),
        "close_target_shares": int(tgt_sh),
        "min_retained_shares": min_hold,
        "short_entry_gate_ok": short_entry_gate_ok,
        "short_addon_gate_ok": short_addon_gate_ok,
        "long_exit_gate_ok": long_exit_gate_ok,
        "short_cover_gate_ok": short_cover_gate_ok,
        "short_cover_signal_ok": short_cover_signal_ok,
        "buy_b_sig": bool(b_sig),
        "buy_scalp_ok": bool(scalp_ok),
        "sell_s_sig": bool(s_sig),
        "cross_market_anchor": str(day.get("cross_market_anchor") or "brent"),
    }
    if is_xop:
        diag_out.update(
            {
                "xop_close": round(float(day.get("xop_close") or 0.0), 2),
                "xop_overnight_pct": round(float(day.get("xop_ov") or 0.0), 2),
                "xop_pos_pct": round(float(b.get("xop_pos", 0.0)) * 100.0, 1),
                "max_xpos_20d_pct": round(float(day.get("max_xpos_20d", 0.0)) * 100.0, 1),
                "ng_overnight_pct": round(float(day.get("ng_ov") or 0.0), 2),
                "spy_overnight_pct": round(float(day.get("spy_ov") or 0.0), 2),
                "daytime_lead_pct": round(float(b.get("daytime_lead_pct", b.get("daytime_brn_pct", 0.0))), 2),
                "xop_bull": bool(day.get("xop_bull")),
            }
        )
    return {
        "trade_date": day["td"],
        "latest_minute": t,
        "data_ready": has_bars,
        "note": "" if has_bars else "当日分钟样本不足 2 根，待行情继续积累",
        "market_indicators": market_indicators,
        "buy": {
            "matched": buy_matched_now,
            "summary": (
                f"买点触发 · {scalp_lbl if (scalp_ok and not b_sig) else buy_tier_lbl}"
                if buy_matched_now
                else f"暂未触发买点（已满足 {buy_hit_cnt}/{len(buy_conditions)} 项指标/门禁）"
            ),
            "tier_label": scalp_lbl if (scalp_ok and not b_sig) else buy_tier_lbl,
            "target_weight_pct": round((scalp_frac if (scalp_ok and not b_sig) else buy_tier_frac) * 100.0, 1),
            "conditions": buy_conditions,
        },
        "sell": {
            "matched": sell_matched_now,
            "summary": (
                f"卖点触发 · {exit_long_lbl if (open_leg and open_leg['side'] == 'buy' and exit_long_ok) else sell_tier_lbl}"
                if sell_matched_now
                else f"暂未触发卖点（已满足 {sell_hit_cnt}/{len(sell_conditions)} 项指标/门禁）"
            ),
            "tier_label": exit_long_lbl if (open_leg and open_leg["side"] == "buy" and exit_long_ok) else sell_tier_lbl,
            "target_weight_pct": round(sell_tier_frac * 100.0, 1),
            "conditions": sell_conditions,
        },
        "diagnostics": diag_out,
    }
