"""Isolated QDII/LOF research-premium calculations and scheduling."""

from __future__ import annotations

import datetime as dt
import html
import json
import re
import time
from html.parser import HTMLParser
from typing import Any
from urllib.parse import quote
from urllib.request import Request
from zoneinfo import ZoneInfo

from .safe_http import SafeHttpTransport


SH_TZ = ZoneInfo("Asia/Shanghai")
PREMIUM_CODES = (
    "160723", "161129", "501018", "160416", "162719",
    "159518", "513350", "162411", "163208",
)
HAOETF_CODES = frozenset({"160723", "161129", "501018", "160416", "162719", "162411", "163208"})
PROXY_CODES = frozenset({"159518", "513350"})
PREMIUM_NAMES = {
    "160723": "嘉实原油", "161129": "原油基金", "501018": "南方原油",
    "160416": "石油基金", "162719": "广发石油", "159518": "标普油气ETF嘉实",
    "513350": "标普油气ETF富国", "162411": "华宝油气", "163208": "诺安油气",
}


def _number(value: object) -> float | None:
    if value is None:
        return None
    text = html.unescape(str(value)).strip().replace(",", "").replace("%", "")
    if not text or text in {"-", "--", "—", "N/A", "NA"}:
        return None
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def calculate_premium_rate(current_price: object, estimated_price: object) -> float | None:
    current = _number(current_price)
    estimate = _number(estimated_price)
    if current is None or estimate is None or current < 0 or estimate <= 0:
        return None
    return current / estimate - 1


def calculate_proxy_estimate(
    nav: object,
    latest_proxy: object,
    previous_proxy: object,
    latest_fx: object,
    previous_fx: object,
) -> dict[str, float]:
    values = tuple(_number(value) for value in (nav, latest_proxy, previous_proxy, latest_fx, previous_fx))
    if any(value is None or value <= 0 for value in values):
        raise ValueError("proxy estimate inputs are incomplete")
    nav_value, latest_proxy_value, previous_proxy_value, latest_fx_value, previous_fx_value = values
    assert all(value is not None for value in values)
    xop_factor = latest_proxy_value / previous_proxy_value
    fx_factor = latest_fx_value / previous_fx_value
    return {
        "estimate": round(nav_value * xop_factor * fx_factor, 6),
        "xopFactor": xop_factor,
        "fxFactor": fx_factor,
    }


def align_factor_closes(xop_rows: list[dict[str, Any]], fx_rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    xop = {str(row.get("date")): row for row in xop_rows if row.get("date")}
    fx = {str(row.get("date")): row for row in fx_rows if row.get("date")}
    dates = sorted(set(xop) & set(fx))
    if len(dates) < 2:
        raise ValueError("XOP and USD/CNY need two common closes")
    selected = dates[-2:]
    return {"xop": [xop[date] for date in selected], "fx": [fx[date] for date in selected]}


class _TableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tbody = 0
        self.row: list[str] | None = None
        self.cell: list[str] | None = None
        self.rows: list[list[str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag.lower() == "tbody": self.tbody += 1
        elif tag.lower() == "tr" and self.tbody and self.row is None: self.row = []
        elif tag.lower() == "td" and self.row is not None and self.cell is None: self.cell = []

    def handle_data(self, data: str) -> None:
        if self.cell is not None: self.cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "td" and self.row is not None and self.cell is not None:
            self.row.append(re.sub(r"\s+", " ", "".join(self.cell)).strip()); self.cell = None
        elif tag.lower() == "tr" and self.row is not None:
            if self.row: self.rows.append(self.row)
            self.row = None; self.cell = None
        elif tag.lower() == "tbody" and self.tbody: self.tbody -= 1


def parse_haoetf_html(source: str, expected_code: str) -> dict[str, Any]:
    parser = _TableParser(); parser.feed(source or "")
    row = next((candidate for candidate in parser.rows if candidate and candidate[0].strip() == expected_code), None)
    if row is None or len(row) < 8:
        raise ValueError("HaoETF quote missing")
    realtime = _number(row[2]); latest = _number(row[4])
    return {
        "code": expected_code,
        "name": row[1].strip() or PREMIUM_NAMES[expected_code],
        "currentPrice": _number(row[7]),
        "estimatedPrice": realtime if realtime is not None else latest,
        "estimateSource": "实时估值" if realtime is not None else "最新估值（非实时）",
        "estimateDate": row[6].strip() or None,
    }


def parse_eastmoney_quote(payload: str | dict[str, Any], expected_code: str) -> dict[str, Any]:
    decoded = json.loads(payload) if isinstance(payload, str) else payload
    data = decoded.get("data") if isinstance(decoded, dict) else None
    if not isinstance(data, dict) or str(data.get("f57") or expected_code) != expected_code:
        raise ValueError("Eastmoney quote missing")
    price = _number(data.get("f43"))
    if price is None:
        raise ValueError("Eastmoney price missing")
    return {"code": expected_code, "name": str(data.get("f58") or PREMIUM_NAMES[expected_code]), "currentPrice": price / 1000}


def parse_fund_nav_html(source: str, reference_date: dt.date | None = None) -> dict[str, Any]:
    reference = reference_date or dt.date.today()
    date_match = re.search(r'class=["\'][^"\']*fix_date[^"\']*["\'][^>]*>\s*\(([^)]+)\)', source or "", re.I)
    nav_match = re.search(r'class=["\'][^"\']*fix_dwjz[^"\']*["\'][^>]*>\s*([^<\s]+)', source or "", re.I)
    nav = _number(nav_match.group(1)) if nav_match else None
    raw_date = date_match.group(1) if date_match else ""
    match = re.search(r"(\d{1,2})[-/](\d{1,2})", raw_date)
    if nav is None or match is None:
        raise ValueError("fund NAV missing")
    date = dt.date(reference.year, int(match.group(1)), int(match.group(2)))
    if date > reference: date = date.replace(year=date.year - 1)
    return {"nav": nav, "date": date.isoformat()}


def parse_yahoo_chart(payload: str | dict[str, Any]) -> list[dict[str, Any]]:
    decoded = json.loads(payload) if isinstance(payload, str) else payload
    result = (((decoded or {}).get("chart") or {}).get("result") or [None])[0]
    if not isinstance(result, dict): raise ValueError("Yahoo chart missing")
    closes = (((result.get("indicators") or {}).get("quote") or [{}])[0].get("close") or [])
    rows = []
    for timestamp, close in zip(result.get("timestamp") or [], closes):
        value = _number(close)
        if value and value > 0:
            rows.append({"date": dt.datetime.fromtimestamp(float(timestamp), tz=dt.timezone.utc).date().isoformat(), "close": value})
    if len(rows) < 2: raise ValueError("Yahoo closes missing")
    return rows[-2:]


def premium_push_slot(now: dt.datetime) -> str | None:
    local = now.astimezone(SH_TZ) if now.tzinfo else now.replace(tzinfo=SH_TZ)
    if local.weekday() >= 5: return None
    minute = local.hour * 60 + local.minute
    due = (
        9 * 60 + 10 <= minute <= 9 * 60 + 30 and (minute - (9 * 60 + 10)) % 5 == 0
        or 9 * 60 + 31 <= minute <= 9 * 60 + 50
        or 9 * 60 + 51 <= minute <= 11 * 60 + 30 and (minute - (9 * 60 + 51)) % 10 == 0
        or 13 * 60 <= minute <= 15 * 60 and (minute - 13 * 60) % 10 == 0
    )
    return local.strftime("%H:%M") if due else None


def format_premium_body(snapshot: dict[str, Any]) -> str:
    lines = [f"QDII/LOF 溢价估算（{snapshot.get('as_of') or '-'}）", "代码 | 名称 | 现价 | 估价 | 实时溢价"]
    for row in snapshot.get("rows", []):
        rate = row.get("premium_rate")
        lines.append(
            f"{row.get('code')} | {row.get('name')} | {row.get('current_price') or '-'} | "
            f"{row.get('estimated_price') or '-'} | {('-' if rate is None else f'{rate * 100:+.2f}%')}"
        )
    return "\n".join(lines)


class PublicPremiumProvider:
    """Public research-only provider; never supplies task or Market quote fields."""

    _SECIDS = {"159518": "0.159518", "513350": "1.513350"}

    def __init__(self, *, timeout_seconds: float = 8, reference_ttl_seconds: float = 600) -> None:
        self.timeout_seconds = timeout_seconds
        self.reference_ttl_seconds = reference_ttl_seconds
        self._reference_cache: tuple[float, dict[str, Any]] | None = None

    def _get(self, base: str, path: str) -> str:
        response = SafeHttpTransport(base, max_body_bytes=2 * 1024 * 1024).request(
            Request(base.rstrip("/") + path, headers={"User-Agent": "THS-Market-Research/1.0"}),
            self.timeout_seconds,
        )
        if not 200 <= response.status < 300:
            raise RuntimeError("PREMIUM_SOURCE_HTTP_FAILED")
        return response.body.decode("utf-8", "ignore")

    def _references(self) -> dict[str, Any]:
        now = time.monotonic()
        if self._reference_cache and now - self._reference_cache[0] < self.reference_ttl_seconds:
            return self._reference_cache[1]
        result: dict[str, Any] = {"nav": {}}
        for code in PROXY_CODES:
            result["nav"][code] = parse_fund_nav_html(
                self._get("https://fund.eastmoney.com", f"/{code}.html"),
                dt.datetime.now(SH_TZ).date(),
            )
        result["xop"] = parse_yahoo_chart(
            self._get("https://query1.finance.yahoo.com", "/v8/finance/chart/XOP?range=5d&interval=1d")
        )
        result["fx"] = parse_yahoo_chart(
            self._get("https://query1.finance.yahoo.com", f"/v8/finance/chart/{quote('CNY=X', safe='=')}?range=5d&interval=1d")
        )
        self._reference_cache = (now, result)
        return result

    def _proxy_quote(self, code: str, references: dict[str, Any]) -> dict[str, Any]:
        secid = self._SECIDS[code]
        quote_payload = self._get(
            "https://push2.eastmoney.com",
            f"/api/qt/stock/get?secid={secid}&fields=f43,f57,f58",
        )
        quote_row = parse_eastmoney_quote(quote_payload, code)
        aligned = align_factor_closes(references["xop"], references["fx"])
        estimate = calculate_proxy_estimate(
            references["nav"][code]["nav"],
            aligned["xop"][1]["close"],
            aligned["xop"][0]["close"],
            aligned["fx"][1]["close"],
            aligned["fx"][0]["close"],
        )
        return {
            **quote_row,
            "estimatedPrice": estimate["estimate"],
            "estimateSource": "XOP 与 USD/CNY 代理估价",
            "estimateDate": references["nav"][code]["date"],
        }

    def fetch(self, previous: dict[str, Any] | None = None) -> dict[str, Any]:
        previous_rows = {row.get("code"): row for row in (previous or {}).get("rows", [])}
        references: dict[str, Any] | None = None
        rows = []
        errors: dict[str, str] = {}
        for code in PREMIUM_CODES:
            try:
                if code in HAOETF_CODES:
                    raw = parse_haoetf_html(
                        self._get("https://www.haoetf.com", f"/qdii/{code}"),
                        code,
                    )
                else:
                    references = references or self._references()
                    raw = self._proxy_quote(code, references)
                current = raw.get("currentPrice")
                estimate = raw.get("estimatedPrice")
                rows.append(
                    {
                        "code": code,
                        "name": raw.get("name") or PREMIUM_NAMES[code],
                        "current_price": current,
                        "estimated_price": estimate,
                        "premium_rate": calculate_premium_rate(current, estimate),
                        "estimate_source": raw.get("estimateSource"),
                        "estimate_date": raw.get("estimateDate"),
                        "source": "RESEARCH_PUBLIC",
                        "stale": False,
                        "error": None,
                    }
                )
            except Exception:
                errors[code] = "PREMIUM_SOURCE_UNAVAILABLE"
                if code in previous_rows:
                    rows.append({**previous_rows[code], "stale": True, "error": errors[code]})
                else:
                    rows.append(
                        {
                            "code": code,
                            "name": PREMIUM_NAMES[code],
                            "current_price": None,
                            "estimated_price": None,
                            "premium_rate": None,
                            "estimate_source": None,
                            "estimate_date": None,
                            "source": "RESEARCH_PUBLIC",
                            "stale": True,
                            "error": errors[code],
                        }
                    )
        return {
            "as_of": dt.datetime.now(SH_TZ).isoformat(timespec="seconds"),
            "rows": rows,
            "valid_count": sum(row["premium_rate"] is not None for row in rows),
            "errors": errors,
        }
