"""Scheduled isolated QDII/LOF premium refresh and push."""

from __future__ import annotations

import asyncio
import datetime as dt
from typing import Callable
from zoneinfo import ZoneInfo

from .research_premium import format_premium_body, premium_push_slot

SH_TZ = ZoneInfo("Asia/Shanghai")


def _in_premium_refresh_window(local: dt.datetime, is_trading_day: Callable[[dt.date], bool] | None) -> bool:
    if is_trading_day is not None:
        if not is_trading_day(local.date()):
            return False
    elif local.weekday() >= 5:
        return False
    minute = local.hour * 60 + local.minute
    return (8 * 60 + 50 <= minute <= 11 * 60 + 35) or (12 * 60 + 30 <= minute <= 15 * 60 + 5)


class PremiumMonitor:
    def __init__(
        self,
        provider: object,
        store: object,
        dispatcher: object,
        *,
        is_trading_day: Callable[[dt.date], bool] | None = None,
    ) -> None:
        self.provider = provider
        self.store = store
        self.dispatcher = dispatcher
        self.is_trading_day = is_trading_day

    async def poll_once(self, now: dt.datetime | None = None, *, force: bool = False) -> dict:
        previous = await asyncio.to_thread(self.store.premium_snapshot)
        current = now or dt.datetime.now(dt.timezone.utc)
        local = current.astimezone(SH_TZ) if current.tzinfo else current.replace(tzinfo=SH_TZ)

        if not force and previous is not None and not _in_premium_refresh_window(local, self.is_trading_day):
            return previous

        snapshot = await asyncio.to_thread(self.provider.fetch, previous=previous)
        await asyncio.to_thread(self.store.save_premium_snapshot, snapshot)

        is_trade_day = self.is_trading_day(local.date()) if self.is_trading_day is not None else (local.weekday() < 5)
        slot = premium_push_slot(local) if is_trade_day else None
        config = await asyncio.to_thread(self.store.push_config, public=False)
        if slot and config.get("premium_push_enabled"):
            local_date = local.date().isoformat()
            claimed = await asyncio.to_thread(
                self.store.claim_push_event,
                f"premium:{local_date}:{slot}",
            )
            if claimed:
                try:
                    await asyncio.to_thread(
                        self.dispatcher.send,
                        config,
                        "QDII/LOF 溢价估算",
                        format_premium_body(snapshot),
                    )
                except Exception:
                    pass
        return snapshot
