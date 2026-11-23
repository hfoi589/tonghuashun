"""Background persistence of approved market snapshots for monitoring/replay."""

from __future__ import annotations

import asyncio
from typing import Any, Callable

from .market_data import fixed_market_error_code
from .research_premium import PREMIUM_NAMES
from .research_replay import build_replay_markers


_FUND_PREFIXES = ("15", "16", "50", "51", "52", "56", "58")


def resolve_push_symbol_name(
    symbol: str,
    series_name: object = None,
    symbol_lookup: Callable[[str], Any] | None = None,
) -> str:
    """Resolve human-readable security name for push notifications."""
    candidate = str(series_name or "").strip()
    if candidate and candidate != symbol:
        return candidate
    if symbol_lookup is not None:
        try:
            looked_up = symbol_lookup(symbol)
            looked_name = str(
                getattr(looked_up, "name", None) if not isinstance(looked_up, str) else looked_up
            ).strip()
            if looked_name and looked_name != symbol:
                return looked_name
        except Exception:
            pass
    return PREMIUM_NAMES.get(symbol, "")


def format_push_symbol(symbol: str, name: object = None) -> str:
    """Format stock/ETF display label combining name and 6-digit code when known."""
    resolved = str(name or "").strip()
    if resolved and resolved != symbol:
        return f"{resolved} ({symbol})"
    return symbol


def format_push_price(symbol: str, price: float) -> str:
    """Format push price with 3 decimals for on-exchange funds and 2 decimals for equities."""
    precision = 3 if str(symbol).startswith(_FUND_PREFIXES) else 2
    return f"{float(price):.{precision}f}"


def _enrich_points_with_fund_flow(
    store: object,
    symbol: str,
    trade_date: str | None,
    points: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    read_ff = getattr(store, "read_fund_flow_history", None)
    if not callable(read_ff) or not points:
        return points
    ff_by_time: dict[str, dict[str, Any]] = {}
    try:
        ff_history = read_ff(symbol, trade_date) if trade_date else read_ff(symbol)
        today_pts = ((ff_history.get("periods") or {}).get("today") or {}).get("points") or []
        for item in today_pts:
            if isinstance(item, dict) and item.get("time"):
                ff_by_time[str(item["time"])] = item
    except Exception:
        return points
    if not ff_by_time:
        return points
    return [
        {
            **point,
            "main_net_inflow": ff.get("main_net_inflow"),
            "main_visible_inflow": ff.get("main_visible_inflow"),
            "main_hidden_inflow": ff.get("main_hidden_inflow"),
            "retail_inflow": ff.get("retail_inflow"),
            "fund_unit": ff.get("unit"),
        }
        if (ff := ff_by_time.get(str(point.get("time") or "")))
        else point
        for point in points
    ]


class ResearchMonitor:
    def __init__(
        self,
        accounts: object,
        broker: object,
        store: object,
        *,
        dispatcher: object | None = None,
        symbol_lookup: Callable[[str], Any] | None = None,
        max_concurrent: int = 4,
    ) -> None:
        self.accounts = accounts
        self.broker = broker
        self.store = store
        self.dispatcher = dispatcher
        self.symbol_lookup = symbol_lookup
        self.max_concurrent = max_concurrent

    async def poll_once(self) -> dict[str, list[str]]:
        symbols = await asyncio.to_thread(self.accounts.list_monitored_symbols)
        semaphore = asyncio.Semaphore(self.max_concurrent)

        async def collect(symbol: str) -> tuple[str, list[str]]:
            async with semaphore:
                try:
                    snapshot = await self.broker.refresh(
                        symbol,
                        detail=True,
                        max_age_seconds=0,
                    )
                    changed = await asyncio.to_thread(
                        self.store.record_snapshot,
                        snapshot,
                    )
                    if changed and self.dispatcher is not None:
                        try:
                            await self._push_changed(symbol, changed)
                        except Exception:
                            # Push is an optional enhancement. It must not turn a
                            # valid market snapshot into a market-source failure.
                            pass
                    return symbol, changed
                except Exception as error:
                    code = fixed_market_error_code(
                        error,
                        "MARKET_QUOTE_UNAVAILABLE",
                    )
                    await asyncio.to_thread(self.store.record_error, symbol, code)
                    return symbol, []

        rows = await asyncio.gather(*(collect(symbol) for symbol in symbols))
        return dict(rows)

    async def _push_changed(self, symbol: str, changed: list[str]) -> None:
        series = await asyncio.to_thread(self.store.read_series, symbol)
        config = await asyncio.to_thread(self.store.push_config, public=False)
        rules = config.get("rules", [])
        points = await asyncio.to_thread(
            _enrich_points_with_fund_flow,
            self.store,
            symbol,
            series.get("trade_date"),
            series["points"],
        )
        markers = build_replay_markers(
            symbol,
            points,
            rules,
            marker_threshold=float(series["macd_settings"]["marker_threshold"]),
        )
        stock_name = resolve_push_symbol_name(
            symbol,
            series.get("name"),
            self.symbol_lookup,
        )
        display_symbol = format_push_symbol(symbol, stock_name)
        changed_minutes = set(changed)
        for marker in markers:
            if marker["time"] not in changed_minutes:
                continue
            event_key = (
                f"{symbol}:{series['trade_date']}:{marker['time']}:{marker['rule_id']}"
            )
            claimed = await asyncio.to_thread(
                self.store.claim_push_event,
                event_key,
            )
            if not claimed:
                continue
            price_text = format_push_price(symbol, float(marker["price"]))
            await asyncio.to_thread(
                self.dispatcher.send,
                config,
                marker["rule_title"],
                f"{display_symbol} {marker['time']} 价格 {price_text}",
            )
