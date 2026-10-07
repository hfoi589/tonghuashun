"""Background persistence of approved market snapshots for monitoring/replay."""

from __future__ import annotations

import asyncio
from typing import Any

from .market_data import fixed_market_error_code
from .research_replay import build_replay_markers


class ResearchMonitor:
    def __init__(
        self,
        accounts: object,
        broker: object,
        store: object,
        *,
        dispatcher: object | None = None,
        max_concurrent: int = 4,
    ) -> None:
        self.accounts = accounts
        self.broker = broker
        self.store = store
        self.dispatcher = dispatcher
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
        markers = build_replay_markers(
            symbol,
            series["points"],
            rules,
            marker_threshold=float(series["macd_settings"]["marker_threshold"]),
        )
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
            await asyncio.to_thread(
                self.dispatcher.send,
                config,
                marker["rule_title"],
                f"{symbol} {marker['time']} 价格 {marker['price']:.2f}",
            )
