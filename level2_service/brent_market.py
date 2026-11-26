"""ICE Brent Crude Continuous (BRN0Y) market-hour collector, backfill, and premium panel."""

from __future__ import annotations

import asyncio
from bisect import bisect_right
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import sqlite3
from threading import RLock
from typing import Any, Callable, Mapping, Sequence
import urllib.request
from zoneinfo import ZoneInfo

from .research_premium import PREMIUM_CODES, PREMIUM_NAMES
from .research_replay import calculate_macd


SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
NY_TZ = ZoneInfo("America/New_York")

BRENT_SYMBOL = "BRN0Y"
BRENT_NAME = "布伦特原油连续"
BRENT_DISPLAY_NAME = "ICE 布伦特原油连续"
BRENT_MARKET = "BRENT"
BRENT_SOURCE = "BRENT_PUBLIC"

_DATE_RE = re.compile(r"^[0-9]{8}\Z")


def _http_get_text(
    url: str,
    *,
    referer: str = "https://finance.sina.com.cn/",
    timeout: float = 8.0,
    encoding: str = "utf-8",
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
        raw = resp.read()
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            return raw.decode("gbk", errors="replace")


def is_brent_market_open(dt_val: datetime | None = None) -> tuple[bool, str]:
    """Return whether ICE Brent continuous trading is active.

    Empirical schedule anchored to US Eastern (America/New_York):
    - Opens Sunday 18:00 ET (Monday 06:00 EDT / 07:00 EST in Asia/Shanghai)
    - Daily settlement/maintenance break Monday-Thursday 18:00-20:00 ET
      (06:00-08:00 CST in summer, 07:00-09:00 CST in winter)
    - Closes Friday 17:00 ET (Saturday 05:00/06:00 CST)
    - Closed Saturday all day and Sunday before 18:00 ET
    """
    current = dt_val if dt_val is not None else datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=SHANGHAI_TZ)
    ny = current.astimezone(NY_TZ)
    wd = ny.weekday()  # Mon=0 .. Sun=6
    minute_of_day = ny.hour * 60 + ny.minute

    if wd == 5:
        return False, "周末休市（ICE 周六休市）"
    if wd == 6:
        if minute_of_day < 18 * 60:
            return False, "周末休市（待美东周日 18:00 开盘）"
        return True, "交易中（ICE 布伦特电子盘）"
    if wd == 4:
        if minute_of_day >= 17 * 60:
            return False, "周末收盘（ICE 周五 17:00 ET 收盘）"
        return True, "交易中（ICE 布伦特电子盘）"
    # Monday - Thursday
    if 18 * 60 <= minute_of_day < 20 * 60:
        return False, "每日结算休市（18:00-20:00 ET）"
    return True, "交易中（ICE 布伦特电子盘）"


def _parse_jsonp_array(text: str) -> list[dict[str, Any]]:
    idx = text.find("([")
    end = text.rfind("])")
    if idx != -1 and end > idx:
        return list(json.loads(text[idx + 1 : end + 1]))
    s_idx = text.find("[")
    e_idx = text.rfind("]")
    if s_idx != -1 and e_idx > s_idx:
        return list(json.loads(text[s_idx : e_idx + 1]))
    return []


def _safe_float(val: object) -> float | None:
    if val is None or val == "":
        return None
    try:
        num = float(str(val).strip().replace(",", ""))
        return num if num == num else None
    except (TypeError, ValueError):
        return None


def _safe_int(val: object, default: int = 0) -> int:
    num = _safe_float(val)
    if num is None:
        return default
    return max(0, int(round(num)))


class BrentDataProvider:
    """Resilient multi-source fetcher for ICE Brent (BRN0Y / hf_OIL / UKOIL.OTC)."""

    WSCN_KLINE_URL = (
        "https://api-ddc-wscn.awtmt.com/market/kline"
        "?prod_code=UKOIL.OTC&tick_count={count}&period_type={period}"
        "&timestamp={timestamp}&fields=tick_at,open_px,close_px,high_px,low_px"
    )
    SINA_MINK_URL = (
        "https://gu.sina.cn/ft/api/jsonp.php/var%20x=/"
        "GlobalService.getMink?symbol=OIL&type={period}"
    )
    SINA_DAILY_URL = (
        "https://stock2.finance.sina.com.cn/futures/api/jsonp.php/var%20x=/"
        "GlobalFuturesService.getGlobalFuturesDailyKLine?symbol=OIL"
    )
    SINA_QUOTE_URL = "https://hq.sinajs.cn/list=hf_OIL"
    TENCENT_QUOTE_URL = "https://qt.gtimg.cn/q=hf_OIL"

    def __init__(
        self,
        *,
        fetch_text: Callable[..., str] | None = None,
        timeout: float = 8.0,
    ) -> None:
        self._fetch_text = fetch_text or _http_get_text
        self.timeout = timeout

    def _get(self, url: str, *, referer: str, encoding: str = "utf-8") -> str:
        return self._fetch_text(url, referer=referer, timeout=self.timeout, encoding=encoding)

    def fetch_daily_history(self, start_date: str = "20260201") -> dict[str, dict[str, Any]]:
        clean_start = start_date.replace("-", "").strip()
        raw = self._get(self.SINA_DAILY_URL, referer="https://finance.sina.com.cn/")
        items = _parse_jsonp_array(raw)
        rows: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, Mapping):
                continue
            d_raw = str(item.get("date") or "").replace("-", "").strip()
            if not _DATE_RE.fullmatch(d_raw):
                continue
            c = _safe_float(item.get("close"))
            if c is None or c <= 0:
                continue
            rows.append(
                {
                    "trade_date": d_raw,
                    "open": _safe_float(item.get("open")) or c,
                    "high": _safe_float(item.get("high")) or c,
                    "low": _safe_float(item.get("low")) or c,
                    "close": c,
                    "volume": _safe_int(item.get("volume"), 0),
                }
            )
        rows.sort(key=lambda r: r["trade_date"])
        out: dict[str, dict[str, Any]] = {}
        prev_close: float | None = None
        for r in rows:
            td = r["trade_date"]
            if td >= clean_start:
                out[td] = {**r, "pre_close": prev_close}
            prev_close = r["close"]
        return out

    def fetch_sina_1m_bars(self) -> list[dict[str, Any]]:
        raw = self._get(
            self.SINA_MINK_URL.format(period=1),
            referer="https://finance.sina.cn/",
        )
        items = _parse_jsonp_array(raw)
        bars: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, Mapping):
                continue
            dt_str = str(item.get("d") or "").strip()
            if len(dt_str) < 16:
                continue
            td = dt_str[:10].replace("-", "")
            hhmm = dt_str[11:16]
            if not (_DATE_RE.fullmatch(td) and len(hhmm) == 5 and hhmm[2] == ":"):
                continue
            c = _safe_float(item.get("c"))
            if c is None or c <= 0:
                continue
            bars.append(
                {
                    "trade_date": td,
                    "minute": hhmm,
                    "open": _safe_float(item.get("o")) or c,
                    "high": _safe_float(item.get("h")) or c,
                    "low": _safe_float(item.get("l")) or c,
                    "close": c,
                    "volume": _safe_int(item.get("v"), 0),
                }
            )
        bars.sort(key=lambda b: (b["trade_date"], b["minute"]))
        return bars

    def fetch_wscn_1m_history(
        self,
        *,
        start_date: str = "20260301",
        end_dt: datetime | None = None,
        max_pages: int = 450,
    ) -> list[dict[str, Any]]:
        clean_start = start_date.replace("-", "").strip()
        start_dt = datetime.strptime(clean_start, "%Y%m%d").replace(tzinfo=SHANGHAI_TZ)
        start_ts = int(start_dt.timestamp())
        end_ref = (end_dt or datetime.now(SHANGHAI_TZ)).astimezone(SHANGHAI_TZ)
        cursor_ts = int(end_ref.timestamp()) + 120

        by_key: dict[tuple[str, str], dict[str, Any]] = {}
        seen_cursors: set[int] = set()
        consecutive_empty = 0
        for _ in range(max_pages):
            if cursor_ts < start_ts or cursor_ts in seen_cursors:
                break
            seen_cursors.add(cursor_ts)
            url = self.WSCN_KLINE_URL.format(count=3000, period=60, timestamp=cursor_ts)
            raw = self._get(url, referer="https://wallstreetcn.com/")
            payload = json.loads(raw)
            data = payload.get("data") if isinstance(payload, Mapping) else None
            if not isinstance(data, Mapping):
                break
            candle = ((data.get("candle") or {}).get("UKOIL.OTC")) or {}
            lines = candle.get("lines") or []
            fields = list(data.get("fields") or ["open_px", "close_px", "high_px", "low_px", "tick_at"])
            if not lines:
                consecutive_empty += 1
                if consecutive_empty > 20:
                    break
                cursor_ts -= 12 * 3600
                continue
            consecutive_empty = 0
            idx_map = {str(name): idx for idx, name in enumerate(fields)}
            ts_idx = idx_map.get("tick_at", len(fields) - 1)
            c_idx = idx_map.get("close_px", 1)
            o_idx = idx_map.get("open_px", 0)
            h_idx = idx_map.get("high_px", 2)
            l_idx = idx_map.get("low_px", 3)

            earliest_ts = None
            for row in lines:
                if not isinstance(row, Sequence) or len(row) <= max(ts_idx, c_idx):
                    continue
                ts_val = int(float(row[ts_idx]))
                if earliest_ts is None or ts_val < earliest_ts:
                    earliest_ts = ts_val
                c_val = _safe_float(row[c_idx])
                if c_val is None or c_val <= 0:
                    continue
                local_dt = datetime.fromtimestamp(ts_val, tz=SHANGHAI_TZ)
                td = local_dt.strftime("%Y%m%d")
                if td < clean_start:
                    continue
                hhmm = local_dt.strftime("%H:%M")
                by_key[(td, hhmm)] = {
                    "trade_date": td,
                    "minute": hhmm,
                    "open": _safe_float(row[o_idx]) if o_idx < len(row) else c_val,
                    "high": _safe_float(row[h_idx]) if h_idx < len(row) else c_val,
                    "low": _safe_float(row[l_idx]) if l_idx < len(row) else c_val,
                    "close": c_val,
                    "volume": 0,
                }
            if earliest_ts is None or earliest_ts <= start_ts:
                break
            next_cursor = earliest_ts - 60
            if next_cursor >= cursor_ts:
                next_cursor = cursor_ts - 12 * 3600
            cursor_ts = next_cursor

        return [by_key[k] for k in sorted(by_key)]

    def fetch_1m_history(
        self,
        *,
        start_date: str = "20260301",
        end_dt: datetime | None = None,
    ) -> list[dict[str, Any]]:
        merged: dict[tuple[str, str], dict[str, Any]] = {}
        try:
            for b in self.fetch_wscn_1m_history(start_date=start_date, end_dt=end_dt):
                merged[(b["trade_date"], b["minute"])] = b
        except Exception:
            pass
        try:
            for b in self.fetch_sina_1m_bars():
                if b["trade_date"] >= start_date.replace("-", "").strip():
                    key = (b["trade_date"], b["minute"])
                    existing = merged.get(key)
                    if existing is None:
                        merged[key] = b
                    else:
                        merged[key] = {**existing, **b, "volume": b["volume"] or existing.get("volume", 0)}
        except Exception:
            pass
        return [merged[k] for k in sorted(merged)]

    def fetch_live_quote(self) -> dict[str, Any]:
        """Fetch live BRN0Y quote + recent 1m bars with Sina primary and Tencent/WSCN fallback."""
        quote: dict[str, Any] | None = None
        for url, encoding, source_tag in (
            (self.SINA_QUOTE_URL, "gbk", "SINA_HF_OIL"),
            (self.TENCENT_QUOTE_URL, "gbk", "TENCENT_HF_OIL"),
        ):
            try:
                raw = self._get(url, referer="https://finance.sina.com.cn/", encoding=encoding)
                m = re.search(r'"([^"]+)"', raw)
                if not m:
                    continue
                parts = m.group(1).split(",")
                if len(parts) < 13:
                    continue
                price = _safe_float(parts[0])
                pre_close = _safe_float(parts[7])
                open_p = _safe_float(parts[8])
                high_p = _safe_float(parts[4])
                low_p = _safe_float(parts[5])
                time_str = str(parts[6] or "").strip()
                date_raw = str(parts[12] or "").replace("-", "").strip()
                vol = _safe_int(parts[14], 0) if len(parts) > 14 else 0
                if price is None or price <= 0 or not _DATE_RE.fullmatch(date_raw):
                    continue
                quote = {
                    "symbol": BRENT_SYMBOL,
                    "name": BRENT_NAME,
                    "trade_date": date_raw,
                    "time": time_str[:5] if len(time_str) >= 5 else datetime.now(SHANGHAI_TZ).strftime("%H:%M"),
                    "clock": time_str,
                    "price": round(price, 3),
                    "pre_close": round(pre_close, 3) if pre_close and pre_close > 0 else None,
                    "open": round(open_p, 3) if open_p and open_p > 0 else round(price, 3),
                    "high": round(high_p, 3) if high_p and high_p > 0 else round(price, 3),
                    "low": round(low_p, 3) if low_p and low_p > 0 else round(price, 3),
                    "volume": vol,
                    "source": source_tag,
                }
                break
            except Exception:
                continue

        recent_bars: list[dict[str, Any]] = []
        try:
            recent_bars = self.fetch_sina_1m_bars()
        except Exception:
            try:
                recent_bars = self.fetch_wscn_1m_history(
                    start_date=(datetime.now(SHANGHAI_TZ) - timedelta(days=2)).strftime("%Y%m%d"),
                    max_pages=2,
                )
            except Exception:
                recent_bars = []

        if quote is None:
            if not recent_bars:
                raise RuntimeError("BRENT_LIVE_QUOTE_UNAVAILABLE")
            last_bar = recent_bars[-1]
            same_day = [b for b in recent_bars if b["trade_date"] == last_bar["trade_date"]]
            prev_day = [b for b in recent_bars if b["trade_date"] < last_bar["trade_date"]]
            pre_close = prev_day[-1]["close"] if prev_day else same_day[0]["open"]
            quote = {
                "symbol": BRENT_SYMBOL,
                "name": BRENT_NAME,
                "trade_date": last_bar["trade_date"],
                "time": last_bar["minute"],
                "clock": f"{last_bar['minute']}:00",
                "price": round(float(last_bar["close"]), 3),
                "pre_close": round(float(pre_close), 3) if pre_close else None,
                "open": round(float(same_day[0]["open"]), 3),
                "high": round(max(float(b["high"]) for b in same_day), 3),
                "low": round(min(float(b["low"]) for b in same_day), 3),
                "volume": sum(int(b.get("volume") or 0) for b in same_day),
                "source": "BRENT_BARS_FALLBACK",
            }
        return {"quote": quote, "bars": recent_bars}


def _group_brent_bars_by_day(
    bars: Sequence[Mapping[str, Any]],
    daily_map: Mapping[str, Mapping[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    """Group minute bars by Shanghai trade_date with cumulative intraday average_price & pre_close."""
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for b in bars:
        td = str(b.get("trade_date") or "")
        if _DATE_RE.fullmatch(td):
            grouped.setdefault(td, []).append(b)

    day_summaries: list[dict[str, Any]] = []
    day_points: dict[str, list[dict[str, Any]]] = {}
    prev_day_close: float | None = None
    for td in sorted(grouped):
        day_bars = sorted(grouped[td], key=lambda x: str(x.get("minute") or ""))
        d_meta = (daily_map or {}).get(td) or {}
        pre = _safe_float(d_meta.get("pre_close")) or prev_day_close
        open_p = _safe_float(d_meta.get("open")) or float(day_bars[0]["open"])
        high_p = max(float(x["high"]) for x in day_bars)
        low_p = min(float(x["low"]) for x in day_bars)
        close_p = float(day_bars[-1]["close"])
        total_vol = _safe_int(d_meta.get("volume"), 0) or sum(int(x.get("volume") or 0) for x in day_bars)
        prev_day_close = close_p

        cum_pv = 0.0
        cum_v = 0
        cum_sum_p = 0.0
        pts: list[dict[str, Any]] = []
        for idx, b in enumerate(day_bars):
            p = float(b["close"])
            v = int(b.get("volume") or 0)
            cum_sum_p += p
            if v > 0:
                cum_pv += p * v
                cum_v += v
            avg_p = (cum_pv / cum_v) if cum_v > 0 else (cum_sum_p / (idx + 1))
            pts.append(
                {
                    "minute": str(b["minute"]),
                    "price": round(p, 3),
                    "average_price": round(avg_p, 4),
                    "volume": v,
                }
            )
        day_points[td] = pts
        day_summaries.append(
            {
                "trade_date": td,
                "pre_close": round(pre, 3) if pre and pre > 0 else None,
                "open": round(open_p, 3),
                "high": round(high_p, 3),
                "low": round(low_p, 3),
                "close": round(close_p, 3),
                "volume": total_vol,
                "point_count": len(pts),
            }
        )
    return day_summaries, day_points


def backfill_brent_to_research_store(
    store: Any,
    *,
    start_date: str = "20260301",
    end_date: str | None = None,
    provider: BrentDataProvider | None = None,
) -> dict[str, Any]:
    """Backfill BRN0Y historical 1m + daily data into SQLiteResearchStore."""
    prov = provider or BrentDataProvider()
    clean_start = start_date.replace("-", "").strip() or "20260301"
    clean_end = end_date.replace("-", "").strip() if end_date else None
    daily_map: dict[str, dict[str, Any]] = {}
    try:
        daily_map = prov.fetch_daily_history(start_date="20260201")
    except Exception:
        daily_map = {}

    bars = prov.fetch_1m_history(start_date=clean_start)
    if clean_end:
        bars = [b for b in bars if b["trade_date"] <= clean_end]
    day_summaries, day_points = _group_brent_bars_by_day(bars, daily_map)

    captured_at = datetime.now(SHANGHAI_TZ).isoformat(timespec="seconds")
    day_rows: list[tuple[Any, ...]] = []
    pt_rows: list[tuple[Any, ...]] = []
    for ds in day_summaries:
        td = ds["trade_date"]
        pre = ds["pre_close"]
        close_p = ds["close"]
        chg_pct = (
            f"{((close_p - pre) / pre * 100.0):+.2f}%"
            if pre and pre > 0
            else None
        )
        day_rows.append(
            (
                BRENT_SYMBOL,
                td,
                BRENT_NAME,
                BRENT_MARKET,
                BRENT_SOURCE,
                f"{pre:.3f}" if pre else None,
                f"{close_p:.3f}",
                chg_pct,
                f"{ds['open']:.3f}",
                f"{ds['high']:.3f}",
                f"{ds['low']:.3f}",
                str(ds["volume"]),
                captured_at,
            )
        )
        for pt in day_points[td]:
            pt_rows.append(
                (
                    BRENT_SYMBOL,
                    td,
                    pt["minute"],
                    f"{pt['price']:.3f}",
                    f"{pt['average_price']:.4f}",
                    str(pt["volume"]),
                    captured_at,
                )
            )

    if day_rows or pt_rows:
        with store._lock, store._connection:
            if day_rows:
                store._connection.executemany(
                    """INSERT INTO research_days(
                           symbol,trade_date,name,market,source,pre_close,
                           quote_price,quote_change_percent,quote_open,quote_high,
                           quote_low,quote_volume,collected_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(symbol,trade_date) DO UPDATE SET
                           name=excluded.name,
                           market=excluded.market,
                           source=excluded.source,
                           pre_close=COALESCE(excluded.pre_close, research_days.pre_close),
                           quote_price=excluded.quote_price,
                           quote_change_percent=COALESCE(excluded.quote_change_percent, research_days.quote_change_percent),
                           quote_open=COALESCE(excluded.quote_open, research_days.quote_open),
                           quote_high=COALESCE(excluded.quote_high, research_days.quote_high),
                           quote_low=COALESCE(excluded.quote_low, research_days.quote_low),
                           quote_volume=COALESCE(NULLIF(excluded.quote_volume, '0'), research_days.quote_volume),
                           collected_at=excluded.collected_at""",
                    day_rows,
                )
            if pt_rows:
                store._connection.executemany(
                    """INSERT INTO research_points(
                           symbol,trade_date,minute,price,average_price,volume,captured_at
                       ) VALUES(?,?,?,?,?,?,?)
                       ON CONFLICT(symbol,trade_date,minute) DO UPDATE SET
                           price=excluded.price,
                           average_price=excluded.average_price,
                           volume=CASE WHEN CAST(COALESCE(excluded.volume,'0') AS INTEGER) > 0
                                       THEN excluded.volume ELSE research_points.volume END,
                           captured_at=excluded.captured_at""",
                    pt_rows,
                )
            if day_summaries:
                last_ds = day_summaries[-1]
                last_pt = day_points[last_ds["trade_date"]][-1]
                store._connection.execute(
                    """INSERT INTO research_monitor_status(
                           symbol,latest_trade_date,latest_time,last_sync_at,last_error
                       ) VALUES(?,?,?,?,NULL)
                       ON CONFLICT(symbol) DO UPDATE SET
                           latest_trade_date=excluded.latest_trade_date,
                           latest_time=excluded.latest_time,
                           last_sync_at=excluded.last_sync_at,
                           last_error=NULL""",
                    (BRENT_SYMBOL, last_ds["trade_date"], last_pt["minute"], captured_at),
                )

    return {
        "symbol": BRENT_SYMBOL,
        "opm_imported": {"days": 0, "points": 0},
        "tdx_days_backfilled": len(day_rows),
        "tdx_points_written": len(pt_rows),
        "line_funds": {"updated_minutes": 0, "reason": "brent_global_futures"},
        "coverage": {
            "symbol": BRENT_SYMBOL,
            "start_date": clean_start,
            "first_date": day_summaries[0]["trade_date"] if day_summaries else None,
            "last_date": day_summaries[-1]["trade_date"] if day_summaries else None,
            "total_days": len(day_summaries),
            "flow_days": 0,
            "price_only_days": len(day_summaries),
            "total_points": len(pt_rows),
            "flow_first_date": None,
            "flow_last_date": None,
        },
    }


def backfill_brent_to_opm_sqlite(
    opm_db_path: Path,
    *,
    start_date: str = "20260301",
    end_date: str | None = None,
    macd_settings: Mapping[str, Any] | None = None,
    provider: BrentDataProvider | None = None,
) -> dict[str, Any]:
    """Backfill BRN0Y historical 1m + trade_days + watchlist into OPM quotes.sqlite3."""
    resolved = Path(opm_db_path).expanduser().resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"OPM database not found: {resolved}")

    prov = provider or BrentDataProvider()
    clean_start = start_date.replace("-", "").strip() or "20260301"
    clean_end = end_date.replace("-", "").strip() if end_date else None
    daily_map: dict[str, dict[str, Any]] = {}
    try:
        daily_map = prov.fetch_daily_history(start_date="20260201")
    except Exception:
        daily_map = {}

    bars = prov.fetch_1m_history(start_date=clean_start)
    if clean_end:
        bars = [b for b in bars if b["trade_date"] <= clean_end]
    day_summaries, day_points = _group_brent_bars_by_day(bars, daily_map)
    if not day_summaries:
        return {"symbol": BRENT_SYMBOL, "days": 0, "points": 0}

    eff_macd = {
        "short": int((macd_settings or {}).get("short") or 10),
        "long": int((macd_settings or {}).get("long") or 20),
        "signal": int((macd_settings or {}).get("signal") or 5),
    }
    captured_at = datetime.now(SHANGHAI_TZ).isoformat(timespec="seconds")
    trade_time_json = json.dumps([["00:00", "06:59"], ["09:00", "23:59"]], ensure_ascii=False)

    day_params: list[tuple[Any, ...]] = []
    pt_params: list[tuple[Any, ...]] = []
    for ds in day_summaries:
        td = ds["trade_date"]
        pts = day_points[td]
        macd_rows = calculate_macd([float(p["price"]) for p in pts], eff_macd)
        day_params.append(
            (
                BRENT_SYMBOL,
                td,
                BRENT_NAME,
                float(ds["pre_close"] or ds["open"] or 0.0),
                1,
                trade_time_json,
                "{}",
                captured_at,
                len(pts),
            )
        )
        for idx, pt in enumerate(pts):
            pt_params.append(
                (
                    BRENT_SYMBOL,
                    td,
                    pt["minute"],
                    float(pt["price"]),
                    float(pt["average_price"]),
                    0.0,
                    int(pt["volume"]),
                    float(macd_rows[idx]["diff"]),
                    float(macd_rows[idx]["dea"]),
                    float(macd_rows[idx]["macd"]),
                    None,
                    None,
                    None,
                    td,
                    captured_at,
                )
            )

    last_ds = day_summaries[-1]
    last_pt = day_points[last_ds["trade_date"]][-1]
    conn = sqlite3.connect(resolved, timeout=15.0)
    try:
        conn.execute("PRAGMA busy_timeout = 15000")
        with conn:
            conn.execute(
                """INSERT INTO watchlist(
                       code, name, active, created_at, updated_at,
                       latest_trade_date, latest_time, last_sync_at, last_error, last_point_count
                   ) VALUES(?, ?, 1, ?, ?, ?, ?, ?, NULL, ?)
                   ON CONFLICT(code) DO UPDATE SET
                       name=excluded.name,
                       active=1,
                       updated_at=excluded.updated_at,
                       latest_trade_date=excluded.latest_trade_date,
                       latest_time=excluded.latest_time,
                       last_sync_at=excluded.last_sync_at,
                       last_error=NULL,
                       last_point_count=excluded.last_point_count""",
                (
                    BRENT_SYMBOL,
                    BRENT_NAME,
                    captured_at,
                    captured_at,
                    last_ds["trade_date"],
                    last_pt["minute"],
                    captured_at,
                    last_ds["point_count"],
                ),
            )
            conn.executemany(
                """INSERT INTO trade_days(
                       code, trade_date, name, pre_close, is_trading,
                       trade_time_json, line_diff_json, captured_at, point_count
                   ) VALUES(?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(code, trade_date) DO UPDATE SET
                       name=excluded.name,
                       pre_close=excluded.pre_close,
                       is_trading=excluded.is_trading,
                       trade_time_json=excluded.trade_time_json,
                       captured_at=excluded.captured_at,
                       point_count=excluded.point_count""",
                day_params,
            )
            conn.executemany(
                """INSERT INTO minute_quotes(
                       code, trade_date, minute, price, avg_price, amount_yuan, volume_shares,
                       diff, dea, macd, big_net_wan, mid_net_wan, small_net_wan, source_date, captured_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(code, trade_date, minute) DO UPDATE SET
                       price=excluded.price,
                       avg_price=excluded.avg_price,
                       volume_shares=CASE WHEN excluded.volume_shares > 0 THEN excluded.volume_shares ELSE minute_quotes.volume_shares END,
                       diff=excluded.diff,
                       dea=excluded.dea,
                       macd=excluded.macd,
                       captured_at=excluded.captured_at""",
                pt_params,
            )
    finally:
        conn.close()

    return {
        "symbol": BRENT_SYMBOL,
        "days": len(day_params),
        "points": len(pt_params),
        "first_date": day_summaries[0]["trade_date"],
        "last_date": day_summaries[-1]["trade_date"],
    }


def upsert_brent_live_to_research_store(
    store: Any,
    quote: Mapping[str, Any],
    recent_bars: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Upsert latest live quote & recent 1m bars into SQLiteResearchStore."""
    td = str(quote.get("trade_date") or datetime.now(SHANGHAI_TZ).strftime("%Y%m%d"))
    price = float(quote["price"])
    pre_close = _safe_float(quote.get("pre_close"))
    hhmm = str(quote.get("time") or datetime.now(SHANGHAI_TZ).strftime("%H:%M"))
    captured_at = datetime.now(SHANGHAI_TZ).isoformat(timespec="seconds")

    # Group recent bars by trade_date (can span yesterday late-night + today)
    bars_by_day: dict[str, list[dict[str, Any]]] = {}
    for b in recent_bars:
        b_td = str(b.get("trade_date") or "")
        if _DATE_RE.fullmatch(b_td):
            bars_by_day.setdefault(b_td, []).append(dict(b))
    today_list = bars_by_day.setdefault(td, [])
    if not any(b.get("minute") == hhmm for b in today_list):
        today_list.append(
            {
                "trade_date": td,
                "minute": hhmm,
                "open": price,
                "high": price,
                "low": price,
                "close": price,
                "volume": int(quote.get("volume") or 0),
            }
        )

    day_summaries, day_points = _group_brent_bars_by_day(
        [b for lst in bars_by_day.values() for b in lst],
        {
            td: {
                "pre_close": pre_close,
                "open": quote.get("open"),
                "high": quote.get("high"),
                "low": quote.get("low"),
                "close": price,
                "volume": quote.get("volume"),
            }
        },
    )

    with store._lock, store._connection:
        for ds in day_summaries:
            d_td = ds["trade_date"]
            d_pre = pre_close if d_td == td and pre_close else ds["pre_close"]
            d_close = price if d_td == td else ds["close"]
            chg_pct = (
                f"{((d_close - d_pre) / d_pre * 100.0):+.2f}%"
                if d_pre and d_pre > 0
                else None
            )
            store._connection.execute(
                """INSERT INTO research_days(
                       symbol,trade_date,name,market,source,pre_close,
                       quote_price,quote_change_percent,quote_open,quote_high,
                       quote_low,quote_volume,collected_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(symbol,trade_date) DO UPDATE SET
                       pre_close=COALESCE(excluded.pre_close, research_days.pre_close),
                       quote_price=excluded.quote_price,
                       quote_change_percent=COALESCE(excluded.quote_change_percent, research_days.quote_change_percent),
                       quote_open=COALESCE(excluded.quote_open, research_days.quote_open),
                       quote_high=COALESCE(excluded.quote_high, research_days.quote_high),
                       quote_low=COALESCE(excluded.quote_low, research_days.quote_low),
                       quote_volume=COALESCE(NULLIF(excluded.quote_volume,'0'), research_days.quote_volume),
                       collected_at=excluded.collected_at""",
                (
                    BRENT_SYMBOL,
                    d_td,
                    BRENT_NAME,
                    BRENT_MARKET,
                    str(quote.get("source") or BRENT_SOURCE),
                    f"{d_pre:.3f}" if d_pre else None,
                    f"{d_close:.3f}",
                    chg_pct,
                    f"{(quote.get('open') if d_td == td and quote.get('open') else ds['open']):.3f}",
                    f"{(quote.get('high') if d_td == td and quote.get('high') else ds['high']):.3f}",
                    f"{(quote.get('low') if d_td == td and quote.get('low') else ds['low']):.3f}",
                    str(quote.get("volume") if d_td == td and quote.get("volume") else ds["volume"]),
                    captured_at,
                ),
            )
            pt_rows = [
                (
                    BRENT_SYMBOL,
                    d_td,
                    pt["minute"],
                    f"{pt['price']:.3f}",
                    f"{pt['average_price']:.4f}",
                    str(pt["volume"]),
                    captured_at,
                )
                for pt in day_points[d_td]
            ]
            if pt_rows:
                store._connection.executemany(
                    """INSERT INTO research_points(
                           symbol,trade_date,minute,price,average_price,volume,captured_at
                       ) VALUES(?,?,?,?,?,?,?)
                       ON CONFLICT(symbol,trade_date,minute) DO UPDATE SET
                           price=excluded.price,
                           average_price=excluded.average_price,
                           volume=CASE WHEN CAST(COALESCE(excluded.volume,'0') AS INTEGER) > 0
                                       THEN excluded.volume ELSE research_points.volume END,
                           captured_at=excluded.captured_at""",
                    pt_rows,
                )
        store._connection.execute(
            """INSERT INTO research_monitor_status(
                   symbol,latest_trade_date,latest_time,last_sync_at,last_error
               ) VALUES(?,?,?,?,NULL)
               ON CONFLICT(symbol) DO UPDATE SET
                   latest_trade_date=excluded.latest_trade_date,
                   latest_time=excluded.latest_time,
                   last_sync_at=excluded.last_sync_at,
                   last_error=NULL""",
            (BRENT_SYMBOL, td, hhmm, captured_at),
        )
    return {"trade_date": td, "time": hhmm, "price": price}


def backfill_oil_funds_to_research_store(
    research_store: Any,
    *,
    codes: Sequence[str] = PREMIUM_CODES,
    start_date: str = "20260301",
    include_intraday: bool = True,
    fetch_text: Callable[..., str] | None = None,
) -> dict[str, Any]:
    """Backfill daily klines and latest intraday points for oil QDII/LOF funds via Tencent public feeds."""
    getter = fetch_text or _http_get_text
    clean_start = str(start_date or "20260301").replace("-", "").strip() or "20260301"
    captured_at = datetime.now(SHANGHAI_TZ).isoformat(timespec="seconds")
    summary: dict[str, Any] = {}

    for raw_code in codes:
        code = str(raw_code).strip()
        if not re.fullmatch(r"\d{6}", code):
            continue
        mkt_prefix = "sh" if code.startswith(("5", "6", "9")) else "sz"
        mkt_upper = "SH" if mkt_prefix == "sh" else "SZ"
        fq_sym = f"{mkt_prefix}{code}"
        fund_name = PREMIUM_NAMES.get(code, code)

        day_rows: list[tuple[Any, ...]] = []
        pt_rows: list[tuple[Any, ...]] = []
        try:
            kline_url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={fq_sym},day,,,260,qfq"
            k_payload = json.loads(getter(kline_url, referer="https://gu.qq.com/", timeout=8.0))
            k_data = ((k_payload.get("data") or {}).get(fq_sym)) or {}
            raw_days = k_data.get("qfqday") or k_data.get("day") or []
            parsed_bars: list[dict[str, Any]] = []
            for row in raw_days:
                if not isinstance(row, Sequence) or len(row) < 6:
                    continue
                td = str(row[0]).replace("-", "").strip()
                if not _DATE_RE.fullmatch(td):
                    continue
                c = _safe_float(row[2])
                if c is None or c <= 0:
                    continue
                parsed_bars.append(
                    {
                        "trade_date": td,
                        "open": _safe_float(row[1]) or c,
                        "close": c,
                        "high": _safe_float(row[3]) or c,
                        "low": _safe_float(row[4]) or c,
                        "volume": int(_safe_float(row[5]) or 0),
                    }
                )
            parsed_bars.sort(key=lambda r: r["trade_date"])
            prev_c: float | None = None
            for bar in parsed_bars:
                td = bar["trade_date"]
                if td >= clean_start:
                    c = bar["close"]
                    pre = prev_c if prev_c and prev_c > 0 else bar["open"]
                    chg = ((c - pre) / pre * 100.0) if pre and pre > 0 else 0.0
                    day_rows.append(
                        (
                            code,
                            td,
                            fund_name,
                            mkt_upper,
                            "TENCENT_PUBLIC",
                            f"{pre:.3f}" if pre else None,
                            f"{c:.3f}",
                            f"{chg:+.2f}%",
                            f"{bar['open']:.3f}",
                            f"{bar['high']:.3f}",
                            f"{bar['low']:.3f}",
                            str(bar["volume"]),
                            captured_at,
                        )
                    )
                prev_c = bar["close"]
        except Exception:
            pass

        if include_intraday:
            try:
                min_url = f"https://ifzq.gtimg.cn/appstock/app/minute/query?code={fq_sym}"
                m_payload = json.loads(getter(min_url, referer="https://gu.qq.com/", timeout=8.0))
                m_block = (((m_payload.get("data") or {}).get(fq_sym)) or {}).get("data") or {}
                m_date = str(m_block.get("date") or "").replace("-", "").strip()
                m_lines = m_block.get("data") or []
                if _DATE_RE.fullmatch(m_date) and m_date >= clean_start and m_lines:
                    prev_lots = 0
                    cum_shares = 0
                    cum_amt = 0.0
                    for line in m_lines:
                        parts = str(line).strip().split()
                        if len(parts) < 3:
                            continue
                        hhmm_raw = parts[0]
                        if len(hhmm_raw) != 4 or not hhmm_raw.isdigit():
                            continue
                        hhmm = f"{hhmm_raw[:2]}:{hhmm_raw[2:]}"
                        if hhmm < "09:30" or hhmm > "15:00":
                            continue
                        p = _safe_float(parts[1])
                        if p is None or p <= 0:
                            continue
                        cum_lots = int(float(parts[2] or 0))
                        delta_lots = max(0, cum_lots - prev_lots)
                        prev_lots = cum_lots
                        vol_shares = delta_lots * 100
                        cum_shares += vol_shares
                        cum_amt += p * vol_shares
                        avg_p = (cum_amt / cum_shares) if cum_shares > 0 else p
                        pt_rows.append(
                            (
                                code,
                                m_date,
                                hhmm,
                                f"{p:.3f}",
                                f"{avg_p:.4f}",
                                str(vol_shares),
                                captured_at,
                            )
                        )
            except Exception:
                pass

        if day_rows or pt_rows:
            with research_store._lock, research_store._connection:
                if day_rows:
                    research_store._connection.executemany(
                        """INSERT INTO research_days(
                               symbol,trade_date,name,market,source,pre_close,
                               quote_price,quote_change_percent,quote_open,quote_high,
                               quote_low,quote_volume,collected_at
                           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                           ON CONFLICT(symbol,trade_date) DO UPDATE SET
                               name=excluded.name,
                               market=excluded.market,
                               source=CASE WHEN research_days.source IN ('test','mock','APP') THEN research_days.source ELSE excluded.source END,
                               pre_close=COALESCE(research_days.pre_close, excluded.pre_close),
                               quote_price=COALESCE(excluded.quote_price, research_days.quote_price),
                               quote_change_percent=COALESCE(excluded.quote_change_percent, research_days.quote_change_percent),
                               quote_open=COALESCE(excluded.quote_open, research_days.quote_open),
                               quote_high=COALESCE(excluded.quote_high, research_days.quote_high),
                               quote_low=COALESCE(excluded.quote_low, research_days.quote_low),
                               quote_volume=COALESCE(research_days.quote_volume, excluded.quote_volume),
                               collected_at=excluded.collected_at""",
                        day_rows,
                    )
                if pt_rows:
                    research_store._connection.executemany(
                        """INSERT INTO research_points(
                               symbol,trade_date,minute,price,average_price,volume,captured_at
                           ) VALUES(?,?,?,?,?,?,?)
                           ON CONFLICT(symbol,trade_date,minute) DO UPDATE SET
                               price=excluded.price,
                               average_price=COALESCE(research_points.average_price, excluded.average_price),
                               volume=CASE WHEN CAST(COALESCE(excluded.volume,'0') AS INTEGER) > 0
                                           THEN excluded.volume ELSE research_points.volume END,
                               captured_at=excluded.captured_at""",
                        pt_rows,
                    )
        summary[code] = {"days": len(day_rows), "intraday_points": len(pt_rows)}
    return summary


class BrentMarketMonitor:
    """Background monitor collecting BRN0Y quotes & 1m bars whenever ICE Brent is open."""

    def __init__(
        self,
        research_store: Any,
        *,
        provider: BrentDataProvider | None = None,
        opm_db_path: Path | None = None,
        auto_backfill: bool = True,
    ) -> None:
        self.research_store = research_store
        self.provider = provider or BrentDataProvider()
        self.opm_db_path = opm_db_path
        self.auto_backfill = auto_backfill
        self._lock = RLock()
        self._latest_live: dict[str, Any] | None = None
        self._backfill_checked = False
        self._last_funds_sync_ts = 0.0

    def latest_live_quote(self) -> dict[str, Any] | None:
        with self._lock:
            return dict(self._latest_live) if self._latest_live is not None else None

    def _ensure_history_if_sparse(self) -> None:
        if not self.auto_backfill or self._backfill_checked:
            return
        with self.research_store._lock:
            row = self.research_store._connection.execute(
                "SELECT COUNT(*) FROM research_days WHERE symbol=? AND trade_date>='20260701'",
                (BRENT_SYMBOL,),
            ).fetchone()
            cnt = int(row[0]) if row else 0
            fund_row = self.research_store._connection.execute(
                "SELECT COUNT(DISTINCT symbol) FROM research_days WHERE symbol IN ({}) AND trade_date>='20260701'".format(
                    ",".join("?" for _ in PREMIUM_CODES)
                ),
                PREMIUM_CODES,
            ).fetchone()
            fund_cnt = int(fund_row[0]) if fund_row else 0
            min_days_row = self.research_store._connection.execute(
                "SELECT MIN(c) FROM (SELECT COUNT(*) AS c FROM research_days WHERE symbol IN ({}) AND trade_date>='20260701' GROUP BY symbol)".format(
                    ",".join("?" for _ in PREMIUM_CODES)
                ),
                PREMIUM_CODES,
            ).fetchone()
            min_fund_days = int(min_days_row[0] or 0) if min_days_row else 0
        self._backfill_checked = True
        if cnt < 80:
            try:
                backfill_brent_to_research_store(
                    self.research_store,
                    start_date="20260301",
                    provider=self.provider,
                )
            except Exception:
                self._backfill_checked = False
        if fund_cnt < len(PREMIUM_CODES) or min_fund_days < 80:
            try:
                backfill_oil_funds_to_research_store(
                    self.research_store,
                    start_date="20260301",
                    include_intraday=True,
                )
            except Exception:
                pass

    def poll_sync(self, now: datetime | None = None, *, force: bool = False) -> dict[str, Any]:
        self._ensure_history_if_sparse()
        import time as _time

        now_local = (now or datetime.now(timezone.utc)).astimezone(SHANGHAI_TZ)
        hhmm_local = now_local.strftime("%H:%M")
        if (
            self.auto_backfill
            and now_local.weekday() < 5
            and ("09:20" <= hhmm_local <= "15:15")
            and (_time.monotonic() - self._last_funds_sync_ts) >= 90.0
        ):
            self._last_funds_sync_ts = _time.monotonic()
            try:
                backfill_oil_funds_to_research_store(
                    self.research_store,
                    start_date=now_local.strftime("%Y%m%d"),
                    include_intraday=True,
                )
            except Exception:
                pass
        is_open, session_label = is_brent_market_open(now)
        if not force and not is_open:
            return {"collected": False, "market_open": False, "session_label": session_label}
        live = self.provider.fetch_live_quote()
        quote = live["quote"]
        bars = live["bars"]
        upsert_brent_live_to_research_store(self.research_store, quote, bars)
        if self.opm_db_path is not None and self.opm_db_path.is_file():
            try:
                backfill_brent_to_opm_sqlite(
                    self.opm_db_path,
                    start_date=quote["trade_date"],
                    provider=self.provider,
                )
            except Exception:
                pass
        snap = {
            **quote,
            "market_open": is_open,
            "session_label": session_label,
            "updated_at": datetime.now(SHANGHAI_TZ).isoformat(timespec="seconds"),
        }
        with self._lock:
            self._latest_live = snap
        return {"collected": True, "market_open": is_open, "session_label": session_label, "quote": snap}

    async def poll_once(self, now: datetime | None = None, *, force: bool = False) -> dict[str, Any]:
        return await asyncio.to_thread(self.poll_sync, now, force=force)


def _downsample_points(points: Sequence[dict[str, Any]], max_count: int) -> list[dict[str, Any]]:
    n = len(points)
    if n <= max_count:
        return list(points)
    step = max(1, (n + max_count - 1) // max_count)
    out = [points[i] for i in range(0, n, step)]
    if out[-1] is not points[-1]:
        out.append(points[-1])
    return out


def build_brent_premium_panel(
    research_store: Any,
    brent_monitor: BrentMarketMonitor | None = None,
    *,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Build BRN0Y live status + 24h/5d/July+ series for /api/internal/goodluck/premium."""
    if research_store is None:
        return None
    local_now = (now or datetime.now(timezone.utc)).astimezone(SHANGHAI_TZ)
    is_open, session_label = is_brent_market_open(local_now)
    live = brent_monitor.latest_live_quote() if brent_monitor is not None else None

    with research_store._lock:
        day_rows = research_store._connection.execute(
            """SELECT trade_date, pre_close, quote_open, quote_high, quote_low,
                      quote_price, quote_change_percent, collected_at
               FROM research_days
               WHERE symbol=?
               ORDER BY trade_date ASC""",
            (BRENT_SYMBOL,),
        ).fetchall()
        if not day_rows and live is None:
            return None

        recent_days = [str(r["trade_date"]) for r in day_rows[-7:]]
        pt_rows: list[sqlite3.Row] = []
        if recent_days:
            placeholders = ",".join("?" for _ in recent_days)
            pt_rows = research_store._connection.execute(
                f"""SELECT trade_date, minute, price, average_price
                    FROM research_points
                    WHERE symbol=? AND trade_date IN ({placeholders})
                    ORDER BY trade_date ASC, REPLACE(minute,':','') ASC""",
                (BRENT_SYMBOL, *recent_days),
            ).fetchall()

        # Load daily + recent intraday data across all 9 oil QDII/LOF funds
        fund_placeholders = ",".join("?" for _ in PREMIUM_CODES)
        all_fund_days = research_store._connection.execute(
            f"""SELECT symbol, trade_date, pre_close, quote_price
                FROM research_days
                WHERE symbol IN ({fund_placeholders}) AND trade_date >= '20260701'
                ORDER BY symbol ASC, trade_date ASC""",
            PREMIUM_CODES,
        ).fetchall()
        lof_days = [r for r in all_fund_days if str(r["symbol"]) == "160723"]
        fund_pt_rows: list[sqlite3.Row] = []
        if recent_days:
            recent_ph = ",".join("?" for _ in recent_days[-3:])
            fund_pt_rows = research_store._connection.execute(
                f"""SELECT symbol, trade_date, minute, price
                    FROM research_points
                    WHERE symbol IN ({fund_placeholders}) AND trade_date IN ({recent_ph})
                    ORDER BY symbol ASC, trade_date ASC, REPLACE(minute,':','') ASC""",
                (*PREMIUM_CODES, *recent_days[-3:]),
            ).fetchall()
        brn_1500_rows = research_store._connection.execute(
            """SELECT trade_date, minute, price
               FROM research_points
               WHERE symbol=? AND trade_date >= '20260701' AND REPLACE(minute,':','') <= '1500'
               ORDER BY trade_date ASC, REPLACE(minute,':','') ASC""",
            (BRENT_SYMBOL,),
        ).fetchall()

    # Determine A-share 15:00 reference date
    today_str = local_now.strftime("%Y%m%d")
    hhmm_now = local_now.strftime("%H:%M")
    brn_1500_by_day: dict[str, tuple[str, float]] = {}
    for r in brn_1500_rows:
        p = _safe_float(r["price"])
        if p and p > 0:
            brn_1500_by_day[str(r["trade_date"])] = (str(r["minute"]), p)

    lof_dates = [str(r["trade_date"]) for r in lof_days]
    if today_str in lof_dates and hhmm_now < "15:00":
        anchor_candidates = [d for d in lof_dates if d < today_str and d in brn_1500_by_day]
    else:
        anchor_candidates = [d for d in lof_dates if d <= today_str and d in brn_1500_by_day]
    if not anchor_candidates:
        anchor_candidates = sorted(brn_1500_by_day.keys())

    ref_1500_date = anchor_candidates[-1] if anchor_candidates else None
    ref_1500_price = brn_1500_by_day[ref_1500_date][1] if ref_1500_date else None

    latest_day = day_rows[-1] if day_rows else None
    last_pt_price = _safe_float(pt_rows[-1]["price"]) if pt_rows else None
    cur_price = (
        _safe_float((live or {}).get("price"))
        or (_safe_float(latest_day["quote_price"]) if latest_day else None)
        or last_pt_price
    )
    pre_close = (
        _safe_float((live or {}).get("pre_close"))
        or (_safe_float(latest_day["pre_close"]) if latest_day else None)
    )
    open_p = _safe_float((live or {}).get("open")) or (_safe_float(latest_day["quote_open"]) if latest_day else None)
    high_p = _safe_float((live or {}).get("high")) or (_safe_float(latest_day["quote_high"]) if latest_day else None)
    low_p = _safe_float((live or {}).get("low")) or (_safe_float(latest_day["quote_low"]) if latest_day else None)

    change = round(cur_price - pre_close, 3) if cur_price is not None and pre_close else None
    change_pct = round((cur_price - pre_close) / pre_close * 100.0, 2) if cur_price is not None and pre_close and pre_close > 0 else None
    since_1500_pct = (
        round((cur_price - ref_1500_price) / ref_1500_price * 100.0, 2)
        if cur_price is not None and ref_1500_price and ref_1500_price > 0
        else None
    )

    # Build 24h intraday (last 2 trading days up to 280 sampled points) and 5d series
    all_recent_pts: list[dict[str, Any]] = []
    for r in pt_rows:
        td = str(r["trade_date"])
        mm = str(r["minute"])
        p = _safe_float(r["price"])
        avg = _safe_float(r["average_price"])
        if p is None or p <= 0:
            continue
        compact = mm.replace(":", "")
        in_ashare = ("0930" <= compact <= "1130") or ("1300" <= compact <= "1500")
        all_recent_pts.append(
            {
                "trade_date": td,
                "minute": mm,
                "time": f"{td[4:6]}-{td[6:8]} {mm}",
                "price": round(p, 3),
                "avg_price": round(avg, 3) if avg else None,
                "in_ashare_session": in_ashare,
            }
        )

    pts_24h_raw = [p for p in all_recent_pts if p["trade_date"] in set(recent_days[-2:])]
    if len(pts_24h_raw) > 1440:
        pts_24h_raw = pts_24h_raw[-1440:]
    intraday_24h = _downsample_points(pts_24h_raw, 240)

    pts_5d_raw = [p for p in all_recent_pts if p["trade_date"] in set(recent_days[-5:])]
    series_5d = _downsample_points(
        [
            {
                "trade_date": p["trade_date"],
                "time": p["time"],
                "price": p["price"],
            }
            for p in pts_5d_raw
        ],
        220,
    )

    # July+ cumulative comparison: BRN0Y vs 160723
    brn_day_close: dict[str, float] = {}
    for r in day_rows:
        td = str(r["trade_date"])
        if td >= "20260701":
            val = brn_1500_by_day.get(td, (None, None))[1] or _safe_float(r["quote_price"])
            if val and val > 0:
                brn_day_close[td] = val
    lof_day_close: dict[str, float] = {}
    for r in lof_days:
        val = _safe_float(r["quote_price"])
        if val and val > 0:
            lof_day_close[str(r["trade_date"])] = val

    common_dates = sorted(set(brn_day_close) & set(lof_day_close))
    if not common_dates:
        common_dates = sorted(brn_day_close)
    daily_july: list[dict[str, Any]] = []
    if common_dates:
        base_brn = brn_day_close[common_dates[0]]
        base_lof = lof_day_close.get(common_dates[0])
        for td in common_dates:
            b_val = brn_day_close[td]
            l_val = lof_day_close.get(td)
            daily_july.append(
                {
                    "trade_date": td,
                    "label": f"{td[4:6]}-{td[6:8]}",
                    "brn_close": round(b_val, 3),
                    "brn_cum_pct": round((b_val - base_brn) / base_brn * 100.0, 2) if base_brn > 0 else 0.0,
                    "lof_close": round(l_val, 3) if l_val else None,
                    "lof_cum_pct": round((l_val - base_lof) / base_lof * 100.0, 2)
                    if l_val and base_lof and base_lof > 0
                    else None,
                }
            )

    # Build per-fund July+ & 24h overlay series keyed by 6-digit fund code
    fund_days_map: dict[str, dict[str, dict[str, float | None]]] = {}
    for r in all_fund_days:
        sym = str(r["symbol"])
        td = str(r["trade_date"])
        close_p = _safe_float(r["quote_price"])
        pre_p = _safe_float(r["pre_close"])
        if close_p and close_p > 0:
            fund_days_map.setdefault(sym, {})[td] = {"close": close_p, "pre_close": pre_p}

    fund_pts_map: dict[str, dict[str, list[tuple[str, float]]]] = {}
    for r in fund_pt_rows:
        sym = str(r["symbol"])
        td = str(r["trade_date"])
        mm = str(r["minute"])
        p = _safe_float(r["price"])
        if p and p > 0:
            fund_pts_map.setdefault(sym, {}).setdefault(td, []).append((mm, p))

    # Build BRN 1m reference price before/at the start of latest A-share day
    fund_overlays: dict[str, dict[str, Any]] = {}
    for code in PREMIUM_CODES:
        f_days = fund_days_map.get(code)
        if not f_days:
            continue
        common = sorted(set(brn_day_close) & set(f_days))
        if not common:
            common = sorted(f_days)
        if not common:
            continue
        base_f = f_days[common[0]]["close"] or 0.0
        base_b = brn_day_close.get(common[0]) or (brn_day_close[sorted(brn_day_close)[0]] if brn_day_close else 0.0)
        f_daily_july: list[dict[str, Any]] = []
        for td in common:
            fv = f_days[td]["close"]
            bv = brn_day_close.get(td)
            f_cum = round((fv - base_f) / base_f * 100.0, 2) if fv and base_f > 0 else 0.0
            b_cum = round((bv - base_b) / base_b * 100.0, 2) if bv and base_b > 0 else None
            f_daily_july.append(
                {
                    "trade_date": td,
                    "label": f"{td[4:6]}-{td[6:8]}",
                    "brn_close": round(bv, 3) if bv else None,
                    "brn_cum_pct": b_cum,
                    "fund_close": round(fv, 3) if fv else None,
                    "fund_cum_pct": f_cum,
                    "spread_pct": round(f_cum - b_cum, 2) if b_cum is not None else None,
                }
            )

        latest_f_td = common[-1]
        latest_f_entry = f_days[latest_f_td]
        latest_f_close = latest_f_entry["close"]
        latest_f_pre = latest_f_entry["pre_close"]
        if (latest_f_pre is None or latest_f_pre <= 0) and len(common) >= 2:
            latest_f_pre = f_days[common[-2]]["close"]
        day_chg_pct = (
            round((latest_f_close - latest_f_pre) / latest_f_pre * 100.0, 2)
            if latest_f_close and latest_f_pre and latest_f_pre > 0
            else None
        )

        # Build 24H intraday comparison aligned to BRN0Y intraday_24h timestamps
        prev_f_td = common[-2] if len(common) >= 2 else None
        brn_anchor_24h = (
            brn_1500_by_day.get(prev_f_td, (None, None))[1]
            if prev_f_td
            else None
        ) or brn_1500_by_day.get(latest_f_td, (None, None))[1] or ref_1500_price
        f_pts_by_day = fund_pts_map.get(code, {})
        f_ts_keys: list[str] = []
        f_ts_vals: list[float] = []
        f_ts_tds: list[str] = []
        for f_td in sorted(f_pts_by_day):
            for mm, fp in f_pts_by_day[f_td]:
                f_ts_keys.append(f"{f_td}{mm.replace(':', '')}")
                f_ts_vals.append(fp)
                f_ts_tds.append(f_td)

        f_intraday_24h: list[dict[str, Any]] = []
        if intraday_24h and brn_anchor_24h and brn_anchor_24h > 0:
            for bp in intraday_24h:
                b_td = bp["trade_date"]
                b_mm = bp["minute"]
                b_p = bp["price"]
                b_pct = round((b_p - brn_anchor_24h) / brn_anchor_24h * 100.0, 2)
                key = f"{b_td}{b_mm.replace(':', '')}"
                fp_val: float | None = None
                f_pct: float | None = None
                post_close = True
                if f_ts_keys and latest_f_pre and latest_f_pre > 0:
                    pos = bisect_right(f_ts_keys, key) - 1
                    if pos >= 0:
                        fp_val = f_ts_vals[pos]
                        matched_td = f_ts_tds[pos]
                        f_pct = round((fp_val - latest_f_pre) / latest_f_pre * 100.0, 2)
                        post_close = not (matched_td == b_td and bp["in_ashare_session"] and f_ts_keys[pos] >= f"{b_td}0930")
                    elif b_td == latest_f_td and b_mm < "09:30":
                        fp_val = latest_f_pre
                        f_pct = 0.0
                        post_close = True
                elif latest_f_close and latest_f_pre and latest_f_pre > 0 and b_td >= latest_f_td:
                    fp_val = latest_f_close
                    f_pct = day_chg_pct
                    post_close = True
                f_intraday_24h.append(
                    {
                        "trade_date": b_td,
                        "time": bp["time"],
                        "in_ashare": bp["in_ashare_session"],
                        "post_close": post_close,
                        "brn_price": b_p,
                        "brn_pct": b_pct,
                        "fund_price": round(fp_val, 3) if fp_val else None,
                        "fund_pct": f_pct,
                        "spread_pct": round(f_pct - b_pct, 2) if f_pct is not None else None,
                    }
                )

        last_j = f_daily_july[-1] if f_daily_july else None
        fund_overlays[code] = {
            "code": code,
            "name": PREMIUM_NAMES.get(code, code),
            "latest_date": latest_f_td,
            "latest_close": round(latest_f_close, 3) if latest_f_close else None,
            "pre_close": round(latest_f_pre, 3) if latest_f_pre else None,
            "day_change_pct": day_chg_pct,
            "cum_july_pct": last_j["fund_cum_pct"] if last_j else None,
            "brn_cum_july_pct": last_j["brn_cum_pct"] if last_j else None,
            "spread_july_pct": last_j["spread_pct"] if last_j else None,
            "daily_july": f_daily_july,
            "intraday_24h": f_intraday_24h,
        }

    return {
        "symbol": BRENT_SYMBOL,
        "name": BRENT_DISPLAY_NAME,
        "unit": "USD/bbl",
        "market_open": is_open,
        "session_label": session_label,
        "price": cur_price,
        "pre_close": pre_close,
        "open": open_p,
        "high": high_p,
        "low": low_p,
        "change": change,
        "change_pct": change_pct,
        "ref_1500_date": ref_1500_date,
        "ref_1500_price": round(ref_1500_price, 3) if ref_1500_price else None,
        "since_1500_pct": since_1500_pct,
        "updated_at": (live or {}).get("updated_at")
        or (str(latest_day["collected_at"]) if latest_day else None),
        "intraday_24h": intraday_24h,
        "series_5d": series_5d,
        "daily_july": daily_july,
        "fund_overlays": fund_overlays,
    }
