"""Quantitative strategy signal engine and risk-gated execution orchestrator.

Bridges Intraday (3s MACD + fund-flow rules) and QDII/LOF Premium signals into
the isolated TonghuashunTradeExecutor (emulator-5556 only, never emulator-5554).
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import copy
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP
import hashlib
import inspect
import json
import logging
import math
from pathlib import Path
import re
import sqlite3
from threading import RLock
import time
from typing import Any, AsyncIterator, Awaitable, Callable, Literal, Mapping, Sequence
import uuid
from zoneinfo import ZoneInfo

from .parsed_values import is_exchange_fund_symbol
from .quant_rules import (
    compute_lot_shares_from_budget,
    enrich_points_with_brent,
    enrich_store_with_line_funds,
    is_t0_turnaround_symbol,
    load_brent_alignment_index,
    normalize_position_sizing,
    resolve_signal_conviction_tier,
)
from .research_monitor import (
    _enrich_points_with_fund_flow,
    format_push_price_and_change,
    format_push_symbol,
    resolve_push_symbol_name,
)
from .research_premium import PREMIUM_NAMES, calculate_premium_rate
from .research_replay import build_replay_markers, calculate_macd, evaluate_rule_detail
from .ths_trade import TradeExecutionError
from .v9_plus_engine import evaluate_v9_plus_live_decision, evaluate_v9_plus_live_indicators


SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
_SYMBOL_RE = re.compile(r"^[0-9]{6}\Z")
_FUND_PREFIXES = ("15", "16", "50", "51", "52", "56", "58")
MIN_RETAINED_SHARES = 100
logger = logging.getLogger(__name__)


def _clean_plain_text_for_push(text: object) -> str:
    """Remove markdown metacharacters and slashes so Bark GET / SC3 markdown never choke."""
    s = str(text or "").strip()
    if not s:
        return ""
    s = s.replace("/", "·").replace("[", "（").replace("]", "）")
    s = re.sub(r"\*+(\d+)", r"尾号\1", s)
    return s.replace("*", "").replace("_", "").replace("`", "").strip(" ·")


def _clean_push_receipt(dialog_message: str | None) -> str:
    """Strip noisy Tonghuashun UI columns and markdown metacharacters for push stability."""
    head = str(dialog_message or "").split(" | 确定")[0].split(" | 行情")[0].strip()
    if not head:
        return ""
    head = head.replace("系统信息 | ", "").replace(" | ", " · ")
    return _clean_plain_text_for_push(head)


def _build_order_push_message(
    *,
    symbol: str,
    name: str,
    side_label: str,
    limit_price: str,
    quantity: int,
    item: Mapping[str, Any],
    status: str,
    dialog_message: str | None = None,
) -> tuple[str, str]:
    """Compose plain-language push title and body without noisy strategy name prefixes."""
    disp = format_push_symbol(symbol, name)
    if status == "PENDING_CONFIRM":
        title = f"量化信号待确认：{side_label} {disp}"
    elif status == "SUBMITTED":
        title = f"量化自动{side_label}：{disp}"
    elif status == "DRY_RUN_CONFIRMED":
        title = f"量化演练{side_label}：{disp}"
    else:
        title = f"量化委托未成（{status}）：{side_label} {disp}"

    try:
        est_amt = round(float(limit_price) * int(quantity), 2)
        amt_txt = f"（约 ¥{est_amt:,.0f}）"
    except (TypeError, ValueError):
        amt_txt = ""
    receipt = _clean_push_receipt(dialog_message)
    line_order = f"委托：限价 ¥{limit_price} × {int(quantity)}股{amt_txt}" + (f" · {receipt}" if receipt else "")

    meaning = _clean_plain_text_for_push(item.get("push_meaning"))
    why = _clean_plain_text_for_push(item.get("push_why"))
    if not meaning:
        if item.get("signal_tag") == "t0-flatten" or ":t0-flatten:" in str(item.get("rule_title") or ""):
            meaning = "尾盘自动平掉日内做T敞口，恢复原有底仓"
        elif "折价" in str(item.get("rule_title") or "") or "溢价" in str(item.get("rule_title") or ""):
            meaning = "折溢价套利委托（折价低吸或高溢价止盈）"
        else:
            meaning = "触发分时买点自动低吸" if side_label == "买入" else "触发分时卖点自动高抛"
    if not why:
        why = _clean_plain_text_for_push(item.get("rule_summary")) or "当前行情满足策略触发阈值"
    return title, f"{line_order}\n含义：{meaning}\n触发：{why}"

VALID_SIGNAL_SOURCES = ("intraday_macd_flow", "lof_premium", "combo_confirm", "quant_rule")
VALID_EXECUTION_MODES = ("dry_run", "semi_auto", "full_auto")
VALID_PRICING_MODES = ("counterparty_book", "fixed_ticks")
VALID_SIZING_MODES = ("model_auto", "fixed_qty")

SIGNAL_SOURCE_LABELS = {
    "intraday_macd_flow": "分时MACD+主力大单共振 (3秒轮询)",
    "lof_premium": "油气LOF实时折溢价套利 (15秒轮询)",
    "combo_confirm": "折溢价+分时动量双因子确认",
    "quant_rule": "量化规则库策略 (3秒轮询)",
}
EXECUTION_MODE_LABELS = {
    "dry_run": "演练模式（弹窗核验后取消，不报柜台）",
    "semi_auto": "半自动模式（推送信号后人工确认下单）",
    "full_auto": "全自动实盘模式（盘中自动报单+超时撤单）",
}

DEFAULT_AUTO_TRADE_CONFIG: dict[str, Any] = {
    "enabled": True,
    "signal_source": "intraday_macd_flow",
    "quant_rule_id": None,
    "execution_mode": "semi_auto",
    "sizing_mode": "fixed_qty",
    "symbols": ["160723"],
    "order_quantity": 100,
    "pricing_mode": "counterparty_book",
    "order_book_level": 3,
    "slippage_ticks": 3,
    "max_slippage_pct": 1.5,
    "refresh_book_on_confirm": True,
    "auto_cancel_seconds": 30,
    "daily_max_buy_amount": 50000.0,
    "block_non_continuous_session": True,
    "poll_interval_seconds": 3.0,
    "confirm_ttl_seconds": 180,
    "circuit_open": False,
    "circuit_reason": "",
    "consecutive_failures": 0,
    "symbol_cooldowns": {},
    "symbol_profiles": {
        "160723": {
            "symbol": "160723",
            "enabled": True,
            "signal_source": "intraday_macd_flow",
            "quant_rule_id": None,
            "sizing_mode": "fixed_qty",
            "capital_cap": 50000.0,
            "order_quantity": 100,
            "order_book_level": 3,
            "reconciled_at": None,
            "reconciled_shares": None,
        }
    },
}

_FALLBACK_INTRADAY_RULES: list[dict[str, Any]] = [
    {
        "id": "auto-macd-flow-buy",
        "enabled": True,
        "side": "buy",
        "title": "MACD大单买入",
        "joiner": "and",
        "conditions": [
            {"id": "c-macd", "type": "macd_signal", "signal": "red"},
            {
                "id": "c-flow",
                "type": "flow_change_pct",
                "flow": "big",
                "minutes": 1,
                "direction": "increase",
                "value": 3.0,
            },
        ],
    },
    {
        "id": "auto-macd-flow-sell",
        "enabled": True,
        "side": "sell",
        "title": "MACD大单卖出",
        "joiner": "and",
        "conditions": [
            {"id": "c-macd", "type": "macd_signal", "signal": "green"},
            {
                "id": "c-flow",
                "type": "flow_change_pct",
                "flow": "big",
                "minutes": 1,
                "direction": "decrease",
                "value": 3.0,
            },
        ],
    },
]

_ACTIVE_BUY_STATUSES = (
    "PENDING_CONFIRM",
    "QUEUED",
    "EXECUTING",
    "RECONCILING",
    "SUBMITTED",
    "AUTO_CANCELLED_TIMEOUT",
    "FILLED_OR_CLOSED",
)
_POSITION_LEDGER_STATUSES = (
    "PENDING_CONFIRM",
    "QUEUED",
    "EXECUTING",
    "RECONCILING",
    "SUBMITTED",
    "FILLED_OR_CLOSED",
)
_GLOBAL_CIRCUIT_ERROR_CODES = frozenset(
    {
        "BROKER_LOGIN_REQUIRED",
        "TRADE_DEVICE_OFFLINE",
        "TRADE_NAVIGATION_FAILED",
        "TRADE_INPUT_READBACK_MISMATCH",
    }
)
_LOCAL_COOLDOWN_ERROR_CODES = frozenset(
    {
        "TRADE_INSUFFICIENT_CASH",
        "TRADE_DAILY_LIMIT_EXCEEDED",
        "TRADE_RETAIN_MIN_SHARES",
        "TRADE_PRICE_OUT_OF_RANGE",
    }
)
_LOCAL_REJECT_KEYWORDS = (
    "资金不足",
    "可用余额",
    "可买",
    "股份不足",
    "可卖",
    "持仓不足",
    "涨停",
    "跌停",
    "价格笼子",
    "废单",
    "超过",
)


def _now_shanghai(now_fn: Callable[[], datetime] | None = None) -> datetime:
    current = now_fn() if now_fn is not None else datetime.now(timezone.utc)
    return current.astimezone(SHANGHAI_TZ) if current.tzinfo else current.replace(tzinfo=SHANGHAI_TZ)


def _iso_shanghai(now_fn: Callable[[], datetime] | None = None) -> str:
    return _now_shanghai(now_fn).isoformat(timespec="seconds")


def is_in_continuous_auction(
    local_dt: datetime,
    is_trading_day: Callable[[date], bool] | None = None,
) -> tuple[bool, str]:
    """Return whether local_dt falls inside A-share continuous auction windows."""
    local = local_dt.astimezone(SHANGHAI_TZ) if local_dt.tzinfo else local_dt.replace(tzinfo=SHANGHAI_TZ)
    trade_day = is_trading_day(local.date()) if is_trading_day is not None else (local.weekday() < 5)
    if not trade_day:
        return False, "非交易日（仅 A 股交易日 09:31-11:29 / 13:00-14:55 放行实盘）"
    minute = local.hour * 60 + local.minute
    if (9 * 60 + 31 <= minute <= 11 * 60 + 29) or (13 * 60 <= minute <= 14 * 60 + 55):
        return True, "连续竞价时段（监控放行）"
    return False, "非连续竞价时段（仅 09:31-11:29 / 13:00-14:55 放行实盘）"


def compute_slippage_limit_price(
    symbol: str,
    market: str | None,
    side: Literal["buy", "sell"],
    raw_price: str | float | Decimal,
    slippage_ticks: int,
) -> tuple[str, int]:
    """Compute tick-aligned limit order price with protective slippage."""
    is_fund = is_exchange_fund_symbol(symbol, market) or symbol.startswith(_FUND_PREFIXES)
    precision = 3 if is_fund else 2
    tick = Decimal("0.001") if is_fund else Decimal("0.01")
    try:
        base = Decimal(str(raw_price).strip())
    except InvalidOperation as error:
        raise ValueError(f"invalid raw price: {raw_price!r}") from error
    if not base.is_finite() or base <= 0:
        raise ValueError(f"non-positive raw price: {raw_price!r}")
    steps = max(0, min(20, int(slippage_ticks)))
    offset = tick * steps
    target = (base + offset) if side == "buy" else max(tick, base - offset)
    quantized = target.quantize(tick)
    return f"{quantized:.{precision}f}", precision


def _to_pos_decimal(raw: object) -> Decimal | None:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        val = Decimal(text)
    except InvalidOperation:
        return None
    if not val.is_finite() or val <= 0:
        return None
    return val


def compute_ashare_price_cage(
    symbol: str,
    market: str | None,
    side: Literal["buy", "sell"],
    order_book: Mapping[str, Any] | None,
) -> tuple[Decimal | None, str | None]:
    """Compute A-share continuous-auction price cage bound (exempting exchange funds)."""
    clean_sym = str(symbol or "").strip()
    if is_exchange_fund_symbol(clean_sym, market) or clean_sym.startswith(_FUND_PREFIXES):
        return None, None
    if not isinstance(order_book, Mapping):
        return None, None

    def _first_valid(ladder_key: str) -> Decimal | None:
        for row in order_book.get(ladder_key) or []:
            if isinstance(row, Mapping):
                val = _to_pos_decimal(row.get("price"))
                if val is not None:
                    return val
        return None

    if side == "buy":
        bench = _first_valid("asks") or _first_valid("bids") or _to_pos_decimal(order_book.get("last_price"))
    else:
        bench = _first_valid("bids") or _first_valid("asks") or _to_pos_decimal(order_book.get("last_price"))
    if bench is None:
        return None, None

    tick = Decimal("0.01")
    mkt_upper = str(market or "").strip().upper()
    is_bse = clean_sym.startswith(("92", "8", "43")) or mkt_upper == "BJ"
    is_star_chinext = clean_sym.startswith(("688", "689", "300", "301"))
    if is_bse:
        delta = max(bench * Decimal("0.05"), Decimal("0.10"))
        label = "北交所5%价格笼子"
    elif is_star_chinext:
        delta = bench * Decimal("0.02")
        label = "双创2%价格笼子"
    else:
        delta = max(bench * Decimal("0.02"), Decimal("0.10"))
        label = "主板2%/0.10元价格笼子"

    if side == "buy":
        limit = (bench + delta).quantize(tick, rounding=ROUND_FLOOR)
        limit_up = _to_pos_decimal(order_book.get("limit_up"))
        if limit_up is not None and limit > limit_up:
            limit = limit_up
        return max(tick, limit), label

    limit = max(tick, (bench - delta).quantize(tick, rounding=ROUND_CEILING))
    limit_down = _to_pos_decimal(order_book.get("limit_down"))
    if limit_down is not None and limit < limit_down:
        limit = limit_down
    return max(tick, limit), label


def coalesce_symbol_candidates(
    candidates: Sequence[Mapping[str, Any]],
    *,
    default_qty: int = 100,
) -> list[dict[str, Any]]:
    """Coalesce multiple rule triggers on one symbol: merge same-side, net opposite-side."""
    if len(candidates) <= 1:
        return [dict(item) for item in candidates]

    closing_items: list[dict[str, Any]] = []
    intraday: list[tuple[int, dict[str, Any]]] = []
    for idx, item in enumerate(candidates):
        row = dict(item)
        if row.get("signal_tag") in {"t0-flatten", "v9-close"}:
            closing_items.append(row)
        else:
            intraday.append((idx, row))

    if len(intraday) <= 1:
        return closing_items + [row for _, row in intraday]

    base_qty = max(100, (int(default_qty) // 100) * 100)

    def _est_shares(row: Mapping[str, Any]) -> int:
        if row.get("qty_override") is not None:
            try:
                q = int(row["qty_override"])
                if q >= 100:
                    return (q // 100) * 100
            except (TypeError, ValueError):
                pass
        scale = float(row.get("weight_scale") or 1.0)
        scaled = int(round((base_qty * scale) / 100.0)) * 100
        return max(100, scaled)

    def _merge_side(items: list[tuple[int, dict[str, Any]]]) -> dict[str, Any] | None:
        if not items:
            return None
        ordered = sorted(
            items,
            key=lambda pair: (int(pair[1].get("binding_priority") or 10), pair[0]),
        )
        primary = dict(ordered[0][1])
        if len(ordered) == 1:
            return primary
        summaries: list[str] = []
        max_shares = 0
        has_qty_override = False
        max_weight: float | None = None
        rule_ids: list[str] = []
        for _, row in ordered:
            rid = str(row.get("rule_id") or "").strip()
            if rid and rid not in rule_ids:
                rule_ids.append(rid)
            sm = str(row.get("rule_summary") or "").strip()
            if sm and sm not in summaries:
                summaries.append(sm)
            if row.get("qty_override") is not None or float(row.get("weight_scale") or 1.0) != 1.0:
                has_qty_override = True
            max_shares = max(max_shares, _est_shares(row))
            if row.get("weight_pct") is not None:
                try:
                    w = float(row["weight_pct"])
                    max_weight = w if max_weight is None else max(max_weight, w)
                except (TypeError, ValueError):
                    pass
        if has_qty_override and max_shares >= 100:
            primary["qty_override"] = max_shares
        if max_weight is not None:
            primary["weight_pct"] = max_weight
        primary["coalesced_rules"] = rule_ids
        joined = "；".join(summaries)
        primary["rule_summary"] = f"[多规则共振×{len(ordered)}] {joined}" if joined else f"[多规则共振×{len(ordered)}]"
        return primary

    buys = [(idx, r) for idx, r in intraday if r.get("side") != "sell"]
    sells = [(idx, r) for idx, r in intraday if r.get("side") == "sell"]
    merged_buy = _merge_side(buys)
    merged_sell = _merge_side(sells)

    if merged_buy is not None and merged_sell is not None:
        net = _est_shares(merged_buy) - _est_shares(merged_sell)
        if abs(net) < 100:
            return closing_items
        winner = dict(merged_buy if net > 0 else merged_sell)
        net_qty = (abs(net) // 100) * 100
        winner["qty_override"] = net_qty
        base_sm = str(winner.get("rule_summary") or "").strip()
        winner["rule_summary"] = f"{base_sm}；[同标的对冲净额 {net_qty}股]" if base_sm else f"[同标的对冲净额 {net_qty}股]"
        return closing_items + [winner]

    out = list(closing_items)
    if merged_buy is not None:
        out.append(merged_buy)
    if merged_sell is not None:
        out.append(merged_sell)
    return out


def resolve_order_limit_price(
    symbol: str,
    market: str | None,
    side: Literal["buy", "sell"],
    raw_price: str | float | Decimal,
    *,
    pricing_mode: str = "counterparty_book",
    order_book_level: int = 3,
    slippage_ticks: int = 3,
    max_slippage_pct: float = 1.5,
    order_book: Mapping[str, Any] | None = None,
) -> tuple[str, int, dict[str, Any]]:
    """Resolve limit price from real 5-level counterparty order book or fixed-tick fallback."""
    is_fund = is_exchange_fund_symbol(symbol, market) or symbol.startswith(_FUND_PREFIXES)
    precision = 3 if is_fund else 2
    tick = Decimal("0.001") if is_fund else Decimal("0.01")
    base_signal = _to_pos_decimal(raw_price)
    if base_signal is None:
        raise ValueError(f"invalid raw price: {raw_price!r}")

    book_copy = copy.deepcopy(dict(order_book)) if isinstance(order_book, Mapping) else None
    lvl = max(1, min(5, int(order_book_level)))
    opp_prefix = "卖" if side == "buy" else "买"

    if pricing_mode == "counterparty_book" and isinstance(order_book, Mapping):
        ladder_key = "asks" if side == "buy" else "bids"
        raw_ladder = order_book.get(ladder_key) or []
        by_level: dict[int, Decimal] = {}
        for row in raw_ladder:
            if not isinstance(row, Mapping):
                continue
            try:
                rlvl = int(row.get("level") or 0)
            except (TypeError, ValueError):
                continue
            rprice = _to_pos_decimal(row.get("price"))
            if 1 <= rlvl <= 5 and rprice is not None:
                by_level[rlvl] = rprice

        chosen: Decimal | None = None
        applied_level: int | None = None
        fallback_note = ""
        for cand_lvl in range(lvl, 0, -1):
            if cand_lvl in by_level:
                chosen = by_level[cand_lvl]
                applied_level = cand_lvl
                break

        limit_up = _to_pos_decimal(order_book.get("limit_up"))
        limit_down = _to_pos_decimal(order_book.get("limit_down"))
        if limit_up is not None and limit_down is not None and limit_down >= limit_up:
            limit_up = None
            limit_down = None
        if chosen is None:
            limit_fallback = limit_up if side == "buy" else limit_down
            if limit_fallback is not None:
                chosen = limit_fallback
                fallback_note = "涨停板兜底" if side == "buy" else "跌停板兜底"

        if chosen is not None:
            pct = Decimal(str(max(0.1, min(10.0, float(max_slippage_pct)))))
            capped = False
            if side == "buy":
                cap_price = (base_signal * (Decimal("1") + pct / Decimal("100"))).quantize(
                    tick, rounding=ROUND_HALF_UP
                )
                cap_price = max(cap_price, base_signal.quantize(tick))
                if chosen > cap_price:
                    chosen = cap_price
                    capped = True
            else:
                floor_price = (base_signal * (Decimal("1") - pct / Decimal("100"))).quantize(
                    tick, rounding=ROUND_HALF_UP
                )
                floor_price = max(tick, min(floor_price, base_signal.quantize(tick)))
                if chosen < floor_price:
                    chosen = floor_price
                    capped = True

            if limit_down is not None and chosen < limit_down:
                chosen = limit_down
            if limit_up is not None and chosen > limit_up:
                chosen = limit_up

            cage_capped = False
            cage_limit, cage_label = compute_ashare_price_cage(symbol, market, side, order_book)
            if cage_limit is not None:
                if side == "buy" and chosen > cage_limit:
                    chosen = cage_limit
                    cage_capped = True
                elif side == "sell" and chosen < cage_limit:
                    chosen = cage_limit
                    cage_capped = True

            quantized = max(tick, chosen.quantize(tick, rounding=ROUND_HALF_UP))
            formatted = f"{quantized:.{precision}f}"
            if applied_level is not None:
                applied_label = f"{opp_prefix}{applied_level}"
                basis_prefix = f"对手盘{applied_label}"
                if applied_level != lvl:
                    basis_prefix += f"(原目标{opp_prefix}{lvl}无单)"
            else:
                applied_label = fallback_note
                basis_prefix = fallback_note
            if capped:
                basis_prefix += f"→偏离{float(pct):.1f}%风控封顶"
            if cage_capped and cage_label:
                basis_prefix += f"→{cage_label}保护"
            price_basis = f"{basis_prefix} (¥{formatted})"
            return formatted, precision, {
                "mode": "counterparty_book",
                "target_level": lvl,
                "applied_level": applied_level,
                "applied_label": applied_label,
                "price_basis": price_basis,
                "capped": capped,
                "cage_capped": cage_capped,
                "order_book": book_copy,
            }

    fallback_price, _ = compute_slippage_limit_price(
        symbol, market, side, raw_price, slippage_ticks
    )
    return fallback_price, precision, {
        "mode": "fixed_ticks",
        "target_level": lvl if pricing_mode == "counterparty_book" else None,
        "applied_level": None,
        "applied_label": f"固定滑点±{int(slippage_ticks)}步",
        "price_basis": (
            f"盘口暂缺回退固定滑点 ±{int(slippage_ticks)} 步 (¥{fallback_price})"
            if pricing_mode == "counterparty_book"
            else f"固定滑点 ±{int(slippage_ticks)} 步 (¥{fallback_price})"
        ),
        "capped": False,
        "order_book": book_copy,
    }


def _normalize_symbol_cooldowns(raw: object) -> dict[str, dict[str, Any]]:
    if not isinstance(raw, Mapping):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for raw_key, item in raw.items():
        if not isinstance(item, Mapping):
            continue
        sym = str(item.get("symbol") or str(raw_key).split(":", 1)[0]).strip()
        side = str(item.get("side") or (str(raw_key).split(":", 1)[1] if ":" in str(raw_key) else "buy")).strip().lower()
        if not _SYMBOL_RE.fullmatch(sym) or side not in {"buy", "sell"}:
            continue
        try:
            until_ep = float(item.get("until_epoch") or 0.0)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(until_ep) or until_ep <= 0.0:
            continue
        key = f"{sym}:{side}"
        out[key] = {
            "symbol": sym,
            "side": side,
            "until_epoch": until_ep,
            "until_at": str(item.get("until_at") or "").strip(),
            "error_code": str(item.get("error_code") or "").strip(),
            "reason": str(item.get("reason") or "").strip(),
        }
    return out


def _normalize_rule_bindings(
    raw_bindings: object,
    fallback_source: str,
    fallback_rule_id: str | None,
) -> list[dict[str, Any]]:
    parsed: list[tuple[int, int, dict[str, Any]]] = []
    if isinstance(raw_bindings, Sequence) and not isinstance(raw_bindings, (str, bytes, bytearray)):
        for idx, item in enumerate(raw_bindings[:8]):
            if not isinstance(item, Mapping):
                continue
            b_src = str(item.get("signal_source") or fallback_source).strip()
            if b_src not in VALID_SIGNAL_SOURCES:
                continue
            q_raw = item.get("quant_rule_id")
            q_id: str | None = None
            if q_raw is not None and str(q_raw).strip():
                cleaned = str(q_raw).strip()
                if re.fullmatch(r"[A-Za-z0-9._-]{2,64}", cleaned):
                    q_id = cleaned
            if b_src == "quant_rule" and q_id is None:
                q_id = fallback_rule_id
            try:
                prio = max(1, min(100, int(item.get("priority", 10 + idx))))
            except (TypeError, ValueError):
                prio = 10 + idx
            try:
                scale = round(max(0.1, min(2.0, float(item.get("weight_scale", 1.0)))), 2)
            except (TypeError, ValueError):
                scale = 1.0
            parsed.append(
                (
                    prio,
                    idx,
                    {
                        "signal_source": b_src,
                        "quant_rule_id": q_id if b_src == "quant_rule" else None,
                        "enabled": bool(item.get("enabled", True)),
                        "priority": prio,
                        "weight_scale": scale,
                    },
                )
            )
    if not parsed:
        return [
            {
                "signal_source": fallback_source,
                "quant_rule_id": fallback_rule_id if fallback_source == "quant_rule" else None,
                "enabled": True,
                "priority": 10,
                "weight_scale": 1.0,
            }
        ]
    parsed.sort(key=lambda row: (row[0], row[1]))
    return [row[2] for row in parsed]


def _normalize_symbol_profile(
    symbol: str,
    raw_profile: Mapping[str, Any] | None,
    fallback_cfg: Mapping[str, Any],
) -> dict[str, Any]:
    raw_p = raw_profile if isinstance(raw_profile, Mapping) else {}
    qrid_raw = raw_p.get("quant_rule_id", fallback_cfg.get("quant_rule_id"))
    qrid: str | None = None
    if qrid_raw is not None and str(qrid_raw).strip():
        cleaned_qrid = str(qrid_raw).strip()
        if re.fullmatch(r"[A-Za-z0-9._-]{2,64}", cleaned_qrid):
            qrid = cleaned_qrid

    sig_src = str(raw_p.get("signal_source") or fallback_cfg.get("signal_source") or "intraday_macd_flow").strip()
    if sig_src not in VALID_SIGNAL_SOURCES:
        sig_src = str(fallback_cfg.get("signal_source") or "intraday_macd_flow")

    sizing_mode = str(raw_p.get("sizing_mode") or fallback_cfg.get("sizing_mode") or "fixed_qty").strip()
    if sizing_mode not in VALID_SIZING_MODES:
        sizing_mode = str(fallback_cfg.get("sizing_mode") or "fixed_qty")

    cap_val = float(fallback_cfg.get("daily_max_buy_amount") or 50000.0)
    if "capital_cap" in raw_p and raw_p.get("capital_cap") is not None:
        try:
            cand_cap = float(raw_p["capital_cap"])
            if 100.0 <= cand_cap <= 10_000_000.0:
                cap_val = round(cand_cap, 2)
        except (TypeError, ValueError):
            pass

    qty_val = int(fallback_cfg.get("order_quantity") or 100)
    if "order_quantity" in raw_p and raw_p.get("order_quantity") is not None:
        try:
            cand_qty = int(raw_p["order_quantity"])
            if 100 <= cand_qty <= 1_000_000 and cand_qty % 100 == 0:
                qty_val = cand_qty
        except (TypeError, ValueError):
            pass

    book_lvl = int(fallback_cfg.get("order_book_level") or 3)
    if "order_book_level" in raw_p and raw_p.get("order_book_level") is not None:
        try:
            cand_lvl = int(raw_p["order_book_level"])
            if 1 <= cand_lvl <= 5:
                book_lvl = cand_lvl
        except (TypeError, ValueError):
            pass

    rec_shares: int | None = None
    if raw_p.get("reconciled_shares") is not None:
        try:
            rs = int(raw_p["reconciled_shares"])
            if rs >= 0:
                rec_shares = rs
        except (TypeError, ValueError):
            pass

    rec_at = str(raw_p.get("reconciled_at") or "").strip() or None
    has_explicit_bindings = (
        isinstance(raw_p.get("rule_bindings"), Sequence)
        and not isinstance(raw_p.get("rule_bindings"), (str, bytes, bytearray))
        and len(raw_p.get("rule_bindings") or []) > 0
    )
    rule_bindings = _normalize_rule_bindings(
        raw_p.get("rule_bindings") if has_explicit_bindings else None,
        sig_src,
        qrid,
    )
    if has_explicit_bindings:
        primary_binding = next((b for b in rule_bindings if b.get("enabled", True)), rule_bindings[0])
        sig_src = str(primary_binding["signal_source"])
        qrid = primary_binding.get("quant_rule_id")
    return {
        "symbol": symbol,
        "enabled": bool(raw_p["enabled"]) if "enabled" in raw_p else True,
        "signal_source": sig_src,
        "quant_rule_id": qrid,
        "sizing_mode": sizing_mode,
        "capital_cap": round(cap_val, 2),
        "order_quantity": qty_val,
        "order_book_level": book_lvl,
        "rule_bindings": rule_bindings,
        "reconciled_at": rec_at,
        "reconciled_shares": rec_shares,
    }


def _normalize_config(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    cfg = copy.deepcopy(DEFAULT_AUTO_TRADE_CONFIG)
    if not isinstance(raw, Mapping):
        return cfg

    if "enabled" in raw:
        cfg["enabled"] = bool(raw["enabled"])

    sig_src = str(raw.get("signal_source") or cfg["signal_source"]).strip()
    if sig_src in VALID_SIGNAL_SOURCES:
        cfg["signal_source"] = sig_src

    if "quant_rule_id" in raw:
        raw_qrid = raw.get("quant_rule_id")
        if raw_qrid is None or str(raw_qrid).strip() == "":
            cfg["quant_rule_id"] = None
        else:
            cleaned_qrid = str(raw_qrid).strip()
            if re.fullmatch(r"[A-Za-z0-9._-]{2,64}", cleaned_qrid):
                cfg["quant_rule_id"] = cleaned_qrid

    exec_mode = str(raw.get("execution_mode") or cfg["execution_mode"]).strip()
    if exec_mode in VALID_EXECUTION_MODES:
        cfg["execution_mode"] = exec_mode

    sizing_mode = str(raw.get("sizing_mode") or cfg["sizing_mode"]).strip()
    if sizing_mode in VALID_SIZING_MODES:
        cfg["sizing_mode"] = sizing_mode

    raw_symbols = raw.get("symbols")
    if isinstance(raw_symbols, str):
        raw_symbols = [s.strip() for s in re.split(r"[,，\s]+", raw_symbols) if s.strip()]
    if isinstance(raw_symbols, list):
        cleaned = []
        for item in raw_symbols:
            sym = str(item).strip()
            if _SYMBOL_RE.fullmatch(sym) and sym not in cleaned:
                cleaned.append(sym)
                if len(cleaned) >= 50:
                    break
        if cleaned:
            cfg["symbols"] = cleaned

    if "order_quantity" in raw:
        try:
            qty = int(raw["order_quantity"])
            if qty >= 100 and qty % 100 == 0 and qty <= 1_000_000:
                cfg["order_quantity"] = qty
        except (TypeError, ValueError):
            pass

    pricing_mode = str(raw.get("pricing_mode") or cfg["pricing_mode"]).strip()
    if pricing_mode in VALID_PRICING_MODES:
        cfg["pricing_mode"] = pricing_mode

    if "order_book_level" in raw:
        try:
            lvl = int(raw["order_book_level"])
            if 1 <= lvl <= 5:
                cfg["order_book_level"] = lvl
        except (TypeError, ValueError):
            pass

    if "max_slippage_pct" in raw:
        try:
            pct = float(raw["max_slippage_pct"])
            if 0.1 <= pct <= 10.0:
                cfg["max_slippage_pct"] = round(pct, 2)
        except (TypeError, ValueError):
            pass

    if "refresh_book_on_confirm" in raw:
        cfg["refresh_book_on_confirm"] = bool(raw["refresh_book_on_confirm"])

    if "slippage_ticks" in raw:
        try:
            ticks = int(raw["slippage_ticks"])
            if 0 <= ticks <= 20:
                cfg["slippage_ticks"] = ticks
        except (TypeError, ValueError):
            pass

    if "auto_cancel_seconds" in raw:
        try:
            sec = int(raw["auto_cancel_seconds"])
            if 0 <= sec <= 600:
                cfg["auto_cancel_seconds"] = sec
        except (TypeError, ValueError):
            pass

    if "daily_max_buy_amount" in raw:
        try:
            cap = float(raw["daily_max_buy_amount"])
            if 100.0 <= cap <= 10_000_000.0:
                cfg["daily_max_buy_amount"] = round(cap, 2)
        except (TypeError, ValueError):
            pass

    if "block_non_continuous_session" in raw:
        cfg["block_non_continuous_session"] = bool(raw["block_non_continuous_session"])

    if "poll_interval_seconds" in raw:
        try:
            poll = float(raw["poll_interval_seconds"])
            if 1.0 <= poll <= 60.0:
                cfg["poll_interval_seconds"] = poll
        except (TypeError, ValueError):
            pass

    if "confirm_ttl_seconds" in raw:
        try:
            ttl = int(raw["confirm_ttl_seconds"])
            if 30 <= ttl <= 3600:
                cfg["confirm_ttl_seconds"] = ttl
        except (TypeError, ValueError):
            pass

    if "circuit_open" in raw:
        cfg["circuit_open"] = bool(raw["circuit_open"])
    if "circuit_reason" in raw:
        cfg["circuit_reason"] = str(raw.get("circuit_reason") or "").strip()
    if "consecutive_failures" in raw:
        try:
            cfg["consecutive_failures"] = max(0, int(raw["consecutive_failures"]))
        except (TypeError, ValueError):
            pass
    if "symbol_cooldowns" in raw:
        cfg["symbol_cooldowns"] = _normalize_symbol_cooldowns(raw.get("symbol_cooldowns"))

    raw_profiles = raw.get("symbol_profiles")
    profiles: dict[str, dict[str, Any]] = {}
    if isinstance(raw_profiles, Mapping):
        for raw_sym, raw_prof in raw_profiles.items():
            sym = str(raw_sym).strip()
            if not _SYMBOL_RE.fullmatch(sym):
                continue
            if sym not in cfg["symbols"] and "symbols" not in raw:
                cfg["symbols"].append(sym)
            if sym in cfg["symbols"]:
                profiles[sym] = _normalize_symbol_profile(sym, raw_prof, cfg)

    for sym in cfg["symbols"]:
        if sym not in profiles:
            existing_p = raw_profiles.get(sym) if isinstance(raw_profiles, Mapping) else None
            profiles[sym] = _normalize_symbol_profile(sym, existing_p, cfg)

    cfg["symbol_profiles"] = profiles
    return cfg


class SQLiteAutoTradeStore:
    """Thread-safe WAL SQLite persistence for strategy config and signal/execution log."""

    SETTINGS_KEY = "auto_trade_config"

    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self._connection = sqlite3.connect(self.path, check_same_thread=False, timeout=5.0)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._connection.execute("PRAGMA synchronous = NORMAL")
        self._connection.execute("PRAGMA busy_timeout = 5000")
        self._migrate()

    def _migrate(self) -> None:
        with self._lock, self._connection:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS research_settings (
                    key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS auto_trade_signals (
                    id TEXT PRIMARY KEY,
                    signal_key TEXT UNIQUE NOT NULL,
                    trade_date TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    name TEXT NOT NULL,
                    side TEXT NOT NULL,
                    side_label TEXT NOT NULL,
                    signal_source TEXT NOT NULL,
                    rule_id TEXT NOT NULL,
                    rule_title TEXT NOT NULL,
                    rule_summary TEXT NOT NULL,
                    minute TEXT NOT NULL,
                    signal_price TEXT NOT NULL,
                    limit_price TEXT NOT NULL,
                    precision INTEGER NOT NULL,
                    quantity INTEGER NOT NULL,
                    estimated_amount REAL NOT NULL,
                    execution_mode TEXT NOT NULL,
                    is_simulation INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL,
                    entrust_no TEXT,
                    dialog_message TEXT,
                    timings_json TEXT,
                    price_basis TEXT,
                    order_book_json TEXT,
                    cancel_pending INTEGER NOT NULL DEFAULT 0,
                    cancel_due_at REAL,
                    expires_at REAL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_auto_trade_signals_status_created
                    ON auto_trade_signals(status, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_auto_trade_signals_date_buy
                    ON auto_trade_signals(trade_date, side, status);
                CREATE TABLE IF NOT EXISTS auto_trade_idempotency (
                    idempotency_key TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    status_code INTEGER NOT NULL,
                    response_json TEXT NOT NULL,
                    expires_at REAL NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (scope, idempotency_key)
                );
                CREATE INDEX IF NOT EXISTS idx_auto_trade_idemp_exp
                    ON auto_trade_idempotency(expires_at);
                """
            )
            cols = {
                row[1]
                for row in self._connection.execute("PRAGMA table_info(auto_trade_signals)").fetchall()
            }
            if "price_basis" not in cols:
                self._connection.execute("ALTER TABLE auto_trade_signals ADD COLUMN price_basis TEXT")
            if "order_book_json" not in cols:
                self._connection.execute("ALTER TABLE auto_trade_signals ADD COLUMN order_book_json TEXT")
        # Ensure defaults are seeded on first boot
        with self._lock, self._connection:
            existing = self._connection.execute(
                "SELECT value_json FROM research_settings WHERE key=?",
                (self.SETTINGS_KEY,),
            ).fetchone()
            if existing is None:
                now_str = _iso_shanghai()
                self._connection.execute(
                    "INSERT INTO research_settings (key, value_json, updated_at) VALUES (?, ?, ?)",
                    (self.SETTINGS_KEY, json.dumps(DEFAULT_AUTO_TRADE_CONFIG, ensure_ascii=False), now_str),
                )

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def get_config(self) -> dict[str, Any]:
        with self._lock:
            row = self._connection.execute(
                "SELECT value_json FROM research_settings WHERE key=?",
                (self.SETTINGS_KEY,),
            ).fetchone()
        if row is None:
            return copy.deepcopy(DEFAULT_AUTO_TRADE_CONFIG)
        try:
            parsed = json.loads(row["value_json"])
        except Exception:
            parsed = None
        return _normalize_config(parsed)

    @staticmethod
    def _build_merged_candidate(current: Mapping[str, Any], patch: Mapping[str, Any]) -> dict[str, Any]:
        patch_dict = dict(patch)
        merged = {**current, **patch_dict}
        pre_norm = _normalize_config(merged)
        # Keep single-symbol legacy callers in sync with symbol_profiles when caller omitted symbol_profiles
        if "symbol_profiles" not in patch_dict and len(pre_norm["symbols"]) == 1:
            sole_sym = pre_norm["symbols"][0]
            existing_prof = (
                (current.get("symbol_profiles") or {}).get(sole_sym)
                if isinstance(current.get("symbol_profiles"), Mapping)
                else {}
            )
            prof_patch: dict[str, Any] = dict(existing_p) if isinstance((existing_p := existing_prof), Mapping) else {}
            if "signal_source" in patch_dict or "quant_rule_id" in patch_dict:
                prof_patch.pop("rule_bindings", None)
            if "signal_source" in patch_dict:
                prof_patch["signal_source"] = pre_norm["signal_source"]
            if "quant_rule_id" in patch_dict:
                prof_patch["quant_rule_id"] = pre_norm["quant_rule_id"]
            if "sizing_mode" in patch_dict:
                prof_patch["sizing_mode"] = pre_norm["sizing_mode"]
            if "daily_max_buy_amount" in patch_dict:
                prof_patch["capital_cap"] = pre_norm["daily_max_buy_amount"]
            if "order_quantity" in patch_dict:
                prof_patch["order_quantity"] = pre_norm["order_quantity"]
            if "order_book_level" in patch_dict:
                prof_patch["order_book_level"] = pre_norm["order_book_level"]
            pre_norm["symbol_profiles"][sole_sym] = _normalize_symbol_profile(sole_sym, prof_patch, pre_norm)
        elif "symbol_profiles" in patch_dict:
            enabled_caps = [
                float(p["capital_cap"])
                for p in pre_norm["symbol_profiles"].values()
                if p.get("enabled", True)
            ]
            if enabled_caps:
                pre_norm["daily_max_buy_amount"] = round(min(10_000_000.0, sum(enabled_caps)), 2)
        return pre_norm

    def update_config(self, patch: Mapping[str, Any]) -> dict[str, Any]:
        with self._lock, self._connection:
            current = self.get_config()
            normalized = self._build_merged_candidate(current, patch)
            self._connection.execute(
                """
                INSERT INTO research_settings (key, value_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value_json = excluded.value_json,
                    updated_at = excluded.updated_at
                """,
                (self.SETTINGS_KEY, json.dumps(normalized, ensure_ascii=False), _iso_shanghai()),
            )
            return normalized

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        timings = None
        if row["timings_json"]:
            try:
                timings = json.loads(row["timings_json"])
            except Exception:
                timings = None
        order_book = None
        if "order_book_json" in row.keys() and row["order_book_json"]:
            try:
                order_book = json.loads(row["order_book_json"])
            except Exception:
                order_book = None
        return {
            "id": row["id"],
            "signal_key": row["signal_key"],
            "trade_date": row["trade_date"],
            "symbol": row["symbol"],
            "name": row["name"],
            "side": row["side"],
            "side_label": row["side_label"],
            "signal_source": row["signal_source"],
            "rule_id": row["rule_id"],
            "rule_title": row["rule_title"],
            "rule_summary": row["rule_summary"],
            "minute": row["minute"],
            "signal_price": row["signal_price"],
            "limit_price": row["limit_price"],
            "precision": int(row["precision"]),
            "quantity": int(row["quantity"]),
            "estimated_amount": float(row["estimated_amount"]),
            "execution_mode": row["execution_mode"],
            "is_simulation": bool(row["is_simulation"]),
            "status": row["status"],
            "entrust_no": row["entrust_no"],
            "dialog_message": row["dialog_message"],
            "timings_ms": timings,
            "price_basis": row["price_basis"] if "price_basis" in row.keys() else None,
            "order_book": order_book,
            "cancel_pending": bool(row["cancel_pending"]),
            "cancel_due_at": row["cancel_due_at"],
            "expires_at": row["expires_at"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def get_signal(self, signal_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM auto_trade_signals WHERE id=?",
                (signal_id,),
            ).fetchone()
        return self._row_to_dict(row) if row is not None else None

    def claim_and_insert_signal(
        self,
        *,
        signal_key: str,
        trade_date: str,
        symbol: str,
        name: str,
        side: Literal["buy", "sell"],
        signal_source: str,
        rule_id: str,
        rule_title: str,
        rule_summary: str,
        minute: str,
        signal_price: str,
        limit_price: str,
        precision: int,
        quantity: int,
        execution_mode: str,
        is_simulation: bool,
        status: str,
        dialog_message: str | None = None,
        price_basis: str | None = None,
        order_book: Mapping[str, Any] | None = None,
        expires_at: float | None = None,
        now_str: str | None = None,
    ) -> dict[str, Any] | None:
        side_label = "买入" if side == "buy" else "卖出"
        est_amount = round(float(limit_price) * int(quantity), 2)
        ts = now_str or _iso_shanghai()
        sig_id = f"sig_{uuid.uuid4().hex[:12]}"
        ob_json = json.dumps(dict(order_book), ensure_ascii=False) if order_book is not None else None
        with self._lock, self._connection:
            existing = self._connection.execute(
                "SELECT 1 FROM auto_trade_signals WHERE signal_key=?",
                (signal_key,),
            ).fetchone()
            if existing is not None:
                return None
            self._connection.execute(
                """
                INSERT INTO auto_trade_signals (
                    id, signal_key, trade_date, symbol, name, side, side_label,
                    signal_source, rule_id, rule_title, rule_summary, minute,
                    signal_price, limit_price, precision, quantity, estimated_amount,
                    execution_mode, is_simulation, status, entrust_no, dialog_message,
                    timings_json, price_basis, order_book_json,
                    cancel_pending, cancel_due_at, expires_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, NULL, ?, ?, 0, NULL, ?, ?, ?)
                """,
                (
                    sig_id,
                    signal_key,
                    trade_date,
                    symbol,
                    name,
                    side,
                    side_label,
                    signal_source,
                    rule_id,
                    rule_title,
                    rule_summary,
                    minute,
                    signal_price,
                    limit_price,
                    precision,
                    int(quantity),
                    est_amount,
                    execution_mode,
                    1 if is_simulation else 0,
                    status,
                    dialog_message,
                    price_basis,
                    ob_json,
                    expires_at,
                    ts,
                    ts,
                ),
            )
            row = self._connection.execute(
                "SELECT * FROM auto_trade_signals WHERE id=?",
                (sig_id,),
            ).fetchone()
            assert row is not None
            return self._row_to_dict(row)

    def update_signal_execution(
        self,
        signal_id: str,
        *,
        status: str,
        limit_price: str | None = None,
        quantity: int | None = None,
        entrust_no: str | None = None,
        dialog_message: str | None = None,
        timings_ms: Mapping[str, Any] | None = None,
        price_basis: str | None = None,
        order_book: Mapping[str, Any] | None = None,
        cancel_pending: bool | None = None,
        cancel_due_at: float | None = None,
        now_str: str | None = None,
    ) -> dict[str, Any]:
        with self._lock, self._connection:
            row = self._connection.execute(
                "SELECT * FROM auto_trade_signals WHERE id=?",
                (signal_id,),
            ).fetchone()
            if row is None:
                raise KeyError(signal_id)
            next_price = limit_price if limit_price is not None else row["limit_price"]
            next_qty = int(quantity) if quantity is not None else int(row["quantity"])
            next_est = round(float(next_price) * next_qty, 2)
            next_entrust = entrust_no if entrust_no is not None else row["entrust_no"]
            next_msg = dialog_message if dialog_message is not None else row["dialog_message"]
            next_timings = (
                json.dumps(dict(timings_ms), ensure_ascii=False)
                if timings_ms is not None
                else row["timings_json"]
            )
            next_basis = price_basis if price_basis is not None else (row["price_basis"] if "price_basis" in row.keys() else None)
            next_ob = (
                json.dumps(dict(order_book), ensure_ascii=False)
                if order_book is not None
                else (row["order_book_json"] if "order_book_json" in row.keys() else None)
            )
            next_cancel_pending = (
                (1 if cancel_pending else 0)
                if cancel_pending is not None
                else int(row["cancel_pending"])
            )
            next_cancel_due = (
                cancel_due_at
                if cancel_pending is not None
                else row["cancel_due_at"]
            )
            ts = now_str or _iso_shanghai()
            self._connection.execute(
                """
                UPDATE auto_trade_signals
                SET status=?,
                    limit_price=?,
                    quantity=?,
                    estimated_amount=?,
                    entrust_no=?,
                    dialog_message=?,
                    timings_json=?,
                    price_basis=?,
                    order_book_json=?,
                    cancel_pending=?,
                    cancel_due_at=?,
                    updated_at=?
                WHERE id=?
                """,
                (
                    status,
                    next_price,
                    next_qty,
                    next_est,
                    next_entrust,
                    next_msg,
                    next_timings,
                    next_basis,
                    next_ob,
                    next_cancel_pending,
                    next_cancel_due,
                    ts,
                    signal_id,
                ),
            )
            updated = self._connection.execute(
                "SELECT * FROM auto_trade_signals WHERE id=?",
                (signal_id,),
            ).fetchone()
            assert updated is not None
            return self._row_to_dict(updated)

    def expire_overdue_signals(self, now_epoch: float, *, now_str: str | None = None) -> int:
        with self._lock:
            has_due = self._connection.execute(
                """
                SELECT 1 FROM auto_trade_signals
                WHERE status='PENDING_CONFIRM' AND expires_at IS NOT NULL AND expires_at < ?
                LIMIT 1
                """,
                (float(now_epoch),),
            ).fetchone()
            if has_due is None:
                return 0
        ts = now_str or _iso_shanghai()
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """
                UPDATE auto_trade_signals
                SET status='EXPIRED',
                    dialog_message=COALESCE(dialog_message, '等待确认超时，信号已自动作废'),
                    updated_at=?
                WHERE status='PENDING_CONFIRM' AND expires_at IS NOT NULL AND expires_at < ?
                """,
                (ts, float(now_epoch)),
            )
            return int(cursor.rowcount or 0)

    def list_today_executed_signals(self, trade_date: str, *, limit: int = 100) -> list[dict[str, Any]]:
        """Return today's non-simulation quant execution records for trade attribution."""
        exec_statuses = ("EXECUTING", "SUBMITTED", "FILLED_OR_CLOSED", "AUTO_CANCELLED_TIMEOUT")
        placeholders = ",".join("?" for _ in exec_statuses)
        with self._lock:
            rows = self._connection.execute(
                f"""
                SELECT * FROM auto_trade_signals
                WHERE trade_date=? AND is_simulation=0 AND status IN ({placeholders})
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (str(trade_date), *exec_statuses, max(1, min(200, int(limit)))),
            ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def list_due_cancels(self, now_epoch: float) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT * FROM auto_trade_signals
                WHERE cancel_pending=1 AND cancel_due_at IS NOT NULL AND cancel_due_at <= ?
                ORDER BY cancel_due_at ASC
                """,
                (float(now_epoch),),
            ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def list_pending_signals(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT * FROM auto_trade_signals
                WHERE status IN ('PENDING_CONFIRM', 'QUEUED', 'EXECUTING', 'RECONCILING')
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (max(1, min(100, int(limit))),),
            ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def list_reconciling_signals(self, trade_date: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT * FROM auto_trade_signals
                WHERE trade_date=? AND is_simulation=0 AND status='RECONCILING'
                ORDER BY created_at ASC
                """,
                (str(trade_date),),
            ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def list_active_opposite_orders(
        self,
        *,
        trade_date: str,
        symbol: str,
        side: Literal["buy", "sell"],
        exclude_signal_id: str | None = None,
    ) -> list[dict[str, Any]]:
        opp_side = "sell" if side == "buy" else "buy"
        params: list[Any] = [str(trade_date), str(symbol).strip(), opp_side]
        extra = ""
        if exclude_signal_id:
            extra = " AND id != ?"
            params.append(exclude_signal_id)
        with self._lock:
            rows = self._connection.execute(
                f"""
                SELECT * FROM auto_trade_signals
                WHERE trade_date=? AND symbol=? AND side=? AND is_simulation=0
                  AND status='SUBMITTED' AND cancel_pending=1{extra}
                ORDER BY created_at ASC
                """,
                tuple(params),
            ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def check_idempotency(
        self,
        *,
        scope: str,
        idempotency_key: str,
        payload_hash: str,
        now_epoch: float,
    ) -> tuple[str, dict[str, Any] | None]:
        with self._lock, self._connection:
            self._connection.execute(
                "DELETE FROM auto_trade_idempotency WHERE expires_at <= ?",
                (float(now_epoch),),
            )
            row = self._connection.execute(
                """
                SELECT payload_hash, status_code, response_json
                FROM auto_trade_idempotency
                WHERE scope=? AND idempotency_key=? AND expires_at > ?
                """,
                (scope, idempotency_key, float(now_epoch)),
            ).fetchone()
        if row is None:
            return "miss", None
        if str(row["payload_hash"]) != str(payload_hash):
            return "conflict", None
        try:
            parsed = json.loads(str(row["response_json"]))
        except Exception:
            return "miss", None
        return "hit", {"status_code": int(row["status_code"]), "body": parsed}

    def save_idempotency(
        self,
        *,
        scope: str,
        idempotency_key: str,
        payload_hash: str,
        status_code: int,
        body: Mapping[str, Any],
        ttl_seconds: float = 86400.0,
        now_epoch: float | None = None,
        now_str: str | None = None,
    ) -> None:
        exp = float(now_epoch if now_epoch is not None else time.time()) + max(60.0, float(ttl_seconds))
        ts = now_str or _iso_shanghai()
        encoded = json.dumps(dict(body), ensure_ascii=False)
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO auto_trade_idempotency (
                    idempotency_key, scope, payload_hash, status_code, response_json, expires_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(scope, idempotency_key) DO UPDATE SET
                    payload_hash = excluded.payload_hash,
                    status_code = excluded.status_code,
                    response_json = excluded.response_json,
                    expires_at = excluded.expires_at
                """,
                (idempotency_key, scope, payload_hash, int(status_code), encoded, exp, ts),
            )

    def list_recent_signals(self, limit: int = 25) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT * FROM auto_trade_signals
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (max(1, min(100, int(limit))),),
            ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def today_buy_usage(self, trade_date: str, *, exclude_signal_id: str | None = None) -> dict[str, Any]:
        placeholders = ",".join("?" for _ in _ACTIVE_BUY_STATUSES)
        params: list[Any] = [trade_date, *_ACTIVE_BUY_STATUSES]
        extra = ""
        if exclude_signal_id:
            extra = " AND id != ?"
            params.append(exclude_signal_id)
        with self._lock:
            row = self._connection.execute(
                f"""
                SELECT COALESCE(SUM(estimated_amount), 0.0) AS total_amount,
                       COUNT(*) AS order_count
                FROM auto_trade_signals
                WHERE trade_date=? AND side='buy' AND is_simulation=0
                  AND status IN ({placeholders}){extra}
                """,
                tuple(params),
            ).fetchone()
        return {
            "trade_date": trade_date,
            "buy_amount": round(float(row["total_amount"] or 0.0), 2),
            "order_count": int(row["order_count"] or 0),
        }

    def today_symbol_buy_usage(
        self,
        trade_date: str,
        symbol: str,
        *,
        exclude_signal_id: str | None = None,
    ) -> dict[str, Any]:
        placeholders = ",".join("?" for _ in _ACTIVE_BUY_STATUSES)
        params: list[Any] = [trade_date, str(symbol).strip(), *_ACTIVE_BUY_STATUSES]
        extra = ""
        if exclude_signal_id:
            extra = " AND id != ?"
            params.append(exclude_signal_id)
        with self._lock:
            row = self._connection.execute(
                f"""
                SELECT COALESCE(SUM(estimated_amount), 0.0) AS total_amount,
                       COUNT(*) AS order_count
                FROM auto_trade_signals
                WHERE trade_date=? AND symbol=? AND side='buy' AND is_simulation=0
                  AND status IN ({placeholders}){extra}
                """,
                tuple(params),
            ).fetchone()
        return {
            "trade_date": trade_date,
            "symbol": str(symbol).strip(),
            "buy_amount": round(float(row["total_amount"] or 0.0), 2),
            "order_count": int(row["order_count"] or 0),
        }

    def today_symbol_activity(self, trade_date: str, symbol: str) -> dict[str, Any]:
        placeholders = ",".join("?" for _ in _POSITION_LEDGER_STATUSES)
        with self._lock:
            rows = self._connection.execute(
                f"""
                SELECT side, quantity, estimated_amount, signal_key
                FROM auto_trade_signals
                WHERE trade_date=? AND symbol=? AND is_simulation=0
                  AND status IN ({placeholders})
                """,
                (trade_date, symbol, *_POSITION_LEDGER_STATUSES),
            ).fetchall()
        buy_shares = 0
        sell_shares = 0
        buy_amount = 0.0
        sell_amount = 0.0
        entry_count = 0
        for row in rows:
            qty = int(row["quantity"] or 0)
            amt = float(row["estimated_amount"] or 0.0)
            is_flatten = ":t0-flatten:" in str(row["signal_key"] or "")
            if not is_flatten:
                entry_count += 1
            if str(row["side"]) == "buy":
                buy_shares += qty
                buy_amount += amt
            else:
                sell_shares += qty
                sell_amount += amt
        return {
            "buy_shares": buy_shares,
            "sell_shares": sell_shares,
            "net_shares": buy_shares - sell_shares,
            "buy_amount": round(buy_amount, 2),
            "sell_amount": round(sell_amount, 2),
            "entry_count": entry_count,
        }

    def rolling_symbol_position(
        self,
        symbol: str,
        *,
        trade_date: str,
        same_day_turnaround: bool,
        exclude_signal_id: str | None = None,
    ) -> dict[str, Any]:
        placeholders = ",".join("?" for _ in _POSITION_LEDGER_STATUSES)
        params: list[Any] = [symbol, *_POSITION_LEDGER_STATUSES]
        extra = ""
        if exclude_signal_id:
            extra = " AND id != ?"
            params.append(exclude_signal_id)
        with self._lock:
            rows = self._connection.execute(
                f"""
                SELECT trade_date, side, quantity
                FROM auto_trade_signals
                WHERE symbol=? AND is_simulation=0
                  AND status IN ({placeholders}){extra}
                """,
                tuple(params),
            ).fetchall()
        cum_buy = 0
        cum_sell = 0
        today_buy = 0
        for row in rows:
            qty = int(row["quantity"] or 0)
            if str(row["side"]) == "buy":
                cum_buy += qty
                if str(row["trade_date"]) == trade_date:
                    today_buy += qty
            else:
                cum_sell += qty
        net_shares = max(0, cum_buy - cum_sell)
        sellable_shares = net_shares if same_day_turnaround else max(0, net_shares - today_buy)
        return {
            "net_shares": net_shares,
            "sellable_shares": sellable_shares,
        }

    def list_today_symbol_signals(self, trade_date: str, symbol: str) -> list[dict[str, Any]]:
        placeholders = ",".join("?" for _ in _POSITION_LEDGER_STATUSES)
        with self._lock:
            rows = self._connection.execute(
                f"""
                SELECT id, signal_key, side, quantity, signal_price, limit_price, minute, status
                FROM auto_trade_signals
                WHERE trade_date=? AND symbol=? AND is_simulation=0
                  AND status IN ({placeholders})
                ORDER BY minute ASC, created_at ASC
                """,
                (trade_date, symbol, *_POSITION_LEDGER_STATUSES),
            ).fetchall()
        return [
            {
                "id": str(r["id"]),
                "signal_key": str(r["signal_key"]),
                "side": str(r["side"]),
                "quantity": int(r["quantity"] or 0),
                "signal_price": str(r["signal_price"]),
                "limit_price": str(r["limit_price"]),
                "minute": str(r["minute"]),
                "status": str(r["status"]),
            }
            for r in rows
        ]

    def net_signal_delta_since(
        self,
        symbol: str,
        *,
        since_iso: str,
        exclude_signal_id: str | None = None,
    ) -> int:
        """Return net executed/active strategy share delta placed strictly after `since_iso`."""
        cleaned_since = str(since_iso or "").strip()
        if not cleaned_since:
            return 0
        placeholders = ",".join("?" for _ in _POSITION_LEDGER_STATUSES)
        params: list[Any] = [symbol, cleaned_since, *_POSITION_LEDGER_STATUSES]
        extra = ""
        if exclude_signal_id:
            extra = " AND id != ?"
            params.append(exclude_signal_id)
        with self._lock:
            rows = self._connection.execute(
                f"""
                SELECT side, quantity
                FROM auto_trade_signals
                WHERE symbol=? AND is_simulation=0 AND created_at > ?
                  AND status IN ({placeholders}){extra}
                """,
                tuple(params),
            ).fetchall()
        delta = 0
        for row in rows:
            qty = int(row["quantity"] or 0)
            if str(row["side"]) == "buy":
                delta += qty
            else:
                delta -= qty
        return delta

    def latest_symbol_signal_price(self, symbol: str) -> float:
        """Return most recent recorded non-simulation signal price for fallback valuation."""
        with self._lock:
            row = self._connection.execute(
                """
                SELECT limit_price, signal_price
                FROM auto_trade_signals
                WHERE symbol=? AND is_simulation=0
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (symbol,),
            ).fetchone()
        if row is None:
            return 0.0
        for raw in (row["limit_price"], row["signal_price"]):
            try:
                val = float(raw or 0.0)
                if val > 0:
                    return val
            except (TypeError, ValueError):
                continue
        return 0.0

    def net_cash_delta_since(
        self,
        *,
        since_iso: str,
        exclude_signal_id: str | None = None,
    ) -> float:
        """Return net executed/active strategy cash delta strictly after `since_iso`."""
        cleaned_since = str(since_iso or "").strip()
        if not cleaned_since:
            return 0.0
        placeholders = ",".join("?" for _ in _POSITION_LEDGER_STATUSES)
        params: list[Any] = [cleaned_since, *_POSITION_LEDGER_STATUSES]
        extra = ""
        if exclude_signal_id:
            extra = " AND id != ?"
            params.append(exclude_signal_id)
        with self._lock:
            rows = self._connection.execute(
                f"""
                SELECT side, quantity, limit_price, signal_price
                FROM auto_trade_signals
                WHERE is_simulation=0 AND created_at > ?
                  AND status IN ({placeholders}){extra}
                """,
                tuple(params),
            ).fetchall()
        delta = 0.0
        for row in rows:
            qty = int(row["quantity"] or 0)
            px = 0.0
            for raw in (row["limit_price"], row["signal_price"]):
                try:
                    val = float(raw or 0.0)
                    if val > 0:
                        px = val
                        break
                except (TypeError, ValueError):
                    continue
            amt = qty * px
            if str(row["side"]) == "buy":
                delta -= amt
            else:
                delta += amt
        return round(delta, 2)


class PriorityTradeScheduler:
    """Serialize broker UI actions by 4-tier priority and Buy/Sell tab affinity."""

    P0_CRITICAL = 0      # Self-trade pre-cancel, closing flatten, manual cancel
    P1_ORDER = 1         # Full-auto / semi-auto confirm orders
    P2_MAINTENANCE = 2   # Timeout auto-cancels
    P3_BACKGROUND = 3    # Account sync, keep-warm, prestage

    def __init__(
        self,
        *,
        exec_lock: asyncio.Lock,
        active_tab_getter: Callable[[], str | None] | None = None,
    ) -> None:
        self._exec_lock = exec_lock
        self._active_tab_getter = active_tab_getter
        self._waiters: list[tuple[int, int, str | None, str | None, asyncio.Future[None]]] = []
        self._seq = 0
        self._last_granted_side: str | None = None
        self._dispatching = False
        self._active_priority: int | None = None
        self._active_side: str | None = None

    @staticmethod
    def _norm_side(side: str | None) -> str | None:
        s = str(side or "").strip().lower()
        return s if s in {"buy", "sell"} else None

    def _current_tab(self) -> str | None:
        if callable(self._active_tab_getter):
            try:
                tab = self._norm_side(self._active_tab_getter())
                if tab is not None:
                    return tab
            except Exception:
                pass
        return self._last_granted_side

    def pending_count(self, *, max_priority: int | None = None) -> int:
        count = 0
        for prio, _seq, _side, _sym, fut in self._waiters:
            if fut.done():
                continue
            if max_priority is None or prio <= max_priority:
                count += 1
        return count

    def has_pending(self, *, max_priority: int | None = None) -> bool:
        return self.pending_count(max_priority=max_priority) > 0

    def is_busy_or_queued(self, *, max_priority: int | None = None) -> bool:
        if self._exec_lock.locked():
            if max_priority is None or self._active_priority is None or self._active_priority <= max_priority:
                return True
        return self.has_pending(max_priority=max_priority)

    def _wake_next(self) -> None:
        if self._dispatching or self._exec_lock.locked():
            return
        self._waiters = [w for w in self._waiters if not w[4].done()]
        if not self._waiters:
            return
        min_prio = min(w[0] for w in self._waiters)
        current_tab = self._current_tab()
        chosen_idx: int | None = None
        if current_tab is not None:
            for idx, (prio, _seq, side, _sym, fut) in enumerate(self._waiters):
                if prio == min_prio and side == current_tab and not fut.done():
                    chosen_idx = idx
                    break
        if chosen_idx is None:
            for idx, (prio, _seq, _side, _sym, fut) in enumerate(self._waiters):
                if prio == min_prio and not fut.done():
                    chosen_idx = idx
                    break
        if chosen_idx is not None:
            _prio, _seq, _side, _sym, fut = self._waiters.pop(chosen_idx)
            self._dispatching = True
            fut.set_result(None)

    @asynccontextmanager
    async def hold(
        self,
        priority: int = P1_ORDER,
        *,
        side: str | None = None,
        symbol: str | None = None,
    ) -> AsyncIterator[None]:
        norm_side = self._norm_side(side)
        loop = asyncio.get_running_loop()
        woke_by_queue = False
        if self._exec_lock.locked() or self._dispatching or self._waiters:
            fut: asyncio.Future[None] = loop.create_future()
            self._seq += 1
            self._waiters.append((int(priority), self._seq, norm_side, symbol, fut))
            self._wake_next()
            try:
                await fut
                woke_by_queue = True
            except BaseException:
                if fut.done() and not fut.cancelled():
                    self._dispatching = False
                self._waiters = [w for w in self._waiters if w[4] is not fut]
                self._wake_next()
                raise
        try:
            await self._exec_lock.acquire()
        except BaseException:
            if woke_by_queue:
                self._dispatching = False
                self._wake_next()
            raise
        if woke_by_queue:
            self._dispatching = False
        self._active_priority = int(priority)
        self._active_side = norm_side
        if norm_side is not None:
            self._last_granted_side = norm_side
        try:
            yield
        finally:
            self._active_priority = None
            self._active_side = None
            self._exec_lock.release()
            self._wake_next()


class AutoTradeEngine:
    """Coordinate strategy signal evaluation, risk gates, push alerts, and broker execution."""

    def __init__(
        self,
        *,
        auto_store: SQLiteAutoTradeStore,
        research_store: object,
        broker: object | None,
        trade_executor: object | None,
        push_dispatcher: object | None = None,
        symbol_lookup: Callable[[str], Any] | None = None,
        is_trading_day: Callable[[date], bool] | None = None,
        now_fn: Callable[[], datetime] | None = None,
        epoch_fn: Callable[[], float] | None = None,
        quant_rule_store: object | None = None,
        order_book_fetcher: Callable[[str, str | None], Mapping[str, Any] | None] | None = None,
        batch_order_book_fetcher: Callable[[Sequence[str]], Mapping[str, Mapping[str, Any]]] | None = None,
    ) -> None:
        self.auto_store = auto_store
        self.research_store = research_store
        self.broker = broker
        self.trade_executor = trade_executor
        self.push_dispatcher = push_dispatcher
        self.symbol_lookup = symbol_lookup
        self.is_trading_day = is_trading_day
        self.now_fn = now_fn
        self.epoch_fn = epoch_fn or time.time
        self.quant_rule_store = quant_rule_store
        self.order_book_fetcher = order_book_fetcher
        self.batch_order_book_fetcher = batch_order_book_fetcher
        self._order_book_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._order_book_ttl_seconds: float = 1.5
        self._exec_lock = asyncio.Lock()
        self._scheduler = PriorityTradeScheduler(
            exec_lock=self._exec_lock,
            active_tab_getter=lambda: getattr(self.trade_executor, "active_tab", None),
        )
        self.last_poll_at: str | None = None
        self._viewer_active_until: float = 0.0
        self._line_funds_last_at: dict[str, float] = {}
        self._last_warm_at: float = 0.0
        self._last_gateway_touch_at: float = 0.0
        self._last_preapproach_prestage_at: float = 0.0
        self._last_account_sync_at: float = 0.0
        self._account_sync_inflight: bool = False
        self.account_sync_interval_seconds: float = 45.0
        self._bg_tasks: set[asyncio.Task[Any]] = set()
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()

    def subscribe(self, *, maxsize: int = 128) -> asyncio.Queue[dict[str, Any]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=max(16, int(maxsize)))
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        self._subscribers.discard(q)

    def _publish_event(self, event_type: str, payload: Mapping[str, Any] | None = None) -> None:
        if not self._subscribers:
            return
        event = {
            "type": event_type,
            "at": self._local_now().isoformat(timespec="seconds"),
            **(dict(payload) if payload else {}),
        }
        for q in list(self._subscribers):
            if q.full():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass

    def _local_now(self) -> datetime:
        return _now_shanghai(self.now_fn)

    def _epoch(self) -> float:
        return float(self.epoch_fn())

    def set_viewer_open(self, viewer_open: bool, *, ttl_seconds: float = 12.0) -> bool:
        if viewer_open:
            self._viewer_active_until = max(
                self._viewer_active_until,
                self._epoch() + max(1.0, float(ttl_seconds)),
            )
        else:
            self._viewer_active_until = 0.0
        return self.is_viewer_active()

    def is_viewer_active(self) -> bool:
        return self._epoch() < self._viewer_active_until

    def _summarize_quant_rules(self, active_id: str | None) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
        qstore = self.quant_rule_store
        list_fn = getattr(qstore, "list_rules", None) if qstore is not None else None
        if not callable(list_fn):
            return [], None
        rows = list_fn()
        summaries: list[dict[str, Any]] = []
        active_summary: dict[str, Any] | None = None
        for r in rows:
            ev = r.get("latest_eval") if isinstance(r.get("latest_eval"), Mapping) else {}
            sm = ev.get("summary") if isinstance(ev.get("summary"), Mapping) else {}
            t0 = ev.get("base_position_t0") if isinstance(ev.get("base_position_t0"), Mapping) else {}
            csim = ev.get("capital_sim") if isinstance(ev.get("capital_sim"), Mapping) else {}
            ps = normalize_position_sizing(r.get("position_sizing"))
            item = {
                "id": r["id"],
                "name": r["name"],
                "source": r["source"],
                "symbols": r.get("symbols") or ["160723"],
                "enabled": bool(r.get("enabled", True)),
                "position_sizing": ps,
                "overall_win_rate_pct": sm.get("overall_win_rate_pct"),
                "buy_win_rate_pct": sm.get("buy_win_rate_pct"),
                "sell_win_rate_pct": sm.get("sell_win_rate_pct"),
                "buy_avg_pct": sm.get("buy_avg_pct"),
                "sell_avg_drop_pct": sm.get("sell_avg_drop_pct"),
                "total_signals": sm.get("total_signals", 0),
                "t0_net_return_pct": t0.get("net_cum_return_pct"),
                "t0_gross_return_pct": t0.get("gross_cum_return_pct"),
                "t0_compound_net_return_pct": t0.get("compound_net_return_pct"),
                "t0_traded_days": t0.get("traded_days"),
                "t0_daily_win_rate_pct": t0.get("daily_win_rate_pct"),
                "t0_max_drawdown_pct": t0.get("max_drawdown_pct"),
                "t0_alpha_vs_hold_pct": t0.get("alpha_vs_hold_pct"),
                "t0_start_date": t0.get("start_date"),
                "capital_net_return_pct": csim.get("net_return_pct"),
                "capital_net_profit": csim.get("net_profit_amount"),
                "capital_avg_shares": csim.get("avg_order_shares"),
                "updated_at": r.get("updated_at"),
            }
            summaries.append(item)
            if active_id and r["id"] == active_id:
                active_summary = item
        if active_summary is None and summaries:
            active_summary = next((x for x in summaries if x["enabled"]), summaries[0])
        return summaries, active_summary

    @staticmethod
    def _parse_pos_float(raw: Any) -> float:
        if raw is None or raw == "":
            return 0.0
        if isinstance(raw, (int, float)):
            return float(raw) if raw == raw else 0.0
        cleaned = str(raw).replace(",", "").replace("¥", "").replace("元", "").strip()
        if not cleaned:
            return 0.0
        try:
            val = float(cleaned)
            return val if val == val else 0.0
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _parse_opt_float(raw: Any) -> float | None:
        if raw is None or raw == "":
            return None
        if isinstance(raw, (int, float)):
            return float(raw) if math.isfinite(float(raw)) else None
        cleaned = str(raw).replace(",", "").replace("¥", "").replace("元", "").strip()
        if not cleaned or cleaned in {"--", "—", "-"}:
            return None
        try:
            val = float(cleaned)
            return val if math.isfinite(val) else None
        except (TypeError, ValueError):
            return None

    def _effective_symbol_config(self, config: Mapping[str, Any], symbol: str) -> dict[str, Any]:
        clean_sym = str(symbol or "").strip()
        eff = dict(config)
        profiles = config.get("symbol_profiles")
        prof = profiles.get(clean_sym) if isinstance(profiles, Mapping) else None
        if isinstance(prof, Mapping):
            eff["symbol_enabled"] = bool(prof.get("enabled", True))
            sig_src = str(prof.get("signal_source") or eff.get("signal_source") or "intraday_macd_flow")
            if sig_src in VALID_SIGNAL_SOURCES:
                eff["signal_source"] = sig_src
            eff["quant_rule_id"] = prof.get("quant_rule_id")
            sz_mode = str(prof.get("sizing_mode") or eff.get("sizing_mode") or "fixed_qty")
            if sz_mode in VALID_SIZING_MODES:
                eff["sizing_mode"] = sz_mode
            eff["daily_max_buy_amount"] = float(prof.get("capital_cap") or eff.get("daily_max_buy_amount") or 50000.0)
            eff["order_quantity"] = int(prof.get("order_quantity") or eff.get("order_quantity") or 100)
            eff["order_book_level"] = int(prof.get("order_book_level") or eff.get("order_book_level") or 3)
            eff["rule_bindings"] = _normalize_rule_bindings(
                prof.get("rule_bindings"),
                str(eff["signal_source"]),
                eff.get("quant_rule_id"),
            )
        else:
            eff["symbol_enabled"] = True
            eff["rule_bindings"] = _normalize_rule_bindings(
                None,
                str(eff.get("signal_source") or "intraday_macd_flow"),
                eff.get("quant_rule_id"),
            )
        return eff

    def _build_capital_ledger(
        self,
        config: Mapping[str, Any],
        trade_date: str,
        *,
        broker_acct: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        acct = broker_acct if isinstance(broker_acct, Mapping) else self._peek_cached_broker_account()
        broker_connected = isinstance(acct, Mapping) and bool(acct.get("logged_in"))
        metrics = acct.get("metrics") if broker_connected and isinstance(acct.get("metrics"), Mapping) else {}
        available_cash = self._parse_opt_float(metrics.get("available_cash")) if broker_connected else None
        total_assets = self._parse_opt_float(metrics.get("total_assets")) if broker_connected else None
        total_market_value = self._parse_opt_float(metrics.get("market_value")) if broker_connected else None
        captured_at = str(acct.get("captured_at") or "").strip() if broker_connected else None

        symbols = [str(s).strip() for s in (config.get("symbols") or ["160723"]) if str(s).strip()]
        if not symbols:
            symbols = ["160723"]
        profiles = config.get("symbol_profiles") if isinstance(config.get("symbol_profiles"), Mapping) else {}
        rows: list[dict[str, Any]] = []

        for sym in symbols:
            raw_prof = profiles.get(sym) if isinstance(profiles, Mapping) else None
            prof = _normalize_symbol_profile(sym, raw_prof, config)
            sym_name, sym_mkt = self._resolve_symbol_meta(sym)
            is_fund = is_exchange_fund_symbol(sym, sym_mkt) or sym.startswith(_FUND_PREFIXES)
            same_day_t0 = is_t0_turnaround_symbol(sym)
            fee_label = "LOF/ETF 万0.5无最低" if is_fund else "A股佣金+卖出印花税0.05%"

            roll = self.auto_store.rolling_symbol_position(
                sym,
                trade_date=trade_date,
                same_day_turnaround=same_day_t0,
            )
            ledger_shares = int(roll["net_shares"])
            broker_pos = self._find_broker_position_for_symbol(sym, acct) if broker_connected else None

            if broker_connected:
                broker_shares: int | None = (
                    max(0, int(self._parse_pos_float(broker_pos.get("shares")))) if broker_pos is not None else 0
                )
                if broker_pos is not None:
                    raw_mv = self._parse_pos_float(broker_pos.get("market_value"))
                    if raw_mv > 0:
                        holding_mv = round(raw_mv, 2)
                    else:
                        px = self._resolve_symbol_latest_price(sym, trade_date, broker_pos)
                        holding_mv = round(broker_shares * px, 2) if broker_shares > 0 and px > 0 else 0.0
                else:
                    holding_mv = 0.0

                rec_shares = prof.get("reconciled_shares")
                rec_at = str(prof.get("reconciled_at") or "").strip()
                if rec_shares is not None:
                    delta = self.auto_store.net_signal_delta_since(sym, since_iso=rec_at) if rec_at else 0
                    expected_shares = max(0, int(rec_shares) + int(delta))
                    pos_diff = int(broker_shares) - expected_shares
                    pos_consistent: bool | None = pos_diff == 0
                elif ledger_shares > 0:
                    pos_diff = int(broker_shares) - ledger_shares
                    pos_consistent = pos_diff == 0
                else:
                    pos_diff = 0
                    pos_consistent = True
            else:
                eff_pos = self._effective_symbol_position(
                    sym,
                    trade_date=trade_date,
                    same_day_turnaround=same_day_t0,
                )
                broker_shares = None
                holding_mv = round(float(eff_pos["market_value"]), 2)
                pos_diff = 0
                pos_consistent = None

            cap_val = round(float(prof["capital_cap"]), 2)
            planned_occ = round(max(0.0, cap_val - holding_mv), 2)
            occ_cash = planned_occ if prof["enabled"] else 0.0
            below_hold = round(max(0.0, holding_mv - cap_val), 2) if prof["enabled"] and holding_mv > 0 else 0.0

            rows.append(
                {
                    "symbol": sym,
                    "name": sym_name,
                    "market": sym_mkt,
                    "is_fund": is_fund,
                    "same_day_t0": same_day_t0,
                    "fee_label": fee_label,
                    "enabled": bool(prof["enabled"]),
                    "signal_source": prof["signal_source"],
                    "quant_rule_id": prof["quant_rule_id"],
                    "sizing_mode": prof["sizing_mode"],
                    "order_quantity": int(prof["order_quantity"]),
                    "order_book_level": int(prof["order_book_level"]),
                    "broker_shares": broker_shares,
                    "ledger_shares": ledger_shares,
                    "holding_market_value": holding_mv,
                    "position_consistent": pos_consistent,
                    "position_diff_shares": pos_diff,
                    "capital_cap": cap_val,
                    "occupied_cash": occ_cash,
                    "planned_occupied_cash": planned_occ,
                    "other_occupied_cash": 0.0,
                    "remaining_allocatable_cash": None,
                    "max_allowed_cap": None,
                    "over_allocated_amount": 0.0,
                    "below_holding_amount": below_hold,
                    "reconciled_at": prof.get("reconciled_at"),
                }
            )

        total_occ = round(sum(float(r["occupied_cash"]) for r in rows), 2)
        total_cap = round(sum(float(r["capital_cap"]) for r in rows if r["enabled"]), 2)
        quant_hold_total = round(sum(float(r["holding_market_value"]) for r in rows), 2)
        non_quant_hold = (
            round(max(0.0, float(total_market_value) - quant_hold_total), 2)
            if total_market_value is not None
            else None
        )

        for r in rows:
            other_occ = round(max(0.0, total_occ - float(r["occupied_cash"])), 2)
            r["other_occupied_cash"] = other_occ
            if available_cash is not None:
                rem = round(max(0.0, float(available_cash) - other_occ), 2)
                r["remaining_allocatable_cash"] = rem
                r["max_allowed_cap"] = round(float(r["holding_market_value"]) + rem, 2)
                r["over_allocated_amount"] = round(max(0.0, float(r["occupied_cash"]) - rem), 2)

        unallocated_cash = (
            round(float(available_cash) - total_occ, 2) if available_cash is not None else None
        )
        conservation_ok = (
            (unallocated_cash >= -0.01 and all(r["below_holding_amount"] <= 0.01 for r in rows))
            if unallocated_cash is not None
            else all(r["below_holding_amount"] <= 0.01 for r in rows)
        )
        return {
            "broker_connected": broker_connected,
            "captured_at": captured_at,
            "total_assets": round(float(total_assets), 2) if total_assets is not None else None,
            "available_cash": round(float(available_cash), 2) if available_cash is not None else None,
            "total_market_value": round(float(total_market_value), 2) if total_market_value is not None else None,
            "quant_holding_market_value": quant_hold_total,
            "non_quant_holding_market_value": non_quant_hold,
            "total_capital_cap": total_cap,
            "total_occupied_cash": total_occ,
            "unallocated_cash": unallocated_cash,
            "conservation_ok": conservation_ok,
            "rows": rows,
        }

    def _peek_cached_broker_account(self) -> Mapping[str, Any] | None:
        fn = getattr(self.trade_executor, "_cached_account_snapshot", None) if self.trade_executor is not None else None
        if not callable(fn):
            return None
        try:
            acct = fn(0.0, allow_stale=True)
        except Exception:
            return None
        if not isinstance(acct, Mapping) or not acct.get("logged_in"):
            return None
        return acct

    def _find_broker_position_for_symbol(
        self,
        symbol: str,
        cached_acct: Mapping[str, Any] | None,
    ) -> Mapping[str, Any] | None:
        if not isinstance(cached_acct, Mapping):
            return None
        positions = cached_acct.get("positions")
        if not isinstance(positions, (list, tuple)):
            return None
        sym_clean = str(symbol or "").strip().upper()
        sym_name = ""
        for pos in positions:
            if not isinstance(pos, Mapping):
                continue
            p_sym = str(pos.get("symbol") or "").strip().upper()
            if p_sym and p_sym == sym_clean:
                return pos
        try:
            resolved_name, _ = self._resolve_symbol_meta(sym_clean)
            sym_name = resolved_name.strip()
        except Exception:
            sym_name = ""
        if not sym_name:
            return None
        for pos in positions:
            if not isinstance(pos, Mapping):
                continue
            p_name = str(pos.get("name") or "").strip()
            if p_name and (sym_name in p_name or p_name in sym_name):
                return pos
        return None

    def _resolve_symbol_latest_price(
        self,
        symbol: str,
        trade_date: str,
        broker_pos: Mapping[str, Any] | None = None,
    ) -> float:
        read_series = getattr(self.research_store, "read_series", None)
        if callable(read_series):
            for call_args in ((symbol, trade_date), (symbol,)):
                try:
                    series = read_series(*call_args)
                except Exception:
                    series = None
                if isinstance(series, Mapping):
                    pts = series.get("points") or []
                    for pt in reversed(pts):
                        if isinstance(pt, Mapping):
                            px = self._parse_pos_float(pt.get("price"))
                            if px > 0:
                                return px
                    for key in ("quote_price", "pre_close"):
                        px = self._parse_pos_float(series.get(key))
                        if px > 0:
                            return px
        try:
            prem_row, _ = self._lookup_premium_row(symbol)
            if isinstance(prem_row, Mapping):
                px = self._parse_pos_float(prem_row.get("current_price"))
                if px > 0:
                    return px
        except Exception:
            pass
        if isinstance(broker_pos, Mapping):
            px = self._parse_pos_float(broker_pos.get("current_price"))
            if px > 0:
                return px
        return self.auto_store.latest_symbol_signal_price(symbol)

    def _effective_symbol_position(
        self,
        symbol: str,
        *,
        trade_date: str,
        same_day_turnaround: bool,
        exclude_signal_id: str | None = None,
    ) -> dict[str, Any]:
        """Reconcile strategy rolling shares with cached broker account position and latest mark value."""
        roll = self.auto_store.rolling_symbol_position(
            symbol,
            trade_date=trade_date,
            same_day_turnaround=same_day_turnaround,
            exclude_signal_id=exclude_signal_id,
        )
        cached_acct = self._peek_cached_broker_account()
        broker_pos = self._find_broker_position_for_symbol(symbol, cached_acct)
        metrics = cached_acct.get("metrics") if isinstance(cached_acct, Mapping) else None
        has_authoritative_metrics = isinstance(metrics, Mapping) and (
            self._parse_opt_float(metrics.get("available_cash")) is not None
            or self._parse_opt_float(metrics.get("total_assets")) is not None
        )
        broker_shares = 0
        if broker_pos is not None or has_authoritative_metrics:
            broker_shares = (
                max(0, int(self._parse_pos_float(broker_pos.get("shares"))))
                if broker_pos is not None
                else 0
            )
            captured_at = str(cached_acct.get("captured_at") or "").strip() if cached_acct is not None else ""
            delta_after = (
                self.auto_store.net_signal_delta_since(
                    symbol,
                    since_iso=captured_at,
                    exclude_signal_id=exclude_signal_id,
                )
                if captured_at
                else 0
            )
            net_shares = max(0, broker_shares + delta_after)
            avail_base = (
                max(0, int(self._parse_pos_float(broker_pos.get("available_shares"))))
                if broker_pos is not None
                else 0
            )
            sellable_shares = (
                net_shares if same_day_turnaround else max(0, min(net_shares, avail_base + min(0, delta_after)))
            )
            source = "broker_account"
        else:
            net_shares = int(roll["net_shares"])
            sellable_shares = int(roll["sellable_shares"])
            source = "strategy_ledger" if net_shares > 0 else "none"

        price = self._resolve_symbol_latest_price(symbol, trade_date, broker_pos)
        if net_shares <= 0:
            market_value = 0.0
        elif price > 0:
            market_value = round(net_shares * price, 2)
        elif broker_pos is not None:
            raw_mv = self._parse_pos_float(broker_pos.get("market_value"))
            if raw_mv > 0 and broker_shares > 0:
                market_value = round(raw_mv * (net_shares / broker_shares), 2)
            else:
                market_value = round(raw_mv, 2)
        else:
            market_value = 0.0

        return {
            "net_shares": net_shares,
            "sellable_shares": sellable_shares,
            "price": round(price, 4) if price > 0 else None,
            "market_value": market_value,
            "source": source,
        }

    def _expected_symbol_ledger_shares(
        self,
        symbol: str,
        config: Mapping[str, Any],
        trade_date: str,
        *,
        exclude_signal_id: str | None = None,
    ) -> int:
        profiles = config.get("symbol_profiles")
        prof = profiles.get(symbol) if isinstance(profiles, Mapping) else None
        if isinstance(prof, Mapping) and prof.get("reconciled_shares") is not None:
            rec_sh = int(prof.get("reconciled_shares") or 0)
            rec_at = str(prof.get("reconciled_at") or "").strip()
            delta = (
                self.auto_store.net_signal_delta_since(
                    symbol,
                    since_iso=rec_at,
                    exclude_signal_id=exclude_signal_id,
                )
                if rec_at
                else 0
            )
            return max(0, rec_sh + delta)
        roll = self.auto_store.rolling_symbol_position(
            symbol,
            trade_date=trade_date,
            same_day_turnaround=True,
            exclude_signal_id=exclude_signal_id,
        )
        return int(roll["net_shares"])

    def _effective_broker_available_cash(self, *, exclude_signal_id: str | None = None) -> float | None:
        acct = self._peek_cached_broker_account()
        if not isinstance(acct, Mapping):
            return None
        metrics = acct.get("metrics")
        if not isinstance(metrics, Mapping):
            return None
        avail = self._parse_opt_float(metrics.get("available_cash"))
        if avail is None:
            return None
        captured_at = str(acct.get("captured_at") or "").strip()
        delta = (
            self.auto_store.net_cash_delta_since(
                since_iso=captured_at,
                exclude_signal_id=exclude_signal_id,
            )
            if captured_at
            else 0.0
        )
        return round(max(0.0, float(avail) + delta), 2)

    def _compute_portfolio_position_usage(
        self,
        config: Mapping[str, Any],
        trade_date: str,
    ) -> dict[str, Any]:
        symbols = [str(s).strip() for s in (config.get("symbols") or ["160723"]) if str(s).strip()]
        if not symbols:
            symbols = ["160723"]
        items = [
            self._effective_symbol_position(
                sym,
                trade_date=trade_date,
                same_day_turnaround=is_t0_turnaround_symbol(sym),
            )
            for sym in symbols
        ]
        total_shares = sum(int(item["net_shares"]) for item in items)
        total_mv = round(sum(float(item["market_value"]) for item in items), 2)
        if any(item["source"] == "broker_account" for item in items):
            source = "broker_account"
        elif total_shares > 0:
            source = "strategy_ledger"
        else:
            source = "none"
        return {
            "shares": total_shares,
            "market_value": total_mv,
            "source": source,
        }

    def get_status(self) -> dict[str, Any]:
        local_now = self._local_now()
        now_epoch = self._epoch()
        self.auto_store.expire_overdue_signals(now_epoch, now_str=local_now.isoformat(timespec="seconds"))
        config = self.auto_store.get_config()
        in_session, reason = is_in_continuous_auction(local_now, self.is_trading_day)
        trade_date = local_now.strftime("%Y%m%d")
        usage = self.auto_store.today_buy_usage(trade_date)
        available_rules, active_rule = self._summarize_quant_rules(config.get("quant_rule_id"))
        source_label = SIGNAL_SOURCE_LABELS.get(config["signal_source"], config["signal_source"])
        if config["signal_source"] == "quant_rule" and active_rule is not None:
            source_label = f"量化规则 · {active_rule['name']}"
        pos_usage = self._compute_portfolio_position_usage(config, trade_date)
        cap_val = float(config["daily_max_buy_amount"])
        is_model_auto = str(config.get("sizing_mode") or "fixed_qty") == "model_auto"
        cfg_syms = [str(s).strip() for s in (config.get("symbols") or ["160723"]) if str(s).strip()]
        use_base_numerator = is_model_auto and (
            any(self._is_t0_net_cap_rule(sym, self._effective_symbol_config(config, sym)) for sym in cfg_syms)
            or pos_usage["market_value"] > 0
        )
        effective_buy_amount = round(pos_usage["market_value"], 2) if use_base_numerator else usage["buy_amount"]
        capital_ledger = self._build_capital_ledger(config, trade_date)
        return {
            "config": config,
            "signal_source_label": source_label,
            "execution_mode_label": EXECUTION_MODE_LABELS.get(config["execution_mode"], config["execution_mode"]),
            "session": {
                "in_session": in_session,
                "reason": reason,
                "server_time": local_now.isoformat(timespec="seconds"),
            },
            "daily_usage": {
                "trade_date": usage["trade_date"],
                "buy_amount": effective_buy_amount,
                "today_buy_amount": usage["buy_amount"],
                "base_position_amount": pos_usage["market_value"],
                "base_position_shares": pos_usage["shares"],
                "position_source": pos_usage["source"],
                "remaining_amount": round(max(0.0, cap_val - effective_buy_amount), 2),
                "daily_max_buy_amount": cap_val,
                "broker_available_cash": self._effective_broker_available_cash(),
                "account_synced_at": capital_ledger.get("captured_at"),
                "order_count": usage["order_count"],
            },
            "capital_ledger": capital_ledger,
            "available_quant_rules": available_rules,
            "active_quant_rule": active_rule,
            "pending_signals": self.auto_store.list_pending_signals(limit=20),
            "recent_signals": self.auto_store.list_recent_signals(limit=25),
            "today_quant_signals": self.auto_store.list_today_executed_signals(trade_date, limit=100),
            "device_serial": getattr(self.trade_executor, "adb_serial", None),
            "executor_available": self.trade_executor is not None,
            "last_poll_at": self.last_poll_at,
            "viewer_active": self.is_viewer_active(),
        }

    def update_config(self, patch: Mapping[str, Any]) -> dict[str, Any]:
        return self.reconcile_and_update_config(patch, reconcile_broker=False)

    def _fetch_live_broker_account_sync(self) -> Mapping[str, Any]:
        if self.trade_executor is None:
            raise TradeExecutionError(
                "TRADE_UNCONFIGURED",
                "交易执行器未配置，无法执行柜台实时持仓与余额强一致校验",
                503,
            )
        fn = self.trade_executor.get_account_summary
        try:
            sig = inspect.signature(fn)
            if "max_age_seconds" in sig.parameters or any(
                p.kind is inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
            ):
                acct = fn(max_age_seconds=0.0)
            else:
                acct = fn()
        except TradeExecutionError:
            raise
        except Exception as error:
            raise TradeExecutionError(
                "TRADE_BROKER_SYNC_FAILED",
                f"实时同步国泰海通柜台持仓与余额失败：{error}",
                503,
            ) from error
        if not isinstance(acct, Mapping) or not acct.get("logged_in"):
            raise TradeExecutionError(
                "BROKER_LOGIN_REQUIRED",
                "同花顺国泰海通未登录或交易机休眠，请先唤醒并同步柜台后再保存资金配置",
                409,
            )
        return acct

    def reconcile_and_update_config(
        self,
        patch: Mapping[str, Any],
        *,
        reconcile_broker: bool = False,
        target_symbol: str | None = None,
    ) -> dict[str, Any]:
        current = self.auto_store.get_config()
        candidate = SQLiteAutoTradeStore._build_merged_candidate(current, patch)
        acct_snapshot: Mapping[str, Any] | None = None

        if reconcile_broker:
            acct_snapshot = self._fetch_live_broker_account_sync()
            trade_date = self._local_now().strftime("%Y%m%d")
            ledger = self._build_capital_ledger(candidate, trade_date, broker_acct=acct_snapshot)
            if ledger.get("available_cash") is None:
                raise TradeExecutionError(
                    "BROKER_BALANCE_UNAVAILABLE",
                    "无法读取国泰海通实时可用资金，已拦截保存以确保资金安全",
                    503,
                )
            rows = list(ledger["rows"])
            if target_symbol:
                clean_target = target_symbol.strip()
                rows.sort(key=lambda item: 0 if item["symbol"] == clean_target else 1)

            for row in rows:
                if row["enabled"] and float(row["below_holding_amount"]) > 0.01:
                    raise TradeExecutionError(
                        "TRADE_CAP_BELOW_HOLDING",
                        f"{row['name']}({row['symbol']}) 柜台实盘持仓市值 ¥{row['holding_market_value']:,.2f}（{row['broker_shares']}股）高于所设总上限 ¥{row['capital_cap']:,.2f}，请至少设为 ¥{math.ceil(row['holding_market_value']):,}",
                        409,
                    )
            for row in rows:
                if row["enabled"] and float(row["over_allocated_amount"]) > 0.01:
                    raise TradeExecutionError(
                        "TRADE_CASH_OVER_ALLOCATED",
                        f"可用余额校验未通过：国泰海通实时可用 ¥{ledger['available_cash']:,.2f}，扣除其他量化已占 ¥{row['other_occupied_cash']:,.2f} 后 {row['symbol']} 最多可占现金 ¥{row['remaining_allocatable_cash']:,.2f}（最高总上限 ¥{row['max_allowed_cap']:,.2f}），当前拟占 ¥{row['occupied_cash']:,.2f} 超出 ¥{row['over_allocated_amount']:,.2f}",
                        409,
                    )
            if float(ledger.get("unallocated_cash") or 0.0) < -0.01:
                raise TradeExecutionError(
                    "TRADE_CASH_OVER_ALLOCATED",
                    f"全部量化标的占用可用余额合计 ¥{ledger['total_occupied_cash']:,.2f} 超出账户可用 ¥{ledger['available_cash']:,.2f}",
                    409,
                )

            captured_at = ledger.get("captured_at")
            next_profiles = copy.deepcopy(candidate.get("symbol_profiles") or {})
            for row in rows:
                sym = row["symbol"]
                if sym in next_profiles:
                    next_profiles[sym]["reconciled_at"] = captured_at
                    next_profiles[sym]["reconciled_shares"] = row["broker_shares"]
            patch = {**dict(patch), "symbol_profiles": next_profiles, "symbols": candidate["symbols"]}

        self.auto_store.update_config(patch)
        status = self.get_status()
        if acct_snapshot is not None:
            status["account_snapshot"] = copy.deepcopy(dict(acct_snapshot))
        return status

    def upsert_symbol_profile(
        self,
        symbol: str,
        profile_patch: Mapping[str, Any],
        *,
        reconcile_broker: bool = True,
    ) -> dict[str, Any]:
        clean_sym = str(symbol or "").strip()
        if not _SYMBOL_RE.fullmatch(clean_sym):
            raise TradeExecutionError("TRADE_INVALID_SYMBOL", "证券代码必须为 6 位数字", 422)

        current = self.auto_store.get_config()
        symbols = list(current.get("symbols") or [])
        if clean_sym not in symbols:
            symbols.append(clean_sym)

        existing_profiles = copy.deepcopy(current.get("symbol_profiles") or {})
        base_prof = dict(existing_profiles.get(clean_sym) or _normalize_symbol_profile(clean_sym, None, current))
        if (
            ("signal_source" in profile_patch or "quant_rule_id" in profile_patch)
            and "rule_bindings" not in profile_patch
        ):
            base_prof.pop("rule_bindings", None)
        merged_prof = _normalize_symbol_profile(clean_sym, {**base_prof, **dict(profile_patch)}, current)
        existing_profiles[clean_sym] = merged_prof

        patch: dict[str, Any] = {
            "symbols": symbols,
            "symbol_profiles": existing_profiles,
        }
        return self.reconcile_and_update_config(
            patch,
            reconcile_broker=reconcile_broker,
            target_symbol=clean_sym,
        )

    def remove_symbol_profile(self, symbol: str) -> dict[str, Any]:
        clean_sym = str(symbol or "").strip()
        current = self.auto_store.get_config()
        symbols = [s for s in (current.get("symbols") or []) if s != clean_sym]
        if not symbols:
            raise TradeExecutionError(
                "TRADE_LAST_SYMBOL_PROTECTED",
                "请至少保留 1 只量化标的；若无需运行可将该标的设为暂停",
                422,
            )
        next_profiles = copy.deepcopy(current.get("symbol_profiles") or {})
        next_profiles.pop(clean_sym, None)
        patch = {
            "symbols": symbols,
            "symbol_profiles": next_profiles,
        }
        return self.reconcile_and_update_config(patch, reconcile_broker=False)

    def reset_circuit(self) -> dict[str, Any]:
        self.auto_store.update_config(
            {
                "circuit_open": False,
                "circuit_reason": "",
                "consecutive_failures": 0,
                "symbol_cooldowns": {},
            }
        )
        self._publish_event("circuit_changed", {"circuit_open": False, "circuit_reason": "", "symbol_cooldowns": {}})
        return self.get_status()

    def _prune_expired_cooldowns(self, config: Mapping[str, Any] | None = None) -> dict[str, dict[str, Any]]:
        cfg = config if config is not None else self.auto_store.get_config()
        raw = cfg.get("symbol_cooldowns")
        if not isinstance(raw, Mapping) or not raw:
            return {}
        now_ep = self._epoch()
        active = {
            str(k): dict(v)
            for k, v in raw.items()
            if isinstance(v, Mapping) and float(v.get("until_epoch") or 0.0) > now_ep
        }
        if len(active) != len(raw):
            self.auto_store.update_config({"symbol_cooldowns": active})
        return active

    def _active_symbol_cooldown(
        self,
        symbol: str,
        side: str,
        *,
        config: Mapping[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        active = self._prune_expired_cooldowns(config)
        return active.get(f"{symbol}:{side}")

    def _set_symbol_cooldown(
        self,
        symbol: str,
        side: str,
        *,
        error_code: str,
        reason: str,
        cooldown_seconds: float = 300.0,
    ) -> dict[str, Any]:
        now_ep = self._epoch()
        until_ep = round(now_ep + max(30.0, float(cooldown_seconds)), 3)
        until_dt = datetime.fromtimestamp(until_ep, tz=timezone.utc).astimezone(SHANGHAI_TZ)
        cfg = self.auto_store.get_config()
        current = self._prune_expired_cooldowns(cfg)
        entry = {
            "symbol": symbol,
            "side": "sell" if side == "sell" else "buy",
            "until_epoch": until_ep,
            "until_at": until_dt.isoformat(timespec="seconds"),
            "error_code": error_code,
            "reason": reason,
        }
        current[f"{entry['symbol']}:{entry['side']}"] = entry
        self.auto_store.update_config({"symbol_cooldowns": current})
        self._publish_event(
            "symbol_cooldown",
            {"symbol": entry["symbol"], "side": entry["side"], "cooldown": entry, "symbol_cooldowns": current},
        )
        return entry

    @staticmethod
    def _is_local_reject(error_code: str | None, dialog_message: str | None) -> bool:
        code = str(error_code or "").strip()
        if code in _LOCAL_COOLDOWN_ERROR_CODES:
            return True
        if code in _GLOBAL_CIRCUIT_ERROR_CODES:
            return False
        msg = str(dialog_message or "")
        return any(kw in msg for kw in _LOCAL_REJECT_KEYWORDS)

    def _lookup_premium_row(self, symbol: str) -> tuple[Mapping[str, Any] | None, float | None]:
        snap_fn = getattr(self.research_store, "premium_snapshot", None)
        snap = snap_fn() if callable(snap_fn) else None
        prem_row: Mapping[str, Any] | None = None
        if isinstance(snap, Mapping):
            for row in snap.get("rows") or []:
                if isinstance(row, Mapping) and row.get("code") == symbol and not row.get("stale"):
                    prem_row = row
                    break
        prem_rate = prem_row.get("premium_rate") if isinstance(prem_row, Mapping) else None
        if prem_rate is None and isinstance(prem_row, Mapping):
            prem_rate = calculate_premium_rate(prem_row.get("current_price"), prem_row.get("estimated_price"))
        return prem_row, (float(prem_rate) if prem_rate is not None else None)

    def _build_point_market_indicators(
        self,
        *,
        points: Sequence[Mapping[str, Any]],
        prem_row: Mapping[str, Any] | None,
        prem_rate: float | None,
        series: Mapping[str, Any] | None,
    ) -> list[dict[str, Any]]:
        indicators: list[dict[str, Any]] = []
        last_pt = points[-1] if points else None
        cur_px = (
            last_pt.get("price")
            if isinstance(last_pt, Mapping) and last_pt.get("price") is not None
            else (prem_row.get("current_price") if isinstance(prem_row, Mapping) else None)
        )
        pre_close = series.get("pre_close") if isinstance(series, Mapping) else None
        if cur_px is not None:
            px_f = float(cur_px)
            hint = f"昨收 {float(pre_close):.3f}" if pre_close else ""
            if pre_close and float(pre_close) > 0:
                chg = (px_f - float(pre_close)) / float(pre_close) * 100.0
                val_str = f"{px_f:.3f} ({chg:+.2f}%)"
            else:
                val_str = f"{px_f:.3f}"
            indicators.append({"key": "price", "label": "最新价", "value": val_str, "raw": round(px_f, 4), "hint": hint})
        if prem_rate is not None:
            est_px = prem_row.get("estimated_price") if isinstance(prem_row, Mapping) else None
            indicators.append(
                {
                    "key": "prem",
                    "label": "实时折溢价率",
                    "value": f"{float(prem_rate) * 100.0:+.2f}%",
                    "raw": round(float(prem_rate) * 100.0, 3),
                    "hint": f"估算净值 {est_px}" if est_px is not None else "",
                }
            )
        if isinstance(last_pt, Mapping):
            avg_p = last_pt.get("average_price")
            if avg_p is not None and float(avg_p) > 0 and cur_px is not None:
                vdev = (float(cur_px) / float(avg_p) - 1.0) * 100.0
                indicators.append(
                    {
                        "key": "vdev",
                        "label": "日内均价 / 偏离",
                        "value": f"{vdev:+.2f}%",
                        "raw": round(vdev, 3),
                        "hint": f"均价 {float(avg_p):.3f}",
                    }
                )
            if last_pt.get("macd") is not None:
                mc_f = float(last_pt["macd"])
                diff_f = float(last_pt.get("diff") or 0.0)
                dea_f = float(last_pt.get("dea") or 0.0)
                indicators.append(
                    {
                        "key": "macd",
                        "label": "分时 MACD",
                        "value": f"{mc_f:+.4f}",
                        "raw": round(mc_f, 5),
                        "hint": f"DIFF {diff_f:+.4f} · DEA {dea_f:+.4f}",
                    }
                )
            if last_pt.get("large_order_net") is not None:
                net_f = float(last_pt["large_order_net"])
                indicators.append(
                    {
                        "key": "flow",
                        "label": "主力大单净额",
                        "value": f"{net_f:+.1f} 万",
                        "raw": round(net_f, 2),
                        "hint": "分时逐笔主力统计",
                    }
                )
        return indicators

    @staticmethod
    def _evaluate_dsl_rule_side(
        symbol: str,
        points: Sequence[Mapping[str, Any]],
        rule_obj: Mapping[str, Any] | None,
        *,
        marker_threshold: float,
        default_title: str,
    ) -> dict[str, Any]:
        if not isinstance(rule_obj, Mapping) or not points:
            return {
                "matched": False,
                "summary": f"{default_title}暂无规则或行情样本",
                "tier_label": None,
                "target_weight_pct": None,
                "conditions": [],
            }
        detail = evaluate_rule_detail(
            symbol,
            points,
            rule_obj,
            marker_threshold=marker_threshold,
            index=len(points) - 1,
        )
        raw_conds = detail.get("condition_results") or []
        conds = [
            {
                "id": str(item.get("id") or f"cond-{idx}"),
                "label": str(item.get("type") or "指标条件"),
                "matched": bool(item.get("matched")),
                "current_value": str(item.get("detail") or ""),
                "target_rule": "OR 任一成立" if rule_obj.get("joiner") == "or" else "AND 全部成立",
                "detail": str(item.get("detail") or ""),
            }
            for idx, item in enumerate(raw_conds)
        ]
        hit_cnt = sum(1 for item in conds if item["matched"])
        matched = bool(detail.get("matched"))
        return {
            "matched": matched,
            "summary": (
                f"{default_title}触发 · {detail.get('summary') or '条件达成'}"
                if matched
                else f"暂未触发{default_title}（满足 {hit_cnt}/{len(conds)} 项）"
            ),
            "tier_label": None,
            "target_weight_pct": None,
            "conditions": conds,
        }

    def _v9_live_context_args(
        self,
        symbol: str,
        config: Mapping[str, Any],
        trade_date: str,
    ) -> tuple[int, float | None, int | None]:
        roll_pos = self._effective_symbol_position(
            symbol,
            trade_date=trade_date,
            same_day_turnaround=True,
        )
        eff_cash = self._effective_broker_available_cash()
        expected_sh = (
            self._expected_symbol_ledger_shares(symbol, config, trade_date)
            if roll_pos["source"] == "broker_account"
            else None
        )
        return int(roll_pos["net_shares"]), eff_cash, expected_sh

    def evaluate_live_snapshot(self, symbol: str | None = None) -> dict[str, Any]:
        """Evaluate current market indicators and separated buy/sell checklist for Live Trade UI."""
        local_now = self._local_now()
        trade_date = local_now.strftime("%Y%m%d")
        config = self.auto_store.get_config()

        target_symbol = str(symbol or "").strip().upper()
        if not _SYMBOL_RE.fullmatch(target_symbol):
            cfg_syms = [str(s).strip().upper() for s in (config.get("symbols") or []) if _SYMBOL_RE.fullmatch(str(s).strip().upper())]
            fallback_sym = cfg_syms[0] if cfg_syms else "160723"
            default_rule = self._get_active_quant_rule(self._effective_symbol_config(config, fallback_sym))
            rule_six = [
                str(s).strip().upper()
                for s in ((default_rule or {}).get("symbols") or [])
                if _SYMBOL_RE.fullmatch(str(s).strip().upper())
            ]
            target_symbol = rule_six[0] if (cfg_syms and rule_six and rule_six[0] in cfg_syms) else fallback_sym

        sym_cfg = self._effective_symbol_config(config, target_symbol)
        signal_source = str(sym_cfg.get("signal_source") or "intraday_macd_flow")
        selected_rule = self._get_active_quant_rule(sym_cfg) if signal_source == "quant_rule" else None

        series: Mapping[str, Any] | None = None
        read_series = getattr(self.research_store, "read_series", None)
        if callable(read_series):
            try:
                series = read_series(target_symbol, trade_date)
            except Exception:
                series = None

        name, market = self._resolve_symbol_meta(
            target_symbol,
            series.get("name") if isinstance(series, Mapping) else None,
            series.get("market") if isinstance(series, Mapping) else None,
        )
        points: list[dict[str, Any]] = []
        actual_td = str(series.get("trade_date") or trade_date) if isinstance(series, Mapping) else trade_date
        if isinstance(series, Mapping) and series.get("points"):
            points = _enrich_points_with_fund_flow(
                self.research_store,
                target_symbol,
                actual_td,
                list(series["points"]),
            )
            try:
                brent_idx = load_brent_alignment_index(self.research_store, actual_td, actual_td)
                prev_dates = [d for d in (series.get("available_dates") or []) if str(d) < actual_td]
                prev_td = str(prev_dates[0]) if prev_dates else None
                enrich_points_with_brent(points, actual_td, prev_td, brent_idx)
            except Exception:
                pass

        source_label = SIGNAL_SOURCE_LABELS.get(signal_source, signal_source)
        strategy_name = source_label
        engine_profile = signal_source
        if selected_rule is not None:
            strategy_name = str(selected_rule.get("name") or strategy_name)
            source_label = f"量化规则 · {strategy_name}"

        base_resp: dict[str, Any] = {
            "symbol": target_symbol,
            "name": name,
            "market": market,
            "trade_date": actual_td,
            "evaluated_at": local_now.isoformat(timespec="seconds"),
            "latest_minute": str(points[-1].get("time") or "") if points else "",
            "signal_source": signal_source,
            "signal_source_label": source_label,
            "strategy_name": strategy_name,
            "engine_profile": engine_profile,
            "data_ready": bool(points),
            "note": "" if points else "当前标的暂无分时采样数据",
            "market_indicators": [],
            "buy": {"matched": False, "summary": "暂未触发买点", "tier_label": None, "target_weight_pct": None, "conditions": []},
            "sell": {"matched": False, "summary": "暂未触发卖点", "tier_label": None, "target_weight_pct": None, "conditions": []},
            "diagnostics": None,
        }

        if signal_source == "quant_rule":
            if selected_rule is None:
                base_resp["data_ready"] = False
                base_resp["note"] = "未配置可用量化规则"
                return base_resp
            q_pos_sizing = normalize_position_sizing(selected_rule.get("position_sizing"))
            q_profile = str(q_pos_sizing.get("engine_profile") or "dsl_rule")
            base_resp["engine_profile"] = q_profile
            if q_profile in {"v9_plus_aggressive", "v9_plus_balanced"}:
                today_sigs = self.auto_store.list_today_symbol_signals(actual_td, target_symbol)
                net_sh, eff_cash, expected_sh = self._v9_live_context_args(target_symbol, sym_cfg, actual_td)
                v9_ind = evaluate_v9_plus_live_indicators(
                    self.research_store,
                    symbol=target_symbol,
                    trade_date=actual_td,
                    now_hhmm=local_now.strftime("%H:%M"),
                    cap_limit=float(sym_cfg.get("daily_max_buy_amount") or 50000.0),
                    rule_spec=selected_rule,
                    pos_sizing=q_pos_sizing,
                    today_signals=today_sigs,
                    rolling_shares=net_sh,
                    broker_available_cash=eff_cash,
                    expected_ledger_shares=expected_sh,
                )
                if v9_ind is None:
                    base_resp["data_ready"] = False
                    base_resp["note"] = "V9+ 日内布伦特/分时样本尚在初始化，请稍候重试"
                    return base_resp
                return {**base_resp, **v9_ind}

            # Standard DSL quant rule
            rule_macd = selected_rule.get("macd_settings") or {}
            threshold = float(rule_macd.get("marker_threshold", 0.00001))
            prem_row, prem_rate = self._lookup_premium_row(target_symbol)
            if not points:
                return base_resp
            prices = [float(p.get("price") or 0.0) for p in points]
            macd_rows = calculate_macd(prices, rule_macd)
            recalc_points = [{**pt, **macd_rows[idx]} for idx, pt in enumerate(points)]
            base_resp["market_indicators"] = self._build_point_market_indicators(
                points=recalc_points,
                prem_row=prem_row,
                prem_rate=prem_rate,
                series=series,
            )
            base_resp["buy"] = self._evaluate_dsl_rule_side(
                target_symbol,
                recalc_points,
                selected_rule.get("buy_rule"),
                marker_threshold=threshold,
                default_title="买点指标",
            )
            base_resp["sell"] = self._evaluate_dsl_rule_side(
                target_symbol,
                recalc_points,
                selected_rule.get("sell_rule"),
                marker_threshold=threshold,
                default_title="卖点指标",
            )
            return base_resp

        prem_row, prem_rate = self._lookup_premium_row(target_symbol)
        base_resp["market_indicators"] = self._build_point_market_indicators(
            points=points,
            prem_row=prem_row,
            prem_rate=prem_rate,
            series=series,
        )

        if signal_source == "lof_premium":
            prem_pct = (float(prem_rate) * 100.0) if prem_rate is not None else None
            buy_hit = prem_rate is not None and float(prem_rate) <= -0.015
            sell_hit = prem_rate is not None and float(prem_rate) >= 0.025
            base_resp["data_ready"] = prem_rate is not None
            base_resp["note"] = "" if prem_rate is not None else "暂无最新实时折溢价数据"
            cur_val = f"实时溢价 {prem_pct:+.2f}%" if prem_pct is not None else "溢价率未知"
            base_resp["buy"] = {
                "matched": buy_hit,
                "summary": "触发深折价套利买点 (≤-1.50%)" if buy_hit else "暂未触发折价买点",
                "tier_label": None,
                "target_weight_pct": None,
                "conditions": [
                    {
                        "id": "lof-buy-prem",
                        "label": "LOF 实时折价阈值",
                        "matched": buy_hit,
                        "current_value": cur_val,
                        "target_rule": "实时溢价率 ≤ -1.50%",
                        "detail": cur_val,
                    }
                ],
            }
            base_resp["sell"] = {
                "matched": sell_hit,
                "summary": "触发高溢价止盈卖点 (≥+2.50%)" if sell_hit else "暂未触发高溢价卖点",
                "tier_label": None,
                "target_weight_pct": None,
                "conditions": [
                    {
                        "id": "lof-sell-prem",
                        "label": "LOF 实时高溢价阈值",
                        "matched": sell_hit,
                        "current_value": cur_val,
                        "target_rule": "实时溢价率 ≥ +2.50%",
                        "detail": cur_val,
                    }
                ],
            }
            return base_resp

        # intraday_macd_flow or combo_confirm
        if not points:
            return base_resp
        push_cfg_fn = getattr(self.research_store, "push_config", None)
        push_cfg = push_cfg_fn(public=False) if callable(push_cfg_fn) else {}
        rules = [r for r in (push_cfg.get("rules") or []) if isinstance(r, Mapping) and r.get("enabled")] or _FALLBACK_INTRADAY_RULES
        threshold = float(((series.get("macd_settings") or {}).get("marker_threshold")) or 0.00001) if isinstance(series, Mapping) else 0.00001
        buy_rule_obj = next((r for r in rules if r.get("side") != "sell"), _FALLBACK_INTRADAY_RULES[0])
        sell_rule_obj = next((r for r in rules if r.get("side") == "sell"), _FALLBACK_INTRADAY_RULES[1])
        buy_eval = self._evaluate_dsl_rule_side(target_symbol, points, buy_rule_obj, marker_threshold=threshold, default_title="买点指标")
        sell_eval = self._evaluate_dsl_rule_side(target_symbol, points, sell_rule_obj, marker_threshold=threshold, default_title="卖点指标")
        if signal_source == "combo_confirm":
            prem_pct = (float(prem_rate) * 100.0) if prem_rate is not None else None
            buy_prem_ok = prem_rate is not None and float(prem_rate) <= 0.005
            sell_prem_ok = prem_rate is not None and float(prem_rate) >= -0.005
            buy_eval["conditions"].append(
                {
                    "id": "combo-buy-prem",
                    "label": "双因子溢价保护",
                    "matched": buy_prem_ok,
                    "current_value": f"{prem_pct:+.2f}%" if prem_pct is not None else "未知",
                    "target_rule": "实时溢价率 ≤ +0.50%",
                    "detail": f"溢价率 {prem_pct:+.2f}%" if prem_pct is not None else "缺少实时溢价率",
                }
            )
            buy_eval["matched"] = bool(buy_eval["matched"] and buy_prem_ok)
            sell_eval["conditions"].append(
                {
                    "id": "combo-sell-prem",
                    "label": "双因子溢价保护",
                    "matched": sell_prem_ok,
                    "current_value": f"{prem_pct:+.2f}%" if prem_pct is not None else "未知",
                    "target_rule": "实时溢价率 ≥ -0.50%",
                    "detail": f"溢价率 {prem_pct:+.2f}%" if prem_pct is not None else "缺少实时溢价率",
                }
            )
            sell_eval["matched"] = bool(sell_eval["matched"] and sell_prem_ok)
        base_resp["buy"] = buy_eval
        base_resp["sell"] = sell_eval
        return base_resp

    def _resolve_symbol_meta(self, symbol: str, series_name: object = None, market: object = None) -> tuple[str, str]:
        name = resolve_push_symbol_name(symbol, series_name, self.symbol_lookup)
        if not name:
            name = PREMIUM_NAMES.get(symbol, symbol)
        resolved_market = str(market or "").strip()
        if not resolved_market and self.symbol_lookup is not None:
            try:
                looked = self.symbol_lookup(symbol)
                resolved_market = str(getattr(looked, "market", "") or "")
            except Exception:
                resolved_market = ""
        return name, resolved_market

    def get_order_book(
        self,
        symbol: str,
        market: str | None = None,
        *,
        force_refresh: bool = False,
    ) -> dict[str, Any] | None:
        sym = str(symbol or "").strip()
        if not force_refresh and sym:
            cached = self._order_book_cache.get(sym)
            if cached is not None and (time.monotonic() - cached[0]) <= self._order_book_ttl_seconds:
                return copy.deepcopy(cached[1])
        if not callable(self.order_book_fetcher):
            return None
        try:
            book = self.order_book_fetcher(sym, market)
        except Exception:
            return None
        if not isinstance(book, Mapping):
            return None
        norm_book = dict(book)
        if sym:
            self._order_book_cache[sym] = (time.monotonic(), copy.deepcopy(norm_book))
        return norm_book

    def prewarm_order_books(self, symbols: Sequence[str]) -> dict[str, dict[str, Any]]:
        clean_syms = [str(s).strip() for s in symbols if _SYMBOL_RE.fullmatch(str(s).strip())]
        if not clean_syms:
            return {}
        if callable(self.batch_order_book_fetcher):
            try:
                batch = self.batch_order_book_fetcher(clean_syms)
                if isinstance(batch, Mapping):
                    now_m = time.monotonic()
                    out: dict[str, dict[str, Any]] = {}
                    for sym, book in batch.items():
                        if isinstance(book, Mapping):
                            b_copy = dict(book)
                            self._order_book_cache[str(sym)] = (now_m, copy.deepcopy(b_copy))
                            out[str(sym)] = b_copy
                    return out
            except Exception:
                pass
        return {}

    def _notify_external_best_effort(self, title: str, body: str) -> None:
        if self.push_dispatcher is None:
            return
        get_push_cfg = getattr(self.research_store, "push_config", None)
        if not callable(get_push_cfg):
            return
        try:
            cfg = get_push_cfg(public=False)
            res = self.push_dispatcher.send(cfg, title, body)
        except Exception as exc:
            logger.warning("auto_trade push send raised: title=%s err=%s", title, exc)
            try:
                cfg = get_push_cfg(public=False)
                self.push_dispatcher.send(cfg, title, body)
            except Exception as retry_exc:
                logger.warning("auto_trade push retry failed: title=%s err=%s", title, retry_exc)
            return

        if isinstance(res, Mapping) and res.get("errors"):
            errors = res.get("errors") or {}
            logger.warning("auto_trade push partial errors: title=%s errors=%s", title, errors)
            failed_channels = {str(k).split(":", 1)[0] for k in errors}
            failed_bark_ids = {
                str(k).split(":", 1)[1]
                for k in errors
                if str(k).startswith("bark:") and ":" in str(k)
            }
            for ch in sorted(failed_channels):
                if ch not in {"bark", "sc3_bot", "wecom"}:
                    continue
                retry_cfg = cfg
                if ch == "bark":
                    if not failed_bark_ids:
                        continue
                    filtered_groups = [
                        g
                        for g in (cfg.get("bark_groups") or [])
                        if isinstance(g, Mapping) and str(g.get("id") or "bark") in failed_bark_ids
                    ]
                    if not filtered_groups:
                        continue
                    retry_cfg = dict(cfg)
                    retry_cfg["bark_groups"] = filtered_groups
                try:
                    self.push_dispatcher.send(retry_cfg, title, body, only_channel=ch)
                except TypeError:
                    break
                except Exception as ch_exc:
                    logger.warning("auto_trade push channel retry failed: ch=%s title=%s err=%s", ch, title, ch_exc)

    def _trip_circuit(self, reason: str) -> None:
        self.auto_store.update_config(
            {"circuit_open": True, "circuit_reason": reason}
        )
        self._publish_event("circuit_changed", {"circuit_open": True, "circuit_reason": reason})
        self._notify_external_best_effort("量化自动交易已熔断暂停", reason)

    def _spawn_bg_task(self, coro_factory: Callable[[], Any]) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(coro_factory())
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    @staticmethod
    def _supports_kwarg(fn: Any, kw: str) -> bool:
        try:
            sig = inspect.signature(fn)
        except (TypeError, ValueError):
            return False
        if kw in sig.parameters:
            return True
        return any(p.kind is inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())

    def _infer_preferred_warm_side(
        self,
        config: Mapping[str, Any] | None = None,
        trade_date: str | None = None,
    ) -> Literal["buy", "sell"]:
        cfg = config if config is not None else self.auto_store.get_config()
        td = trade_date or self._local_now().strftime("%Y%m%d")
        for sym in cfg.get("symbols") or []:
            try:
                act = self.auto_store.today_symbol_activity(td, str(sym))
                if int(act.get("net_shares") or 0) > 0:
                    return "sell"
            except Exception:
                continue
        return "buy"

    def _call_keep_warm_sync(
        self,
        warm_fn: Any,
        *,
        preferred_side: Literal["buy", "sell"],
        now_epoch: float,
    ) -> tuple[Any, bool]:
        should_refresh_gw = (now_epoch - self._last_gateway_touch_at) >= 480.0
        supports_gw = self._supports_kwarg(warm_fn, "refresh_gateway")
        if should_refresh_gw and supports_gw:
            res = warm_fn(side=preferred_side, refresh_gateway=True)
            if not isinstance(res, Mapping) or res.get("ok", True):
                self._last_gateway_touch_at = now_epoch
            return res, True
        res = warm_fn(side=preferred_side)
        return res, False

    async def _run_prestage_bg(self, signal_row: Mapping[str, Any]) -> None:
        prestage_fn = getattr(self.trade_executor, "prestage_order", None)
        if not callable(prestage_fn) or self._scheduler.is_busy_or_queued(max_priority=PriorityTradeScheduler.P1_ORDER):
            return
        side = "sell" if signal_row.get("side") == "sell" else "buy"
        sym = str(signal_row["symbol"])
        try:
            async with self._scheduler.hold(
                PriorityTradeScheduler.P3_BACKGROUND,
                side=side,
                symbol=sym,
            ):
                if self._scheduler.has_pending(max_priority=PriorityTradeScheduler.P1_ORDER):
                    return
                await asyncio.to_thread(
                    prestage_fn,
                    symbol=sym,
                    expected_name=signal_row.get("name"),
                    side=side,
                    price=str(signal_row["limit_price"]),
                    quantity=int(signal_row["quantity"]),
                    precision=int(signal_row.get("precision") or 3),
                )
        except (Exception, asyncio.CancelledError):
            return

    def _schedule_prestage(self, signal_row: Mapping[str, Any]) -> None:
        if self.trade_executor is None or not callable(getattr(self.trade_executor, "prestage_order", None)):
            return
        if self._scheduler.is_busy_or_queued(max_priority=PriorityTradeScheduler.P1_ORDER):
            return
        try:
            if len(self.auto_store.list_pending_signals(limit=2)) >= 2:
                return
        except Exception:
            pass
        snapshot = dict(signal_row)
        self._spawn_bg_task(lambda: self._run_prestage_bg(snapshot))

    async def _run_keep_warm_bg(self) -> None:
        warm_fn = getattr(self.trade_executor, "keep_warm", None)
        if not callable(warm_fn) or self._scheduler.is_busy_or_queued(max_priority=PriorityTradeScheduler.P2_MAINTENANCE):
            return
        preferred_side = self._infer_preferred_warm_side()
        try:
            async with self._scheduler.hold(
                PriorityTradeScheduler.P3_BACKGROUND,
                side=preferred_side,
                symbol="keep_warm",
            ):
                if self._scheduler.has_pending(max_priority=PriorityTradeScheduler.P2_MAINTENANCE):
                    return
                await asyncio.to_thread(
                    self._call_keep_warm_sync,
                    warm_fn,
                    preferred_side=preferred_side,
                    now_epoch=self._epoch(),
                )
        except (Exception, asyncio.CancelledError):
            return

    def _schedule_keep_warm(self) -> None:
        if self.trade_executor is None or not callable(getattr(self.trade_executor, "keep_warm", None)):
            return
        if self._scheduler.is_busy_or_queued(max_priority=PriorityTradeScheduler.P2_MAINTENANCE):
            return
        self._spawn_bg_task(self._run_keep_warm_bg)

    async def _sync_broker_account(
        self,
        *,
        max_age_seconds: float = 40.0,
        warm_after: bool = True,
        delay_seconds: float = 0.0,
    ) -> Mapping[str, Any] | None:
        if self.trade_executor is None:
            return None
        get_acct_fn = getattr(self.trade_executor, "get_account_summary", None)
        if not callable(get_acct_fn):
            return None
        if delay_seconds > 0:
            try:
                await asyncio.sleep(delay_seconds)
            except asyncio.CancelledError:
                return None
        if self._account_sync_inflight or self._scheduler.is_busy_or_queued(max_priority=PriorityTradeScheduler.P2_MAINTENANCE):
            return None
        self._account_sync_inflight = True
        self._last_account_sync_at = self._epoch()
        preferred_side = self._infer_preferred_warm_side()
        try:
            async with self._scheduler.hold(
                PriorityTradeScheduler.P3_BACKGROUND,
                side=preferred_side,
                symbol="account_sync",
            ):
                if self._supports_kwarg(get_acct_fn, "max_age_seconds"):
                    acct = await asyncio.wait_for(
                        asyncio.to_thread(get_acct_fn, max_age_seconds=max_age_seconds),
                        timeout=8.0,
                    )
                else:
                    acct = await asyncio.wait_for(
                        asyncio.to_thread(get_acct_fn),
                        timeout=8.0,
                    )
                if warm_after and not self._scheduler.has_pending(max_priority=PriorityTradeScheduler.P2_MAINTENANCE):
                    warm_fn = getattr(self.trade_executor, "keep_warm", None)
                    if callable(warm_fn):
                        try:
                            now_warm = self._epoch()
                            gw_due = (now_warm - self._last_gateway_touch_at) >= 480.0
                            await asyncio.wait_for(
                                asyncio.to_thread(
                                    self._call_keep_warm_sync,
                                    warm_fn,
                                    preferred_side=preferred_side,
                                    now_epoch=now_warm,
                                ),
                                timeout=8.0 if gw_due else 4.0,
                            )
                        except (Exception, asyncio.CancelledError):
                            pass
                await self._reconcile_reconciling_signals_locked()
                return acct if isinstance(acct, Mapping) else None
        except (Exception, asyncio.CancelledError):
            return None
        finally:
            self._account_sync_inflight = False

    @staticmethod
    def _match_order_in_list(
        orders: Sequence[Mapping[str, Any]],
        *,
        symbol: str,
        name: str,
        side: str,
        price: str,
        quantity: int,
        entrust_no: str | None = None,
    ) -> Mapping[str, Any] | None:
        name_prefix = str(name or "").strip()[:3]
        try:
            target_px = float(price)
        except (TypeError, ValueError):
            target_px = -1.0
        for row in orders:
            row_status = str(row.get("status") or "")
            if any(w in row_status for w in ("废单", "拒绝", "作废")):
                continue
            if entrust_no and row.get("entrust_no") == entrust_no:
                return row
            row_sym = str(row.get("symbol") or "").strip()
            row_name = str(row.get("name") or "").strip()
            sym_ok = (row_sym == symbol) or bool(name_prefix and name_prefix in row_name)
            if not sym_ok or str(row.get("side") or "") != side:
                continue
            try:
                row_px = float(str(row.get("price") or "").replace(",", "").strip())
            except (TypeError, ValueError):
                row_px = -1.0
            if int(row.get("quantity") or 0) == int(quantity) and (
                target_px <= 0 or abs(row_px - target_px) < 1e-3
            ):
                return row
        return None

    async def _reconcile_grey_order_locked(
        self,
        signal_row: Mapping[str, Any],
        *,
        formatted_price: str,
        eff_qty: int,
        res: dict[str, Any],
    ) -> tuple[dict[str, Any], str]:
        """Verify grey-state NO_DIALOG_OBSERVED/UNCONFIRMED orders against broker today orders."""
        orders = res.get("orders") if isinstance(res.get("orders"), list) else []
        match = self._match_order_in_list(
            orders,
            symbol=str(signal_row["symbol"]),
            name=str(signal_row["name"]),
            side=str(signal_row["side"]),
            price=formatted_price,
            quantity=eff_qty,
            entrust_no=res.get("entrust_no"),
        )
        if match is None and self.trade_executor is not None:
            get_orders_fn = getattr(self.trade_executor, "get_today_orders", None)
            if callable(get_orders_fn):
                try:
                    sig = inspect.signature(get_orders_fn)
                    if "max_age_seconds" in sig.parameters or any(
                        p.kind is inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
                    ):
                        latest_orders_doc = await asyncio.to_thread(get_orders_fn, max_age_seconds=0.0)
                    else:
                        latest_orders_doc = await asyncio.to_thread(get_orders_fn)
                    orders = list((latest_orders_doc or {}).get("orders") or [])
                    match = self._match_order_in_list(
                        orders,
                        symbol=str(signal_row["symbol"]),
                        name=str(signal_row["name"]),
                        side=str(signal_row["side"]),
                        price=formatted_price,
                        quantity=eff_qty,
                        entrust_no=res.get("entrust_no"),
                    )
                except TradeExecutionError:
                    res["dialog_message"] = (
                        f"{res.get('dialog_message') or '回执未知'} | 灰态回查时设备离线，进入 RECONCILING 锁账待核"
                    )
                    return res, "RECONCILING"
        if match is not None:
            match_status = str(match.get("status") or "")
            if any(w in match_status for w in ("已撤", "撤单", "部撤")):
                res["accepted"] = False
                res["status"] = "REJECTED"
                res["matched_order"] = dict(match)
                res["dialog_message"] = f"{res.get('dialog_message') or ''} | 委托回查状态为{match_status}".strip(" |")
                return res, "REJECTED"
            res["accepted"] = True
            res["status"] = "SUBMITTED"
            res["matched_order"] = dict(match)
            res["entrust_no"] = match.get("entrust_no") or res.get("entrust_no")
            res["orders"] = orders
            res["dialog_message"] = (
                f"{res.get('dialog_message') or '已提交'} | 灰态回查当日委托确认已报"
            ).strip(" |")
            return res, "SUBMITTED"
        return res, "FAILED"

    async def _reconcile_reconciling_signals_locked(self) -> None:
        if self.trade_executor is None:
            return
        local_now = self._local_now()
        trade_date = local_now.strftime("%Y%m%d")
        pending_rec = self.auto_store.list_reconciling_signals(trade_date)
        if not pending_rec:
            return
        get_orders_fn = getattr(self.trade_executor, "get_today_orders", None)
        if not callable(get_orders_fn):
            return
        try:
            sig = inspect.signature(get_orders_fn)
            if "max_age_seconds" in sig.parameters or any(
                p.kind is inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
            ):
                orders_doc = await asyncio.to_thread(get_orders_fn, max_age_seconds=0.0)
            else:
                orders_doc = await asyncio.to_thread(get_orders_fn)
        except Exception:
            return
        orders = list((orders_doc or {}).get("orders") or [])
        local_iso = local_now.isoformat(timespec="seconds")
        cfg = self.auto_store.get_config()
        auto_sec = int(cfg.get("auto_cancel_seconds") or 0)
        for row in pending_rec:
            match = self._match_order_in_list(
                orders,
                symbol=str(row["symbol"]),
                name=str(row["name"]),
                side=str(row["side"]),
                price=str(row["limit_price"]),
                quantity=int(row["quantity"]),
                entrust_no=row.get("entrust_no"),
            )
            if match is not None:
                cancel_pending = auto_sec > 0 and bool(match.get("cancellable", True))
                self.auto_store.update_signal_execution(
                    row["id"],
                    status="SUBMITTED",
                    entrust_no=match.get("entrust_no") or row.get("entrust_no"),
                    dialog_message=f"{row.get('dialog_message') or ''} | 后台核销确认已报".strip(" |"),
                    cancel_pending=cancel_pending,
                    cancel_due_at=(self._epoch() + auto_sec) if cancel_pending else None,
                    now_str=local_iso,
                )
            else:
                self.auto_store.update_signal_execution(
                    row["id"],
                    status="FAILED",
                    dialog_message=f"{row.get('dialog_message') or ''} | 后台核销未发现有效挂单，已释放预扣".strip(" |"),
                    now_str=local_iso,
                )

    def _schedule_account_sync(
        self,
        *,
        max_age_seconds: float = 40.0,
        warm_after: bool = True,
        delay_seconds: float = 0.0,
    ) -> None:
        if self.trade_executor is None or not callable(getattr(self.trade_executor, "get_account_summary", None)):
            if warm_after:
                self._schedule_keep_warm()
            return
        if self._account_sync_inflight or (delay_seconds <= 0 and self._exec_lock.locked()):
            return
        self._spawn_bg_task(
            lambda: self._sync_broker_account(
                max_age_seconds=max_age_seconds,
                warm_after=warm_after,
                delay_seconds=delay_seconds,
            )
        )

    async def _run_due_auto_cancels(self) -> None:
        now_epoch = self._epoch()
        local_iso = self._local_now().isoformat(timespec="seconds")
        due_rows = self.auto_store.list_due_cancels(now_epoch)
        if not due_rows or self.trade_executor is None:
            return
        cancel_fn = getattr(self.trade_executor, "cancel_order", None)
        if not callable(cancel_fn):
            return
        synced_after_cancel = False
        for row in due_rows:
            async with self._scheduler.hold(
                PriorityTradeScheduler.P2_MAINTENANCE,
                side=row.get("side"),
                symbol=row.get("symbol"),
            ):
                try:
                    res = await asyncio.to_thread(
                        cancel_fn,
                        entrust_no=row.get("entrust_no"),
                        symbol=row["symbol"],
                        expected_name=row["name"],
                        side=row["side"],
                    )
                    if res.get("ok"):
                        self._last_gateway_touch_at = self._epoch()
                        msg = str(res.get("dialog_message") or "超时未成交已自动撤单")
                        prev_msg = str(row.get("dialog_message") or "").strip()
                        combined = f"{prev_msg} | {msg}" if prev_msg else msg
                        inv_fn = getattr(self.trade_executor, "invalidate_snapshots", None)
                        if callable(inv_fn):
                            inv_fn(account=True, orders=True, trades=True)
                        self.auto_store.update_signal_execution(
                            row["id"],
                            status="AUTO_CANCELLED_TIMEOUT",
                            dialog_message=combined,
                            cancel_pending=False,
                            cancel_due_at=None,
                            now_str=local_iso,
                        )
                        if not row.get("is_simulation"):
                            synced_after_cancel = True
                            self._notify_external_best_effort(
                                f"量化委托超时撤单：{row['side_label']} {format_push_symbol(row['symbol'], row['name'])}",
                                f"委托：限价 ¥{row['limit_price']} × {row['quantity']}股\n原因：挂单满30秒未成交，已自动撤单释放资金与额度",
                            )
                    else:
                        self.auto_store.update_signal_execution(
                            row["id"],
                            status=row["status"],
                            cancel_pending=False,
                            cancel_due_at=None,
                            now_str=local_iso,
                        )
                        if not row.get("is_simulation"):
                            self._notify_external_best_effort(
                                f"量化委托超时异常：{row['side_label']} {format_push_symbol(row['symbol'], row['name'])}",
                                f"委托：限价 ¥{row['limit_price']} × {row['quantity']}股\n原因：挂单满30秒未成交且自动撤单未确认成功，请核查同花顺委托列表",
                            )
                except TradeExecutionError as error:
                    if error.error_code == "TRADE_ORDER_NOT_FOUND":
                        prev_msg = str(row.get("dialog_message") or "").strip()
                        combined = f"{prev_msg} | 撤单检查：已成或已不在可撤队列" if prev_msg else "已成或已不在可撤队列"
                        inv_fn = getattr(self.trade_executor, "invalidate_snapshots", None)
                        if callable(inv_fn):
                            inv_fn(account=True, orders=True, trades=True)
                        self.auto_store.update_signal_execution(
                            row["id"],
                            status="FILLED_OR_CLOSED",
                            dialog_message=combined,
                            cancel_pending=False,
                            cancel_due_at=None,
                            now_str=local_iso,
                        )
                        if not row.get("is_simulation"):
                            synced_after_cancel = True
                    elif error.error_code == "BROKER_LOGIN_REQUIRED":
                        self.auto_store.update_signal_execution(
                            row["id"],
                            status=row["status"],
                            cancel_pending=False,
                            cancel_due_at=None,
                            now_str=local_iso,
                        )
                        self._trip_circuit(error.message)
                    elif not row.get("is_simulation"):
                        self.auto_store.update_signal_execution(
                            row["id"],
                            status=row["status"],
                            cancel_pending=False,
                            cancel_due_at=None,
                            now_str=local_iso,
                        )
                        self._notify_external_best_effort(
                            f"量化委托超时异常：{row['side_label']} {format_push_symbol(row['symbol'], row['name'])}",
                            f"委托：限价 ¥{row['limit_price']} × {row['quantity']}股\n原因：30秒撤单复核异常（{error.message}）",
                        )
        if synced_after_cancel:
            self._last_account_sync_at = now_epoch
            self._schedule_account_sync(max_age_seconds=0.0, warm_after=True, delay_seconds=2.5)

    def _get_active_quant_rule(self, config: Mapping[str, Any]) -> dict[str, Any] | None:
        qstore = self.quant_rule_store
        if qstore is None:
            return None
        selected_rule: dict[str, Any] | None = None
        req_id = config.get("quant_rule_id")
        if req_id and callable(getattr(qstore, "get_rule", None)):
            selected_rule = qstore.get_rule(str(req_id))
        if selected_rule is None and callable(getattr(qstore, "list_rules", None)):
            all_q = qstore.list_rules()
            selected_rule = next((x for x in all_q if x.get("enabled", True)), all_q[0] if all_q else None)
        return selected_rule

    def _is_t0_net_cap_rule(self, symbol: str, config: Mapping[str, Any]) -> bool:
        if str(config.get("sizing_mode") or "fixed_qty") != "model_auto":
            return False
        if str(config.get("signal_source") or "") != "quant_rule":
            return False
        if not is_t0_turnaround_symbol(symbol):
            return False
        active_rule = self._get_active_quant_rule(config)
        if active_rule is None:
            return False
        ps = normalize_position_sizing(active_rule.get("position_sizing"))
        return ps["mode"] == "cash_pool_rolling" or ps.get("engine_profile") in {
            "v9_plus_aggressive",
            "v9_plus_balanced",
        }

    def _max_sellable_above_floor(
        self,
        symbol: str,
        trade_date: str,
        *,
        exclude_signal_id: str | None = None,
    ) -> tuple[int, int, str]:
        """Return (net_shares, max_sellable_shares_above_floor, position_source)."""
        pos = self._effective_symbol_position(
            symbol,
            trade_date=trade_date,
            same_day_turnaround=is_t0_turnaround_symbol(symbol),
            exclude_signal_id=exclude_signal_id,
        )
        net_sh = int(pos["net_shares"])
        raw_sellable = min(int(pos["sellable_shares"]), max(0, net_sh - MIN_RETAINED_SHARES))
        max_sell = (max(0, raw_sellable) // 100) * 100
        return net_sh, max_sell, str(pos["source"])

    def _resolve_signal_quantity(
        self,
        *,
        symbol: str,
        side: Literal["buy", "sell"],
        limit_price: str,
        config: Mapping[str, Any],
        candidate: Mapping[str, Any] | None,
        trade_date: str,
        is_simulation: bool = False,
    ) -> tuple[int, str | None, str | None]:
        """Return (lot_aligned_shares, sizing_note, skip_reason) for fixed_qty or model_auto."""
        if str(config.get("sizing_mode") or "fixed_qty") != "model_auto":
            return int(config["order_quantity"]), None, None

        cap = float(config.get("daily_max_buy_amount") or 20000.0)
        raw_ps = candidate.get("position_sizing") if isinstance(candidate, Mapping) else None
        if raw_ps is None:
            active_rule = self._get_active_quant_rule(config)
            raw_ps = active_rule.get("position_sizing") if isinstance(active_rule, Mapping) else None
        ps = normalize_position_sizing(raw_ps)
        weight_pct = float(
            (candidate.get("weight_pct") if isinstance(candidate, Mapping) else None)
            or ps["default_weight_pct"]
        )
        tier_label = str(
            (candidate.get("sizing_tier") if isinstance(candidate, Mapping) else None)
            or f"默认仓位({weight_pct:.0f}%)"
        )
        price_f = float(limit_price)
        lot = int(ps["lot_size"])

        if ps["mode"] == "base_t0_close":
            act = self.auto_store.today_symbol_activity(trade_date, symbol)
            if not is_simulation and act["entry_count"] >= int(ps["max_daily_entries"]):
                return 0, None, f"已达模型单日最大做T次数({ps['max_daily_entries']})"
            used_amt = float(act["buy_amount"] if side == "buy" else act["sell_amount"])
            rem_pct = max(0.0, float(ps["max_position_pct"]) - (used_amt / cap * 100.0)) if cap > 0 else 0.0
            eff_w = min(weight_pct, rem_pct)
            avail = max(0.0, cap - used_amt)
            eff_cash: float | None = None
            if side == "buy" and not is_simulation:
                avail = min(avail, max(0.0, cap - float(self.auto_store.today_symbol_buy_usage(trade_date, symbol)["buy_amount"])))
                eff_cash = self._effective_broker_available_cash()
                if eff_cash is not None:
                    avail = min(avail, max(0.0, eff_cash * 0.9995))
            alloc = min(avail, cap * (eff_w / 100.0))
            qty = compute_lot_shares_from_budget(alloc, price_f, lot)
            if side == "sell" and not is_simulation:
                net_sh, max_sell, pos_src = self._max_sellable_above_floor(symbol, trade_date)
                if pos_src != "none" or net_sh > 0:
                    if max_sell < lot:
                        return 0, None, f"强制保留底仓 {MIN_RETAINED_SHARES} 股（当前持 {net_sh} 股，延续同花顺收益统计）"
                    qty = min(qty, max_sell)
            if qty < lot:
                if eff_cash is not None and (eff_cash * 0.9995) < price_f * lot:
                    return 0, None, f"柜台实时可用资金 ¥{eff_cash:.0f} 不足买 {lot} 股({price_f:.3f})"
                return 0, None, f"可用额度 ¥{avail:.0f} 不足买/卖 100 股({price_f:.3f})"
            note = f"模型做T[{tier_label} {eff_w:.0f}% · 上限¥{cap:.0f} → {qty}股]"
            return qty, note, None

        # cash_pool_rolling mode
        turnaround_raw = ps["allow_same_day_turnaround"]
        same_day_t0 = is_t0_turnaround_symbol(symbol) if turnaround_raw == "auto" else bool(turnaround_raw)
        pos = self._effective_symbol_position(symbol, trade_date=trade_date, same_day_turnaround=same_day_t0)
        if side == "buy":
            if not is_simulation:
                act = self.auto_store.today_symbol_activity(trade_date, symbol)
                if act["entry_count"] >= int(ps["max_daily_entries"]):
                    return 0, None, f"已达模型单日最大买入次数({ps['max_daily_entries']})"
            deployed = float(pos["net_shares"]) * price_f
            max_pos_avail = max(0.0, cap * (float(ps["max_position_pct"]) / 100.0) - deployed)
            eff_cash = None if is_simulation else self._effective_broker_available_cash()
            if not is_simulation:
                daily_rem = max(0.0, cap - float(self.auto_store.today_symbol_buy_usage(trade_date, symbol)["buy_amount"]))
                avail_cash = min(max_pos_avail, daily_rem)
                if eff_cash is not None:
                    avail_cash = min(avail_cash, max(0.0, eff_cash * 0.9995))
            else:
                avail_cash = max_pos_avail if max_pos_avail > 0 else cap
            alloc = min(avail_cash, cap * (weight_pct / 100.0))
            qty = compute_lot_shares_from_budget(alloc, price_f, lot)
            if qty < lot:
                if eff_cash is not None and (eff_cash * 0.9995) < price_f * lot:
                    return 0, None, f"柜台实时可用资金 ¥{eff_cash:.0f} 不足买 {lot} 股"
                return 0, None, f"资金池剩余可用 ¥{avail_cash:.0f} 不足 100 股"
            note = f"资金池[{tier_label} {weight_pct:.0f}% · 上限¥{cap:.0f} → {qty}股]"
            return qty, note, None

        if is_simulation and int(pos["sellable_shares"]) < lot:
            virtual_full = compute_lot_shares_from_budget(cap, price_f, lot)
            qty = max(lot, int(round((virtual_full * (weight_pct / 100.0)) / lot)) * lot) if virtual_full >= lot else lot
            return qty, f"模拟减仓[{tier_label} {weight_pct:.0f}% → {qty}股]", None
        if not is_simulation:
            sellable = (
                max(0, min(int(pos["sellable_shares"]), int(pos["net_shares"]) - MIN_RETAINED_SHARES)) // lot
            ) * lot
            if sellable < lot:
                return 0, None, f"强制保留底仓 {MIN_RETAINED_SHARES} 股（当前持 {pos['net_shares']} 股，延续同花顺收益统计）"
        else:
            sellable = int(pos["sellable_shares"])
            if sellable < lot:
                return 0, None, "当前策略持仓无可卖整手股数"
        raw_sell = int(round((int(pos["net_shares"]) * (weight_pct / 100.0)) / lot)) * lot
        qty = min(sellable, max(lot, raw_sell))
        note = f"资金池减仓[{tier_label} {weight_pct:.0f}% · 可卖{sellable}股 → {qty}股]"
        return qty, note, None

    def _evaluate_symbol_candidates(
        self,
        *,
        symbol: str,
        config: Mapping[str, Any],
        trade_date: str,
    ) -> list[dict[str, Any]]:
        bindings = _normalize_rule_bindings(
            config.get("rule_bindings"),
            str(config.get("signal_source") or "intraday_macd_flow"),
            config.get("quant_rule_id"),
        )
        enabled_bindings = [b for b in bindings if b.get("enabled", True)]
        if not enabled_bindings:
            return []
        all_candidates: list[dict[str, Any]] = []
        for binding in enabled_bindings:
            b_cfg = {
                **dict(config),
                "signal_source": binding["signal_source"],
                "quant_rule_id": binding.get("quant_rule_id"),
            }
            sub_items = self._evaluate_single_rule_candidates(
                symbol=symbol,
                config=b_cfg,
                trade_date=trade_date,
            )
            scale = float(binding.get("weight_scale") or 1.0)
            prio = int(binding.get("priority") or 10)
            for item in sub_items:
                item["binding_priority"] = prio
                item["weight_scale"] = scale
                if scale != 1.0:
                    if item.get("qty_override") is not None:
                        item["qty_override"] = max(100, int(round((int(item["qty_override"]) * scale) / 100.0)) * 100)
                    elif item.get("weight_pct") is not None:
                        item["weight_pct"] = round(max(1.0, min(100.0, float(item["weight_pct"]) * scale)), 1)
            all_candidates.extend(sub_items)
        return coalesce_symbol_candidates(
            all_candidates,
            default_qty=int(config.get("order_quantity") or 100),
        )

    def _evaluate_single_rule_candidates(
        self,
        *,
        symbol: str,
        config: Mapping[str, Any],
        trade_date: str,
    ) -> list[dict[str, Any]]:
        read_series = getattr(self.research_store, "read_series", None)
        if not callable(read_series):
            return []
        try:
            series = read_series(symbol, trade_date)
        except Exception:
            series = None

        points: list[dict[str, Any]] = []
        market = ""
        name, market = self._resolve_symbol_meta(
            symbol,
            series.get("name") if isinstance(series, Mapping) else None,
            series.get("market") if isinstance(series, Mapping) else None,
        )

        intraday_matches: list[dict[str, Any]] = []
        if isinstance(series, Mapping) and series.get("points"):
            actual_td = str(series.get("trade_date") or trade_date)
            points = _enrich_points_with_fund_flow(
                self.research_store,
                symbol,
                actual_td,
                list(series["points"]),
            )
            try:
                brent_idx = load_brent_alignment_index(self.research_store, actual_td, actual_td)
                prev_dates = [d for d in (series.get("available_dates") or []) if str(d) < actual_td]
                prev_td = str(prev_dates[0]) if prev_dates else None
                enrich_points_with_brent(points, actual_td, prev_td, brent_idx)
            except Exception:
                pass
            if len(points) >= 2:
                candidate_times = {str(p.get("time") or "") for p in points[-2:] if p.get("time")}
                push_cfg_fn = getattr(self.research_store, "push_config", None)
                push_cfg = push_cfg_fn(public=False) if callable(push_cfg_fn) else {}
                rules = [
                    r for r in (push_cfg.get("rules") or [])
                    if isinstance(r, Mapping) and r.get("enabled")
                ] or _FALLBACK_INTRADAY_RULES
                threshold = float(((series.get("macd_settings") or {}).get("marker_threshold")) or 0.00001)
                markers = build_replay_markers(symbol, points, rules, marker_threshold=threshold)
                matching_rules_by_id = {str(r.get("id") or ""): r for r in rules}
                for marker in markers:
                    mtime = str(marker.get("time") or "")
                    if mtime not in candidate_times:
                        continue
                    rid = str(marker.get("rule_id") or "")
                    rule_obj = matching_rules_by_id.get(rid)
                    summary = ""
                    if rule_obj is not None:
                        detail = evaluate_rule_detail(
                            symbol,
                            points,
                            rule_obj,
                            marker_threshold=threshold,
                            index=int(marker.get("index", len(points) - 1)),
                        )
                        summary = str(detail.get("summary") or "")
                    intraday_matches.append(
                        {
                            "rule_id": rid,
                            "rule_title": str(marker.get("rule_title") or "分时策略信号"),
                            "rule_summary": summary or f"分时MACD/主力大单共振 ({mtime})",
                            "side": "sell" if marker.get("side") == "sell" else "buy",
                            "minute": mtime,
                            "price": str(marker["price"]),
                        }
                    )

        signal_source = config["signal_source"]
        if signal_source == "quant_rule":
            selected_rule = self._get_active_quant_rule(config)
            if selected_rule is None or not points or len(points) < 2:
                return []
            rule_symbols = [str(s).strip() for s in (selected_rule.get("symbols") or []) if str(s).strip()]
            if rule_symbols and symbol not in rule_symbols:
                return []
            q_pos_sizing = normalize_position_sizing(selected_rule.get("position_sizing"))
            if q_pos_sizing.get("engine_profile") in {"v9_plus_aggressive", "v9_plus_balanced"}:
                actual_td = str(series.get("trade_date") or trade_date) if isinstance(series, Mapping) else trade_date
                local_now_dt = self._local_now()
                local_hhmm = local_now_dt.strftime("%H:%M")
                today_sigs = self.auto_store.list_today_symbol_signals(actual_td, symbol)
                net_sh, eff_cash, expected_sh = self._v9_live_context_args(symbol, config, actual_td)
                decision = evaluate_v9_plus_live_decision(
                    self.research_store,
                    symbol=symbol,
                    trade_date=actual_td,
                    now_hhmm=local_hhmm,
                    now_dt=local_now_dt,
                    cap_limit=float(config.get("daily_max_buy_amount") or 50000.0),
                    rule_spec=selected_rule,
                    pos_sizing=q_pos_sizing,
                    today_signals=today_sigs,
                    rolling_shares=net_sh,
                    broker_available_cash=eff_cash,
                    expected_ledger_shares=expected_sh,
                )
                use_fixed_qty = str(config.get("sizing_mode") or "fixed_qty") != "model_auto"
                out_v9: list[dict[str, Any]] = []
                for cand in decision.get("intraday") or []:
                    enriched_cand = {
                        **cand,
                        "name": name,
                        "market": market,
                        "trade_date": actual_td,
                        "position_sizing": q_pos_sizing,
                    }
                    if use_fixed_qty:
                        enriched_cand["qty_override"] = int(config["order_quantity"])
                    out_v9.append(enriched_cand)
                return out_v9
            rule_macd = selected_rule.get("macd_settings") or {}
            prices = [float(p.get("price") or 0.0) for p in points]
            macd_rows = calculate_macd(prices, rule_macd)
            recalc_points = [{**pt, **macd_rows[idx]} for idx, pt in enumerate(points)]
            q_rules: list[dict[str, Any]] = []
            if isinstance(selected_rule.get("buy_rule"), Mapping):
                q_rules.append({**dict(selected_rule["buy_rule"]), "enabled": True})
            if isinstance(selected_rule.get("sell_rule"), Mapping):
                q_rules.append({**dict(selected_rule["sell_rule"]), "enabled": True})
            if not q_rules:
                return []
            threshold = float(rule_macd.get("marker_threshold", 0.00001))
            q_markers = build_replay_markers(symbol, recalc_points, q_rules, marker_threshold=threshold)
            candidate_times = {str(p.get("time") or "") for p in recalc_points[-2:] if p.get("time")}
            q_by_id = {str(r.get("id") or ""): r for r in q_rules}
            q_matches: list[dict[str, Any]] = []
            for marker in q_markers:
                mtime = str(marker.get("time") or "")
                if mtime not in candidate_times:
                    continue
                rid = str(marker.get("rule_id") or "")
                rule_obj = q_by_id.get(rid)
                m_idx = int(marker.get("index", len(recalc_points) - 1))
                m_side = "sell" if marker.get("side") == "sell" else "buy"
                summary = ""
                if rule_obj is not None:
                    detail = evaluate_rule_detail(
                        symbol,
                        recalc_points,
                        rule_obj,
                        marker_threshold=threshold,
                        index=m_idx,
                    )
                    summary = str(detail.get("summary") or "")
                tier_w, tier_lbl = resolve_signal_conviction_tier(
                    symbol,
                    recalc_points,
                    m_idx,
                    m_side,
                    q_pos_sizing,
                    marker_threshold=threshold,
                )
                q_matches.append(
                    {
                        "rule_id": str(selected_rule["id"]),
                        "rule_title": f"{selected_rule['name']} · {marker.get('rule_title') or ('买点' if m_side == 'buy' else '卖点')}",
                        "rule_summary": summary or f"量化规则 {selected_rule['name']} ({mtime})",
                        "side": m_side,
                        "minute": mtime,
                        "price": str(marker["price"]),
                        "name": name,
                        "market": market,
                        "trade_date": trade_date,
                        "position_sizing": q_pos_sizing,
                        "weight_pct": tier_w,
                        "sizing_tier": tier_lbl,
                    }
                )
            return q_matches

        if signal_source == "intraday_macd_flow":
            return [
                {**item, "name": name, "market": market, "trade_date": trade_date}
                for item in intraday_matches
            ]

        # Premium signal assessment (used by lof_premium and combo_confirm)
        snap_fn = getattr(self.research_store, "premium_snapshot", None)
        snap = snap_fn() if callable(snap_fn) else None
        prem_row = None
        if isinstance(snap, Mapping):
            for row in snap.get("rows") or []:
                if isinstance(row, Mapping) and row.get("code") == symbol and not row.get("stale"):
                    prem_row = row
                    break
        prem_rate = prem_row.get("premium_rate") if isinstance(prem_row, Mapping) else None
        if prem_rate is None and isinstance(prem_row, Mapping):
            prem_rate = calculate_premium_rate(prem_row.get("current_price"), prem_row.get("estimated_price"))

        if signal_source == "lof_premium":
            if prem_rate is None or prem_row is None:
                return []
            latest_minute = str(points[-1].get("time") or "") if points else self._local_now().strftime("%H:%M")
            cur_price = prem_row.get("current_price") or (points[-1].get("price") if points else None)
            if cur_price is None:
                return []
            if float(prem_rate) <= -0.015:
                return [
                    {
                        "rule_id": "lof-discount-buy",
                        "rule_title": "LOF/QDII 折价套利买入",
                        "rule_summary": f"实时折价率 {float(prem_rate) * 100:.2f}% <= -1.50%",
                        "side": "buy",
                        "minute": latest_minute,
                        "price": str(cur_price),
                        "name": name,
                        "market": market,
                        "trade_date": trade_date,
                    }
                ]
            if float(prem_rate) >= 0.025:
                return [
                    {
                        "rule_id": "lof-premium-sell",
                        "rule_title": "LOF/QDII 高溢价止盈卖出",
                        "rule_summary": f"实时溢价率 {float(prem_rate) * 100:+.2f}% >= +2.50%",
                        "side": "sell",
                        "minute": latest_minute,
                        "price": str(cur_price),
                        "name": name,
                        "market": market,
                        "trade_date": trade_date,
                    }
                ]
            return []

        # combo_confirm: intraday marker + non-extreme adverse premium alignment
        if prem_rate is None:
            return []
        confirmed: list[dict[str, Any]] = []
        for item in intraday_matches:
            if item["side"] == "buy" and float(prem_rate) <= 0.005:
                confirmed.append(
                    {
                        **item,
                        "rule_title": f"双因子买点·{item['rule_title']}",
                        "rule_summary": f"{item['rule_summary']}；溢价率 {float(prem_rate) * 100:+.2f}% <= +0.50%",
                        "name": name,
                        "market": market,
                        "trade_date": trade_date,
                    }
                )
            elif item["side"] == "sell" and float(prem_rate) >= -0.005:
                confirmed.append(
                    {
                        **item,
                        "rule_title": f"双因子卖点·{item['rule_title']}",
                        "rule_summary": f"{item['rule_summary']}；溢价率 {float(prem_rate) * 100:+.2f}% >= -0.50%",
                        "name": name,
                        "market": market,
                        "trade_date": trade_date,
                    }
                )
        return confirmed

    async def _cancel_opposite_active_orders_locked(
        self,
        *,
        trade_date: str,
        symbol: str,
        side: Literal["buy", "sell"],
        local_iso: str,
        exclude_signal_id: str | None = None,
    ) -> list[str]:
        opp_rows = self.auto_store.list_active_opposite_orders(
            trade_date=trade_date,
            symbol=symbol,
            side=side,
            exclude_signal_id=exclude_signal_id,
        )
        if not opp_rows or self.trade_executor is None:
            return []
        cancel_fn = getattr(self.trade_executor, "cancel_order", None)
        if not callable(cancel_fn):
            return []
        cancelled_ids: list[str] = []
        inv_fn = getattr(self.trade_executor, "invalidate_snapshots", None)
        for opp in opp_rows:
            try:
                res = await asyncio.to_thread(
                    cancel_fn,
                    entrust_no=opp.get("entrust_no"),
                    symbol=opp["symbol"],
                    expected_name=opp["name"],
                    side=opp["side"],
                )
                if isinstance(res, Mapping) and res.get("ok"):
                    self._last_gateway_touch_at = self._epoch()
                    if callable(inv_fn):
                        inv_fn(account=True, orders=True, trades=True)
                    prev_msg = str(opp.get("dialog_message") or "").strip()
                    note = f"同标的反向{('卖出' if side == 'sell' else '买入')}信号防自成交预撤单"
                    updated_opp = self.auto_store.update_signal_execution(
                        opp["id"],
                        status="SUPERSEDED_CANCELLED",
                        dialog_message=f"{prev_msg} | {note}" if prev_msg else note,
                        cancel_pending=False,
                        cancel_due_at=None,
                        now_str=local_iso,
                    )
                    self._publish_event("signal_updated", {"signal": updated_opp})
                    cancelled_ids.append(str(opp["id"]))
            except TradeExecutionError as error:
                if error.error_code == "TRADE_ORDER_NOT_FOUND":
                    if callable(inv_fn):
                        inv_fn(account=True, orders=True, trades=True)
                    prev_msg = str(opp.get("dialog_message") or "").strip()
                    note = "反向委托防自成交核查：已成或已不在可撤队列"
                    updated_opp = self.auto_store.update_signal_execution(
                        opp["id"],
                        status="FILLED_OR_CLOSED",
                        dialog_message=f"{prev_msg} | {note}" if prev_msg else note,
                        cancel_pending=False,
                        cancel_due_at=None,
                        now_str=local_iso,
                    )
                    self._publish_event("signal_updated", {"signal": updated_opp})
                elif error.error_code == "BROKER_LOGIN_REQUIRED":
                    self._trip_circuit(error.message)
                    raise
                else:
                    logger.warning(
                        "self-trade opposite cancel warning symbol=%s id=%s err=%s",
                        symbol,
                        opp.get("id"),
                        error.error_code,
                    )
        return cancelled_ids

    async def _run_trade_for_signal(
        self,
        signal_row: dict[str, Any],
        config: dict[str, Any],
        *,
        dry_run: bool,
        override_price: str | None = None,
        override_quantity: int | None = None,
        override_book_level: int | None = None,
        enqueued_at_mono: float | None = None,
        priority: int | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        if self.trade_executor is None:
            raise TradeExecutionError("TRADE_UNCONFIGURED", "交易执行器未配置", 503)

        config = self._effective_symbol_config(config, str(signal_row.get("symbol") or ""))

        precision = int(signal_row["precision"])
        has_manual_price = override_price is not None and bool(str(override_price).strip())
        if has_manual_price:
            raw_price_init = str(override_price).strip()
            try:
                dec_init = Decimal(raw_price_init)
            except InvalidOperation as error:
                raise TradeExecutionError("TRADE_INVALID_PRICE", "委托价格格式无效", 422) from error
            if not dec_init.is_finite() or dec_init <= 0:
                raise TradeExecutionError("TRADE_INVALID_PRICE", "委托价格必须大于 0", 422)

        base_eff_qty = int(override_quantity) if override_quantity is not None else int(signal_row["quantity"])
        if base_eff_qty < 100 or base_eff_qty % 100 != 0 or base_eff_qty > 1_000_000:
            raise TradeExecutionError("TRADE_INVALID_QUANTITY", "委托数量必须为 100 的正整数倍", 422)

        local_now_pre = self._local_now()
        in_session, _ = is_in_continuous_auction(local_now_pre, self.is_trading_day)
        if not dry_run and config["block_non_continuous_session"] and not in_session:
            raise TradeExecutionError(
                "TRADE_SESSION_BLOCKED",
                "当前处于非 A 股连续竞价时段（仅 09:31-11:29 / 13:00-14:55 允许实盘委托），已拦截休市/夜市报单；可点击「演练核验」校验下单流程",
                409,
            )

        wait_start = enqueued_at_mono if enqueued_at_mono is not None else time.monotonic()
        sig_key_val = str(signal_row.get("signal_key") or "")
        is_closing_flatten = (
            ":t0-flatten:" in sig_key_val
            or ":v9-close:" in sig_key_val
            or str(signal_row.get("rule_title") or "").startswith("[尾盘平T")
        )
        eff_priority = (
            int(priority)
            if priority is not None
            else (PriorityTradeScheduler.P0_CRITICAL if is_closing_flatten else PriorityTradeScheduler.P1_ORDER)
        )

        async with self._scheduler.hold(
            eff_priority,
            side=signal_row.get("side"),
            symbol=signal_row.get("symbol"),
        ):
            queue_wait_ms = max(0, round((time.monotonic() - wait_start) * 1000))
            latest_row = self.auto_store.get_signal(str(signal_row["id"]))
            if latest_row is not None:
                if latest_row["status"] not in {"PENDING_CONFIRM", "QUEUED", "EXECUTING"}:
                    raise TradeExecutionError(
                        "TRADE_SIGNAL_ALREADY_HANDLED",
                        f"信号当前状态为 {latest_row['status']}，不可重复处理",
                        409,
                    )
                signal_row = latest_row

            local_now = self._local_now()
            local_iso = local_now.isoformat(timespec="seconds")
            trade_date = local_now.strftime("%Y%m%d")

            updated_basis: str | None = None
            updated_book: Mapping[str, Any] | None = None
            if has_manual_price:
                raw_price = str(override_price).strip()
            else:
                raw_price = signal_row["limit_price"]
                eff_mode = (
                    "counterparty_book"
                    if override_book_level is not None
                    else str(config.get("pricing_mode") or "counterparty_book")
                )
                eff_level = int(
                    override_book_level
                    if override_book_level is not None
                    else (config.get("order_book_level") or 3)
                )
                want_refresh = (
                    eff_mode == "counterparty_book"
                    and (
                        override_book_level is not None
                        or bool(config.get("refresh_book_on_confirm", True))
                        or queue_wait_ms >= 1200
                    )
                )
                if want_refresh:
                    if callable(self.order_book_fetcher):
                        try:
                            live_book = await asyncio.wait_for(
                                asyncio.to_thread(self.get_order_book, signal_row["symbol"], None, force_refresh=True),
                                timeout=0.6,
                            )
                        except (asyncio.TimeoutError, Exception):
                            live_book = None
                    else:
                        live_book = None
                    book_for_calc = live_book if live_book is not None else signal_row.get("order_book")
                    if isinstance(book_for_calc, Mapping):
                        anchor_price = (
                            str(book_for_calc.get("last_price") or "").strip()
                            or str(signal_row["signal_price"])
                        )
                        resolved_p, _, price_meta = resolve_order_limit_price(
                            signal_row["symbol"],
                            str(book_for_calc.get("market") or ""),
                            "sell" if signal_row["side"] == "sell" else "buy",
                            anchor_price,
                            pricing_mode="counterparty_book",
                            order_book_level=eff_level,
                            slippage_ticks=int(config.get("slippage_ticks") or 3),
                            max_slippage_pct=float(config.get("max_slippage_pct") or 1.5),
                            order_book=book_for_calc,
                        )
                        raw_price = resolved_p
                        updated_basis = price_meta.get("price_basis")
                        updated_book = price_meta.get("order_book")

            try:
                dec_price = Decimal(raw_price)
            except InvalidOperation as error:
                raise TradeExecutionError("TRADE_INVALID_PRICE", "委托价格格式无效", 422) from error
            if not dec_price.is_finite() or dec_price <= 0:
                raise TradeExecutionError("TRADE_INVALID_PRICE", "委托价格必须大于 0", 422)
            formatted_price = f"{dec_price:.{precision}f}"

            eff_qty = base_eff_qty

            # Self-trade prevention: cancel any active opposite open orders before reservation/submit
            if not dry_run and not signal_row.get("is_simulation"):
                await self._cancel_opposite_active_orders_locked(
                    trade_date=trade_date,
                    symbol=signal_row["symbol"],
                    side="sell" if signal_row["side"] == "sell" else "buy",
                    local_iso=local_iso,
                    exclude_signal_id=signal_row.get("id"),
                )

            # Atomic lock-inside reservation and risk gate (eliminates TOCTOU overbuy/oversell)
            if not dry_run and signal_row["side"] == "buy" and not signal_row["is_simulation"]:
                planned = round(float(dec_price) * eff_qty, 2)
                eff_broker_cash = self._effective_broker_available_cash(exclude_signal_id=signal_row["id"])
                if self._is_t0_net_cap_rule(signal_row["symbol"], config):
                    pos = self._effective_symbol_position(
                        signal_row["symbol"],
                        trade_date=trade_date,
                        same_day_turnaround=True,
                        exclude_signal_id=signal_row["id"],
                    )
                    held_val = round(int(pos["net_shares"]) * float(dec_price), 2)
                    rem_cap = max(0.0, float(config["daily_max_buy_amount"]) - held_val)
                    if eff_broker_cash is not None:
                        rem_cap = min(rem_cap, max(0.0, eff_broker_cash * 0.9995))
                    if (
                        override_quantity is None
                        and not has_manual_price
                        and str(config.get("sizing_mode") or "fixed_qty") == "model_auto"
                        and planned > rem_cap + 1e-6
                    ):
                        clamped_qty = compute_lot_shares_from_budget(rem_cap, float(dec_price), 100)
                        if 100 <= clamped_qty < eff_qty:
                            eff_qty = clamped_qty
                            planned = round(float(dec_price) * eff_qty, 2)
                    if held_val + planned > float(config["daily_max_buy_amount"]) + 1e-6:
                        msg = f"触发T+0净持仓上限保护：当前已持 {held_val:.2f} + 本笔 {planned:.2f} > 上限 {config['daily_max_buy_amount']:.2f}"
                        self.auto_store.update_signal_execution(
                            signal_row["id"],
                            status="RISK_BLOCKED",
                            limit_price=formatted_price,
                            quantity=eff_qty,
                            price_basis=updated_basis,
                            order_book=updated_book,
                            dialog_message=msg,
                            now_str=local_iso,
                        )
                        self._set_symbol_cooldown(signal_row["symbol"], "buy", error_code="TRADE_DAILY_LIMIT_EXCEEDED", reason=msg)
                        raise TradeExecutionError(
                            "TRADE_DAILY_LIMIT_EXCEEDED",
                            f"超出策略净持仓金额上限（¥{config['daily_max_buy_amount']:.2f}），已风控拦截",
                            409,
                        )
                else:
                    usage = self.auto_store.today_symbol_buy_usage(
                        trade_date,
                        signal_row["symbol"],
                        exclude_signal_id=signal_row["id"],
                    )
                    if (
                        eff_broker_cash is not None
                        and override_quantity is None
                        and not has_manual_price
                        and str(config.get("sizing_mode") or "fixed_qty") == "model_auto"
                        and planned > max(0.0, eff_broker_cash * 0.9995) + 1e-6
                    ):
                        rem_cash = min(
                            max(0.0, float(config["daily_max_buy_amount"]) - usage["buy_amount"]),
                            max(0.0, eff_broker_cash * 0.9995),
                        )
                        clamped_qty = compute_lot_shares_from_budget(rem_cash, float(dec_price), 100)
                        if 100 <= clamped_qty < eff_qty:
                            eff_qty = clamped_qty
                            planned = round(float(dec_price) * eff_qty, 2)
                    if usage["buy_amount"] + planned > float(config["daily_max_buy_amount"]) + 1e-6:
                        msg = f"触发 {signal_row['symbol']} 单日买入限额保护：已用 {usage['buy_amount']:.2f} + 本笔 {planned:.2f} > 上限 {config['daily_max_buy_amount']:.2f}"
                        self.auto_store.update_signal_execution(
                            signal_row["id"],
                            status="RISK_BLOCKED",
                            limit_price=formatted_price,
                            quantity=eff_qty,
                            price_basis=updated_basis,
                            order_book=updated_book,
                            dialog_message=msg,
                            now_str=local_iso,
                        )
                        self._set_symbol_cooldown(signal_row["symbol"], "buy", error_code="TRADE_DAILY_LIMIT_EXCEEDED", reason=msg)
                        raise TradeExecutionError(
                            "TRADE_DAILY_LIMIT_EXCEEDED",
                            f"超出 {signal_row['symbol']} 最大买入金额上限（¥{config['daily_max_buy_amount']:.2f}），已风控拦截",
                            409,
                        )
                if eff_broker_cash is not None and planned > max(0.0, eff_broker_cash * 0.9995) + 1e-6:
                    msg = f"超出国泰海通实时可用资金保护：可用 ¥{eff_broker_cash:.2f} < 本笔所需 ¥{planned:.2f}"
                    self.auto_store.update_signal_execution(
                        signal_row["id"],
                        status="RISK_BLOCKED",
                        limit_price=formatted_price,
                        quantity=eff_qty,
                        price_basis=updated_basis,
                        order_book=updated_book,
                        dialog_message=msg,
                        now_str=local_iso,
                    )
                    self._set_symbol_cooldown(signal_row["symbol"], "buy", error_code="TRADE_INSUFFICIENT_CASH", reason=msg)
                    raise TradeExecutionError(
                        "TRADE_INSUFFICIENT_CASH",
                        f"超出国泰海通实时可用资金（¥{eff_broker_cash:.2f}），已风控拦截",
                        409,
                    )
            elif not dry_run and signal_row["side"] == "sell" and not signal_row["is_simulation"]:
                net_total, max_sell, pos_src = self._max_sellable_above_floor(
                    signal_row["symbol"],
                    trade_date,
                    exclude_signal_id=signal_row["id"],
                )
                if (pos_src != "none" or net_total > 0) and eff_qty > max_sell:
                    if override_quantity is None and max_sell >= 100:
                        eff_qty = max_sell
                    else:
                        msg = (
                            f"触发强制留底 {MIN_RETAINED_SHARES} 股规则：当前持仓 {net_total} 股，"
                            f"需保留至少 {MIN_RETAINED_SHARES} 股延续同花顺累计收益统计"
                        )
                        self.auto_store.update_signal_execution(
                            signal_row["id"],
                            status="RISK_BLOCKED",
                            limit_price=formatted_price,
                            quantity=eff_qty,
                            price_basis=updated_basis,
                            order_book=updated_book,
                            dialog_message=msg,
                            now_str=local_iso,
                        )
                        self._set_symbol_cooldown(signal_row["symbol"], "sell", error_code="TRADE_RETAIN_MIN_SHARES", reason=msg)
                        raise TradeExecutionError("TRADE_RETAIN_MIN_SHARES", msg, 409)

            self.auto_store.update_signal_execution(
                signal_row["id"],
                status="EXECUTING",
                limit_price=formatted_price,
                quantity=eff_qty,
                price_basis=updated_basis,
                order_book=updated_book,
                now_str=local_iso,
            )
            try:
                res = await asyncio.to_thread(
                    self.trade_executor.place_order,
                    symbol=signal_row["symbol"],
                    expected_name=signal_row["name"],
                    side=signal_row["side"],
                    price=formatted_price,
                    quantity=eff_qty,
                    precision=precision,
                    dry_run=dry_run,
                    auto_cancel=False,
                )
            except TradeExecutionError as error:
                revert_status = "PENDING_CONFIRM" if error.error_code == "TRADE_DEVICE_BUSY" else "FAILED"
                self.auto_store.update_signal_execution(
                    signal_row["id"],
                    status=revert_status,
                    dialog_message=error.message,
                    now_str=local_iso,
                )
                if error.error_code == "BROKER_LOGIN_REQUIRED":
                    self._trip_circuit(error.message)
                elif error.error_code == "TRADE_DEVICE_BUSY":
                    pass
                elif self._is_local_reject(error.error_code, error.message):
                    self._set_symbol_cooldown(
                        signal_row["symbol"],
                        signal_row["side"],
                        error_code=error.error_code,
                        reason=error.message,
                    )
                else:
                    failures = int(config.get("consecutive_failures") or 0) + 1
                    patch: dict[str, Any] = {"consecutive_failures": failures}
                    if failures >= 3:
                        patch["circuit_open"] = True
                        patch["circuit_reason"] = f"连续设备/执行异常达到 {failures} 次：{error.message}"
                    self.auto_store.update_config(patch)
                    if patch.get("circuit_open"):
                        self._publish_event("circuit_changed", {"circuit_open": True, "circuit_reason": patch["circuit_reason"]})
                        self._notify_external_best_effort("量化自动交易已熔断暂停", patch["circuit_reason"])
                raise

            res = dict(res)
            res_status = str(res.get("status") or "")
            grey_override_status: str | None = None
            if (not dry_run) and res_status in {"NO_DIALOG_OBSERVED", "UNCONFIRMED"}:
                res, grey_override_status = await self._reconcile_grey_order_locked(
                    signal_row,
                    formatted_price=formatted_price,
                    eff_qty=eff_qty,
                    res=res,
                )
                res_status = str(res.get("status") or "")

            if dry_run:
                final_status = "DRY_RUN_CONFIRMED"
                cancel_pending = False
                cancel_due_at = None
            elif grey_override_status == "RECONCILING":
                final_status = "RECONCILING"
                cancel_pending = False
                cancel_due_at = None
                self._notify_external_best_effort(
                    f"量化委托待核销：{signal_row['side_label']} {format_push_symbol(signal_row['symbol'], signal_row['name'])}",
                    f"委托：限价 ¥{formatted_price} × {eff_qty}股\n说明：提交后弹窗未捕获且核查时设备断开，已保留资金预扣待自动核销",
                )
            elif res.get("accepted"):
                final_status = "SUBMITTED"
                if int(config.get("consecutive_failures") or 0) != 0:
                    self.auto_store.update_config({"consecutive_failures": 0})
                auto_sec = int(config.get("auto_cancel_seconds") or 0)
                cancel_pending = auto_sec > 0
                cancel_due_at = (self._epoch() + auto_sec) if cancel_pending else None
            else:
                final_status = "REJECTED" if res_status == "REJECTED" else "FAILED"
                cancel_pending = False
                cancel_due_at = None
                dlg = str(res.get("dialog_message") or final_status)
                if self._is_local_reject(None, dlg):
                    self._set_symbol_cooldown(
                        signal_row["symbol"],
                        signal_row["side"],
                        error_code="BROKER_LOCAL_REJECT",
                        reason=dlg,
                    )
                else:
                    failures = int(config.get("consecutive_failures") or 0) + 1
                    patch = {"consecutive_failures": failures}
                    if failures >= 3:
                        patch["circuit_open"] = True
                        patch["circuit_reason"] = f"连续未知拒单达到 {failures} 次：{dlg}"
                    self.auto_store.update_config(patch)
                    if patch.get("circuit_open"):
                        self._publish_event("circuit_changed", {"circuit_open": True, "circuit_reason": patch["circuit_reason"]})
                        self._notify_external_best_effort("量化自动交易已熔断暂停", patch["circuit_reason"])

            raw_timings = dict(res.get("timings_ms") or {})
            raw_timings.setdefault("queue_wait_ms", queue_wait_ms)
            res["timings_ms"] = raw_timings

            updated = self.auto_store.update_signal_execution(
                signal_row["id"],
                status=final_status,
                limit_price=formatted_price,
                quantity=eff_qty,
                entrust_no=res.get("entrust_no"),
                dialog_message=res.get("dialog_message"),
                timings_ms=raw_timings,
                cancel_pending=cancel_pending,
                cancel_due_at=cancel_due_at,
                now_str=local_iso,
            )
            self._publish_event("signal_updated", {"signal": updated})
            if (not dry_run) and res.get("accepted"):
                self._last_gateway_touch_at = self._epoch()
                if not signal_row["is_simulation"]:
                    self._schedule_account_sync(max_age_seconds=0.0, warm_after=True, delay_seconds=2.5)
            return updated, res

    async def _emit_trade_signal(
        self,
        *,
        symbol: str,
        item: Mapping[str, Any],
        config: Mapping[str, Any],
        trade_date: str,
        now_epoch: float,
        local_iso: str,
        sig_key: str,
        qty_override: int | None = None,
        skip_cap_check: bool = False,
        deferred_execs: list[Callable[[], Awaitable[None]]] | None = None,
    ) -> int:
        config = self._effective_symbol_config(config, symbol)
        is_closing_flatten = (
            item.get("signal_tag") in {"t0-flatten", "v9-close"}
            or ":t0-flatten:" in sig_key
            or ":v9-close:" in sig_key
        )
        if not is_closing_flatten and self._active_symbol_cooldown(symbol, str(item["side"]), config=config) is not None:
            return 0
        live_book = (
            await asyncio.to_thread(self.get_order_book, symbol, item.get("market"))
            if config.get("pricing_mode", "counterparty_book") == "counterparty_book"
            else None
        )
        try:
            limit_price, precision, price_meta = resolve_order_limit_price(
                symbol,
                item.get("market"),
                item["side"],
                item["price"],
                pricing_mode=str(config.get("pricing_mode") or "counterparty_book"),
                order_book_level=int(config.get("order_book_level") or 3),
                slippage_ticks=int(config["slippage_ticks"]),
                max_slippage_pct=float(config.get("max_slippage_pct") or 1.5),
                order_book=live_book,
            )
        except ValueError:
            return 0

        price_basis = str(price_meta.get("price_basis") or "")
        base_summary = str(item.get("rule_summary") or "")
        if qty_override is not None:
            qty = int(qty_override)
            sizing_note = None
            skip_reason = None
        else:
            qty, sizing_note, skip_reason = self._resolve_signal_quantity(
                symbol=symbol,
                side="sell" if item["side"] == "sell" else "buy",
                limit_price=limit_price,
                config=config,
                candidate=item,
                trade_date=trade_date,
                is_simulation=False,
            )
        parts = [base_summary] if base_summary else []
        if sizing_note and sizing_note not in base_summary:
            parts.append(sizing_note)
        if price_basis and price_basis not in base_summary:
            parts.append(price_basis)
        rule_summary_with_basis = "；".join(parts)

        if qty < 100:
            if item["side"] == "buy" and skip_reason:
                blocked_row = self.auto_store.claim_and_insert_signal(
                    signal_key=sig_key,
                    trade_date=trade_date,
                    symbol=symbol,
                    name=str(item["name"]),
                    side="buy",
                    signal_source=str(config["signal_source"]),
                    rule_id=str(item["rule_id"]),
                    rule_title=str(item["rule_title"]),
                    rule_summary=rule_summary_with_basis,
                    minute=str(item["minute"]),
                    signal_price=str(item["price"]),
                    limit_price=limit_price,
                    precision=precision,
                    quantity=int(config["order_quantity"]),
                    execution_mode=str(config["execution_mode"]),
                    is_simulation=False,
                    status="RISK_BLOCKED",
                    dialog_message=f"动态仓位风控拦截：{skip_reason}",
                    price_basis=price_basis,
                    order_book=price_meta.get("order_book"),
                    now_str=local_iso,
                )
                if blocked_row is not None:
                    self._set_symbol_cooldown(symbol, "buy", error_code="TRADE_INSUFFICIENT_CASH", reason=str(blocked_row["dialog_message"] or skip_reason))
                    self._notify_external_best_effort(
                        f"量化风控拦截 · {format_push_symbol(symbol, str(item['name']))}",
                        str(blocked_row["dialog_message"] or ""),
                    )
            return 0

        planned_amount = round(float(limit_price) * qty, 2)
        over_cap = False
        cap_block_msg = ""
        if not skip_cap_check and item["side"] == "buy":
            eff_broker_cash = self._effective_broker_available_cash()
            fee_safe_cash = max(0.0, eff_broker_cash * 0.9995) if eff_broker_cash is not None else None
            use_net_cap = bool(item.get("t0_net_cap_mode")) or self._is_t0_net_cap_rule(symbol, config)
            if use_net_cap:
                pos = self._effective_symbol_position(
                    symbol,
                    trade_date=trade_date,
                    same_day_turnaround=True,
                )
                held_amt = round(int(pos["net_shares"]) * float(limit_price), 2)
                rem_cap = max(0.0, float(config["daily_max_buy_amount"]) - held_amt)
                if fee_safe_cash is not None:
                    rem_cap = min(rem_cap, fee_safe_cash)
                if str(config.get("sizing_mode") or "fixed_qty") == "model_auto":
                    max_afford_qty = compute_lot_shares_from_budget(rem_cap, float(limit_price), 100)
                    if qty > max_afford_qty:
                        if max_afford_qty >= 100:
                            qty = max_afford_qty
                            planned_amount = round(float(limit_price) * qty, 2)
                            clamp_note = f"限价{limit_price}对齐资金/上限→{qty}股"
                            rule_summary_with_basis = (
                                f"{rule_summary_with_basis}；{clamp_note}"
                                if rule_summary_with_basis
                                else clamp_note
                            )
                        elif int(pos["net_shares"]) >= 100 and (fee_safe_cash is None or fee_safe_cash >= float(limit_price) * 100):
                            return 0
                        elif fee_safe_cash is not None and fee_safe_cash < max(0.0, float(config["daily_max_buy_amount"]) - held_amt):
                            over_cap = True
                            cap_block_msg = (
                                f"柜台实时可用资金不足：可用 ¥{eff_broker_cash:.2f} "
                                f"不足按限价 {limit_price} 买 100 股"
                            )
                        else:
                            over_cap = True
                            cap_block_msg = (
                                f"T+0净仓位金额上限拦截：上限 {config['daily_max_buy_amount']:.2f} "
                                f"不足按限价 {limit_price} 买 100 股"
                            )
                if not over_cap and held_amt + planned_amount > float(config["daily_max_buy_amount"]) + 1e-6:
                    over_cap = True
                    cap_block_msg = (
                        f"T+0净仓位金额上限拦截：已持 {held_amt:.2f} + 本笔 {planned_amount:.2f} > {config['daily_max_buy_amount']:.2f}"
                    )
            else:
                usage = self.auto_store.today_symbol_buy_usage(trade_date, symbol)
                if fee_safe_cash is not None and str(config.get("sizing_mode") or "fixed_qty") == "model_auto":
                    rem_cap = min(
                        max(0.0, float(config["daily_max_buy_amount"]) - usage["buy_amount"]),
                        fee_safe_cash,
                    )
                    max_afford_qty = compute_lot_shares_from_budget(rem_cap, float(limit_price), 100)
                    if qty > max_afford_qty:
                        if max_afford_qty >= 100:
                            qty = max_afford_qty
                            planned_amount = round(float(limit_price) * qty, 2)
                            clamp_note = f"限价{limit_price}对齐可用资金→{qty}股"
                            rule_summary_with_basis = (
                                f"{rule_summary_with_basis}；{clamp_note}"
                                if rule_summary_with_basis
                                else clamp_note
                            )
                        else:
                            over_cap = True
                            cap_block_msg = (
                                f"柜台实时可用资金不足：可用 ¥{eff_broker_cash:.2f} "
                                f"不足按限价 {limit_price} 买 100 股"
                            )
                if not over_cap and usage["buy_amount"] + planned_amount > float(config["daily_max_buy_amount"]) + 1e-6:
                    over_cap = True
                    cap_block_msg = (
                        f"{symbol} 单日累计买入上限拦截：已用 {usage['buy_amount']:.2f} + 本笔 {planned_amount:.2f} > {config['daily_max_buy_amount']:.2f}"
                    )
            if not over_cap and fee_safe_cash is not None and planned_amount > fee_safe_cash + 1e-6:
                over_cap = True
                cap_block_msg = (
                    f"柜台实时可用资金不足：可用 ¥{eff_broker_cash:.2f}，本笔所需 ¥{planned_amount:.2f}"
                )
        elif item["side"] == "sell":
            net_total, max_sell, pos_src = self._max_sellable_above_floor(symbol, trade_date)
            if (pos_src != "none" or net_total > 0) and qty > max_sell:
                if max_sell >= 100:
                    qty = max_sell
                    planned_amount = round(float(limit_price) * qty, 2)
                    floor_note = f"强制留底{MIN_RETAINED_SHARES}股→卖{qty}股"
                    rule_summary_with_basis = (
                        f"{rule_summary_with_basis}；{floor_note}"
                        if rule_summary_with_basis
                        else floor_note
                    )
                else:
                    over_cap = True
                    cap_block_msg = (
                        f"触发强制留底 {MIN_RETAINED_SHARES} 股保护：当前持仓 {net_total} 股，"
                        f"需保留至少 {MIN_RETAINED_SHARES} 股延续同花顺收益统计"
                    )
        if over_cap:
            inserted = self.auto_store.claim_and_insert_signal(
                signal_key=sig_key,
                trade_date=trade_date,
                symbol=symbol,
                name=str(item["name"]),
                side=item["side"],
                signal_source=str(config["signal_source"]),
                rule_id=str(item["rule_id"]),
                rule_title=str(item["rule_title"]),
                rule_summary=rule_summary_with_basis,
                minute=str(item["minute"]),
                signal_price=str(item["price"]),
                limit_price=limit_price,
                precision=precision,
                quantity=qty,
                execution_mode=str(config["execution_mode"]),
                is_simulation=False,
                status="RISK_BLOCKED",
                dialog_message=cap_block_msg,
                price_basis=price_basis,
                order_book=price_meta.get("order_book"),
                now_str=local_iso,
            )
            if inserted is not None:
                err_code = "TRADE_RETAIN_MIN_SHARES" if item["side"] == "sell" else "TRADE_DAILY_LIMIT_EXCEEDED"
                self._set_symbol_cooldown(symbol, str(item["side"]), error_code=err_code, reason=cap_block_msg)
                self._notify_external_best_effort(
                    f"量化风控拦截 · {format_push_symbol(symbol, str(item['name']))}",
                    str(inserted["dialog_message"] or ""),
                )
            return 0

        initial_status = (
            "PENDING_CONFIRM"
            if config["execution_mode"] == "semi_auto"
            else ("QUEUED" if deferred_execs is not None else "EXECUTING")
        )
        expires_at = (
            now_epoch + int(config["confirm_ttl_seconds"])
            if initial_status == "PENDING_CONFIRM"
            else None
        )
        inserted = self.auto_store.claim_and_insert_signal(
            signal_key=sig_key,
            trade_date=trade_date,
            symbol=symbol,
            name=str(item["name"]),
            side=item["side"],
            signal_source=str(config["signal_source"]),
            rule_id=str(item["rule_id"]),
            rule_title=str(item["rule_title"]),
            rule_summary=rule_summary_with_basis,
            minute=str(item["minute"]),
            signal_price=str(item["price"]),
            limit_price=limit_price,
            precision=precision,
            quantity=qty,
            execution_mode=str(config["execution_mode"]),
            is_simulation=False,
            status=initial_status,
            price_basis=price_basis,
            order_book=price_meta.get("order_book"),
            expires_at=expires_at,
            now_str=local_iso,
        )
        if inserted is None:
            return 0
        if initial_status == "PENDING_CONFIRM":
            self._publish_event("signal_pending", {"signal": inserted, "config": dict(config)})
            self._schedule_prestage(inserted)
            if not self.is_viewer_active():
                push_title, push_body = _build_order_push_message(
                    symbol=symbol,
                    name=str(item["name"]),
                    side_label=str(inserted["side_label"]),
                    limit_price=str(limit_price),
                    quantity=int(qty),
                    item=item,
                    status="PENDING_CONFIRM",
                )
                self._notify_external_best_effort(push_title, push_body)
        else:
            exec_prio = PriorityTradeScheduler.P0_CRITICAL if is_closing_flatten else PriorityTradeScheduler.P1_ORDER
            enqueued_mono = time.monotonic()
            cfg_copy = dict(config)
            item_copy = dict(item)
            is_dry = cfg_copy["execution_mode"] == "dry_run"

            async def _execute_emitted() -> None:
                try:
                    updated, _ = await self._run_trade_for_signal(
                        inserted,
                        cfg_copy,
                        dry_run=is_dry,
                        enqueued_at_mono=enqueued_mono,
                        priority=exec_prio,
                    )
                    push_title, push_body = _build_order_push_message(
                        symbol=symbol,
                        name=str(item_copy["name"]),
                        side_label=str(updated["side_label"]),
                        limit_price=str(updated["limit_price"]),
                        quantity=int(updated["quantity"]),
                        item=item_copy,
                        status=str(updated["status"]),
                        dialog_message=updated.get("dialog_message"),
                    )
                    self._notify_external_best_effort(push_title, push_body)
                except TradeExecutionError as err:
                    self._notify_external_best_effort(
                        f"量化委托异常拦截：{inserted['side_label']} {format_push_symbol(symbol, str(item_copy['name']))}",
                        f"委托：限价 ¥{limit_price} × {qty}股\n原因：{err.message}",
                    )

            if deferred_execs is not None:
                deferred_execs.append(_execute_emitted)
            else:
                await _execute_emitted()
        return 1

    def _maybe_schedule_preapproach_prestage(
        self,
        *,
        config: Mapping[str, Any],
        trade_date: str,
        now_epoch: float,
    ) -> None:
        if self.trade_executor is None or not callable(getattr(self.trade_executor, "prestage_order", None)):
            return
        if (now_epoch - self._last_preapproach_prestage_at) < 30.0:
            return
        if self._scheduler.is_busy_or_queued(max_priority=PriorityTradeScheduler.P2_MAINTENANCE):
            return
        try:
            if len(self.auto_store.list_pending_signals(limit=2)) >= 2:
                return
        except Exception:
            return
        read_series = getattr(self.research_store, "read_series", None)
        snap_fn = getattr(self.research_store, "premium_snapshot", None)
        prem_snap = snap_fn() if callable(snap_fn) else None
        for symbol in config.get("symbols") or []:
            sym_cfg = self._effective_symbol_config(config, str(symbol))
            if not sym_cfg.get("symbol_enabled", True) or sym_cfg.get("execution_mode") != "full_auto":
                continue
            sym = str(symbol)
            if self._active_symbol_cooldown(sym, "buy", config=sym_cfg) and self._active_symbol_cooldown(sym, "sell", config=sym_cfg):
                continue
            near_side: str | None = None
            ref_price = "2.100" if sym.startswith(_FUND_PREFIXES) else "10.00"
            if sym_cfg.get("signal_source") == "lof_premium" and isinstance(prem_snap, Mapping):
                for row in prem_snap.get("rows") or []:
                    if isinstance(row, Mapping) and row.get("code") == sym and not row.get("stale"):
                        pr = row.get("premium_rate")
                        if pr is None:
                            pr = calculate_premium_rate(row.get("current_price"), row.get("estimated_price"))
                        if row.get("current_price"):
                            ref_price = str(row["current_price"])
                        if pr is not None:
                            if float(pr) <= -0.015 * 0.85 and self._active_symbol_cooldown(sym, "buy", config=sym_cfg) is None:
                                near_side = "buy"
                            elif float(pr) >= 0.025 * 0.85 and self._active_symbol_cooldown(sym, "sell", config=sym_cfg) is None:
                                near_side = "sell"
                        break
            elif sym_cfg.get("signal_source") == "quant_rule" and callable(read_series):
                try:
                    series = read_series(sym, trade_date)
                except Exception:
                    series = None
                pts = series.get("points") if isinstance(series, Mapping) else None
                if isinstance(pts, list) and pts:
                    last_pt = pts[-1]
                    if last_pt.get("price"):
                        ref_price = str(last_pt["price"])
                    q_rule = self._get_active_quant_rule(sym_cfg)
                    if isinstance(q_rule, Mapping):
                        macd_val = last_pt.get("macd")
                        for side_key, rule_key in (("buy", "buy_rule"), ("sell", "sell_rule")):
                            if self._active_symbol_cooldown(sym, side_key, config=sym_cfg) is not None:
                                continue
                            r_obj = q_rule.get(rule_key)
                            if not isinstance(r_obj, Mapping):
                                continue
                            for cond in r_obj.get("conditions") or []:
                                if not isinstance(cond, Mapping) or "threshold" not in cond:
                                    continue
                                try:
                                    thr = abs(float(cond["threshold"]))
                                except (TypeError, ValueError):
                                    continue
                                if thr > 0 and macd_val is not None:
                                    try:
                                        if abs(float(macd_val)) >= thr * 0.85:
                                            near_side = side_key
                                            break
                                    except (TypeError, ValueError):
                                        pass
                            if near_side:
                                break
            if near_side is None:
                continue
            sym_name, sym_mkt = self._resolve_symbol_meta(sym)
            prec = 3 if is_exchange_fund_symbol(sym) or sym.startswith(_FUND_PREFIXES) else 2
            try:
                fmt_px = f"{float(ref_price):.{prec}f}"
            except (TypeError, ValueError):
                continue
            self._last_preapproach_prestage_at = now_epoch
            self._schedule_prestage(
                {
                    "symbol": sym,
                    "name": sym_name,
                    "market": sym_mkt,
                    "side": near_side,
                    "limit_price": fmt_px,
                    "quantity": int(sym_cfg.get("order_quantity") or 100),
                    "precision": prec,
                }
            )
            return

    async def poll_once(self, *, force_session: bool = False) -> dict[str, Any]:
        local_now = self._local_now()
        now_epoch = self._epoch()
        local_iso = local_now.isoformat(timespec="seconds")
        self.last_poll_at = local_iso

        self.auto_store.expire_overdue_signals(now_epoch, now_str=local_iso)
        await self._run_due_auto_cancels()

        config = self.auto_store.get_config()
        if not config["enabled"] or config["circuit_open"]:
            return {"triggered": 0, "skipped": "disabled_or_circuit"}

        in_session, _ = is_in_continuous_auction(local_now, self.is_trading_day)
        if config["block_non_continuous_session"] and not force_session and not in_session:
            return {"triggered": 0, "skipped": "outside_continuous_session"}

        trade_date = local_now.strftime("%Y%m%d")
        now_hhmm = local_now.strftime("%H:%M")

        if (
            in_session
            and not force_session
            and self.trade_executor is not None
            and self._peek_cached_broker_account() is None
            and (now_epoch - self._last_account_sync_at) >= 15.0
            and not self._exec_lock.locked()
            and not self._account_sync_inflight
        ):
            await self._sync_broker_account(max_age_seconds=0.0, warm_after=True)
            self._last_warm_at = self._epoch()

        triggered = 0
        deferred_execs: list[Callable[[], Awaitable[None]]] = []
        active_configs: list[tuple[str, dict[str, Any]]] = []
        for raw_sym in config["symbols"]:
            sym = str(raw_sym).strip()
            sym_cfg = self._effective_symbol_config(config, sym)
            if sym_cfg.get("symbol_enabled", True):
                active_configs.append((sym, sym_cfg))

        if active_configs and callable(self.batch_order_book_fetcher):
            try:
                await asyncio.to_thread(
                    self.prewarm_order_books,
                    [sym for sym, _ in active_configs],
                )
            except Exception:
                pass

        poll_sem = asyncio.Semaphore(6)

        async def _prepare_symbol_plan(symbol: str, sym_cfg: dict[str, Any]) -> dict[str, Any]:
            async with poll_sem:
                sym_q_rule = self._get_active_quant_rule(sym_cfg) if sym_cfg["signal_source"] == "quant_rule" else None
                sym_ps = normalize_position_sizing(sym_q_rule.get("position_sizing") if sym_q_rule else None)
                flatten_window_active = (
                    sym_cfg.get("sizing_mode") == "model_auto"
                    and sym_q_rule is not None
                    and sym_ps["mode"] == "base_t0_close"
                    and str(sym_ps.get("engine_profile") or "standard") == "standard"
                    and in_session
                    and str(sym_ps["close_flatten_time"]) <= now_hhmm <= "14:55"
                )
                sym_profile = str(sym_ps.get("engine_profile") or "standard")
                v9_close_window_active = (
                    sym_cfg.get("sizing_mode") == "model_auto"
                    and sym_q_rule is not None
                    and sym_profile in {"v9_plus_aggressive", "v9_plus_balanced"}
                    and in_session
                    and str(sym_ps["close_flatten_time"]) <= now_hhmm <= "14:55"
                )

                if self.broker is not None:
                    try:
                        snapshot = await self.broker.refresh(symbol, detail=True, max_age_seconds=0)
                        await asyncio.to_thread(self.research_store.record_snapshot, snapshot)
                    except Exception:
                        pass
                    if is_exchange_fund_symbol(symbol):
                        last_lf = self._line_funds_last_at.get(symbol, 0.0)
                        if now_epoch - last_lf >= 15.0:
                            self._line_funds_last_at[symbol] = now_epoch
                            try:
                                await asyncio.to_thread(
                                    enrich_store_with_line_funds,
                                    self.research_store,
                                    symbol,
                                    trade_date=trade_date,
                                    now=local_now,
                                )
                            except Exception:
                                pass

                candidates = await asyncio.to_thread(
                    self._evaluate_symbol_candidates,
                    symbol=symbol,
                    config=sym_cfg,
                    trade_date=trade_date,
                )
                v9_dec = None
                if v9_close_window_active and sym_q_rule is not None:
                    rule_syms = [str(s).strip() for s in (sym_q_rule.get("symbols") or []) if str(s).strip()]
                    if not rule_syms or symbol in rule_syms:
                        today_sigs = self.auto_store.list_today_symbol_signals(trade_date, symbol)
                        net_sh, eff_cash, expected_sh = self._v9_live_context_args(symbol, sym_cfg, trade_date)
                        v9_dec = await asyncio.to_thread(
                            evaluate_v9_plus_live_decision,
                            self.research_store,
                            symbol=symbol,
                            trade_date=trade_date,
                            now_hhmm=now_hhmm,
                            now_dt=local_now,
                            cap_limit=float(sym_cfg.get("daily_max_buy_amount") or 50000.0),
                            rule_spec=sym_q_rule,
                            pos_sizing=sym_ps,
                            today_signals=today_sigs,
                            rolling_shares=net_sh,
                            broker_available_cash=eff_cash,
                            expected_ledger_shares=expected_sh,
                        )
                return {
                    "symbol": symbol,
                    "sym_cfg": sym_cfg,
                    "sym_q_rule": sym_q_rule,
                    "sym_ps": sym_ps,
                    "flatten_window_active": flatten_window_active,
                    "v9_dec": v9_dec,
                    "candidates": candidates,
                }

        symbol_plans = (
            await asyncio.gather(*(_prepare_symbol_plan(sym, cfg) for sym, cfg in active_configs))
            if len(active_configs) > 1
            else ([await _prepare_symbol_plan(active_configs[0][0], active_configs[0][1])] if active_configs else [])
        )

        for plan in symbol_plans:
            symbol = plan["symbol"]
            sym_cfg = plan["sym_cfg"]
            sym_q_rule = plan["sym_q_rule"]
            sym_ps = plan["sym_ps"]
            flatten_window_active = plan["flatten_window_active"]
            v9_dec = plan["v9_dec"]
            for item in plan["candidates"]:
                sig_tag = str(item.get("signal_tag") or item["rule_id"])
                sig_key = f"{symbol}:{trade_date}:{item['minute']}:{sig_tag}:{item['side']}"
                triggered += await self._emit_trade_signal(
                    symbol=symbol,
                    item=item,
                    config=sym_cfg,
                    trade_date=trade_date,
                    now_epoch=now_epoch,
                    local_iso=local_iso,
                    sig_key=sig_key,
                    qty_override=item.get("qty_override"),
                    deferred_execs=deferred_execs,
                )

            if isinstance(v9_dec, Mapping):
                rebal = v9_dec.get("close_rebalance")
                if isinstance(rebal, Mapping) and sym_ps is not None:
                    sym_name, sym_mkt = self._resolve_symbol_meta(symbol)
                    fl_hhmm = str(sym_ps["close_flatten_time"])
                    rebal_item = {
                        **dict(rebal),
                        "name": sym_name,
                        "market": sym_mkt,
                        "trade_date": trade_date,
                    }
                    triggered += await self._emit_trade_signal(
                        symbol=symbol,
                        item=rebal_item,
                        config=sym_cfg,
                        trade_date=trade_date,
                        now_epoch=now_epoch,
                        local_iso=local_iso,
                        sig_key=f"{symbol}:{trade_date}:{fl_hhmm}:v9-close:{rebal['side']}",
                        qty_override=int(rebal["qty_override"]),
                        deferred_execs=deferred_execs,
                    )

            # Automatic tail close-flatten to restore base position when model_auto + base_t0_close
            if flatten_window_active and sym_q_rule is not None:
                act = self.auto_store.today_symbol_activity(trade_date, symbol)
                net_sh = int(act["net_shares"])
                if abs(net_sh) >= 100:
                    flatten_side: Literal["buy", "sell"] = "sell" if net_sh > 0 else "buy"
                    flatten_qty = (abs(net_sh) // 100) * 100
                    if flatten_side == "sell":
                        net_total, max_sell, pos_src = self._max_sellable_above_floor(symbol, trade_date)
                        if pos_src != "none" or net_total > 0:
                            flatten_qty = min(flatten_qty, max_sell)
                    if flatten_qty < 100:
                        continue
                    sym_name, sym_mkt = self._resolve_symbol_meta(symbol)
                    fl_book = self.get_order_book(symbol, sym_mkt)
                    ref_px = (
                        str((fl_book or {}).get("last_price") or "").strip()
                        or ("2.100" if symbol.startswith(_FUND_PREFIXES) else "10.00")
                    )
                    if not fl_book:
                        read_series = getattr(self.research_store, "read_series", None)
                        if callable(read_series):
                            try:
                                sr = read_series(symbol, trade_date)
                                if isinstance(sr, Mapping) and sr.get("points"):
                                    ref_px = str(sr["points"][-1].get("price") or ref_px)
                            except Exception:
                                pass
                    fl_hhmm = str(sym_ps["close_flatten_time"])
                    fl_item = {
                        "rule_id": str(sym_q_rule["id"]),
                        "rule_title": f"[尾盘平T恢复底仓] {sym_q_rule['name']}",
                        "rule_summary": f"早盘/日内净敞口 {net_sh:+d} 股，按 {fl_hhmm} 闭环规则自动反向平仓 {flatten_qty} 股恢复底仓",
                        "side": flatten_side,
                        "minute": now_hhmm,
                        "price": ref_px,
                        "name": sym_name,
                        "market": sym_mkt,
                        "trade_date": trade_date,
                    }
                    triggered += await self._emit_trade_signal(
                        symbol=symbol,
                        item=fl_item,
                        config=sym_cfg,
                        trade_date=trade_date,
                        now_epoch=now_epoch,
                        local_iso=local_iso,
                        sig_key=f"{symbol}:{trade_date}:{fl_hhmm}:t0-flatten:{flatten_side}",
                        qty_override=flatten_qty,
                        skip_cap_check=(flatten_side == "buy"),
                        deferred_execs=deferred_execs,
                    )

        if deferred_execs:
            await asyncio.gather(*(fn() for fn in deferred_execs))

        if in_session and not force_session and triggered == 0:
            self._maybe_schedule_preapproach_prestage(
                config=config,
                trade_date=trade_date,
                now_epoch=now_epoch,
            )

        if (
            in_session
            and not force_session
            and triggered == 0
            and self.trade_executor is not None
            and not self._exec_lock.locked()
            and (now_epoch - self._last_warm_at) >= self.account_sync_interval_seconds
            and not self.auto_store.list_pending_signals(limit=1)
        ):
            has_acct = callable(getattr(self.trade_executor, "get_account_summary", None))
            has_warm = callable(getattr(self.trade_executor, "keep_warm", None))
            if has_acct and (now_epoch - self._last_account_sync_at) >= self.account_sync_interval_seconds:
                self._last_account_sync_at = now_epoch
                self._last_warm_at = now_epoch
                self._schedule_account_sync(max_age_seconds=40.0, warm_after=True)
            elif has_warm:
                self._last_warm_at = now_epoch
                self._schedule_keep_warm()

        return {"triggered": triggered}

    async def simulate_signal(
        self,
        *,
        symbol: str | None = None,
        side: Literal["buy", "sell"] = "buy",
        viewer_open: bool | None = None,
    ) -> dict[str, Any]:
        if viewer_open is not None:
            self.set_viewer_open(bool(viewer_open))
        config = self.auto_store.get_config()
        target_symbol = (symbol or config["symbols"][0] or "160723").strip()
        if not _SYMBOL_RE.fullmatch(target_symbol):
            raise TradeExecutionError("TRADE_INVALID_SYMBOL", "证券代码必须为 6 位数字", 422)
        config = self._effective_symbol_config(config, target_symbol)

        series = None
        read_series = getattr(self.research_store, "read_series", None)
        if callable(read_series):
            try:
                series = read_series(target_symbol)
            except Exception:
                series = None

        name, market = self._resolve_symbol_meta(
            target_symbol,
            series.get("name") if isinstance(series, Mapping) else None,
            series.get("market") if isinstance(series, Mapping) else None,
        )
        live_book = (
            await asyncio.to_thread(self.get_order_book, target_symbol, market)
            if callable(self.order_book_fetcher)
            else None
        )
        raw_price = "2.109" if target_symbol.startswith(_FUND_PREFIXES) else "10.00"
        if isinstance(live_book, Mapping) and live_book.get("last_price"):
            raw_price = str(live_book["last_price"])
        elif isinstance(series, Mapping) and series.get("points"):
            last_pt = series["points"][-1]
            if last_pt.get("price"):
                raw_price = str(last_pt["price"])

        limit_price, precision, price_meta = resolve_order_limit_price(
            target_symbol,
            market,
            side,
            raw_price,
            pricing_mode=str(config.get("pricing_mode") or "counterparty_book"),
            order_book_level=int(config.get("order_book_level") or 3),
            slippage_ticks=int(config["slippage_ticks"]),
            max_slippage_pct=float(config.get("max_slippage_pct") or 1.5),
            order_book=live_book,
        )
        price_basis = str(price_meta.get("price_basis") or "")
        local_now = self._local_now()
        local_iso = local_now.isoformat(timespec="seconds")
        now_epoch = self._epoch()
        minute = local_now.strftime("%H:%M")
        sig_key = f"sim:{target_symbol}:{now_epoch:.6f}:{uuid.uuid4().hex[:6]}"
        _, active_q_rule = self._summarize_quant_rules(config.get("quant_rule_id"))
        sim_rule_id = (
            str(active_q_rule["id"])
            if config["signal_source"] == "quant_rule" and active_q_rule is not None
            else "simulate-macd-flow"
        )
        sim_source_label = (
            f"量化规则 · {active_q_rule['name']}"
            if config["signal_source"] == "quant_rule" and active_q_rule is not None
            else SIGNAL_SOURCE_LABELS.get(config["signal_source"], "分时MACD+主力大单共振")
        )
        sim_qty = int(config["order_quantity"])
        sim_sizing_note: str | None = None
        if config.get("sizing_mode") == "model_auto":
            active_q_full = self._get_active_quant_rule(config) if config["signal_source"] == "quant_rule" else None
            active_q_ps = normalize_position_sizing(active_q_full.get("position_sizing") if active_q_full else None)
            cand_override: dict[str, Any] | None = (
                {"position_sizing": active_q_full.get("position_sizing")} if active_q_full else None
            )
            v9_diag_note: str | None = None
            if (
                active_q_full is not None
                and active_q_ps.get("engine_profile") in {"v9_plus_aggressive", "v9_plus_balanced"}
            ):
                sim_td = str((series or {}).get("trade_date") or local_now.strftime("%Y%m%d"))
                sim_net_sh, sim_eff_cash, sim_expected_sh = self._v9_live_context_args(
                    target_symbol, config, sim_td
                )
                try:
                    v9_res = await asyncio.to_thread(
                        evaluate_v9_plus_live_decision,
                        self.research_store,
                        symbol=target_symbol,
                        trade_date=sim_td,
                        now_hhmm=minute,
                        cap_limit=float(config.get("daily_max_buy_amount") or 50000.0),
                        rule_spec=active_q_full,
                        pos_sizing=active_q_ps,
                        today_signals=[],
                        rolling_shares=sim_net_sh,
                        broker_available_cash=sim_eff_cash,
                        expected_ledger_shares=sim_expected_sh,
                    )
                    diag = v9_res.get("diagnostics")
                    if isinstance(diag, Mapping):
                        tier_w = diag["buy_weight_pct"] if side == "buy" else diag["sell_weight_pct"]
                        tier_l = diag["buy_tier"] if side == "buy" else diag["sell_tier"]
                        cand_override = {
                            **(cand_override or {}),
                            "weight_pct": tier_w,
                            "sizing_tier": tier_l,
                        }
                        v9_diag_note = (
                            f"V9+实时状态: 汇调溢价{diag['prem_pct']:+.2f}% / 价差{diag['sp_fx_pct']:+.2f}% / "
                            f"BRN2年分位{diag['brn_pos_pct']:.1f}% / 尾盘目标{diag['close_target_weight_pct']:.0f}%"
                        )
                except Exception:
                    v9_diag_note = None
            dyn_qty, dyn_note, _ = self._resolve_signal_quantity(
                symbol=target_symbol,
                side=side,
                limit_price=limit_price,
                config=config,
                candidate=cand_override,
                trade_date=local_now.strftime("%Y%m%d"),
                is_simulation=True,
            )
            if dyn_qty >= 100:
                sim_qty = dyn_qty
            sim_sizing_note = "；".join(p for p in (dyn_note, v9_diag_note) if p) or None
        sim_summary = f"人工触发模拟信号：信号价 {raw_price}；定价依据 {price_basis}"
        if sim_sizing_note:
            sim_summary = f"{sim_sizing_note}；{sim_summary}"
        inserted = self.auto_store.claim_and_insert_signal(
            signal_key=sig_key,
            trade_date=local_now.strftime("%Y%m%d"),
            symbol=target_symbol,
            name=name,
            side=side,
            signal_source=config["signal_source"],
            rule_id=sim_rule_id,
            rule_title=f"[模拟演练] {sim_source_label}",
            rule_summary=sim_summary,
            minute=minute,
            signal_price=str(raw_price),
            limit_price=limit_price,
            precision=precision,
            quantity=sim_qty,
            execution_mode=config["execution_mode"],
            is_simulation=True,
            status="PENDING_CONFIRM",
            dialog_message="模拟待确认信号：可在弹窗核对实时五档盘口并点击「演练核验」或验证连续竞价风控拦截",
            price_basis=price_basis,
            order_book=price_meta.get("order_book"),
            expires_at=now_epoch + 300,
            now_str=local_iso,
        )
        assert inserted is not None
        self._publish_event("signal_pending", {"signal": inserted, "config": dict(config)})
        self._schedule_prestage(inserted)
        if not self.is_viewer_active():
            sim_push_title, sim_push_body = _build_order_push_message(
                symbol=target_symbol,
                name=name,
                side_label=str(inserted["side_label"]),
                limit_price=str(limit_price),
                quantity=int(inserted["quantity"]),
                item={
                    "push_meaning": "模拟演练待确认委托（仅校验弹窗与风控，不发真单）",
                    "push_why": sim_sizing_note or f"报价依据：{price_basis}",
                },
                status="PENDING_CONFIRM",
            )
            self._notify_external_best_effort(sim_push_title, sim_push_body)
        return {"signal": inserted, "strategy": self.get_status()}

    async def decide_signal(
        self,
        signal_id: str,
        *,
        action: Literal["approve", "reject"],
        dry_run: bool = False,
        override_price: str | None = None,
        override_quantity: int | None = None,
        override_book_level: int | None = None,
    ) -> dict[str, Any]:
        local_now = self._local_now()
        now_epoch = self._epoch()
        local_iso = local_now.isoformat(timespec="seconds")

        row = self.auto_store.get_signal(signal_id)
        if row is None:
            raise KeyError(signal_id)
        if row["status"] != "PENDING_CONFIRM":
            raise TradeExecutionError(
                "TRADE_SIGNAL_ALREADY_HANDLED",
                f"信号当前状态为 {row['status']}，不可重复处理",
                409,
            )

        if action == "reject":
            updated = self.auto_store.update_signal_execution(
                signal_id,
                status="DISMISSED",
                dialog_message="已人工忽略该交易信号",
                now_str=local_iso,
            )
            return {"signal": updated, "order_result": None, "strategy": self.get_status()}

        if row["expires_at"] is not None and now_epoch > float(row["expires_at"]):
            self.auto_store.update_signal_execution(
                signal_id,
                status="EXPIRED",
                dialog_message="确认信号已超过有效期，请等待下次信号",
                now_str=local_iso,
            )
            raise TradeExecutionError("TRADE_SIGNAL_EXPIRED", "确认信号已超时失效", 409)

        config = self.auto_store.get_config()
        updated, order_result = await self._run_trade_for_signal(
            row,
            config,
            dry_run=dry_run,
            override_price=override_price,
            override_quantity=override_quantity,
            override_book_level=override_book_level,
        )
        return {"signal": updated, "order_result": order_result, "strategy": self.get_status()}
