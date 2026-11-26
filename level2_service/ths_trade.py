"""Protected Android UI trade executor for Guotai Haitong inside Tonghuashun.

Hard isolation rules:
- Operates ONLY on the authorized core emulator (never emulator-5554 / fund role).
- Never stores, logs, requests, or submits broker passwords or verification codes.
- Aborts immediately on any password, login, CAPTCHA, or verification prompt.
- Performs mandatory UI readback verification before clicking order submission.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import re
from threading import RLock
import time
from typing import Any, Callable, Iterator, Literal, Protocol, runtime_checkable
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

from .runner import APP_ACTIVITY, APP_PACKAGE, RunnerControl


SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
DEFAULT_PROTECTED_SERIAL = "emulator-5554"
MIN_RETAINED_SHARES = 100

_BOUNDS_RE = re.compile(r"^\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]$")
_SYMBOL_RE = re.compile(r"^[0-9]{6}\Z")
_ENTRUST_NO_RE = re.compile(r"(?:合同编号|委托编号|合同号|编号)[:：\s]*([A-Za-z0-9_-]{3,32})")
_ACCOUNT_LABEL_RE = re.compile(
    r"((?:资金账号|资金帐户|股东代码|股东账号|客户号|账号|帐户|账户)[:：\s*]*)"
    r"([A-Za-z]?\d{5,})"
)
_LONG_DIGITS_RE = re.compile(r"(?<![\d.])[A-Za-z]?\d{8,}(?![\d.%万亿元股手])")

_AUTH_BLOCK_KEYWORDS = (
    "请输入交易密码",
    "请输入资金密码",
    "请输入通讯密码",
    "交易密码",
    "通讯密码",
    "资金密码",
    "手势解锁",
    "手势密码",
    "人脸识别",
    "图形验证码",
    "短信验证码",
    "登录已超时",
    "请先登录",
    "立即登录",
    "重新登录",
)

_CONFIRM_BUTTON_TEXTS = (
    "确认买入",
    "确认卖出",
    "确定买入",
    "确定卖出",
    "确认委托",
    "确认撤单",
    "确定撤单",
    "确认下单",
    "继续委托",
    "继续买入",
    "继续卖出",
    "隔夜委托",
    "确定提交",
    "继续",
    "确定",
    "确认",
    "提交",
)
_CANCEL_BUTTON_TEXTS = ("取消", "放弃", "暂不", "返回修改")
_ACK_BUTTON_TEXTS = ("确定", "知道了", "我知道了", "好的", "好", "关闭", "确认", "返回")

_MAIN_PAGE_CHROME_RIDS = frozenset({
    "com.hexin.plat.android:id/page_title_view",
    "com.hexin.plat.android:id/navi_buttonbar",
    "com.hexin.plat.android:id/tab_a",
    "com.hexin.plat.android:id/btn_transaction",
    "com.hexin.plat.android:id/recyclerview_id",
    "com.hexin.plat.android:id/frame_layout",
    "com.hexin.plat.android:id/btn",
    "com.hexin.plat.android:id/drwt",
    "com.hexin.plat.android:id/drcj",
    "com.hexin.plat.android:id/empty_tips",
    "com.hexin.plat.android:id/capitalLayout",
    "com.hexin.plat.android:id/title",
})

_CANCELLABLE_KEYWORDS = ("未报", "待报", "正报", "已报", "已申报", "部成", "部分成交", "待撤")
_TERMINAL_ORDER_KEYWORDS = ("全部成交", "已成", "已撤", "撤单", "废单", "部撤", "拒绝", "作废")

_HOLDING_FOOTER_WORDS = frozenset(
    {"查看已清仓股票", "持仓管理", "批量买入", "批量卖出", "止盈止损", "持仓资讯", "资产分析"}
)


class TradeExecutionError(RuntimeError):
    """Structured trade error safe to propagate across internal bridge & UI."""

    def __init__(self, error_code: str, message: str, status_code: int = 503) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message
        self.status_code = status_code


def mask_sensitive_numbers(text: str, *, keep_symbols: set[str] | None = None) -> str:
    """Mask broker account numbers and shareholder IDs while keeping prices/quantities/entrust_no."""
    if not text:
        return text
    allowed = set(keep_symbols or set())
    for m in _ENTRUST_NO_RE.finditer(text):
        allowed.add(m.group(1))
        # Also protect trailing digit part if prefixed by multi-char letters like HT2026...
        digits_match = re.search(r"[A-Za-z]?\d{8,}", m.group(1))
        if digits_match:
            allowed.add(digits_match.group(0))

    def _mask_labeled(match: re.Match[str]) -> str:
        prefix, raw = match.group(1), match.group(2)
        if raw in allowed:
            return match.group(0)
        tail = raw[-4:] if len(raw) >= 4 else raw
        return f"{prefix}****{tail}"

    masked = _ACCOUNT_LABEL_RE.sub(_mask_labeled, text)

    def _mask_long(match: re.Match[str]) -> str:
        raw = match.group(0)
        if raw in allowed:
            return raw
        return f"****{raw[-4:]}"

    return _LONG_DIGITS_RE.sub(_mask_long, masked)


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone(SHANGHAI_TZ).isoformat(timespec="seconds")


def _parse_bounds(raw: str | None) -> tuple[int, int, int, int] | None:
    if not raw:
        return None
    match = _BOUNDS_RE.fullmatch(raw.strip())
    if match is None:
        return None
    left, top, right, bottom = (int(value) for value in match.groups())
    return (left, top, right, bottom)


def _bounds_center(bounds: tuple[int, int, int, int]) -> tuple[float, float]:
    return ((bounds[0] + bounds[2]) / 2.0, (bounds[1] + bounds[3]) / 2.0)


def _parse_float(raw: str | None) -> float | None:
    if not raw:
        return None
    cleaned = raw.strip().replace(",", "").replace("股", "").replace("元", "").replace("%", "")
    if cleaned in {"", "--", "-"}:
        return None
    try:
        value = float(cleaned)
    except ValueError:
        return None
    return value


def _parse_int(raw: str | None) -> int | None:
    value = _parse_float(raw)
    if value is None:
        return None
    return int(round(value))


def _is_numeric_like(raw: str | None) -> bool:
    if not raw:
        return False
    cleaned = raw.strip().replace(",", "").replace("%", "")
    if cleaned in {"", "--", "-"}:
        return False
    return bool(re.fullmatch(r"[+-]?\d+(?:\.\d+)?", cleaned))


_GENERIC_RISK_PROMPT_KEYWORDS = (
    "风险",
    "溢价",
    "提示",
    "揭示",
    "波动",
    "偏离",
    "继续委托",
    "继续买入",
    "继续卖出",
)


def _is_confirm_dialog_symbol_safe(joined_text: str, symbol: str, name_prefix: str) -> bool:
    """Allow symbol/name-matched confirm dialogs or generic pre-order risk/LOF-premium prompts."""
    if symbol in joined_text or (name_prefix and name_prefix in joined_text):
        return True
    codes = set(re.findall(r"(?<!\d)\d{6}(?!\d)", joined_text))
    if codes and symbol not in codes:
        return False
    return (not codes) and any(kw in joined_text for kw in _GENERIC_RISK_PROMPT_KEYWORDS)


def _normalize_side(side: str | None) -> str:
    text = (side or "").strip()
    if "买" in text or text.lower() == "buy":
        return "buy"
    if "卖" in text or text.lower() == "sell":
        return "sell"
    return "unknown"


def _side_label(side: str) -> str:
    if side == "buy":
        return "买入"
    if side == "sell":
        return "卖出"
    return "未知"


def is_status_cancellable(status: str | None) -> bool:
    text = (status or "").strip()
    if not text:
        return False
    if any(token in text for token in _TERMINAL_ORDER_KEYWORDS):
        return False
    return any(token in text for token in _CANCELLABLE_KEYWORDS)


def detect_auth_blocking(root: ElementTree.Element) -> str | None:
    """Return a non-empty reason when a password/login/verification gate is visible."""
    for node in root.iter():
        if (node.attrib.get("package") or "").startswith("com.android.systemui"):
            continue
        if node.attrib.get("password") == "true":
            return "检测到密码输入框"
        text = (node.attrib.get("text") or "").strip()
        if not text:
            continue
        for keyword in _AUTH_BLOCK_KEYWORDS:
            if keyword in text:
                return keyword
    return None


@dataclass(frozen=True)
class DialogInfo:
    kind: Literal["confirm", "alert", "action_sheet"]
    texts: list[str]
    message: str
    confirm_button: tuple[str, tuple[int, int, int, int]] | None = None
    cancel_button: tuple[str, tuple[int, int, int, int]] | None = None
    ack_button: tuple[str, tuple[int, int, int, int]] | None = None
    action_button: tuple[str, tuple[int, int, int, int]] | None = None


def _is_inside_chrome(ancestors: list[ElementTree.Element]) -> bool:
    for anc in ancestors:
        rid = anc.attrib.get("resource-id") or ""
        if rid in {
            "com.hexin.plat.android:id/navi_buttonbar",
            "com.hexin.plat.android:id/tab_widget",
            "com.hexin.plat.android:id/recyclerview_id",
            "com.hexin.plat.android:id/transaction_layout",
            "com.hexin.plat.android:id/trade_layout",
            "com.hexin.plat.android:id/fix_title",
        }:
            return True
    return False


def _collect_ancestors(root: ElementTree.Element) -> dict[int, list[ElementTree.Element]]:
    parent_map: dict[int, list[ElementTree.Element]] = {}

    def walk(node: ElementTree.Element, ancestors: list[ElementTree.Element]) -> None:
        parent_map[id(node)] = ancestors
        for child in node:
            walk(child, [*ancestors, node])

    walk(root, [])
    return parent_map


def _has_main_page_chrome(scope: ElementTree.Element) -> bool:
    for node in scope.iter():
        if (node.attrib.get("resource-id") or "") in _MAIN_PAGE_CHROME_RIDS:
            return True
    return False


def detect_modal_dialog(root: ElementTree.Element) -> DialogInfo | None:
    """Inspect whether an overlay confirmation/alert/action dialog is currently displayed."""
    parent_map = _collect_ancestors(root)

    # Identify overlay containers: either android:id/content has >1 top-level Window/FrameLayout
    # or a focused standalone Dialog window is the sole non-systemui top window.
    top_windows = [
        child
        for child in list(root)
        if not (child.attrib.get("package") or "").startswith("com.android.systemui")
    ]
    search_roots: list[ElementTree.Element] = []
    if len(top_windows) > 1:
        # The last window in hierarchy is the topmost overlay / popup / dialog window
        search_roots.append(top_windows[-1])
    else:
        if len(top_windows) == 1:
            win = top_windows[0]
            win_bounds = _parse_bounds(win.attrib.get("bounds"))
            is_compact_window = win_bounds is not None and (win_bounds[3] - win_bounds[1]) < 1600
            if not _has_main_page_chrome(win) or is_compact_window:
                search_roots.append(win)
        # Check if android:id/content has a popup sibling above frame_layout
        for node in root.iter():
            if node.attrib.get("resource-id") == "android:id/content":
                children = list(node)
                if len(children) >= 2:
                    # Last child might be floating dialog or guide overlay
                    last = children[-1]
                    if last.attrib.get("resource-id") != "com.hexin.plat.android:id/frame_layout":
                        search_roots.append(last)

    def _scan_scope(scope: ElementTree.Element, *, require_non_chrome: bool) -> tuple[
        list[str],
        tuple[str, tuple[int, int, int, int]] | None,
        tuple[str, tuple[int, int, int, int]] | None,
        tuple[str, tuple[int, int, int, int]] | None,
        tuple[str, tuple[int, int, int, int]] | None,
        list[tuple[str, tuple[int, int, int, int]]],
    ]:
        texts: list[str] = []
        conf = None
        canc = None
        ack = None
        act = None
        clickable_buttons: list[tuple[str, tuple[int, int, int, int]]] = []
        for node in scope.iter():
            if (node.attrib.get("package") or "").startswith("com.android.systemui"):
                continue
            if require_non_chrome and _is_inside_chrome(parent_map.get(id(node), [])):
                continue
            text = (node.attrib.get("text") or "").strip()
            rid = node.attrib.get("resource-id") or ""
            if not text:
                continue
            # Ignore tooltip bubbles
            if rid in {"com.hexin.plat.android:id/bubble_guide_content"}:
                continue
            bounds = _parse_bounds(node.attrib.get("bounds"))
            texts.append(text)
            is_clickable = node.attrib.get("clickable") == "true" or any(
                anc.attrib.get("clickable") == "true" for anc in parent_map.get(id(node), [])[-2:]
            )
            if bounds is not None and is_clickable:
                if len(text) <= 16 and text not in {"国泰海通证券", "A股", "港股通"}:
                    clickable_buttons.append((text, bounds))
                if text in _CANCEL_BUTTON_TEXTS and canc is None:
                    canc = (text, bounds)
                elif text in _CONFIRM_BUTTON_TEXTS and conf is None:
                    conf = (text, bounds)
                elif text in _ACK_BUTTON_TEXTS and ack is None:
                    ack = (text, bounds)
                elif text in {"撤单", "立即撤单"} and act is None:
                    act = (text, bounds)
        return texts, conf, canc, ack, act, clickable_buttons

    for scope in search_roots:
        texts, confirm_candidate, cancel_candidate, ack_candidate, cancel_action_candidate, clickable_buttons = _scan_scope(
            scope, require_non_chrome=False
        )
        if not confirm_candidate and not ack_candidate and not cancel_action_candidate:
            dedup_btns: list[tuple[str, tuple[int, int, int, int]]] = []
            seen_btn_keys: set[tuple[str, tuple[int, int, int, int]]] = set()
            for item in clickable_buttons:
                if item not in seen_btn_keys:
                    seen_btn_keys.add(item)
                    dedup_btns.append(item)
            if cancel_candidate and len(dedup_btns) == 2:
                other = next((item for item in dedup_btns if item != cancel_candidate), None)
                if other is not None:
                    confirm_candidate = other
            elif len(dedup_btns) == 1 and not _has_main_page_chrome(scope):
                ack_candidate = dedup_btns[0]

        if confirm_candidate and cancel_candidate:
            msg = mask_sensitive_numbers(" | ".join(texts))
            return DialogInfo(
                kind="confirm",
                texts=[mask_sensitive_numbers(t) for t in texts],
                message=msg,
                confirm_button=confirm_candidate,
                cancel_button=cancel_candidate,
            )
        if ack_candidate or confirm_candidate:
            btn = ack_candidate or confirm_candidate
            msg = mask_sensitive_numbers(" | ".join(texts))
            return DialogInfo(
                kind="alert",
                texts=[mask_sensitive_numbers(t) for t in texts],
                message=msg,
                ack_button=btn,
            )
        if cancel_action_candidate and cancel_candidate:
            msg = mask_sensitive_numbers(" | ".join(texts))
            return DialogInfo(
                kind="action_sheet",
                texts=[mask_sensitive_numbers(t) for t in texts],
                message=msg,
                action_button=cancel_action_candidate,
                cancel_button=cancel_candidate,
            )

    # Fallback: scan entire tree excluding known page chrome for AlertDialog nodes
    texts, confirm_candidate, cancel_candidate, ack_candidate, _, _ = _scan_scope(
        root, require_non_chrome=True
    )
    if confirm_candidate and cancel_candidate:
        dialog_texts = [t for t in texts if t not in {"国泰海通证券", "A股", "港股通"}]
        return DialogInfo(
            kind="confirm",
            texts=[mask_sensitive_numbers(t) for t in dialog_texts],
            message=mask_sensitive_numbers(" | ".join(dialog_texts)),
            confirm_button=confirm_candidate,
            cancel_button=cancel_candidate,
        )
    if ack_candidate or confirm_candidate:
        btn = ack_candidate or confirm_candidate
        dialog_texts = [t for t in texts if t not in {"国泰海通证券", "A股", "港股通"}]
        return DialogInfo(
            kind="alert",
            texts=[mask_sensitive_numbers(t) for t in dialog_texts],
            message=mask_sensitive_numbers(" | ".join(dialog_texts)),
            ack_button=btn,
        )
    return None


def parse_account_metrics_xml(root: ElementTree.Element) -> dict[str, Any]:
    """Parse broker name, masked account, and capital cells from holdings/landing XML."""
    broker_name = ""
    account_masked = ""
    position_ratio = None
    titles: list[tuple[str, tuple[int, int, int, int]]] = []
    values: list[tuple[str, tuple[int, int, int, int]]] = []

    for node in root.iter():
        rid = node.attrib.get("resource-id") or ""
        text = (node.attrib.get("text") or "").strip()
        if not text:
            continue
        if rid in {"com.hexin.plat.android:id/page_title_view", "com.hexin.plat.android:id/qs_name_text"}:
            broker_name = text
        elif rid in {"com.hexin.plat.android:id/account_info_view", "com.hexin.plat.android:id/account_text"}:
            account_masked = mask_sensitive_numbers(text)
        elif rid == "com.hexin.plat.android:id/position_text":
            position_ratio = text.replace("仓位", "").strip()
        elif rid in {
            "com.hexin.plat.android:id/capital_cell_title",
            "com.hexin.plat.android:id/capital_title_text",
            "com.hexin.plat.android:id/yingkui_title_text",
            "com.hexin.plat.android:id/shizhi_title_text",
            "com.hexin.plat.android:id/dryk_title_text",
        }:
            b = _parse_bounds(node.attrib.get("bounds"))
            if b:
                titles.append((text, b))
        elif rid in {
            "com.hexin.plat.android:id/capital_cell_value",
            "com.hexin.plat.android:id/capital_value_text",
            "com.hexin.plat.android:id/yingkui_value_text",
            "com.hexin.plat.android:id/shizhi_value_text",
            "com.hexin.plat.android:id/dryk_value_text",
        }:
            b = _parse_bounds(node.attrib.get("bounds"))
            if b:
                values.append((text, b))

    metrics_by_title: dict[str, str] = {}
    for title_text, tb in titles:
        best_val = None
        best_dist = 10**9
        for val_text, vb in values:
            # Value sits right below title in the same column
            if vb[1] < tb[1]:
                continue
            dy = vb[1] - tb[3]
            dx = abs(vb[0] - tb[0])
            if dy <= 140 and dx <= 80:
                dist = dx * 2 + max(0, dy)
                if dist < best_dist:
                    best_dist = dist
                    best_val = val_text
        if best_val is not None:
            metrics_by_title[title_text] = best_val

    return {
        "broker_name": broker_name or "国泰海通证券",
        "account_masked": account_masked or "****",
        "position_ratio": position_ratio,
        "metrics": {
            "total_assets": metrics_by_title.get("总资产"),
            "available_cash": metrics_by_title.get("可用"),
            "market_value": metrics_by_title.get("总市值"),
            "holding_profit": metrics_by_title.get("浮动盈亏"),
            "withdrawable_cash": metrics_by_title.get("可取"),
        },
    }


def _find_recyclerviews(root: ElementTree.Element) -> list[ElementTree.Element]:
    return [
        node
        for node in root.iter()
        if node.attrib.get("resource-id") == "com.hexin.plat.android:id/recyclerview_id"
    ]


def _row_texts(node: ElementTree.Element) -> list[str]:
    return [
        t
        for child in node.iter()
        if (t := (child.attrib.get("text") or "").strip())
    ]


def parse_holdings_xml(
    root: ElementTree.Element,
    *,
    name_to_symbol: Callable[[str], str | None] | None = None,
) -> list[dict[str, Any]]:
    """Parse position rows from the 持仓 tab RecyclerView."""
    rows: list[dict[str, Any]] = []
    for rv in _find_recyclerviews(root):
        for child in list(rv):
            texts = _row_texts(child)
            if not texts:
                continue
            if any(word in texts for word in _HOLDING_FOOTER_WORDS):
                continue
            # Expected 8 values:
            # [name, market_value, profit, profit_pct, shares, available_shares, cost_price, current_price]
            if len(texts) < 8:
                continue
            name = texts[0]
            if not _is_numeric_like(texts[1]) or not _is_numeric_like(texts[4]):
                continue
            symbol = name_to_symbol(name) if callable(name_to_symbol) else None
            rows.append(
                {
                    "symbol": symbol,
                    "name": name,
                    "market_value": texts[1],
                    "profit": texts[2],
                    "profit_pct": texts[3],
                    "shares": _parse_int(texts[4]) or 0,
                    "available_shares": _parse_int(texts[5]) or 0,
                    "cost_price": texts[6],
                    "current_price": texts[7],
                }
            )
    return rows


def parse_orders_xml(
    root: ElementTree.Element,
    *,
    name_to_symbol: Callable[[str], str | None] | None = None,
    max_y: int | None = None,
    force_cancellable: bool = False,
) -> list[dict[str, Any]]:
    """Parse order rows from 当日委托 or 撤单 upper RecyclerView."""
    rows: list[dict[str, Any]] = []
    for rv in _find_recyclerviews(root):
        rv_bounds = _parse_bounds(rv.attrib.get("bounds"))
        if max_y is not None and rv_bounds is not None and rv_bounds[1] >= max_y:
            continue
        for idx, child in enumerate(list(rv)):
            child_bounds = _parse_bounds(child.attrib.get("bounds"))
            if max_y is not None and child_bounds is not None and child_bounds[1] >= max_y:
                continue
            texts = _row_texts(child)
            # Expected 8 values in 4 columns:
            # [name, time, entrust_price, avg_price, entrust_qty, filled_qty, side_label, status]
            if len(texts) < 8:
                continue
            name, entrust_time, entrust_price, avg_price, entrust_qty, filled_qty, side_raw, status = texts[:8]
            if not _is_numeric_like(entrust_price) or not _is_numeric_like(entrust_qty):
                continue
            side = _normalize_side(side_raw)
            if side == "unknown":
                continue
            symbol = name_to_symbol(name) if callable(name_to_symbol) else None
            cancellable = True if force_cancellable else is_status_cancellable(status)
            rows.append(
                {
                    "entrust_no": None,
                    "symbol": symbol,
                    "name": name,
                    "side": side,
                    "side_label": _side_label(side),
                    "price": entrust_price,
                    "avg_price": avg_price,
                    "quantity": _parse_int(entrust_qty) or 0,
                    "filled_quantity": _parse_int(filled_qty) or 0,
                    "status": status,
                    "time": entrust_time,
                    "cancellable": cancellable,
                    "bounds": list(child_bounds) if child_bounds else None,
                    "row_index": idx,
                }
            )
    return rows


def parse_trades_xml(
    root: ElementTree.Element,
    *,
    name_to_symbol: Callable[[str], str | None] | None = None,
) -> list[dict[str, Any]]:
    """Parse deal rows from 当日成交 RecyclerView using explicit resource IDs."""
    rows: list[dict[str, Any]] = []
    for rv in _find_recyclerviews(root):
        for child in list(rv):
            fields: dict[str, str] = {}
            for node in child.iter():
                rid = node.attrib.get("resource-id") or ""
                text = (node.attrib.get("text") or "").strip()
                if text and rid:
                    fields[rid] = text
            name = fields.get("com.hexin.plat.android:id/stock_name")
            deal_time = fields.get("com.hexin.plat.android:id/deal_time")
            price = fields.get("com.hexin.plat.android:id/price")
            amount = fields.get("com.hexin.plat.android:id/amount")
            opt_type = fields.get("com.hexin.plat.android:id/opt_type")
            deal_money = fields.get("com.hexin.plat.android:id/deal_money")
            if not name or not price or not amount:
                continue
            side = _normalize_side(opt_type)
            symbol = name_to_symbol(name) if callable(name_to_symbol) else None
            rows.append(
                {
                    "trade_no": None,
                    "entrust_no": None,
                    "symbol": symbol,
                    "name": name,
                    "side": side,
                    "side_label": _side_label(side),
                    "filled_price": price,
                    "filled_quantity": _parse_int(amount) or 0,
                    "filled_amount": deal_money or "0",
                    "time": deal_time or "",
                    "status": "已成",
                }
            )
    return rows


def parse_form_readback(root: ElementTree.Element) -> dict[str, Any]:
    """Read back stock code, name, price, quantity, limits, and availability from order XML."""
    code = ""
    stock_name = ""
    price_text = ""
    qty_text = ""
    limit_down = None
    limit_up = None
    available_text = ""
    keyboard_open = False
    has_submit_button = False
    code_top: int | None = None
    fallback_edits: list[tuple[int, str]] = []

    def _first_edit_text(container: ElementTree.Element) -> str:
        for child in container.iter():
            if child.attrib.get("class") == "android.widget.EditText":
                return (child.attrib.get("text") or "").strip()
        return ""

    for node in root.iter():
        rid = node.attrib.get("resource-id") or ""
        text = (node.attrib.get("text") or "").strip()
        if rid in {"com.hexin.plat.android:id/keyboard", "com.hexin.plat.android:id/dialogplus_outmost_container"}:
            keyboard_open = True
        elif rid == "com.hexin.plat.android:id/auto_stockcode" and node.attrib.get("class") == "android.widget.EditText":
            code = text
            b = _parse_bounds(node.attrib.get("bounds"))
            if b is not None:
                code_top = b[1]
        elif rid == "com.hexin.plat.android:id/stockname":
            stock_name = text
        elif rid == "com.hexin.plat.android:id/stockprice":
            price_text = _first_edit_text(node)
        elif rid == "com.hexin.plat.android:id/stockvolume":
            qty_text = _first_edit_text(node)
        elif rid == "com.hexin.plat.android:id/dietingprice":
            limit_down = text if _is_numeric_like(text) else None
        elif rid == "com.hexin.plat.android:id/zhangtingprice":
            limit_up = text if _is_numeric_like(text) else None
        elif rid == "com.hexin.plat.android:id/couldbuy":
            available_text = text
        elif rid == "com.hexin.plat.android:id/btn_transaction":
            has_submit_button = True
        elif (
            node.attrib.get("class") == "android.widget.EditText"
            and rid != "com.hexin.plat.android:id/auto_stockcode"
            and ((node.attrib.get("package") or "") in {"", APP_PACKAGE})
        ):
            b = _parse_bounds(node.attrib.get("bounds"))
            if b is not None:
                fallback_edits.append((b[1], text))

    # compressed=True can elide non-clickable stockprice/stockvolume wrappers while keeping inner EditText nodes.
    if (not price_text or not qty_text) and fallback_edits:
        sorted_edits = sorted(fallback_edits, key=lambda item: item[0])
        if code_top is not None:
            below = [t for top_y, t in sorted_edits if top_y > code_top]
            if len(below) >= 2:
                sorted_edits_texts = below
            else:
                sorted_edits_texts = [t for _, t in sorted_edits]
        else:
            sorted_edits_texts = [t for _, t in sorted_edits]
        if len(sorted_edits_texts) >= 2:
            if not price_text:
                price_text = sorted_edits_texts[0]
            if not qty_text:
                qty_text = sorted_edits_texts[1]

    return {
        "symbol": code,
        "name": stock_name,
        "price": price_text,
        "quantity": qty_text,
        "limit_down": limit_down,
        "limit_up": limit_up,
        "available_text": available_text,
        "keyboard_open": keyboard_open,
        "has_submit_button": has_submit_button,
    }


def _default_u2_factory(
    adb_server_socket: str | None,
    u2_endpoint: str | None = None,
) -> Callable[[str], Any]:
    endpoint_clean = (u2_endpoint or "").strip()

    def connect(serial: str) -> Any:
        import uiautomator2 as u2  # type: ignore[import-not-found]

        if endpoint_clean.startswith(("http://", "https://")):
            try:
                return u2.connect(endpoint_clean)
            except Exception:
                pass
        if adb_server_socket and adb_server_socket.startswith("tcp:"):
            endpoint = adb_server_socket.removeprefix("tcp:")
            host, separator, port_text = endpoint.rpartition(":")
            if separator and host and port_text.isdigit():
                import adbutils  # type: ignore[import-not-found]

                adbutils.adb = adbutils.AdbClient(host=host, port=int(port_text))
        return u2.connect(serial)

    return connect


_TAB_KEY_MAP: dict[str, str] = {
    "买入": "buy",
    "卖出": "sell",
    "撤单": "cancel",
    "持仓": "holdings",
    "查询": "query",
}


@runtime_checkable
class BrokerTradeAdapter(Protocol):
    """Broker-agnostic trade adapter contract (UI executor or direct socket/miniQMT)."""

    adb_serial: str

    def invalidate_snapshots(
        self,
        *,
        account: bool = True,
        orders: bool = True,
        trades: bool = True,
    ) -> None: ...

    def get_account_summary(self, *, max_age_seconds: float = 0.0) -> dict[str, Any]: ...
    def get_today_orders(self, *, max_age_seconds: float = 0.0) -> dict[str, Any]: ...
    def get_today_trades(self, *, max_age_seconds: float = 0.0) -> dict[str, Any]: ...
    def prestage_order(
        self,
        *,
        symbol: str,
        expected_name: str | None = None,
        side: Literal["buy", "sell"],
        price: str | float,
        quantity: int,
        precision: int = 2,
    ) -> dict[str, Any]: ...
    def keep_warm(
        self,
        *,
        side: Literal["buy", "sell"] = "buy",
        refresh_gateway: bool = False,
        gateway_max_idle_seconds: float = 480.0,
    ) -> dict[str, Any]: ...
    def probe_broker_session(self, *, preferred_side: Literal["buy", "sell"] = "buy") -> dict[str, Any]: ...
    def place_order(
        self,
        *,
        symbol: str,
        expected_name: str | None = None,
        side: Literal["buy", "sell"],
        price: str,
        quantity: int,
        precision: int = 2,
        dry_run: bool = False,
        auto_cancel: bool = False,
        reconcile_mode: Literal["fast", "full"] = "fast",
    ) -> dict[str, Any]: ...
    def cancel_order(
        self,
        *,
        entrust_no: str | None = None,
        symbol: str | None = None,
        expected_name: str | None = None,
        side: str | None = None,
    ) -> dict[str, Any]: ...


@dataclass
class TonghuashunTradeExecutor:
    """Thread-safe, device-isolated Tonghuashun UI trading executor."""

    adb_serial: str
    prohibited_serial: str | None = DEFAULT_PROTECTED_SERIAL
    runner_control: RunnerControl | None = None
    task_store: Any | None = None
    adb_server_socket: str | None = None
    u2_endpoint: str | None = None
    runner_yield_timeout_seconds: float = 0.0
    u2_factory: Callable[[str], Any] | None = None
    name_to_symbol: Callable[[str], str | None] | None = None
    _lock: RLock = field(default_factory=RLock, init=False, repr=False)
    _cache_lock: RLock = field(default_factory=RLock, init=False, repr=False)
    _device: Any | None = field(default=None, init=False, repr=False)
    _last_verified_at: float = field(default=0.0, init=False, repr=False)
    _prestaged_state: dict[str, Any] | None = field(default=None, init=False, repr=False)
    _active_tab: str | None = field(default=None, init=False, repr=False)
    _last_gateway_touch_at: float = field(default=0.0, init=False, repr=False)
    _last_account: dict[str, Any] | None = field(default=None, init=False, repr=False)
    _last_account_at: float = field(default=0.0, init=False, repr=False)
    _last_account_date: str = field(default="", init=False, repr=False)
    _last_orders: list[dict[str, Any]] = field(default_factory=list, init=False, repr=False)
    _last_orders_at: float = field(default=0.0, init=False, repr=False)
    _last_orders_date: str = field(default="", init=False, repr=False)
    _last_trades: list[dict[str, Any]] = field(default_factory=list, init=False, repr=False)
    _last_trades_at: float = field(default=0.0, init=False, repr=False)
    _last_trades_date: str = field(default="", init=False, repr=False)

    @property
    def active_tab(self) -> str | None:
        with self._cache_lock:
            return self._active_tab

    @staticmethod
    def _today_shanghai_compact() -> str:
        return datetime.now(timezone.utc).astimezone(SHANGHAI_TZ).strftime("%Y%m%d")

    def invalidate_snapshots(
        self,
        *,
        account: bool = True,
        orders: bool = True,
        trades: bool = True,
    ) -> None:
        with self._cache_lock:
            if account:
                self._last_account_at = 0.0
            if orders:
                self._last_orders_at = 0.0
            if trades:
                self._last_trades_at = 0.0

    def __post_init__(self) -> None:
        serial = (self.adb_serial or "").strip()
        if not serial:
            raise ValueError("trade executor requires adb_serial")
        forbidden = {DEFAULT_PROTECTED_SERIAL}
        if self.prohibited_serial:
            forbidden.add(self.prohibited_serial.strip())
        if serial in forbidden:
            raise ValueError(f"refusing to bind trade executor to protected device {serial}")
        self.adb_serial = serial
        if self.u2_factory is None:
            self.u2_factory = _default_u2_factory(self.adb_server_socket, self.u2_endpoint)

    @contextmanager
    def _guarded_device_session(self, *, blocking: bool) -> Iterator[Any | None]:
        if not self._lock.acquire(blocking=blocking):
            yield None
            return
        control = self.runner_control
        prev_paused: bool | None = None
        try:
            serial = self.adb_serial
            if serial == DEFAULT_PROTECTED_SERIAL or (
                self.prohibited_serial and serial == self.prohibited_serial
            ):
                raise TradeExecutionError(
                    "TRADE_UNCONFIGURED",
                    "受保护资金虚拟机禁止交易操作",
                    503,
                )
            yield_timeout = max(0.0, float(self.runner_yield_timeout_seconds or 0.0))
            if control is not None:
                with control._maintenance_gate:
                    if control._lock_owner is not None or control.has_active_operation:
                        if not blocking:
                            yield None
                            return
                        raise TradeExecutionError(
                            "TRADE_DEVICE_BUSY",
                            "采集设备当前被管理员接管或正在维护，请稍后再试",
                            409,
                        )
                    has_running = self.task_store is not None and self.task_store.has_running_task()
                    if has_running and (not blocking or yield_timeout <= 0.0):
                        if not blocking:
                            yield None
                            return
                        raise TradeExecutionError(
                            "TRADE_DEVICE_BUSY",
                            "采集设备正在执行队列任务，请稍后再试",
                            409,
                        )
                    prev_paused = control.queue_paused
                    control.queue_paused = True

                if blocking and self.task_store is not None and self.task_store.has_running_task():
                    deadline = time.monotonic() + yield_timeout
                    while self.task_store.has_running_task():
                        if time.monotonic() >= deadline:
                            raise TradeExecutionError(
                                "TRADE_DEVICE_BUSY",
                                "采集设备正在执行队列任务，请稍后再试",
                                409,
                            )
                        time.sleep(0.05)
                        with control._maintenance_gate:
                            if control._lock_owner is not None or control.has_active_operation:
                                prev_paused = None
                                raise TradeExecutionError(
                                    "TRADE_DEVICE_BUSY",
                                    "采集设备当前被管理员接管或正在维护，请稍后再试",
                                    409,
                                )
            elif self.task_store is not None and self.task_store.has_running_task():
                if not blocking:
                    yield None
                    return
                if yield_timeout > 0.0:
                    deadline = time.monotonic() + yield_timeout
                    while self.task_store.has_running_task():
                        if time.monotonic() >= deadline:
                            raise TradeExecutionError(
                                "TRADE_DEVICE_BUSY",
                                "采集设备正在执行队列任务，请稍后再试",
                                409,
                            )
                        time.sleep(0.05)
                else:
                    raise TradeExecutionError(
                        "TRADE_DEVICE_BUSY",
                        "采集设备正在执行队列任务，请稍后再试",
                        409,
                    )

            device = self._connect_device()
            yield device
        finally:
            try:
                if control is not None and prev_paused is not None:
                    with control._maintenance_gate:
                        if control._lock_owner is None and not control.has_active_operation:
                            control.queue_paused = prev_paused
            finally:
                self._lock.release()

    @contextmanager
    def _device_guard(self) -> Iterator[Any]:
        with self._guarded_device_session(blocking=True) as device:
            assert device is not None
            yield device

    @contextmanager
    def _try_device_guard(self) -> Iterator[Any | None]:
        with self._guarded_device_session(blocking=False) as device:
            yield device

    @staticmethod
    def _apply_low_latency_config(device: Any) -> None:
        jsonrpc = getattr(device, "jsonrpc", None)
        set_cfg = getattr(jsonrpc, "setConfigurator", None) if jsonrpc is not None else None
        if callable(set_cfg):
            try:
                set_cfg(
                    {
                        "waitForIdleTimeout": 0,
                        "waitForSelectorTimeout": 0,
                        "actionAcknowledgmentTimeout": 20,
                        "keyInjectionDelay": 0,
                    }
                )
            except Exception:
                pass

    def _connect_device(self) -> Any:
        now = time.monotonic()
        if self._device is not None and (now - self._last_verified_at) < 25.0:
            return self._device
        try:
            fresh_connect = self._device is None
            if fresh_connect:
                assert self.u2_factory is not None
                self._device = self.u2_factory(self.adb_serial)
                self._apply_low_latency_config(self._device)
            _ = self._device.info
            self._last_verified_at = time.monotonic()
            return self._device
        except Exception as error:
            self._device = None
            self._last_verified_at = 0.0
            raise TradeExecutionError(
                "TRADE_DEVICE_OFFLINE",
                f"交易设备 {self.adb_serial} 连接不可用",
                503,
            ) from error

    def _dump_root(self, device: Any, *, compressed: bool = True) -> ElementTree.Element:
        def _read_xml(target: Any) -> ElementTree.Element:
            try:
                raw = target.dump_hierarchy(compressed=compressed)
            except TypeError:
                raw = target.dump_hierarchy()
            self._last_verified_at = time.monotonic()
            return ElementTree.fromstring(raw)

        try:
            return _read_xml(device)
        except Exception:
            self._device = None
            self._last_verified_at = 0.0
            try:
                reconnected = self._connect_device()
                return _read_xml(reconnected)
            except Exception as retry_error:
                self._device = None
                self._last_verified_at = 0.0
                raise TradeExecutionError(
                    "TRADE_NAVIGATION_FAILED",
                    "无法读取同花顺交易界面节点树",
                    503,
                ) from retry_error

    def _dismiss_guide_overlays(self, device: Any, root: ElementTree.Element) -> ElementTree.Element:
        for _ in range(3):
            found = False
            for node in root.iter():
                rid = node.attrib.get("resource-id") or ""
                if rid == "com.hexin.plat.android:id/fenshi_mmdl_popupview":
                    bounds = _parse_bounds(node.attrib.get("bounds"))
                    if bounds:
                        device.click(int(bounds[0] + 20), int(bounds[1] + 20))
                        time.sleep(0.6)
                        found = True
                        break
                if rid == "com.hexin.plat.android:id/bubble_guide_content":
                    bounds = _parse_bounds(node.attrib.get("bounds"))
                    if bounds:
                        cx, cy = _bounds_center(bounds)
                        device.click(int(cx), int(cy))
                        time.sleep(0.5)
                        found = True
                        break
            if not found:
                break
            root = self._dump_root(device)
        return root

    def _check_auth_gate(self, root: ElementTree.Element) -> None:
        reason = detect_auth_blocking(root)
        if reason is not None:
            raise TradeExecutionError(
                "BROKER_LOGIN_REQUIRED",
                f"国泰海通证券需要人工验证（{reason}），已中止自动交易操作",
                409,
            )

    def _tap_bounds(self, device: Any, bounds: tuple[int, int, int, int]) -> None:
        cx, cy = _bounds_center(bounds)
        device.click(int(round(cx)), int(round(cy)))

    def _find_node(
        self,
        root: ElementTree.Element,
        *,
        resource_id: str | None = None,
        text: str | None = None,
    ) -> ElementTree.Element | None:
        for node in root.iter():
            if resource_id is not None and node.attrib.get("resource-id") != resource_id:
                continue
            if text is not None and (node.attrib.get("text") or "").strip() != text:
                continue
            return node
        return None

    def _click_node(
        self,
        device: Any,
        root: ElementTree.Element,
        *,
        resource_id: str | None = None,
        text: str | None = None,
    ) -> bool:
        node = self._find_node(root, resource_id=resource_id, text=text)
        if node is None:
            return False
        bounds = _parse_bounds(node.attrib.get("bounds"))
        if bounds is None:
            return False
        self._tap_bounds(device, bounds)
        return True

    def _click_landing_entry(
        self,
        device: Any,
        root: ElementTree.Element,
        *,
        prefer_tab: str = "持仓",
    ) -> bool:
        if prefer_tab == "买入":
            rid_order = (
                "com.hexin.plat.android:id/menu_buy_text",
                "com.hexin.plat.android:id/menu_holdings_text",
            )
            text_order = ("买入", "持仓")
        elif prefer_tab == "卖出":
            rid_order = (
                "com.hexin.plat.android:id/menu_sell_text",
                "com.hexin.plat.android:id/menu_holdings_text",
            )
            text_order = ("卖出", "持仓")
        else:
            rid_order = (
                "com.hexin.plat.android:id/menu_holdings_text",
                "com.hexin.plat.android:id/menu_buy_text",
            )
            text_order = ("持仓", "买入")

        for rid in rid_order:
            if self._click_node(device, root, resource_id=rid):
                return True

        for target_text in text_order:
            for node in root.iter():
                if (node.attrib.get("text") or "").strip() != target_text:
                    continue
                if node.attrib.get("resource-id") == "com.hexin.plat.android:id/title":
                    continue
                bounds = _parse_bounds(node.attrib.get("bounds"))
                if bounds is None:
                    continue
                _, cy = _bounds_center(bounds)
                if 216 <= cy <= 1750:
                    self._tap_bounds(device, bounds)
                    return True
        return False

    def _ensure_trade_workspace(self, device: Any, *, prefer_tab: str = "持仓") -> ElementTree.Element:
        """Ensure Tonghuashun is foregrounded and inside Guotai Haitong 5-tab workspace."""
        app_started = False
        for _ in range(5):
            root = self._dump_root(device, compressed=True)
            self._check_auth_gate(root)
            root = self._dismiss_guide_overlays(device, root)

            has_app_node = any((node.attrib.get("package") or "") == APP_PACKAGE for node in root.iter())
            if not has_app_node and not app_started:
                app_started = True
                try:
                    current = device.app_current()
                    if not isinstance(current, dict) or current.get("package") != APP_PACKAGE:
                        device.app_start(APP_PACKAGE, APP_ACTIVITY)
                        time.sleep(2.0)
                        continue
                except Exception:
                    pass

            # Check if search popup or modal dialog was left open from an earlier prompt
            if self._click_node(device, root, resource_id="com.hexin.plat.android:id/close_click"):
                time.sleep(0.35)
                continue
            modal = detect_modal_dialog(root)
            if modal is not None:
                if modal.cancel_button:
                    self._tap_bounds(device, modal.cancel_button[1])
                    time.sleep(0.25)
                    continue
                if modal.ack_button:
                    self._tap_bounds(device, modal.ack_button[1])
                    time.sleep(0.25)
                    continue

            # If in 5-tab workspace (买入/卖出/撤单/持仓/查询 + page_title_view)
            has_buy_tab = self._find_node(root, resource_id="com.hexin.plat.android:id/btn", text="买入") is not None
            has_hold_tab = self._find_node(root, resource_id="com.hexin.plat.android:id/btn", text="持仓") is not None
            if has_buy_tab and has_hold_tab:
                title_node = self._find_node(root, resource_id="com.hexin.plat.android:id/page_title_view")
                title_text = (title_node.attrib.get("text") or "").strip() if title_node is not None else ""
                if "模拟" in title_text:
                    raise TradeExecutionError(
                        "BROKER_LOGIN_REQUIRED",
                        "当前处于模拟炒股页面，请确认国泰海通实盘账户登录状态",
                        409,
                    )
                return root

            # If inside 查询子页面 (当日委托/当日成交/历史委托/历史成交), press back once
            if self._find_node(root, resource_id="com.hexin.plat.android:id/btn", text="当日委托") is not None:
                device.press("back")
                time.sleep(0.45)
                continue

            # If on 交易 landing page (tab_a / weituo_first_page): scroll back to top if menu scrolled off-screen
            tab_a = self._find_node(root, resource_id="com.hexin.plat.android:id/tab_a")
            weituo_landing = self._find_node(root, resource_id="com.hexin.plat.android:id/weituo_first_page")
            if tab_a is not None or weituo_landing is not None:
                other_tab_selected = any(
                    (n := self._find_node(root, resource_id=rid)) is not None and n.attrib.get("selected") == "true"
                    for rid in (
                        "com.hexin.plat.android:id/tab_fund",
                        "com.hexin.plat.android:id/tab_qh",
                        "com.hexin.plat.android:id/tab_jicunjin",
                        "com.hexin.plat.android:id/tab_mn",
                    )
                )
                if tab_a is not None and (
                    other_tab_selected
                    or (weituo_landing is None and tab_a.attrib.get("selected") != "true")
                ):
                    self._click_node(device, root, resource_id="com.hexin.plat.android:id/tab_a")
                    time.sleep(0.6)
                    root = self._dump_root(device, compressed=True)
                if not self._click_landing_entry(device, root, prefer_tab=prefer_tab):
                    root = self._scroll_to_top(device, swipes=2)
                    self._check_auth_gate(root)
                    root = self._dismiss_guide_overlays(device, root)
                    if not self._click_landing_entry(device, root, prefer_tab=prefer_tab):
                        continue
                time.sleep(0.9)
                continue

            # If bottom tab bar visible with 交易 (unselected), tap it
            trade_nav = self._find_node(root, resource_id="com.hexin.plat.android:id/title", text="交易")
            if trade_nav is not None and trade_nav.attrib.get("selected") != "true":
                bounds = _parse_bounds(trade_nav.attrib.get("bounds"))
                if bounds is not None:
                    self._tap_bounds(device, bounds)
                    time.sleep(1.2)
                    continue

            device.press("back")
            time.sleep(0.8)

        raise TradeExecutionError(
            "TRADE_NAVIGATION_FAILED",
            "无法进入同花顺国泰海通证券交易工作区",
            503,
        )

    def _switch_main_tab(self, device: Any, label: str) -> ElementTree.Element:
        root = self._ensure_trade_workspace(device, prefer_tab=label)
        btn = self._find_node(root, resource_id="com.hexin.plat.android:id/btn", text=label)
        if btn is None:
            raise TradeExecutionError("TRADE_NAVIGATION_FAILED", f"未找到交易页签「{label}」", 503)
        if btn.attrib.get("selected") != "true":
            bounds = _parse_bounds(btn.attrib.get("bounds"))
            if bounds is None:
                raise TradeExecutionError("TRADE_NAVIGATION_FAILED", f"交易页签「{label}」不可点击", 503)
            self._tap_bounds(device, bounds)
            deadline = time.monotonic() + 0.65
            for _ in range(8):
                time.sleep(0.07)
                root = self._dump_root(device, compressed=True)
                switched_btn = self._find_node(root, resource_id="com.hexin.plat.android:id/btn", text=label)
                if switched_btn is not None and switched_btn.attrib.get("selected") == "true":
                    break
                if time.monotonic() >= deadline:
                    break
            self._check_auth_gate(root)
            root = self._dismiss_guide_overlays(device, root)
        if label in _TAB_KEY_MAP:
            with self._cache_lock:
                self._active_tab = _TAB_KEY_MAP[label]
        return root

    def _tap_refresh(self, device: Any, root: ElementTree.Element) -> ElementTree.Element:
        for rid in (
            "com.hexin.plat.android:id/refresh_img",
            "com.hexin.plat.android:id/refresh_image",
        ):
            if self._click_node(device, root, resource_id=rid):
                time.sleep(0.5)
                self._last_gateway_touch_at = time.monotonic()
                return self._dump_root(device)
        return root

    def _open_query_subpage(self, device: Any, sub_label: Literal["当日委托", "当日成交"]) -> ElementTree.Element:
        # Check if already on query subpage
        root = self._dump_root(device)
        self._check_auth_gate(root)
        tab_node = self._find_node(root, resource_id="com.hexin.plat.android:id/btn", text=sub_label)
        if tab_node is None:
            root = self._switch_main_tab(device, "查询")
            entry_rid = (
                "com.hexin.plat.android:id/drwt"
                if sub_label == "当日委托"
                else "com.hexin.plat.android:id/drcj"
            )
            if not self._click_node(device, root, resource_id=entry_rid) and not self._click_node(
                device, root, text=sub_label
            ):
                raise TradeExecutionError("TRADE_NAVIGATION_FAILED", f"无法打开「{sub_label}」页面", 503)
            time.sleep(0.75)
            root = self._dump_root(device)
            return self._tap_refresh(device, root)
        if tab_node.attrib.get("selected") != "true":
            self._click_node(device, root, resource_id="com.hexin.plat.android:id/btn", text=sub_label)
            time.sleep(0.65)
            root = self._dump_root(device)
        root = self._scroll_to_top(device, swipes=1)
        root = self._tap_refresh(device, root)
        return root

    def _scroll_to_top(self, device: Any, *, swipes: int = 1) -> ElementTree.Element:
        for _ in range(max(1, swipes)):
            device.swipe(0.5, 0.35, 0.5, 0.82, duration=0.22)
            time.sleep(0.45)
        return self._dump_root(device)

    def _collect_scrolled_rows(
        self,
        device: Any,
        initial_root: ElementTree.Element,
        parser: Callable[[ElementTree.Element], list[dict[str, Any]]],
        key_fn: Callable[[dict[str, Any]], tuple[Any, ...]],
        *,
        max_swipes: int = 3,
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        seen: set[tuple[Any, ...]] = set()
        root = initial_root
        for step in range(max_swipes + 1):
            batch = parser(root)
            added = 0
            for item in batch:
                key = key_fn(item)
                if key in seen:
                    continue
                seen.add(key)
                items.append(item)
                added += 1
            if step == max_swipes or len(batch) < 4 or (step > 0 and added == 0):
                break
            device.swipe(0.5, 0.78, 0.5, 0.42, duration=0.25)
            time.sleep(0.4)
            root = self._dump_root(device)
        return items

    def _cached_account_snapshot(self, max_age_seconds: float, *, allow_stale: bool = False) -> dict[str, Any] | None:
        today_key = self._today_shanghai_compact()
        with self._cache_lock:
            if self._last_account is None or (self._last_account_date and self._last_account_date != today_key):
                return None
            if self._last_account_at <= 0.0:
                if not allow_stale:
                    return None
                age = max(1.0, float(max_age_seconds) + 1.0)
            else:
                age = max(0.0, time.monotonic() - self._last_account_at)
                if not allow_stale and age > max_age_seconds:
                    return None
            payload = dict(self._last_account)
            payload["cached"] = True
            payload["stale_while_busy"] = allow_stale and (self._last_account_at <= 0.0 or age > max_age_seconds)
            payload["cache_age_seconds"] = round(age, 1)
            return payload

    def _cached_orders_snapshot(self, max_age_seconds: float, *, allow_stale: bool = False) -> dict[str, Any] | None:
        today_key = self._today_shanghai_compact()
        with self._cache_lock:
            if self._last_orders_at <= 0.0 or (self._last_orders_date and self._last_orders_date != today_key):
                return None
            age = max(0.0, time.monotonic() - self._last_orders_at)
            if not allow_stale and age > max_age_seconds:
                return None
            return {
                "device_serial": self.adb_serial,
                "orders": [dict(item) for item in self._last_orders],
                "captured_at": _now_iso(),
                "cached": True,
                "stale_while_busy": allow_stale and age > max_age_seconds,
                "cache_age_seconds": round(age, 1),
            }

    def _cached_trades_snapshot(self, max_age_seconds: float, *, allow_stale: bool = False) -> dict[str, Any] | None:
        today_key = self._today_shanghai_compact()
        with self._cache_lock:
            if self._last_trades_at <= 0.0 or (self._last_trades_date and self._last_trades_date != today_key):
                return None
            age = max(0.0, time.monotonic() - self._last_trades_at)
            if not allow_stale and age > max_age_seconds:
                return None
            return {
                "device_serial": self.adb_serial,
                "trades": [dict(item) for item in self._last_trades],
                "captured_at": _now_iso(),
                "cached": True,
                "stale_while_busy": allow_stale and age > max_age_seconds,
                "cache_age_seconds": round(age, 1),
            }

    def _read_account_summary_unlocked(self, device: Any) -> dict[str, Any]:
        root = self._switch_main_tab(device, "持仓")
        for _ in range(2):
            if self._find_node(root, resource_id="com.hexin.plat.android:id/capital_cell_title") is not None:
                break
            root = self._scroll_to_top(device, swipes=1)
        root = self._tap_refresh(device, root)
        if self._find_node(root, resource_id="com.hexin.plat.android:id/capital_cell_title") is None:
            root = self._scroll_to_top(device, swipes=1)
        summary = parse_account_metrics_xml(root)
        positions = self._collect_scrolled_rows(
            device,
            root,
            lambda r: parse_holdings_xml(r, name_to_symbol=self.name_to_symbol),
            lambda row: (row["name"], row["market_value"], row["shares"]),
            max_swipes=3,
        )
        payload = {
            "device_serial": self.adb_serial,
            "broker_name": summary["broker_name"],
            "account_masked": summary["account_masked"],
            "logged_in": True,
            "position_ratio": summary["position_ratio"],
            "metrics": summary["metrics"],
            "positions": positions,
            "captured_at": _now_iso(),
            "cached": False,
        }
        today_key = self._today_shanghai_compact()
        self._last_gateway_touch_at = time.monotonic()
        with self._cache_lock:
            self._last_account = dict(payload)
            self._last_account_at = time.monotonic()
            self._last_account_date = today_key
        return payload

    def get_account_summary(self, *, max_age_seconds: float = 0.0) -> dict[str, Any]:
        if max_age_seconds > 0.0:
            hit = self._cached_account_snapshot(max_age_seconds)
            if hit is not None:
                return hit
            with self._try_device_guard() as device:
                if device is None:
                    stale = self._cached_account_snapshot(max_age_seconds, allow_stale=True)
                    if stale is not None:
                        return stale
                else:
                    return self._read_account_summary_unlocked(device)
        with self._device_guard() as device:
            return self._read_account_summary_unlocked(device)

    def _read_cancellable_orders_on_tab(self, device: Any) -> list[dict[str, Any]]:
        root = self._switch_main_tab(device, "撤单")
        root = self._tap_refresh(device, root)
        if self._find_node(root, resource_id="com.hexin.plat.android:id/empty_tips") is not None:
            return []
        divider = self._find_node(root, resource_id="com.hexin.plat.android:id/cannot_chedan_title_text")
        max_y = None
        if divider is not None:
            b = _parse_bounds(divider.attrib.get("bounds"))
            if b is not None:
                max_y = b[1]
        return parse_orders_xml(
            root,
            name_to_symbol=self.name_to_symbol,
            max_y=max_y,
            force_cancellable=True,
        )

    def _read_today_orders_unlocked(self, device: Any) -> list[dict[str, Any]]:
        root = self._open_query_subpage(device, "当日委托")
        orders = self._collect_scrolled_rows(
            device,
            root,
            lambda r: parse_orders_xml(r, name_to_symbol=self.name_to_symbol),
            lambda row: (row["name"], row["time"], row["side"], row["price"], row["quantity"]),
            max_swipes=3,
        )
        # Strip screen bounds before external serialization
        for item in orders:
            item.pop("bounds", None)
        today_key = self._today_shanghai_compact()
        self._last_gateway_touch_at = time.monotonic()
        with self._cache_lock:
            self._last_orders = [dict(item) for item in orders]
            self._last_orders_at = time.monotonic()
            self._last_orders_date = today_key
        return orders

    def _read_today_trades_unlocked(self, device: Any) -> list[dict[str, Any]]:
        root = self._open_query_subpage(device, "当日成交")
        trades = self._collect_scrolled_rows(
            device,
            root,
            lambda r: parse_trades_xml(r, name_to_symbol=self.name_to_symbol),
            lambda row: (
                row["name"],
                row["time"],
                row["side"],
                row["filled_price"],
                row["filled_quantity"],
                row["filled_amount"],
            ),
            max_swipes=3,
        )
        today_key = self._today_shanghai_compact()
        self._last_gateway_touch_at = time.monotonic()
        with self._cache_lock:
            self._last_trades = [dict(item) for item in trades]
            self._last_trades_at = time.monotonic()
            self._last_trades_date = today_key
        return trades

    def get_today_orders(self, *, max_age_seconds: float = 0.0) -> dict[str, Any]:
        if max_age_seconds > 0.0:
            hit = self._cached_orders_snapshot(max_age_seconds)
            if hit is not None:
                return hit
            with self._try_device_guard() as device:
                if device is None:
                    stale = self._cached_orders_snapshot(max_age_seconds, allow_stale=True)
                    if stale is not None:
                        return stale
                else:
                    orders = self._read_today_orders_unlocked(device)
                    return {
                        "device_serial": self.adb_serial,
                        "orders": orders,
                        "captured_at": _now_iso(),
                        "cached": False,
                    }
        with self._device_guard() as device:
            orders = self._read_today_orders_unlocked(device)
            return {
                "device_serial": self.adb_serial,
                "orders": orders,
                "captured_at": _now_iso(),
                "cached": False,
            }

    def get_today_trades(self, *, max_age_seconds: float = 0.0) -> dict[str, Any]:
        if max_age_seconds > 0.0:
            hit = self._cached_trades_snapshot(max_age_seconds)
            if hit is not None:
                return hit
            with self._try_device_guard() as device:
                if device is None:
                    stale = self._cached_trades_snapshot(max_age_seconds, allow_stale=True)
                    if stale is not None:
                        return stale
                else:
                    trades = self._read_today_trades_unlocked(device)
                    return {
                        "device_serial": self.adb_serial,
                        "trades": trades,
                        "captured_at": _now_iso(),
                        "cached": False,
                    }
        with self._device_guard() as device:
            trades = self._read_today_trades_unlocked(device)
            return {
                "device_serial": self.adb_serial,
                "trades": trades,
                "captured_at": _now_iso(),
                "cached": False,
            }

    def _fill_order_form(
        self,
        device: Any,
        *,
        symbol: str,
        side: Literal["buy", "sell"],
        price_str: str,
        quantity: int,
    ) -> tuple[dict[str, Any], ElementTree.Element, dict[str, int]]:
        tab_label = "买入" if side == "buy" else "卖出"
        t_nav0 = time.monotonic()
        root = self._switch_main_tab(device, tab_label)
        nav_ms = round((time.monotonic() - t_nav0) * 1000)

        t_fill0 = time.monotonic()
        initial_rb = parse_form_readback(root)
        target_price = float(price_str)
        same_symbol_ready = (
            initial_rb.get("symbol") == symbol
            and bool(initial_rb.get("name"))
            and not initial_rb.get("keyboard_open")
            and bool(initial_rb.get("has_submit_button"))
        )

        if same_symbol_ready:
            readback = initial_rb
        else:
            code_box = device(
                resourceId="com.hexin.plat.android:id/auto_stockcode",
                className="android.widget.EditText",
            )
            if (
                self._find_node(root, resource_id="com.hexin.plat.android:id/auto_stockcode") is None
                and not code_box.exists
            ):
                raise TradeExecutionError("TRADE_NAVIGATION_FAILED", "未找到证券代码输入框", 503)
            code_box.click()
            time.sleep(0.05)
            device(
                resourceId="com.hexin.plat.android:id/auto_stockcode",
                className="android.widget.EditText",
            ).set_text(symbol)

            # Wait for Tonghuashun to resolve symbol name and populate auto-price
            resolved = False
            readback = {}
            deadline = time.monotonic() + 5.0
            for _ in range(45):
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.08)
                root = self._dump_root(device, compressed=True)
                self._check_auth_gate(root)
                readback = parse_form_readback(root)
                if (
                    readback["symbol"] == symbol
                    and readback["name"]
                    and not readback["keyboard_open"]
                    and readback["has_submit_button"]
                ):
                    resolved = True
                    break
                # If autocomplete suggestion row appeared with exact code, tap it
                if readback["keyboard_open"]:
                    exact = self._find_node(root, text=symbol)
                    if exact is not None and exact.attrib.get("class") != "android.widget.EditText":
                        b = _parse_bounds(exact.attrib.get("bounds"))
                        if b:
                            self._tap_bounds(device, b)
                            time.sleep(0.10)

            if not resolved:
                raise TradeExecutionError(
                    "TRADE_NAVIGATION_FAILED",
                    f"输入代码 {symbol} 后证券名称或表单未就绪",
                    503,
                )

        # Verify price within UI-displayed limits if limits are numeric
        limit_down = _parse_float(readback.get("limit_down"))
        limit_up = _parse_float(readback.get("limit_up"))
        if limit_down is not None and target_price < limit_down - 1e-6:
            raise TradeExecutionError(
                "TRADE_PRICE_OUT_OF_RANGE",
                f"委托价格 {price_str} 低于当前跌停价 {readback.get('limit_down')}",
                422,
            )
        if limit_up is not None and target_price > limit_up + 1e-6:
            raise TradeExecutionError(
                "TRADE_PRICE_OUT_OF_RANGE",
                f"委托价格 {price_str} 高于当前涨停价 {readback.get('limit_up')}",
                422,
            )

        cur_price = _parse_float(readback.get("price"))
        cur_qty = _parse_int(readback.get("quantity"))
        need_price = (not same_symbol_ready) or cur_price is None or abs(cur_price - target_price) > 1e-4
        need_qty = (not same_symbol_ready) or cur_qty != quantity
        prestaged_hit = 1 if (same_symbol_ready and not need_price and not need_qty) else 0

        if need_price or need_qty:
            if need_price:
                has_price_node = self._find_node(root, resource_id="com.hexin.plat.android:id/stockprice") is not None
                price_input = device(resourceId="com.hexin.plat.android:id/stockprice").child(
                    className="android.widget.EditText"
                )
                if not has_price_node and not price_input.exists:
                    raise TradeExecutionError("TRADE_NAVIGATION_FAILED", "未找到价格输入框", 503)
                price_input.set_text(price_str)
            if need_qty:
                has_qty_node = self._find_node(root, resource_id="com.hexin.plat.android:id/stockvolume") is not None
                qty_input = device(resourceId="com.hexin.plat.android:id/stockvolume").child(
                    className="android.widget.EditText"
                )
                if not has_qty_node and not qty_input.exists:
                    raise TradeExecutionError("TRADE_NAVIGATION_FAILED", "未找到数量输入框", 503)
                qty_input.set_text(str(quantity))
            fill_ms = round((time.monotonic() - t_fill0) * 1000)

            t_rb0 = time.monotonic()
            time.sleep(0.04)
            final_root = self._dump_root(device, compressed=True)
            final_root = self._dismiss_guide_overlays(device, final_root)
            final_readback = parse_form_readback(final_root)
            tmp_price = _parse_float(final_readback.get("price"))
            tmp_qty = _parse_int(final_readback.get("quantity"))
            if (
                final_readback.get("symbol") != symbol
                or tmp_price is None
                or abs(tmp_price - target_price) > 1e-4
                or tmp_qty != quantity
            ):
                # Retry once with uncompressed dump to rule out UI flush lag or wrapper elision
                time.sleep(0.06)
                final_root = self._dump_root(device, compressed=False)
                final_root = self._dismiss_guide_overlays(device, final_root)
                final_readback = parse_form_readback(final_root)
            readback_ms = round((time.monotonic() - t_rb0) * 1000)
        else:
            fill_ms = round((time.monotonic() - t_fill0) * 1000)
            t_rb0 = time.monotonic()
            final_root = self._dismiss_guide_overlays(device, root)
            final_readback = parse_form_readback(final_root)
            readback_ms = round((time.monotonic() - t_rb0) * 1000)

        read_price = _parse_float(final_readback.get("price"))
        read_qty = _parse_int(final_readback.get("quantity"))

        if (
            final_readback.get("symbol") != symbol
            or read_price is None
            or abs(read_price - target_price) > 1e-4
            or read_qty != quantity
        ):
            raise TradeExecutionError(
                "TRADE_INPUT_READBACK_MISMATCH",
                (
                    f"下单表单回读校验失败，已安全阻断提交："
                    f"代码={final_readback.get('symbol')!r}, "
                    f"价格={final_readback.get('price')!r}, "
                    f"数量={final_readback.get('quantity')!r}"
                ),
                409,
            )

        return final_readback, final_root, {
            "nav_ms": nav_ms,
            "fill_ms": fill_ms,
            "readback_ms": readback_ms,
            "prestaged_hit": prestaged_hit,
        }

    def _cancel_order_unlocked(
        self,
        device: Any,
        *,
        entrust_no: str | None = None,
        symbol: str | None = None,
        expected_name: str | None = None,
        side: str | None = None,
    ) -> dict[str, Any]:
        cancellable = self._read_cancellable_orders_on_tab(device)
        target = None
        for row in cancellable:
            if entrust_no and row.get("entrust_no") == entrust_no:
                target = row
                break
            name = row.get("name") or ""
            row_sym = row.get("symbol")
            sym_match = (
                (symbol and row_sym == symbol)
                or (expected_name and (expected_name in name or name in expected_name))
            )
            side_match = side is None or row.get("side") == side
            if sym_match and side_match:
                target = row
                break
        if target is None and len(cancellable) == 1 and symbol is None and entrust_no is None:
            target = cancellable[0]

        if target is None or not target.get("bounds"):
            raise TradeExecutionError(
                "TRADE_ORDER_NOT_FOUND",
                "当前撤单列表中未找到匹配的可撤委托",
                404,
            )

        b = target["bounds"]
        self._tap_bounds(device, (int(b[0]), int(b[1]), int(b[2]), int(b[3])))
        self._last_gateway_touch_at = time.monotonic()
        time.sleep(1.0)
        dialog_msg_parts: list[str] = []

        for _ in range(4):
            root = self._dump_root(device)
            modal = detect_modal_dialog(root)
            if modal is None:
                break
            dialog_msg_parts.append(modal.message)
            if modal.kind == "action_sheet" and modal.action_button:
                self._tap_bounds(device, modal.action_button[1])
                time.sleep(0.9)
                continue
            if modal.kind == "confirm" and modal.confirm_button:
                self._tap_bounds(device, modal.confirm_button[1])
                time.sleep(1.2)
                continue
            if modal.kind == "alert" and modal.ack_button:
                self._tap_bounds(device, modal.ack_button[1])
                time.sleep(0.8)
                continue
            break

        remaining = self._read_cancellable_orders_on_tab(device)
        orders = self._read_today_orders_unlocked(device)
        still_open = any(
            (
                (entrust_no and row.get("entrust_no") == entrust_no)
                or (
                    symbol
                    and (row.get("symbol") == symbol or (expected_name and expected_name in (row.get("name") or "")))
                    and (side is None or row.get("side") == side)
                )
            )
            for row in remaining
        )
        return {
            "ok": not still_open,
            "cancelled_order": {k: v for k, v in target.items() if k != "bounds"},
            "dialog_message": mask_sensitive_numbers(" ; ".join(dialog_msg_parts)) or (
                "撤单请求已提交" if not still_open else "撤单后仍存在待撤挂单，请复核"
            ),
            "remaining_cancellable_count": len(remaining),
            "orders": orders,
            "captured_at": _now_iso(),
        }

    def _validate_order_params(
        self,
        *,
        symbol: str,
        side: Literal["buy", "sell"],
        price: str | float,
        quantity: int,
        precision: int,
    ) -> str:
        if not _SYMBOL_RE.fullmatch(symbol):
            raise TradeExecutionError("TRADE_INVALID_SYMBOL", "证券代码必须为 6 位数字", 422)
        if side not in {"buy", "sell"}:
            raise TradeExecutionError("TRADE_INVALID_SIDE", "交易方向必须为 buy 或 sell", 422)
        if quantity < 100 or quantity % 100 != 0:
            raise TradeExecutionError("TRADE_INVALID_QUANTITY", "委托股数必须为 100 的正整数倍", 422)
        try:
            dec_price = Decimal(str(price))
        except InvalidOperation as error:
            raise TradeExecutionError("TRADE_INVALID_PRICE", "委托价格格式无效", 422) from error
        if not dec_price.is_finite() or dec_price <= 0:
            raise TradeExecutionError("TRADE_INVALID_PRICE", "委托价格必须大于 0", 422)
        return f"{dec_price:.{precision}f}"

    def prestage_order(
        self,
        *,
        symbol: str,
        expected_name: str | None = None,
        side: Literal["buy", "sell"],
        price: str | float,
        quantity: int,
        precision: int = 2,
    ) -> dict[str, Any]:
        """Pre-navigate and pre-fill order inputs without clicking submit (best-effort non-blocking)."""
        price_str = self._validate_order_params(
            symbol=symbol,
            side=side,
            price=price,
            quantity=quantity,
            precision=precision,
        )
        with self._try_device_guard() as device:
            if device is None:
                return {"ok": False, "skipped": "device_busy", "device_serial": self.adb_serial}
            t_total0 = time.monotonic()
            readback, _, form_timings = self._fill_order_form(
                device,
                symbol=symbol,
                side=side,
                price_str=price_str,
                quantity=quantity,
            )
            self._prestaged_state = {
                "symbol": symbol,
                "side": side,
                "price": price_str,
                "quantity": quantity,
                "name": readback.get("name") or expected_name or symbol,
                "at": time.monotonic(),
            }
            return {
                "ok": True,
                "device_serial": self.adb_serial,
                "symbol": symbol,
                "name": readback.get("name") or expected_name or symbol,
                "side": side,
                "price": price_str,
                "quantity": quantity,
                "timings_ms": {
                    **form_timings,
                    "total": round((time.monotonic() - t_total0) * 1000),
                },
            }

    def keep_warm(
        self,
        *,
        side: Literal["buy", "sell"] = "buy",
        refresh_gateway: bool = False,
        gateway_max_idle_seconds: float = 480.0,
    ) -> dict[str, Any]:
        """Keep emulator-5556 Tonghuashun session and trade tab warm when idle."""
        tab_label = "卖出" if side == "sell" else "买入"
        with self._try_device_guard() as device:
            if device is None:
                return {"ok": False, "skipped": "device_busy", "device_serial": self.adb_serial}
            now_m = time.monotonic()
            gateway_idle = (
                self._last_gateway_touch_at > 0.0
                and (now_m - self._last_gateway_touch_at) >= max(30.0, float(gateway_max_idle_seconds))
            )
            gateway_refreshed = False
            root = self._switch_main_tab(device, tab_label)
            if refresh_gateway or gateway_idle:
                has_refresh = (
                    self._find_node(root, resource_id="com.hexin.plat.android:id/refresh_img") is not None
                    or self._find_node(root, resource_id="com.hexin.plat.android:id/refresh_image") is not None
                )
                if has_refresh:
                    self._tap_refresh(device, root)
                    gateway_refreshed = True
                else:
                    hold_root = self._switch_main_tab(device, "持仓")
                    self._tap_refresh(device, hold_root)
                    gateway_refreshed = True
                    self._switch_main_tab(device, tab_label)
            return {
                "ok": True,
                "device_serial": self.adb_serial,
                "tab": tab_label,
                "gateway_refreshed": gateway_refreshed,
            }

    def probe_broker_session(self, *, preferred_side: Literal["buy", "sell"] = "buy") -> dict[str, Any]:
        """Verify emulator-5556 broker login state, refresh initial account ledger, and park on trade tab."""
        tab_label = "卖出" if preferred_side == "sell" else "买入"
        with self._device_guard() as device:
            summary = self._read_account_summary_unlocked(device)
            self._switch_main_tab(device, tab_label)
            self._last_gateway_touch_at = time.monotonic()
            metrics = summary.get("metrics") if isinstance(summary.get("metrics"), dict) else {}
            return {
                "ok": True,
                "status": "READY",
                "device_serial": self.adb_serial,
                "broker_name": summary.get("broker_name"),
                "account_masked": summary.get("account_masked"),
                "available_cash": metrics.get("available_cash"),
                "positions_count": len(summary.get("positions") or []),
                "tab": tab_label,
            }

    def _resolve_known_holding_shares(
        self,
        symbol: str,
        expected_name: str | None,
        available_text: str | None,
    ) -> int | None:
        today_key = self._today_shanghai_compact()
        acct_shares: int | None = None
        acct_fresh = False
        with self._cache_lock:
            acct = self._last_account
            acct_fresh = self._last_account_at > 0.0
            if isinstance(acct, dict) and self._last_account_date == today_key:
                positions = acct.get("positions")
                if isinstance(positions, list):
                    for pos in positions:
                        if not isinstance(pos, dict):
                            continue
                        p_sym = str(pos.get("symbol") or "").strip()
                        p_name = str(pos.get("name") or "").strip()
                        name_hit = bool(expected_name and p_name and (expected_name in p_name or p_name in expected_name))
                        if (p_sym and p_sym == symbol) or name_hit:
                            acct_shares = max(0, int(pos.get("shares") or 0))
                            break
                    if acct_shares is None:
                        metrics = acct.get("metrics")
                        if isinstance(metrics, dict) and (
                            metrics.get("available_cash") is not None or metrics.get("total_assets") is not None
                        ):
                            acct_shares = 0

        ui_sellable: int | None = None
        raw_avail = str(available_text or "")
        if "可卖" in raw_avail:
            m = re.search(r"(\d+)", raw_avail.replace(",", ""))
            if m:
                ui_sellable = max(0, int(m.group(1)))

        if acct_fresh and acct_shares is not None:
            return max(acct_shares, ui_sellable) if ui_sellable is not None else acct_shares
        if ui_sellable is not None:
            return ui_sellable
        return acct_shares

    def place_order(
        self,
        *,
        symbol: str,
        expected_name: str | None = None,
        side: Literal["buy", "sell"],
        price: str,
        quantity: int,
        precision: int = 2,
        dry_run: bool = False,
        auto_cancel: bool = False,
        reconcile_mode: Literal["fast", "full"] = "fast",
    ) -> dict[str, Any]:
        price_str = self._validate_order_params(
            symbol=symbol,
            side=side,
            price=price,
            quantity=quantity,
            precision=precision,
        )

        with self._device_guard() as device:
            t_total0 = time.monotonic()
            readback, final_root, form_timings = self._fill_order_form(
                device,
                symbol=symbol,
                side=side,
                price_str=price_str,
                quantity=quantity,
            )

            if side == "sell" and not dry_run:
                known_shares = self._resolve_known_holding_shares(
                    symbol,
                    expected_name or readback.get("name"),
                    readback.get("available_text"),
                )
                if known_shares is not None and (known_shares - quantity) < MIN_RETAINED_SHARES:
                    raise TradeExecutionError(
                        "TRADE_RETAIN_MIN_SHARES",
                        (
                            f"触发强制留底 {MIN_RETAINED_SHARES} 股规则：当前持仓 {known_shares} 股，"
                            f"卖出 {quantity} 股后剩余不足 {MIN_RETAINED_SHARES} 股"
                            f"（保留 {MIN_RETAINED_SHARES} 股延续同花顺累计收益统计）"
                        ),
                        409,
                    )

            # Click submit button and poll for confirm / immediate alert dialog
            t_sub0 = time.monotonic()
            if not self._click_node(device, final_root, resource_id="com.hexin.plat.android:id/btn_transaction"):
                fallback_root = self._dump_root(device, compressed=True)
                if not self._click_node(device, fallback_root, resource_id="com.hexin.plat.android:id/btn_transaction"):
                    raise TradeExecutionError("TRADE_NAVIGATION_FAILED", "未找到下单提交按钮", 503)

            confirm_preview: list[str] = []
            dialog_message = ""
            entrust_no: str | None = None
            submitted = False
            accepted = False
            status = "UNKNOWN"
            gateway_reply_ms = 0

            modal_root = final_root
            modal: DialogInfo | None = None
            sub_deadline = time.monotonic() + 2.5
            for _ in range(32):
                if time.monotonic() >= sub_deadline:
                    break
                time.sleep(0.06)
                modal_root = self._dump_root(device, compressed=True)
                modal = detect_modal_dialog(modal_root)
                if modal is not None:
                    break
            submit_confirm_ms = round((time.monotonic() - t_sub0) * 1000)

            if modal is not None and modal.kind == "confirm":
                confirm_preview = modal.texts
                # Guard: confirm dialog must reference the intended symbol/name or be a generic risk/premium warning
                joined = " ".join(confirm_preview)
                name_prefix = (expected_name or readback.get("name") or "")[:3]
                if not _is_confirm_dialog_symbol_safe(joined, symbol, name_prefix):
                    if modal.cancel_button:
                        self._tap_bounds(device, modal.cancel_button[1])
                    raise TradeExecutionError(
                        "TRADE_INPUT_READBACK_MISMATCH",
                        "委托确认弹窗标的与目标证券不一致，已自动取消",
                        409,
                    )
                if dry_run:
                    if modal.cancel_button:
                        self._tap_bounds(device, modal.cancel_button[1])
                        time.sleep(0.04)
                    submitted = False
                    accepted = False
                    status = "DRY_RUN_CONFIRMED"
                    dialog_message = "演练模式：已校验下单确认弹窗并取消，未向柜台发送报单"
                else:
                    assert modal.confirm_button is not None
                    t_gw0 = time.monotonic()
                    last_confirm_msg = modal.message
                    last_confirm_tap_at = t_gw0
                    self._tap_bounds(device, modal.confirm_button[1])
                    self._last_gateway_touch_at = t_gw0
                    submitted = True
                    self._prestaged_state = None
                    post_messages: list[str] = []
                    gw_deadline = t_gw0 + 3.5
                    for poll_idx in range(36):
                        if time.monotonic() >= gw_deadline:
                            break
                        time.sleep(0.06)
                        post_root = self._dump_root(device, compressed=True)
                        self._check_auth_gate(post_root)
                        post_modal = detect_modal_dialog(post_root)
                        if post_modal is None:
                            toast_hit = False
                            try:
                                toast_obj = getattr(device, "toast", None)
                                if toast_obj is not None:
                                    toast_msg = str(toast_obj.get_message(0.05, 0.08, "") or "").strip()
                                    if toast_msg:
                                        post_messages.append(f"Toast: {toast_msg}")
                                        m_toast = _ENTRUST_NO_RE.search(toast_msg)
                                        if m_toast and not entrust_no:
                                            entrust_no = m_toast.group(1)
                                        toast_hit = True
                            except Exception:
                                pass
                            if toast_hit:
                                break
                            if (time.monotonic() - t_gw0) < 0.18 and poll_idx < 2:
                                continue
                            break
                        if (
                            post_modal.kind == "confirm"
                            and post_modal.message == last_confirm_msg
                            and (time.monotonic() - last_confirm_tap_at) < 0.75
                        ):
                            # Same confirm dialog is still dismissing
                            continue
                        if post_modal.message and (not post_messages or post_messages[-1] != post_modal.message):
                            post_messages.append(post_modal.message)
                        m = _ENTRUST_NO_RE.search(post_modal.message)
                        if m and not entrust_no:
                            entrust_no = m.group(1)
                        if post_modal.kind == "confirm" and post_modal.confirm_button:
                            if not _is_confirm_dialog_symbol_safe(post_modal.message, symbol, name_prefix):
                                if post_modal.cancel_button:
                                    self._tap_bounds(device, post_modal.cancel_button[1])
                                raise TradeExecutionError(
                                    "TRADE_INPUT_READBACK_MISMATCH",
                                    "二次确认弹窗标的与目标证券不一致，已自动取消",
                                    409,
                                )
                            t_gw0 = time.monotonic()
                            last_confirm_msg = post_modal.message
                            last_confirm_tap_at = t_gw0
                            gw_deadline = t_gw0 + 3.5
                            self._tap_bounds(device, post_modal.confirm_button[1])
                            continue
                        btn = post_modal.ack_button or post_modal.confirm_button or post_modal.cancel_button
                        if btn is not None:
                            self._tap_bounds(device, btn[1])
                            time.sleep(0.04)
                        break

                    if not post_messages:
                        try:
                            toast_obj = getattr(device, "toast", None)
                            if toast_obj is not None:
                                toast_msg = str(toast_obj.get_message(0.2, 0.35, "") or "").strip()
                                if toast_msg:
                                    post_messages.append(f"Toast: {toast_msg}")
                                    m_toast = _ENTRUST_NO_RE.search(toast_msg)
                                    if m_toast and not entrust_no:
                                        entrust_no = m_toast.group(1)
                        except Exception:
                            pass

                    dialog_message = " ; ".join(post_messages)
                    gateway_reply_ms = round((time.monotonic() - t_gw0) * 1000)
            elif modal is not None and modal.kind == "alert":
                dialog_message = modal.message
                submitted = True
                accepted = False
                status = "REJECTED"
                if modal.ack_button:
                    self._tap_bounds(device, modal.ack_button[1])
                    time.sleep(0.04)
            else:
                submitted = not dry_run
                accepted = False
                status = "NO_DIALOG_OBSERVED"
                dialog_message = "点击下单后未检测到确认弹窗，请核查当日委托列表"

            # Reconcile open orders & today's records (fast receipt path stays hot on 买入/卖出 tab)
            t_rec0 = time.monotonic()
            auto_cancel_attempted = False
            auto_cancel_ok: bool | None = None
            auto_cancel_message: str | None = None
            resolved_name = expected_name or readback.get("name") or symbol
            matching_open: list[dict[str, Any]] = []
            actual_reconcile_mode = "full"

            rejected_words = (
                "非交易时间",
                "非委托时间",
                "清算",
                "休市",
                "闭市",
                "网关开关关闭",
                "网关关闭",
                "开关关闭",
                "通道关闭",
                "夜市未开",
                "停用",
                "维护",
                "不足",
                "失败",
                "错误",
                "超过",
                "禁止",
                "无法",
                "不允许",
                "不能",
                "未开通",
                "无效",
                "不支持",
            )
            success_words = ("委托成功", "申报成功", "委托已提交", "已申报", "隔夜委托已", "预埋")
            is_fast_reject = (modal is not None and modal.kind == "alert") or (
                not entrust_no
                and any(w in dialog_message for w in rejected_words)
                and not any(w in dialog_message for w in success_words)
            )
            can_fast_reconcile = (
                reconcile_mode == "fast"
                and not auto_cancel
                and status != "NO_DIALOG_OBSERVED"
                and (bool(entrust_no) or any(w in dialog_message for w in success_words))
                and not any(w in dialog_message for w in rejected_words)
            )

            if dry_run:
                actual_reconcile_mode = "skip"
                orders = [dict(item) for item in self._last_orders]
                trades = [dict(item) for item in self._last_trades]
                matched_order = None
            elif is_fast_reject:
                actual_reconcile_mode = "skip"
                accepted = False
                status = "REJECTED"
                orders = [dict(item) for item in self._last_orders]
                trades = [dict(item) for item in self._last_trades]
                matched_order = None
            elif can_fast_reconcile:
                actual_reconcile_mode = "fast"
                accepted = True
                status = "SUBMITTED"
                matched_order = {
                    "entrust_no": entrust_no,
                    "symbol": symbol,
                    "name": readback.get("name") or resolved_name,
                    "side": side,
                    "side_label": _side_label(side),
                    "price": price_str,
                    "avg_price": f"{0.0:.{precision}f}",
                    "quantity": quantity,
                    "filled_quantity": 0,
                    "status": "已报",
                    "time": datetime.now(timezone.utc).astimezone(SHANGHAI_TZ).strftime("%H:%M:%S"),
                    "cancellable": True,
                }
                deduped = [
                    dict(row)
                    for row in self._last_orders
                    if not (entrust_no and row.get("entrust_no") == entrust_no)
                ]
                now_m = time.monotonic()
                with self._cache_lock:
                    self._last_orders = [dict(matched_order), *deduped[:49]]
                    self._last_orders_at = now_m
                    self._last_orders_date = self._today_shanghai_compact()
                    self._last_account_at = 0.0
                    self._last_trades_at = 0.0
                    orders = [dict(item) for item in self._last_orders]
                    trades = [dict(item) for item in self._last_trades]
            else:
                cancel_orders: list[dict[str, Any]] | None = None
                cancellable_list = self._read_cancellable_orders_on_tab(device)
                matching_open = [
                    row
                    for row in cancellable_list
                    if (
                        (entrust_no and row.get("entrust_no") == entrust_no)
                        or (
                            (row.get("symbol") == symbol or resolved_name[:3] in (row.get("name") or ""))
                            and row.get("side") == side
                        )
                    )
                ]
                if auto_cancel and matching_open:
                    auto_cancel_attempted = True
                    try:
                        cancel_res = self._cancel_order_unlocked(
                            device,
                            entrust_no=entrust_no,
                            symbol=symbol,
                            expected_name=resolved_name,
                            side=side,
                        )
                        auto_cancel_ok = bool(cancel_res.get("ok"))
                        auto_cancel_message = cancel_res.get("dialog_message")
                        if auto_cancel_ok and isinstance(cancel_res.get("orders"), list):
                            cancel_orders = cancel_res["orders"]
                    except TradeExecutionError as err:
                        auto_cancel_ok = False
                        auto_cancel_message = err.message

                orders = (
                    cancel_orders
                    if cancel_orders is not None
                    else self._read_today_orders_unlocked(device)
                )
                trades = self._read_today_trades_unlocked(device)
                if submitted and not dry_run:
                    with self._cache_lock:
                        self._last_account_at = 0.0

                matched_order = next(
                    (
                        row
                        for row in orders
                        if (
                            (entrust_no and row.get("entrust_no") == entrust_no)
                            or (
                                (row.get("symbol") == symbol or resolved_name[:3] in (row.get("name") or ""))
                                and row.get("side") == side
                                and abs((_parse_float(row.get("price")) or -1) - float(price_str)) < 1e-4
                                and row.get("quantity") == quantity
                            )
                        )
                    ),
                    None,
                )

                if status != "NO_DIALOG_OBSERVED":
                    matched_status = (matched_order.get("status") or "") if matched_order else ""
                    if auto_cancel_attempted and auto_cancel_ok:
                        accepted = True
                        status = "AUTO_CANCELLED"
                    elif matching_open or (
                        matched_order is not None and not any(w in matched_status for w in ("废单", "拒绝", "作废"))
                    ):
                        accepted = True
                        status = "SUBMITTED"
                    elif entrust_no or any(w in dialog_message for w in success_words):
                        accepted = True
                        status = "SUBMITTED"
                    elif any(w in dialog_message for w in rejected_words) or (
                        matched_order is not None and any(w in matched_status for w in ("废单", "拒绝", "作废"))
                    ):
                        accepted = False
                        status = "REJECTED"
                    else:
                        accepted = False
                        status = "UNCONFIRMED"
                        if not dialog_message:
                            dialog_message = "已点击确认提交，未捕获柜台弹窗或新增委托记录（可能处于盘后清算时段）"
                elif matching_open:
                    accepted = True
                    status = "SUBMITTED"
                    if auto_cancel_attempted and auto_cancel_ok:
                        status = "AUTO_CANCELLED"

            reconcile_ms = round((time.monotonic() - t_rec0) * 1000)
            total_ms = round((time.monotonic() - t_total0) * 1000)
            timings_ms = {
                **form_timings,
                "submit_confirm_ms": submit_confirm_ms,
                "gateway_reply_ms": gateway_reply_ms,
                "reconcile_ms": reconcile_ms,
                "total": total_ms,
            }

            return {
                "device_serial": self.adb_serial,
                "symbol": symbol,
                "name": readback.get("name") or resolved_name,
                "side": side,
                "side_label": _side_label(side),
                "price": price_str,
                "quantity": quantity,
                "dry_run": dry_run,
                "submitted": submitted,
                "accepted": accepted,
                "status": status,
                "entrust_no": entrust_no,
                "dialog_message": mask_sensitive_numbers(dialog_message, keep_symbols={symbol}),
                "confirm_preview": confirm_preview,
                "form_readback": {
                    "symbol": readback.get("symbol"),
                    "name": readback.get("name"),
                    "price": readback.get("price"),
                    "quantity": readback.get("quantity"),
                    "limit_down": readback.get("limit_down"),
                    "limit_up": readback.get("limit_up"),
                    "available_text": readback.get("available_text"),
                },
                "matched_order": matched_order,
                "auto_cancel_attempted": auto_cancel_attempted,
                "auto_cancel_ok": auto_cancel_ok,
                "auto_cancel_message": auto_cancel_message,
                "orders": orders,
                "trades": trades,
                "prestaged": bool(form_timings.get("prestaged_hit")),
                "reconcile_mode": actual_reconcile_mode,
                "timings_ms": timings_ms,
                "captured_at": _now_iso(),
            }

    def cancel_order(
        self,
        *,
        entrust_no: str | None = None,
        symbol: str | None = None,
        expected_name: str | None = None,
        side: str | None = None,
    ) -> dict[str, Any]:
        if not entrust_no and not symbol:
            raise TradeExecutionError("TRADE_INVALID_CANCEL", "撤单至少需要提供 entrust_no 或 symbol", 422)
        with self._device_guard() as device:
            try:
                result = self._cancel_order_unlocked(
                    device,
                    entrust_no=entrust_no,
                    symbol=symbol,
                    expected_name=expected_name,
                    side=side,
                )
            except TradeExecutionError as err:
                if err.error_code == "TRADE_ORDER_NOT_FOUND":
                    self.invalidate_snapshots(account=True, orders=True, trades=True)
                raise
            result["device_serial"] = self.adb_serial
            if result.get("ok"):
                self.invalidate_snapshots(account=True, orders=False, trades=True)
            return result
