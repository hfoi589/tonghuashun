from __future__ import annotations

import asyncio
import datetime as dt

from level2_service.premium_monitor import PremiumMonitor


class Provider:
    def fetch(self, previous=None):
        return {"as_of": "2026-08-31T09:31:00+08:00", "rows": [{"code": "160723", "name": "嘉实原油", "current_price": 1.2, "estimated_price": 1.0, "premium_rate": 0.2}]}


class Store:
    def __init__(self):
        self.snapshot = None
        self.events = set()

    def premium_snapshot(self): return self.snapshot
    def save_premium_snapshot(self, snapshot): self.snapshot = snapshot; return snapshot
    def push_config(self, public=False): return {"premium_push_enabled": True, "enabled": True, "bark_groups": [{"id": "phone"}]}
    def claim_push_event(self, key):
        if key in self.events: return False
        self.events.add(key); return True


class Dispatcher:
    def __init__(self): self.calls = []
    def send(self, config, title, body): self.calls.append((title, body)); return {"ok": True}


def test_premium_monitor_refreshes_and_deduplicates_scheduled_push():
    store = Store(); dispatcher = Dispatcher()
    monitor = PremiumMonitor(Provider(), store, dispatcher)
    now = dt.datetime(2026, 8, 31, 9, 31, tzinfo=dt.timezone(dt.timedelta(hours=8)))

    asyncio.run(monitor.poll_once(now))
    asyncio.run(monitor.poll_once(now))

    assert store.snapshot["rows"][0]["premium_rate"] == 0.2
    assert len(dispatcher.calls) == 1
