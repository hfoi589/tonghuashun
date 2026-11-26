from __future__ import annotations

import asyncio
import base64
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from level2_service.app_sessions import AccountSessionBundle, EncryptedFileSessionProvider
from level2_service.fund_flow_history import FundFlowHistoryMonitor
from level2_service.models import FUND_FLOW_METRICS, FUND_FLOW_PERIODS, MetricKind
from level2_service.parsed_values import DirectReadOutcome, DirectRequestError
from level2_service.research_store import SQLiteResearchStore
from level2_service.session_alert import PreopenSessionProbe, SessionHealthNotifier


SH = ZoneInfo("Asia/Shanghai")


def _session_provider(tmp_path: Path, **kwargs) -> EncryptedFileSessionProvider:
    key = base64.urlsafe_b64encode(b"session-alert-test-key-32-bytes!").decode("ascii")
    return EncryptedFileSessionProvider(tmp_path / "sessions", key, **kwargs)


def _store_with_push(tmp_path: Path, *, enabled: bool = True) -> SQLiteResearchStore:
    store = SQLiteResearchStore(tmp_path / "research.db")
    store.save_push_config(
        {
            "enabled": enabled,
            "bark_groups": [
                {"id": "g1", "name": "Ops", "base_url": "https://api.day.app", "device_key": "bark-secret"}
            ]
            if enabled
            else [],
            "sc3_bot": {"enabled": False},
            "wecom": {"enabled": False},
            "rules": [],
        }
    )
    return store


class _FakeDispatcher:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def send(self, _config: dict, title: str, body: str) -> dict[str, object]:
        self.sent.append((title, body))
        return {"ok": True, "channels": {"bark": {"sent": 1}}, "errors": {}}


def _valid_outcome() -> DirectReadOutcome:
    values = {kind: None for kind in MetricKind}
    for period, _label, unit_kind in FUND_FLOW_PERIODS:
        values[unit_kind] = "万元"
        for name, kind in FUND_FLOW_METRICS[period].items():
            values[kind] = "120.50" if name != "retail_inflow" else "-120.50"
    return DirectReadOutcome(values=values, source_errors={"core_metrics": None, "main_fund_flow": None})


def test_session_notifier_dedupes_per_window_reminds_across_windows_and_recovers(tmp_path: Path) -> None:
    store = _store_with_push(tmp_path, enabled=True)
    provider = _session_provider(tmp_path)
    bundle1 = AccountSessionBundle(
        role="main_fund_flow",
        cookie="sess=secret-cookie-value",
        user_agent="ua-1",
        platform="android",
        updated_at=datetime(2026, 11, 24, 18, 0, tzinfo=timezone.utc),
    )
    provider.put(bundle1)

    now_holder = [datetime(2026, 11, 25, 8, 50, tzinfo=SH)]
    dispatcher = _FakeDispatcher()
    notifier = SessionHealthNotifier(
        store,
        dispatcher,
        provider,
        clock=lambda: now_holder[0],
    )

    assert notifier.notify_error("main_fund_flow", "DIRECT_SESSION_EXPIRED", stage="盘前 08:50 自动预检") is True
    assert len(dispatcher.sent) == 1
    title, body = dispatcher.sent[0]
    assert "暗盘" in title
    assert "DIRECT_SESSION_EXPIRED" in body
    assert "secret-cookie-value" not in body

    # Same preopen window & same credential -> deduplicated.
    now_holder[0] = datetime(2026, 11, 25, 9, 10, tzinfo=SH)
    assert notifier.notify_error("main_fund_flow", "DIRECT_SESSION_EXPIRED", stage="盘中暗盘定时轮询") is False
    assert len(dispatcher.sent) == 1

    # Morning continuous window -> reminds once again if still unrenewed.
    now_holder[0] = datetime(2026, 11, 25, 9, 31, tzinfo=SH)
    assert notifier.notify_error("main_fund_flow", "DIRECT_SESSION_EXPIRED", stage="盘中暗盘定时轮询") is True
    assert len(dispatcher.sent) == 2

    # Admin refreshes the session -> recovery alert fires once for the new bundle.
    now_holder[0] = datetime(2026, 11, 25, 9, 43, tzinfo=SH)
    bundle2 = AccountSessionBundle(
        role="main_fund_flow",
        cookie="sess=renewed-cookie",
        user_agent="ua-2",
        platform="android",
        updated_at=datetime(2026, 11, 25, 1, 43, tzinfo=timezone.utc),
    )
    provider.put(bundle2)
    assert notifier.notify_recovered("main_fund_flow", stage="管理后台续签") is True
    assert notifier.notify_recovered("main_fund_flow", stage="盘中暗盘定时轮询") is False
    assert len(dispatcher.sent) == 3
    assert "已恢复" in dispatcher.sent[2][0]

    # Even in the same morning window, expiry of the new credential triggers a fresh alert.
    assert notifier.notify_error("main_fund_flow", "DIRECT_SESSION_EXPIRED", stage="直连请求") is True
    assert len(dispatcher.sent) == 4


def test_session_notifier_skips_when_push_disabled_and_never_claims_key(tmp_path: Path) -> None:
    store = _store_with_push(tmp_path, enabled=False)
    provider = _session_provider(tmp_path)
    dispatcher = _FakeDispatcher()
    notifier = SessionHealthNotifier(
        store,
        dispatcher,
        provider,
        clock=lambda: datetime(2026, 11, 25, 9, 35, tzinfo=SH),
    )

    assert notifier.notify_error("main_fund_flow", "DIRECT_SESSION_EXPIRED") is False
    assert dispatcher.sent == []

    # Enabling push later must still deliver the alert in the same window.
    store.save_push_config(
        {
            "enabled": True,
            "bark_groups": [{"id": "g1", "name": "Ops", "base_url": "https://api.day.app", "device_key": "k"}],
            "sc3_bot": {"enabled": False},
            "wecom": {"enabled": False},
            "rules": [],
        }
    )
    assert notifier.notify_error("main_fund_flow", "DIRECT_SESSION_EXPIRED") is True
    assert len(dispatcher.sent) == 1


def test_preopen_session_probe_respects_trading_days_and_notifies_expired_sessions(tmp_path: Path) -> None:
    store = _store_with_push(tmp_path, enabled=True)
    provider = _session_provider(tmp_path)
    provider.put(
        AccountSessionBundle(
            role="main_fund_flow",
            cookie="sess=old",
            user_agent="ua",
            platform="android",
            updated_at=datetime(2026, 11, 24, 10, 0, tzinfo=timezone.utc),
        )
    )
    dispatcher = _FakeDispatcher()
    now_holder = [datetime(2026, 11, 25, 8, 45, tzinfo=SH)]
    notifier = SessionHealthNotifier(store, dispatcher, provider, clock=lambda: now_holder[0])

    class Accounts:
        @staticmethod
        def list_watchlist_symbols():
            return ["160723", "601872"]

    fund_calls: list[tuple[str, bool]] = []

    class FundSource:
        @staticmethod
        def read_direct(symbol: str, *, force: bool = False):
            fund_calls.append((symbol, force))
            raise DirectRequestError("DIRECT_SESSION_EXPIRED")

    probe = PreopenSessionProbe(
        is_trading_day=lambda d: d == date(2026, 11, 25),
        accounts=Accounts(),
        fund_source=FundSource(),
        core_source=None,
        session_provider=provider,
        notifier=notifier,
        clock=lambda: now_holder[0],
    )

    # Before 08:50 -> skips.
    assert asyncio.run(probe.poll_once()) == {}
    assert fund_calls == []

    # Non-trading day -> skips.
    now_holder[0] = datetime(2026, 11, 26, 8, 55, tzinfo=SH)
    assert asyncio.run(probe.poll_once()) == {}

    # 08:51 on trading day -> skips exchange fund 160723, probes 601872 with force=True, alerts once for both missing core & expired fund.
    now_holder[0] = datetime(2026, 11, 25, 8, 51, tzinfo=SH)
    result = asyncio.run(probe.poll_once())
    assert fund_calls == [("601872", True)]
    assert result == {
        "main_fund_flow": "DIRECT_SESSION_EXPIRED",
        "core_metrics": "DIRECT_SESSION_UNAVAILABLE",
    }
    assert len(dispatcher.sent) == 2
    assert any("暗盘" in title for title, _ in dispatcher.sent)
    assert any("L2 核心" in title for title, _ in dispatcher.sent)

    # Second tick same day -> does not re-probe until next day.
    assert asyncio.run(probe.poll_once()) == {}
    assert len(fund_calls) == 1


def test_fund_flow_monitor_notifies_expired_and_recovered(tmp_path: Path) -> None:
    store = _store_with_push(tmp_path, enabled=True)
    provider = _session_provider(tmp_path)
    bundle = AccountSessionBundle(
        role="main_fund_flow",
        cookie="sess=1",
        user_agent="ua",
        platform="android",
        updated_at=datetime(2026, 11, 25, 1, 0, tzinfo=timezone.utc),
    )
    provider.put(bundle)
    dispatcher = _FakeDispatcher()
    now_holder = [datetime(2026, 11, 25, 9, 31, tzinfo=SH)]
    notifier = SessionHealthNotifier(store, dispatcher, provider, clock=lambda: now_holder[0])

    state = {"expired": True}

    class Accounts:
        @staticmethod
        def list_watchlist_symbols():
            return ["601872"]

    class BatchFundSource:
        @staticmethod
        def read_direct_batch(symbols, *, force_all_periods: bool = False):
            del force_all_periods
            if state["expired"]:
                raise DirectRequestError("DIRECT_SESSION_EXPIRED")
            return {sym: _valid_outcome() for sym in symbols}

    monitor = FundFlowHistoryMonitor(
        Accounts(),
        BatchFundSource(),
        store,
        is_market_open=lambda: True,
        session_notifier=notifier,
        clock=lambda: now_holder[0],
    )

    asyncio.run(monitor.poll_once())
    assert len(dispatcher.sent) == 1
    assert "Cookie 已过期" in dispatcher.sent[0][0]

    # Once collection succeeds again, recovery notice fires once.
    state["expired"] = False
    now_holder[0] = datetime(2026, 11, 25, 9, 43, tzinfo=SH)
    asyncio.run(monitor.poll_once())
    assert len(dispatcher.sent) == 2
    assert "已恢复" in dispatcher.sent[1][0]


def test_preopen_probe_dual_window_and_trade_workspace_recovery(tmp_path) -> None:
    from level2_service.ths_trade import TradeExecutionError

    store = _store_with_push(tmp_path)
    dispatcher = _FakeDispatcher()
    now_holder = [datetime(2026, 11, 26, 8, 55, tzinfo=SH)]

    class _TradeExecutorStub:
        def __init__(self) -> None:
            self.login_expired = True
            self.calls = 0

        def probe_broker_session(self) -> dict[str, object]:
            self.calls += 1
            if self.login_expired:
                raise TradeExecutionError("BROKER_LOGIN_REQUIRED", "需要重新认证", 409)
            return {"ok": True, "status": "READY"}

    executor = _TradeExecutorStub()
    notifier = SessionHealthNotifier(
        store=store,
        dispatcher=dispatcher,
        session_provider=None,
        clock=lambda: now_holder[0],
    )
    probe = PreopenSessionProbe(
        is_trading_day=lambda _d: True,
        accounts=None,
        fund_source=None,
        core_source=None,
        session_provider=None,
        notifier=notifier,
        trade_executor=executor,
        clock=lambda: now_holder[0],
    )

    morning_res = asyncio.run(probe.poll_once())
    assert morning_res == {"trade_workspace": "BROKER_LOGIN_REQUIRED"}
    assert executor.calls == 1
    assert len(dispatcher.sent) == 1
    assert "交易登录态" in dispatcher.sent[0][1] or "BROKER_LOGIN_REQUIRED" in dispatcher.sent[0][1]

    # Same morning window will not re-probe.
    now_holder[0] = datetime(2026, 11, 26, 9, 5, tzinfo=SH)
    assert asyncio.run(probe.poll_once()) == {}
    assert executor.calls == 1

    # Midday window 12:52 triggers second probe and dispatches recovery notice.
    executor.login_expired = False
    now_holder[0] = datetime(2026, 11, 26, 12, 52, tzinfo=SH)
    midday_res = asyncio.run(probe.poll_once())
    assert midday_res == {"trade_workspace": "READY"}
    assert executor.calls == 2
    assert len(dispatcher.sent) == 2
    assert "已恢复" in dispatcher.sent[1][0]
    store.close()
