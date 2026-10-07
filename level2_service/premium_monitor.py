"""Scheduled isolated QDII/LOF premium refresh and push."""

from __future__ import annotations

import asyncio
import datetime as dt

from .research_premium import format_premium_body, premium_push_slot


class PremiumMonitor:
    def __init__(self, provider: object, store: object, dispatcher: object) -> None:
        self.provider = provider
        self.store = store
        self.dispatcher = dispatcher

    async def poll_once(self, now: dt.datetime | None = None) -> dict:
        previous = await asyncio.to_thread(self.store.premium_snapshot)
        snapshot = await asyncio.to_thread(self.provider.fetch, previous=previous)
        await asyncio.to_thread(self.store.save_premium_snapshot, snapshot)
        current = now or dt.datetime.now(dt.timezone.utc)
        slot = premium_push_slot(current)
        config = await asyncio.to_thread(self.store.push_config, public=False)
        if slot and config.get("premium_push_enabled"):
            local_date = current.astimezone(dt.timezone(dt.timedelta(hours=8))).date().isoformat()
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
