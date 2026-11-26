from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from argon2 import PasswordHasher
from fastapi.testclient import TestClient
import pytest

from level2_service.api import create_app
from level2_service.auto_trade import (
    SHANGHAI_TZ,
    AutoTradeEngine,
    SQLiteAutoTradeStore,
    compute_slippage_limit_price,
    is_in_continuous_auction,
)
from level2_service.market_accounts import SQLiteMarketAccountStore
from level2_service.parsed_values import SymbolLookup
from level2_service.quant_rules import SQLiteQuantRuleStore
from level2_service.research_store import SQLiteResearchStore
from level2_service.ths_trade import TonghuashunTradeExecutor, TradeExecutionError


TOKEN = "auto-trade-bridge-token"
CATALOG = {
    "160723": SymbolLookup(symbol="160723", name="嘉实原油", market="36"),
    "601872": SymbolLookup(symbol="601872", name="招商轮船", market="17"),
}


def _headers(role: str = "admin", user: str = "domain:wilson", token: str = TOKEN) -> dict[str, str]:
    return {
        "X-Goodluck-Internal-Token": token,
        "X-Goodluck-User": user,
        "X-Goodluck-Role": role,
    }


@dataclass
class _StubWidget:
    device: "_FakeU2Device"
    selector: dict

    @property
    def exists(self) -> bool:
        return True

    def click(self) -> None:
        pass

    def set_text(self, value: str) -> None:
        rid = self.selector.get("resourceId")
        if rid == "com.hexin.plat.android:id/auto_stockcode":
            self.device.form_code = value
        elif rid == "com.hexin.plat.android:id/stockprice":
            self.device.form_price = value
        elif rid == "com.hexin.plat.android:id/stockvolume":
            self.device.form_qty = value

    def child(self, **kwargs):
        return _StubWidget(self.device, {**self.selector, "child": kwargs})


@dataclass
class _FakeU2Device:
    screen: str = "buy"
    form_code: str = "160723"
    form_price: str = "2.109"
    form_qty: str = "100"
    order_open: bool = False
    info: dict = field(default_factory=lambda: {"displayWidth": 1080, "displayHeight": 1920})

    def __call__(self, **kwargs):
        return _StubWidget(self, kwargs)

    def app_current(self) -> dict[str, str]:
        return {"package": "com.hexin.plat.android"}

    def press(self, _key: str) -> None:
        self.screen = "holdings"

    def swipe(self, *_args, **_kwargs) -> None:
        pass

    def click(self, x: int, y: int) -> None:
        if self.screen == "buy" and 195 <= x <= 702 and 1007 <= y <= 1130:
            self.screen = "confirm"
        elif self.screen == "confirm":
            if 600 <= x <= 900:
                self.order_open = True
                self.screen = "receipt"
            else:
                self.screen = "buy"
        elif self.screen == "receipt":
            self.screen = "buy"
        elif self.screen == "cancel" and 0 <= x <= 1080 and 446 <= y <= 611:
            self.screen = "cancel_confirm"
        elif self.screen == "cancel_confirm" and 600 <= x <= 900:
            self.order_open = False
            self.screen = "cancel"
        elif 432 <= x <= 648 and 216 <= y <= 336:
            self.screen = "cancel"
        elif 864 <= x <= 1080 and 216 <= y <= 336:
            self.screen = "query"
        elif self.screen == "query" and 337 <= y <= 481:
            self.screen = "trades"
        elif self.screen == "query" and y > 481:
            self.screen = "orders"
        elif 0 <= x <= 270 and 216 <= y <= 336 and self.screen in {"orders", "trades"}:
            self.screen = "orders"
        elif 270 <= x <= 540 and 216 <= y <= 336 and self.screen in {"orders", "trades"}:
            self.screen = "trades"
        elif 0 <= x <= 216 and 216 <= y <= 336:
            self.screen = "buy"
        elif 648 <= x <= 864 and 216 <= y <= 336:
            self.screen = "holdings"

    def dump_hierarchy(self, compressed: bool = False) -> str:
        del compressed
        if self.screen == "buy":
            return f"""<hierarchy>
  <node package="com.hexin.plat.android" resource-id="com.hexin.plat.android:id/frame_layout">
    <node resource-id="com.hexin.plat.android:id/page_title_view" text="国泰海通证券" />
    <node resource-id="com.hexin.plat.android:id/account_info_view" text="**9306" />
    <node resource-id="com.hexin.plat.android:id/btn" text="买入" selected="true" bounds="[0,216][216,336]" />
    <node resource-id="com.hexin.plat.android:id/btn" text="卖出" selected="false" bounds="[216,216][432,336]" />
    <node resource-id="com.hexin.plat.android:id/btn" text="撤单" selected="false" bounds="[432,216][648,336]" />
    <node resource-id="com.hexin.plat.android:id/btn" text="持仓" selected="false" bounds="[648,216][864,336]" />
    <node resource-id="com.hexin.plat.android:id/btn" text="查询" selected="false" bounds="[864,216][1080,336]" />
    <node resource-id="com.hexin.plat.android:id/auto_stockcode" class="android.widget.EditText" text="{self.form_code}" bounds="[48,421][341,529]" />
    <node resource-id="com.hexin.plat.android:id/stockname" text="嘉实原油LOF" bounds="[341,421][702,529]" />
    <node resource-id="com.hexin.plat.android:id/stockprice"><node class="android.widget.EditText" text="{self.form_price}" bounds="[156,556][594,664]" /></node>
    <node resource-id="com.hexin.plat.android:id/dietingprice" text="1.898" />
    <node resource-id="com.hexin.plat.android:id/zhangtingprice" text="2.320" />
    <node resource-id="com.hexin.plat.android:id/stockvolume"><node class="android.widget.EditText" text="{self.form_qty}" bounds="[156,736][594,844]" /></node>
    <node resource-id="com.hexin.plat.android:id/btn_transaction" clickable="true" bounds="[195,1007][702,1130]"><node text="买 入" clickable="true" bounds="[195,1007][702,1130]" /></node>
  </node>
</hierarchy>"""
        if self.screen == "confirm":
            return f"""<hierarchy>
  <node package="com.hexin.plat.android"><node text="委托买入确认" /><node text="证券代码 {self.form_code} 嘉实原油LOF" /><node text="委托价格 {self.form_price}" /><node text="委托数量 {self.form_qty}" /><node text="取消" clickable="true" bounds="[100,1200][400,1300]" /><node text="确认买入" clickable="true" bounds="[600,1200][900,1300]" /></node>
</hierarchy>"""
        if self.screen == "receipt":
            return """<hierarchy><node package="com.hexin.plat.android"><node text="提示" /><node text="委托已提交，合同编号: HT20261126001" /><node text="确定" clickable="true" bounds="[300,1200][780,1300]" /></node></hierarchy>"""
        if self.screen == "cancel":
            if not self.order_open:
                return """<hierarchy><node package="com.hexin.plat.android" resource-id="com.hexin.plat.android:id/frame_layout"><node resource-id="com.hexin.plat.android:id/btn" text="买入" selected="false" bounds="[0,216][216,336]" /><node resource-id="com.hexin.plat.android:id/btn" text="撤单" selected="true" bounds="[432,216][648,336]" /><node resource-id="com.hexin.plat.android:id/btn" text="持仓" selected="false" bounds="[648,216][864,336]" /><node resource-id="com.hexin.plat.android:id/btn" text="查询" selected="false" bounds="[864,216][1080,336]" /><node resource-id="com.hexin.plat.android:id/empty_tips" text="当前无可撤委托单" /></node></hierarchy>"""
            return f"""<hierarchy><node package="com.hexin.plat.android" resource-id="com.hexin.plat.android:id/frame_layout"><node resource-id="com.hexin.plat.android:id/btn" text="买入" selected="false" bounds="[0,216][216,336]" /><node resource-id="com.hexin.plat.android:id/btn" text="撤单" selected="true" bounds="[432,216][648,336]" /><node resource-id="com.hexin.plat.android:id/btn" text="持仓" selected="false" bounds="[648,216][864,336]" /><node resource-id="com.hexin.plat.android:id/btn" text="查询" selected="false" bounds="[864,216][1080,336]" /><node resource-id="com.hexin.plat.android:id/recyclerview_id" bounds="[0,446][1080,650]"><node bounds="[0,446][1080,611]"><node text="嘉实原油" /><node text="10:05:00" /><node text="{self.form_price}" /><node text="0.000" /><node text="100" /><node text="0" /><node text="买入" /><node text="已报" /></node></node><node resource-id="com.hexin.plat.android:id/cannot_chedan_title_text" text="今日成交单" bounds="[435,1010][645,1118]" /></node></hierarchy>"""
        if self.screen == "cancel_confirm":
            return """<hierarchy><node package="com.hexin.plat.android"><node text="确认撤单？" /><node text="取消" clickable="true" bounds="[100,1200][400,1300]" /><node text="确定" clickable="true" bounds="[600,1200][900,1300]" /></node></hierarchy>"""
        if self.screen == "query":
            return """<hierarchy><node package="com.hexin.plat.android" resource-id="com.hexin.plat.android:id/frame_layout"><node resource-id="com.hexin.plat.android:id/btn" text="买入" bounds="[0,216][216,336]" /><node resource-id="com.hexin.plat.android:id/drwt" text="当日委托" bounds="[0,481][1080,625]" /><node resource-id="com.hexin.plat.android:id/drcj" text="当日成交" bounds="[0,337][1080,481]" /></node></hierarchy>"""
        if self.screen == "orders":
            status = "已报" if self.order_open else "订单已撤销"
            return f"""<hierarchy><node package="com.hexin.plat.android" resource-id="com.hexin.plat.android:id/frame_layout"><node resource-id="com.hexin.plat.android:id/btn" text="当日委托" selected="true" bounds="[0,216][270,336]" /><node resource-id="com.hexin.plat.android:id/btn" text="当日成交" selected="false" bounds="[270,216][540,336]" /><node resource-id="com.hexin.plat.android:id/recyclerview_id"><node bounds="[0,446][1080,611]"><node text="嘉实原油" /><node text="10:05:00" /><node text="{self.form_price}" /><node text="0.000" /><node text="100" /><node text="0" /><node text="买入" /><node text="{status}" /></node></node></node></hierarchy>"""
        if self.screen == "trades":
            return """<hierarchy><node package="com.hexin.plat.android" resource-id="com.hexin.plat.android:id/frame_layout"><node resource-id="com.hexin.plat.android:id/btn" text="当日委托" selected="false" bounds="[0,216][270,336]" /><node resource-id="com.hexin.plat.android:id/btn" text="当日成交" selected="true" bounds="[270,216][540,336]" /><node resource-id="com.hexin.plat.android:id/recyclerview_id" /></node></hierarchy>"""
        return """<hierarchy><node package="com.hexin.plat.android" resource-id="com.hexin.plat.android:id/frame_layout"><node resource-id="com.hexin.plat.android:id/page_title_view" text="国泰海通证券" /><node resource-id="com.hexin.plat.android:id/btn" text="买入" selected="false" bounds="[0,216][216,336]" /><node resource-id="com.hexin.plat.android:id/btn" text="撤单" selected="false" bounds="[432,216][648,336]" /><node resource-id="com.hexin.plat.android:id/btn" text="持仓" selected="true" bounds="[648,216][864,336]" /><node resource-id="com.hexin.plat.android:id/btn" text="查询" selected="false" bounds="[864,216][1080,336]" /></node></hierarchy>"""


class _FakeDispatcher:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def send(self, config: dict, title: str, body: str, *, only_channel: str | None = None) -> dict:
        del config, only_channel
        self.sent.append((title, body))
        return {"ok": True, "channels": {"bark": {"sent": 1}}, "errors": {}}


def _seed_bullish_160723(rstore: SQLiteResearchStore, trade_date: str = "20261126") -> None:
    rstore.save_push_config(
        {
            "enabled": True,
            "rules": [
                {
                    "id": "rule-macd-buy",
                    "enabled": True,
                    "side": "buy",
                    "title": "MACD大单买入",
                    "joiner": "and",
                    "conditions": [
                        {"id": "c1", "type": "code_filter", "codes": ["160723"]},
                        {"id": "c2", "type": "macd_signal", "signal": "red"},
                        {
                            "id": "c3",
                            "type": "flow_change_pct",
                            "flow": "big",
                            "minutes": 1,
                            "direction": "increase",
                            "value": 3.0,
                        },
                    ],
                }
            ],
        }
    )
    with rstore._lock, rstore._connection:
        rstore._connection.execute(
            """
            INSERT OR REPLACE INTO research_days (
                symbol, trade_date, name, market, source, pre_close,
                quote_price, quote_change_percent, collected_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("160723", trade_date, "嘉实原油", "36", "CORE", "2.100", "2.130", "+1.43%", "2026-11-26T10:05:00+08:00"),
        )
        # Construct points where final bar crosses DIFF > DEA upward and large_order_net jumps > 3%
        prices = [2.100] * 12 + [2.090, 2.080, 2.070, 2.060, 2.055, 2.050, 2.048, 2.046, 2.045, 2.045, 2.045, 2.046, 2.085]
        for idx, price in enumerate(prices):
            minute = f"{9 + (30 + idx) // 60:02d}:{(30 + idx) % 60:02d}"
            flow = 100.0 if idx < len(prices) - 1 else 250.0
            rstore._connection.execute(
                """
                INSERT OR REPLACE INTO research_points (
                    symbol, trade_date, minute, price, average_price, volume,
                    large_order_net, large_order_amount, retail_count, macdfs, captured_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("160723", trade_date, minute, f"{price:.3f}", f"{price:.3f}", "1000", f"{flow:.1f}", f"{flow:.1f}", "500", "0", "2026-11-26T10:05:00+08:00"),
            )


def test_default_config_slippage_and_session_windows(tmp_path: Path) -> None:
    store = SQLiteAutoTradeStore(tmp_path / "research.db")
    cfg = store.get_config()
    assert cfg["enabled"] is True
    assert cfg["signal_source"] == "intraday_macd_flow"
    assert cfg["execution_mode"] == "semi_auto"
    assert cfg["symbols"] == ["160723"]
    assert cfg["order_quantity"] == 100
    assert cfg["slippage_ticks"] == 3
    assert cfg["auto_cancel_seconds"] == 30
    assert cfg["daily_max_buy_amount"] == 50000.0
    assert cfg["block_non_continuous_session"] is True
    store.close()

    assert compute_slippage_limit_price("160723", "36", "buy", "2.109", 3) == ("2.112", 3)
    assert compute_slippage_limit_price("160723", "36", "sell", "2.109", 3) == ("2.106", 3)
    assert compute_slippage_limit_price("601872", "17", "buy", "15.29", 3) == ("15.32", 2)

    assert is_in_continuous_auction(datetime(2026, 11, 26, 9, 30, tzinfo=SHANGHAI_TZ))[0] is False
    assert is_in_continuous_auction(datetime(2026, 11, 26, 9, 31, tzinfo=SHANGHAI_TZ))[0] is True
    assert is_in_continuous_auction(datetime(2026, 11, 26, 11, 29, tzinfo=SHANGHAI_TZ))[0] is True
    assert is_in_continuous_auction(datetime(2026, 11, 26, 11, 30, tzinfo=SHANGHAI_TZ))[0] is False
    assert is_in_continuous_auction(datetime(2026, 11, 26, 13, 0, tzinfo=SHANGHAI_TZ))[0] is True
    assert is_in_continuous_auction(datetime(2026, 11, 26, 14, 55, tzinfo=SHANGHAI_TZ))[0] is True
    assert is_in_continuous_auction(datetime(2026, 11, 26, 14, 56, tzinfo=SHANGHAI_TZ))[0] is False
    assert is_in_continuous_auction(datetime(2026, 11, 26, 18, 50, tzinfo=SHANGHAI_TZ))[0] is False
    assert is_in_continuous_auction(datetime(2026, 11, 28, 10, 0, tzinfo=SHANGHAI_TZ))[0] is False


def test_simulate_blocks_live_outside_session_but_allows_dry_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("level2_service.ths_trade.time.sleep", lambda _s: None)
    db_path = tmp_path / "research.db"
    rstore = SQLiteResearchStore(db_path)
    astore = SQLiteAutoTradeStore(db_path)
    dispatcher = _FakeDispatcher()
    dev = _FakeU2Device()
    executor = TonghuashunTradeExecutor(
        adb_serial="emulator-5556",
        u2_factory=lambda _s: dev,
        name_to_symbol=lambda n: "160723" if "嘉实原油" in n else None,
    )
    # 18:55 CST (night session)
    engine = AutoTradeEngine(
        auto_store=astore,
        research_store=rstore,
        broker=None,
        trade_executor=executor,
        push_dispatcher=dispatcher,
        symbol_lookup=lambda s: CATALOG.get(s),
        now_fn=lambda: datetime(2026, 11, 25, 18, 55, tzinfo=SHANGHAI_TZ),
        epoch_fn=lambda: 1_800_000_000.0,
    )

    sim = asyncio.run(engine.simulate_signal(symbol="160723", side="buy"))
    sig = sim["signal"]
    assert sig["status"] == "PENDING_CONFIRM"
    assert sig["limit_price"] == "2.112"
    assert len(dispatcher.sent) == 1

    # Live approve MUST be blocked by continuous-auction gate and signal MUST stay PENDING_CONFIRM
    with pytest.raises(TradeExecutionError) as exc:
        asyncio.run(engine.decide_signal(sig["id"], action="approve", dry_run=False))
    assert exc.value.error_code == "TRADE_SESSION_BLOCKED"
    assert exc.value.status_code == 409
    assert astore.get_signal(sig["id"])["status"] == "PENDING_CONFIRM"

    # Dry-run approve succeeds even outside session
    dry = asyncio.run(engine.decide_signal(sig["id"], action="approve", dry_run=True))
    assert dry["signal"]["status"] == "DRY_RUN_CONFIRMED"
    assert dry["order_result"]["status"] == "DRY_RUN_CONFIRMED"
    rstore.close()
    astore.close()


def test_semi_auto_poll_detects_signal_approves_and_cancels_after_30s(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("level2_service.ths_trade.time.sleep", lambda _s: None)
    db_path = tmp_path / "research.db"
    rstore = SQLiteResearchStore(db_path)
    _seed_bullish_160723(rstore, "20261126")
    astore = SQLiteAutoTradeStore(db_path)
    dispatcher = _FakeDispatcher()
    dev = _FakeU2Device()
    executor = TonghuashunTradeExecutor(
        adb_serial="emulator-5556",
        u2_factory=lambda _s: dev,
        name_to_symbol=lambda n: "160723" if "嘉实原油" in n else None,
    )
    clock = {"now": 1_800_000_000.0}
    engine = AutoTradeEngine(
        auto_store=astore,
        research_store=rstore,
        broker=None,
        trade_executor=executor,
        push_dispatcher=dispatcher,
        symbol_lookup=lambda s: CATALOG.get(s),
        is_trading_day=lambda _d: True,
        now_fn=lambda: datetime(2026, 11, 26, 10, 5, tzinfo=SHANGHAI_TZ),
        epoch_fn=lambda: clock["now"],
    )

    poll1 = asyncio.run(engine.poll_once())
    assert poll1["triggered"] == 1
    assert len(dispatcher.sent) == 1
    # 2nd poll same minute is idempotent
    poll2 = asyncio.run(engine.poll_once())
    assert poll2["triggered"] == 0

    pending = astore.list_pending_signals()
    assert len(pending) == 1
    sig = pending[0]
    assert sig["limit_price"] == "2.088"

    # Human confirms buy in semi-auto mode
    approved = asyncio.run(engine.decide_signal(sig["id"], action="approve", dry_run=False))
    assert approved["signal"]["status"] == "SUBMITTED"
    assert approved["signal"]["cancel_pending"] is True
    assert dev.order_open is True

    # Before 30s passes, poll_once does NOT cancel
    clock["now"] += 20.0
    asyncio.run(engine.poll_once())
    assert dev.order_open is True

    # After 30s timeout passes, poll_once automatically cancels the unfilled order
    clock["now"] += 15.0
    asyncio.run(engine.poll_once())
    assert dev.order_open is False
    final_sig = astore.get_signal(sig["id"])
    assert final_sig is not None
    assert final_sig["status"] == "AUTO_CANCELLED_TIMEOUT"
    assert final_sig["cancel_pending"] is False
    rstore.close()
    astore.close()


def test_daily_buy_cap_circuit_breaker_and_bridge_routes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("level2_service.ths_trade.time.sleep", lambda _s: None)
    db_path = tmp_path / "research.db"
    rstore = SQLiteResearchStore(db_path)
    _seed_bullish_160723(rstore, "20261126")
    astore = SQLiteAutoTradeStore(db_path)
    astore.update_config({"daily_max_buy_amount": 150.0})

    class _AuthGateDevice(_FakeU2Device):
        def dump_hierarchy(self, compressed: bool = False) -> str:
            if self.screen == "buy":
                return '<hierarchy><node package="com.hexin.plat.android" text="请输入交易密码" password="true" /></hierarchy>'
            return super().dump_hierarchy(compressed=compressed)

    dev = _FakeU2Device()
    executor = TonghuashunTradeExecutor(
        adb_serial="emulator-5556",
        u2_factory=lambda _s: dev,
        name_to_symbol=lambda n: "160723" if "嘉实原油" in n else None,
    )
    engine = AutoTradeEngine(
        auto_store=astore,
        research_store=rstore,
        broker=None,
        trade_executor=executor,
        symbol_lookup=lambda s: CATALOG.get(s),
        is_trading_day=lambda _d: True,
        now_fn=lambda: datetime(2026, 11, 26, 10, 5, tzinfo=SHANGHAI_TZ),
        epoch_fn=lambda: 1_800_000_000.0,
    )

    # 100 shares @ 2.148 = 214.8 > 150.0 -> RISK_BLOCKED
    asyncio.run(engine.poll_once())
    recent = astore.list_recent_signals()
    assert recent[0]["status"] == "RISK_BLOCKED"

    # Raise daily cap via FastAPI bridge & test routes + auth gate
    app = create_app(
        admin_password_hash=PasswordHasher().hash("admin-pass-123"),
        admin_session_secret="a" * 32,
        symbol_lookup=lambda sym: CATALOG[sym],
        market_account_store=SQLiteMarketAccountStore(tmp_path / "market.db"),
        research_store=rstore,
        goodluck_internal_token=TOKEN,
        trade_executor=executor,
        auto_trade_engine=engine,
    )
    client = TestClient(app)
    assert client.get("/api/internal/goodluck/trade/strategy", headers=_headers(role="user")).status_code == 403

    put_res = client.put(
        "/api/internal/goodluck/trade/strategy",
        headers=_headers(role="admin"),
        json={
            "enabled": True,
            "signal_source": "intraday_macd_flow",
            "execution_mode": "semi_auto",
            "symbols": ["160723"],
            "order_quantity": 100,
            "slippage_ticks": 3,
            "auto_cancel_seconds": 30,
            "daily_max_buy_amount": 50000,
            "block_non_continuous_session": True,
        },
    )
    assert put_res.status_code == 200
    assert put_res.json()["config"]["daily_max_buy_amount"] == 50000.0

    sim_res = client.post(
        "/api/internal/goodluck/trade/strategy/simulate",
        headers=_headers(role="admin"),
        json={"symbol": "160723", "side": "buy"},
    )
    assert sim_res.status_code == 200
    sig_id = sim_res.json()["signal"]["id"]

    # Switch device to trigger BROKER_LOGIN_REQUIRED -> trips circuit breaker
    executor._device = _AuthGateDevice()
    blocked_res = client.post(
        f"/api/internal/goodluck/trade/strategy/signals/{sig_id}/decide",
        headers=_headers(role="admin"),
        json={"action": "approve", "dry_run": False},
    )
    assert blocked_res.status_code == 409
    assert client.get("/api/internal/goodluck/trade/strategy", headers=_headers(role="admin")).json()["config"]["circuit_open"] is True

    reset_res = client.post("/api/internal/goodluck/trade/strategy/reset-circuit", headers=_headers(role="admin"))
    assert reset_res.status_code == 200
    assert reset_res.json()["config"]["circuit_open"] is False
    rstore.close()
    astore.close()


def test_viewer_open_suppresses_external_push_and_resumes_when_closed(tmp_path: Path) -> None:
    db_path = tmp_path / "research.db"
    rstore = SQLiteResearchStore(db_path)
    _seed_bullish_160723(rstore, "20261126")
    astore = SQLiteAutoTradeStore(db_path)
    dispatcher = _FakeDispatcher()
    clock = {"now": 1_800_000_000.0}
    engine = AutoTradeEngine(
        auto_store=astore,
        research_store=rstore,
        broker=None,
        trade_executor=None,
        push_dispatcher=dispatcher,
        symbol_lookup=lambda s: CATALOG.get(s),
        is_trading_day=lambda _d: True,
        now_fn=lambda: datetime(2026, 11, 26, 10, 5, tzinfo=SHANGHAI_TZ),
        epoch_fn=lambda: clock["now"],
    )
    app = create_app(
        admin_password_hash=PasswordHasher().hash("admin-pass-123"),
        admin_session_secret="a" * 32,
        symbol_lookup=lambda sym: CATALOG[sym],
        market_account_store=SQLiteMarketAccountStore(tmp_path / "market.db"),
        research_store=rstore,
        goodluck_internal_token=TOKEN,
        auto_trade_engine=engine,
    )
    client = TestClient(app)

    # Admin page open -> viewer_open=1 marks viewer active and suppresses external push
    res_open = client.get("/api/internal/goodluck/trade/strategy?viewer_open=1", headers=_headers(role="admin"))
    assert res_open.status_code == 200
    assert res_open.json()["viewer_active"] is True

    poll_while_open = asyncio.run(engine.poll_once())
    assert poll_while_open["triggered"] == 1
    assert len(dispatcher.sent) == 0

    sim_while_open = client.post(
        "/api/internal/goodluck/trade/strategy/simulate",
        headers=_headers(role="admin"),
        json={"symbol": "160723", "side": "buy", "viewer_open": True},
    )
    assert sim_while_open.status_code == 200
    assert sim_while_open.json()["signal"]["status"] == "PENDING_CONFIRM"
    assert len(dispatcher.sent) == 0

    # Admin leaves/closes page -> viewer_open=0 (or TTL expiry) resumes external push
    res_closed = client.get("/api/internal/goodluck/trade/strategy?viewer_open=0", headers=_headers(role="admin"))
    assert res_closed.status_code == 200
    assert res_closed.json()["viewer_active"] is False

    sim_while_closed = client.post(
        "/api/internal/goodluck/trade/strategy/simulate",
        headers=_headers(role="admin"),
        json={"symbol": "160723", "side": "sell", "viewer_open": False},
    )
    assert sim_while_closed.status_code == 200
    assert len(dispatcher.sent) == 1

    # TTL expiration also restores external push automatically
    client.get("/api/internal/goodluck/trade/strategy?viewer_open=1", headers=_headers(role="admin"))
    assert engine.is_viewer_active() is True
    clock["now"] += 15.0
    assert engine.is_viewer_active() is False
    asyncio.run(engine.simulate_signal(symbol="160723", side="buy"))
    assert len(dispatcher.sent) == 2

    rstore.close()
    astore.close()


def test_counterparty_order_book_level3_pricing_and_confirm_refresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from level2_service.auto_trade import resolve_order_limit_price
    from level2_service.public_market import TencentPublicMarketProvider

    monkeypatch.setattr("level2_service.ths_trade.time.sleep", lambda _s: None)

    fields = [""] * 50
    fields[0] = "51"
    fields[1] = "嘉实原油LOF"
    fields[2] = "160723"
    fields[3] = "2.109"
    fields[4] = "2.100"
    fields[5] = "2.102"
    fields[9:19] = ["2.108", "1386", "2.107", "341", "2.105", "332", "2.104", "757", "2.103", "155"]
    fields[19:29] = ["2.109", "499", "2.111", "93", "2.114", "95", "2.116", "608", "2.118", "56"]
    fields[30] = "20261125143512"
    fields[47] = "2.310"
    fields[48] = "1.890"
    sample_quote = f'v_sz160723="{"~".join(fields)}";'.encode("gb18030")

    provider = TencentPublicMarketProvider(fetch=lambda _url, _t: sample_quote)
    book = provider.read_order_book(CATALOG["160723"])
    assert book["last_price"] == "2.109"
    assert book["source_time"] == "14:35:12"
    assert book["asks"][2] == {"level": 3, "label": "卖3", "price": "2.114", "volume_lots": 95}
    assert book["bids"][2] == {"level": 3, "label": "买3", "price": "2.105", "volume_lots": 332}

    # Gapped asks: Buy targets 卖3 (2.114), NOT blind +0.003 (2.112 which wouldn't even hit 卖2/卖3)
    buy_p, prec, buy_meta = resolve_order_limit_price(
        "160723", "36", "buy", "2.109", pricing_mode="counterparty_book", order_book_level=3, order_book=book
    )
    assert prec == 3
    assert buy_p == "2.114"
    assert "卖3" in buy_meta["price_basis"]

    # Gapped bids: Sell targets 买3 (2.105)
    sell_p, _, sell_meta = resolve_order_limit_price(
        "160723", "36", "sell", "2.109", pricing_mode="counterparty_book", order_book_level=3, order_book=book
    )
    assert sell_p == "2.105"
    assert "买3" in sell_meta["price_basis"]

    # Protective slippage cap prevents extreme thin-book price runaway
    extreme_book = {
        **book,
        "asks": [
            {"level": 1, "label": "卖1", "price": "2.120", "volume_lots": 10},
            {"level": 2, "label": "卖2", "price": "2.150", "volume_lots": 10},
            {"level": 3, "label": "卖3", "price": "2.250", "volume_lots": 10},
            {"level": 4, "label": "卖4", "price": None, "volume_lots": 0},
            {"level": 5, "label": "卖5", "price": None, "volume_lots": 0},
        ],
    }
    capped_p, _, capped_meta = resolve_order_limit_price(
        "160723",
        "36",
        "buy",
        "2.100",
        pricing_mode="counterparty_book",
        order_book_level=3,
        max_slippage_pct=1.5,
        order_book=extreme_book,
    )
    assert capped_p == "2.132"  # 2.100 * 1.015 = 2.1315 -> 2.132
    assert capped_meta["capped"] is True

    # Empty upper levels fall back to highest valid ask <= target level (卖2)
    partial_book = {
        **book,
        "asks": [
            {"level": 1, "label": "卖1", "price": "2.290", "volume_lots": 20},
            {"level": 2, "label": "卖2", "price": "2.300", "volume_lots": 30},
            {"level": 3, "label": "卖3", "price": None, "volume_lots": 0},
            {"level": 4, "label": "卖4", "price": None, "volume_lots": 0},
            {"level": 5, "label": "卖5", "price": None, "volume_lots": 0},
        ],
    }
    fb_p, _, fb_meta = resolve_order_limit_price(
        "160723", "36", "buy", "2.288", pricing_mode="counterparty_book", order_book_level=3, order_book=partial_book
    )
    assert fb_p == "2.300"
    assert fb_meta["applied_level"] == 2

    # Confirm-time live order book refresh + override_book_level=2 + /trade/order-book endpoint
    db_path = tmp_path / "research.db"
    rstore = SQLiteResearchStore(db_path)
    astore = SQLiteAutoTradeStore(db_path)
    dev = _FakeU2Device()
    executor = TonghuashunTradeExecutor(
        adb_serial="emulator-5556",
        u2_factory=lambda _s: dev,
        name_to_symbol=lambda n: "160723" if "嘉实原油" in n else None,
    )
    current_book = {"val": book}
    shifted_book = {
        **book,
        "last_price": "2.115",
        "asks": [
            {"level": 1, "label": "卖1", "price": "2.116", "volume_lots": 88},
            {"level": 2, "label": "卖2", "price": "2.118", "volume_lots": 77},
            {"level": 3, "label": "卖3", "price": "2.121", "volume_lots": 66},
            {"level": 4, "label": "卖4", "price": "2.122", "volume_lots": 55},
            {"level": 5, "label": "卖5", "price": "2.124", "volume_lots": 44},
        ],
    }
    engine = AutoTradeEngine(
        auto_store=astore,
        research_store=rstore,
        broker=None,
        trade_executor=executor,
        symbol_lookup=lambda s: CATALOG.get(s),
        is_trading_day=lambda _d: True,
        now_fn=lambda: datetime(2026, 11, 26, 10, 5, tzinfo=SHANGHAI_TZ),
        epoch_fn=lambda: 1_800_000_000.0,
        order_book_fetcher=lambda _sym, _mkt=None: current_book["val"],
    )
    app = create_app(
        admin_password_hash=PasswordHasher().hash("admin-pass-123"),
        admin_session_secret="a" * 32,
        symbol_lookup=lambda sym: CATALOG[sym],
        market_account_store=SQLiteMarketAccountStore(tmp_path / "market.db"),
        research_store=rstore,
        goodluck_internal_token=TOKEN,
        trade_executor=executor,
        auto_trade_engine=engine,
    )
    client = TestClient(app)

    ob_res = client.get("/api/internal/goodluck/trade/order-book/160723", headers=_headers(role="admin"))
    assert ob_res.status_code == 200
    assert ob_res.json()["asks"][2]["price"] == "2.114"

    # Simulated buy signal initially gets 卖3 = 2.114
    sim = client.post(
        "/api/internal/goodluck/trade/strategy/simulate",
        headers=_headers(role="admin"),
        json={"symbol": "160723", "side": "buy", "viewer_open": True},
    ).json()
    assert sim["signal"]["limit_price"] == "2.114"
    assert sim["strategy"]["config"]["pricing_mode"] == "counterparty_book"
    assert sim["strategy"]["config"]["order_book_level"] == 3

    # Order book moves higher before user clicks confirm, and user picks 卖2 (override_book_level=2 -> 2.118)
    current_book["val"] = shifted_book
    dec = client.post(
        f"/api/internal/goodluck/trade/strategy/signals/{sim['signal']['id']}/decide",
        headers=_headers(role="admin"),
        json={"action": "approve", "dry_run": False, "override_book_level": 2},
    )
    assert dec.status_code == 200
    assert dec.json()["signal"]["limit_price"] == "2.118"
    assert "卖2" in dec.json()["signal"]["price_basis"]
    assert dev.form_price == "2.118"

    rstore.close()
    astore.close()


def test_semi_auto_signal_triggers_background_prestage_and_warm_keepalive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("level2_service.ths_trade.time.sleep", lambda _s: None)

    async def _run() -> None:
        db_path = tmp_path / "research.db"
        rstore = SQLiteResearchStore(db_path)
        astore = SQLiteAutoTradeStore(db_path)
        dev = _FakeU2Device(screen="holdings", form_code="600000", form_price="10.00", form_qty="200")
        base_executor = TonghuashunTradeExecutor(
            adb_serial="emulator-5556",
            u2_factory=lambda _s: dev,
            name_to_symbol=lambda n: "160723" if "嘉实原油" in n else None,
        )
        warm_calls = {"count": 0}
        orig_keep_warm = base_executor.keep_warm

        def _wrapped_keep_warm(*, side: str = "buy") -> dict:
            warm_calls["count"] += 1
            return orig_keep_warm(side="sell" if side == "sell" else "buy")

        monkeypatch.setattr(base_executor, "keep_warm", _wrapped_keep_warm)
        clock = {"now": 1_800_000_000.0}
        engine = AutoTradeEngine(
            auto_store=astore,
            research_store=rstore,
            broker=None,
            trade_executor=base_executor,
            symbol_lookup=lambda s: CATALOG.get(s),
            is_trading_day=lambda _d: True,
            now_fn=lambda: datetime(2026, 11, 26, 10, 5, tzinfo=SHANGHAI_TZ),
            epoch_fn=lambda: clock["now"],
        )

        # Simulate signal starts on holdings page -> schedules async prestage to fill buy form
        sim = await engine.simulate_signal(symbol="160723", side="buy", viewer_open=True)
        if engine._bg_tasks:
            await asyncio.gather(*list(engine._bg_tasks), return_exceptions=True)

        assert dev.screen == "buy"
        assert dev.form_code == "160723"
        assert dev.form_price == "2.112"
        assert dev.form_qty == "100"
        assert warm_calls["count"] == 0

        # Confirm hits prestaged form (0 fill) and fast-reconcile (stays on buy screen)
        dec = await engine.decide_signal(sim["signal"]["id"], action="approve", dry_run=False)
        assert dec["signal"]["status"] == "SUBMITTED"
        assert dec["order_result"]["prestaged"] is True
        assert dec["order_result"]["reconcile_mode"] == "fast"
        assert dec["order_result"]["timings_ms"]["prestaged_hit"] == 1
        assert dev.screen == "buy"

        # Idle continuous-auction poll triggers keep_warm after 45s with no pending signals
        dev.screen = "holdings"
        clock["now"] += 50.0
        await engine.poll_once()
        if engine._bg_tasks:
            await asyncio.gather(*list(engine._bg_tasks), return_exceptions=True)
        assert warm_calls["count"] == 1
        assert dev.screen == "buy"

        rstore.close()
        astore.close()

    asyncio.run(_run())


def test_model_auto_dynamic_shares_and_1453_flatten(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from level2_service.quant_rules import SQLiteQuantRuleStore

    monkeypatch.setattr("level2_service.ths_trade.time.sleep", lambda _s: None)
    db_path = tmp_path / "research.db"
    rstore = SQLiteResearchStore(db_path)
    _seed_bullish_160723(rstore, "20261126")
    qstore = SQLiteQuantRuleStore(db_path)
    qstore.upsert_rule(
        {
            "id": "ai-v3-dyn-t0",
            "name": "动态分档做T策略",
            "source": "ai",
            "symbols": ["160723"],
            "macd_settings": {"short": 10, "long": 20, "signal": 5, "marker_threshold": 0.00001},
            "buy_rule": {"title": "MACD买", "joiner": "and", "conditions": [{"type": "macd_signal", "signal": "red"}]},
            "sell_rule": {"title": "MACD卖", "joiner": "and", "conditions": [{"type": "macd_signal", "signal": "green"}]},
            "position_sizing": {
                "mode": "base_t0_close",
                "default_weight_pct": 50.0,
                "max_position_pct": 100.0,
                "max_daily_entries": 2,
                "close_flatten_time": "14:53",
                "conviction_tiers": [
                    {
                        "label": "强动量重仓60%",
                        "weight_pct": 60.0,
                        "joiner": "and",
                        "conditions": [{"type": "price_threshold", "direction": "gte", "value": 2.0}],
                    }
                ],
            },
        }
    )
    astore = SQLiteAutoTradeStore(db_path)
    astore.update_config(
        {
            "enabled": True,
            "signal_source": "quant_rule",
            "quant_rule_id": "ai-v3-dyn-t0",
            "execution_mode": "semi_auto",
            "sizing_mode": "model_auto",
            "symbols": ["160723"],
            "order_quantity": 100,
            "pricing_mode": "fixed_ticks",
            "slippage_ticks": 3,
            "daily_max_buy_amount": 10000.0,
            "block_non_continuous_session": True,
        }
    )

    clock_dt = {"dt": datetime(2026, 11, 26, 10, 5, tzinfo=SHANGHAI_TZ), "ep": 1_800_000_000.0}

    class _BiDirFakeDevice(_FakeU2Device):
        def click(self, x: int, y: int) -> None:
            if 216 <= x < 432 and 216 <= y <= 336:
                self.screen = "sell"
            elif self.screen == "sell" and 195 <= x <= 702 and 1007 <= y <= 1130:
                self.screen = "confirm"
            else:
                super().click(x, y)

        def dump_hierarchy(self, compressed: bool = False) -> str:
            if self.screen == "cancel" and not self.order_open:
                return """<hierarchy><node package="com.hexin.plat.android" resource-id="com.hexin.plat.android:id/frame_layout"><node resource-id="com.hexin.plat.android:id/btn" text="买入" selected="false" bounds="[0,216][216,336]" /><node resource-id="com.hexin.plat.android:id/btn" text="卖出" selected="false" bounds="[216,216][432,336]" /><node resource-id="com.hexin.plat.android:id/btn" text="撤单" selected="true" bounds="[432,216][648,336]" /><node resource-id="com.hexin.plat.android:id/btn" text="持仓" selected="false" bounds="[648,216][864,336]" /><node resource-id="com.hexin.plat.android:id/btn" text="查询" selected="false" bounds="[864,216][1080,336]" /><node resource-id="com.hexin.plat.android:id/empty_tips" text="当前无可撤委托单" /></node></hierarchy>"""
            if self.screen == "sell":
                self.screen = "buy"
                try:
                    xml_buy = super().dump_hierarchy(compressed)
                finally:
                    self.screen = "sell"
                return (
                    xml_buy.replace('text="买入" selected="true"', 'text="买入" selected="false"')
                    .replace(
                        'text="撤单" selected="false"',
                        'text="卖出" selected="true" bounds="[216,216][432,336]" /><node resource-id="com.hexin.plat.android:id/btn" text="撤单" selected="false"',
                    )
                    .replace('text="买 入"', 'text="卖 出"')
                )
            if self.screen == "buy":
                return super().dump_hierarchy(compressed).replace('<node resource-id="com.hexin.plat.android:id/btn" text="撤单"', '<node resource-id="com.hexin.plat.android:id/btn" text="卖出" selected="false" bounds="[216,216][432,336]" /><node resource-id="com.hexin.plat.android:id/btn" text="撤单"')
            if self.screen == "confirm":
                return f"""<hierarchy><node package="com.hexin.plat.android"><node text="委托确认" /><node text="证券代码 {self.form_code} 嘉实原油LOF" /><node text="委托价格 {self.form_price}" /><node text="委托数量 {self.form_qty}" /><node text="取消" clickable="true" bounds="[100,1200][400,1300]" /><node text="确认卖出" clickable="true" bounds="[600,1200][900,1300]" /></node></hierarchy>"""
            return super().dump_hierarchy(compressed)

    dev = _BiDirFakeDevice()
    executor = TonghuashunTradeExecutor(
        adb_serial="emulator-5556",
        u2_factory=lambda _s: dev,
        name_to_symbol=lambda n: "160723" if "嘉实原油" in n else None,
    )
    engine = AutoTradeEngine(
        auto_store=astore,
        research_store=rstore,
        broker=None,
        trade_executor=executor,
        symbol_lookup=lambda s: CATALOG.get(s),
        is_trading_day=lambda _d: True,
        now_fn=lambda: clock_dt["dt"],
        epoch_fn=lambda: clock_dt["ep"],
        quant_rule_store=qstore,
    )

    # Morning buy: budget = 10000 * 60% = 6000 @ 2.088 -> floor(6000 / (2.088*100))*100 = 2800 shares (NOT fixed 100)
    res_morning = asyncio.run(engine.poll_once())
    assert res_morning["triggered"] == 1
    pending = astore.list_pending_signals()
    assert len(pending) == 1
    sig = pending[0]
    assert sig["side"] == "buy"
    assert sig["quantity"] == 2800
    assert "强动量重仓60%" in sig["rule_summary"]

    # Confirm morning buy and mark exchange order filled (no longer open on cancel queue)
    approved = asyncio.run(engine.decide_signal(sig["id"], action="approve", dry_run=False))
    assert approved["signal"]["status"] == "SUBMITTED"
    assert dev.form_qty == "2800"
    dev.order_open = False

    # At 14:53, automatic tail flatten triggers sell of 2700 shares (retaining mandatory 100 shares)
    clock_dt["dt"] = datetime(2026, 11, 26, 14, 53, tzinfo=SHANGHAI_TZ)
    clock_dt["ep"] += 17280.0
    res_tail = asyncio.run(engine.poll_once())
    assert res_tail["triggered"] == 1
    pending_tail = astore.list_pending_signals()
    assert len(pending_tail) == 1
    tail_sig = pending_tail[0]
    assert tail_sig["side"] == "sell"
    assert tail_sig["quantity"] == 2700
    assert "尾盘平T恢复底仓" in tail_sig["rule_title"]

    rstore.close()
    qstore.close()
    astore.close()


def _setup_v9_plus_fixture(
    db_path: Path,
    *,
    execution_mode: str = "semi_auto",
    daily_max_buy_amount: float = 50000.0,
    pricing_mode: str = "fixed_ticks",
    slippage_ticks: int = 0,
    order_book_level: int = 3,
    order_book_fetcher: Any = None,
) -> dict[str, Any]:
    import json as _json
    import time as _time
    from level2_service.quant_rules import SQLiteQuantRuleStore
    from level2_service.v9_plus_engine import V9MacroDataStore

    rstore = SQLiteResearchStore(db_path)
    qstore = SQLiteQuantRuleStore(db_path)
    astore = SQLiteAutoTradeStore(db_path)

    # Seed macro cache: NAV=2.000, FX=6.70, OIL 2y [60.0 .. 90.0]
    V9MacroDataStore(rstore, allow_network=False)
    now_ts = _time.time()
    oil_rows = [
        ["20251101", int(datetime(2025, 11, 2, 6, 0, tzinfo=SHANGHAI_TZ).timestamp()), 60.0],
        ["20261125", int(datetime(2026, 11, 26, 6, 0, tzinfo=SHANGHAI_TZ).timestamp()), 90.0],
    ]
    with rstore._lock, rstore._connection:
        rstore._connection.execute(
            "INSERT OR REPLACE INTO quant_macro_cache VALUES (?, ?, ?)",
            ("nav_160723", _json.dumps({"20261124": 2.000, "20261125": 2.000}), now_ts),
        )
        rstore._connection.execute(
            "INSERT OR REPLACE INTO quant_macro_cache VALUES (?, ?, ?)",
            ("oil_daily_2y", _json.dumps(oil_rows), now_ts),
        )
        rstore._connection.execute(
            "INSERT OR REPLACE INTO quant_macro_cache VALUES (?, ?, ?)",
            ("fx_usdcny_series", _json.dumps({"ts": [int(datetime(2026, 11, 1, 9, 0, tzinfo=SHANGHAI_TZ).timestamp())], "px": [6.70]}), now_ts),
        )

    def _seed_day_pts(sym: str, td: str, pre: float, bars: list[tuple[str, float]], market: str = "36") -> None:
        with rstore._lock, rstore._connection:
            rstore._connection.execute(
                """INSERT OR REPLACE INTO research_days(
                       symbol, trade_date, name, market, source, pre_close, quote_price, collected_at
                   ) VALUES(?,?,?,?,?,?,?,?)""",
                (sym, td, sym, market, "TEST", f"{pre:.3f}", f"{bars[-1][1]:.3f}", f"{td}T15:00:00+08:00"),
            )
            rstore._connection.executemany(
                """INSERT OR REPLACE INTO research_points(
                       symbol, trade_date, minute, price, average_price, volume, captured_at
                   ) VALUES(?,?,?,?,?,?,?)""",
                [(sym, td, hhmm, f"{px:.3f}", f"{px:.3f}", "10000", f"{td}T15:00:00+08:00") for hhmm, px in bars],
            )

    def _hm(m_idx: int) -> str:
        tot = 9 * 60 + 31 + m_idx
        return f"{tot // 60:02d}:{tot % 60:02d}"

    _seed_day_pts("BRN0Y", "20261125", 70.0, [("09:30", 70.0), ("15:00", 70.0)], market="ICE")
    _seed_day_pts(
        "160723",
        "20261125",
        2.000,
        [(f"09:{31 + i:02d}", 2.000) for i in range(25)] + [("15:00", 2.000)],
    )

    brn_today = [("09:30", 70.0)] + [(_hm(i), 70.5 if i < 25 else 72.1) for i in range(35)]
    lof_today = [(_hm(i), 2.000) for i in range(29)] + [
        ("10:00", 1.995), ("10:01", 1.990), ("10:02", 1.986), ("10:03", 1.983), ("10:04", 1.981), ("10:05", 1.980)
    ]
    _seed_day_pts("BRN0Y", "20261126", 70.0, brn_today, market="ICE")
    _seed_day_pts("160723", "20261126", 2.000, lof_today)

    qstore.upsert_rule(
        {
            "id": "ai-160723-v9-plus-aggressive",
            "name": "V9+ 进取版",
            "source": "ai",
            "symbols": ["160723", "BRN0Y"],
            "macd_settings": {"short": 10, "long": 20, "signal": 5, "marker_threshold": 0.0002},
            "buy_rule": {"title": "正T买", "joiner": "and", "conditions": [{"type": "brent_spread_pct", "direction": "lte", "value": -0.25}]},
            "sell_rule": {"title": "反T卖", "joiner": "and", "conditions": [{"type": "brent_spread_pct", "direction": "gte", "value": 0.42}]},
            "position_sizing": {
                "mode": "cash_pool_rolling",
                "engine_profile": "v9_plus_aggressive",
                "default_weight_pct": 80.0,
                "max_position_pct": 100.0,
                "max_daily_entries": 8,
                "cooldown_minutes": 4,
                "close_flatten_time": "14:53",
            },
        }
    )
    astore.update_config(
        {
            "enabled": True,
            "signal_source": "quant_rule",
            "quant_rule_id": "ai-160723-v9-plus-aggressive",
            "execution_mode": execution_mode,
            "sizing_mode": "model_auto",
            "symbols": ["160723"],
            "pricing_mode": pricing_mode,
            "order_book_level": order_book_level,
            "slippage_ticks": slippage_ticks,
            "daily_max_buy_amount": daily_max_buy_amount,
            "block_non_continuous_session": True,
        }
    )

    clock_dt = {"dt": datetime(2026, 11, 26, 10, 5, tzinfo=SHANGHAI_TZ), "ep": 1_800_000_000.0}

    class _BiDirFakeDevice(_FakeU2Device):
        def click(self, x: int, y: int) -> None:
            if 216 <= x < 432 and 216 <= y <= 336:
                self.screen = "sell"
            elif self.screen == "sell" and 195 <= x <= 702 and 1007 <= y <= 1130:
                self.screen = "confirm"
            else:
                super().click(x, y)

        def dump_hierarchy(self, compressed: bool = False) -> str:
            if self.screen == "cancel" and not self.order_open:
                return """<hierarchy><node package="com.hexin.plat.android" resource-id="com.hexin.plat.android:id/frame_layout"><node resource-id="com.hexin.plat.android:id/btn" text="买入" selected="false" bounds="[0,216][216,336]" /><node resource-id="com.hexin.plat.android:id/btn" text="卖出" selected="false" bounds="[216,216][432,336]" /><node resource-id="com.hexin.plat.android:id/btn" text="撤单" selected="true" bounds="[432,216][648,336]" /><node resource-id="com.hexin.plat.android:id/btn" text="持仓" selected="false" bounds="[648,216][864,336]" /><node resource-id="com.hexin.plat.android:id/btn" text="查询" selected="false" bounds="[864,216][1080,336]" /><node resource-id="com.hexin.plat.android:id/empty_tips" text="当前无可撤委托单" /></node></hierarchy>"""
            if self.screen == "sell":
                self.screen = "buy"
                try:
                    xml_buy = super().dump_hierarchy(compressed)
                finally:
                    self.screen = "sell"
                return (
                    xml_buy.replace('text="买入" selected="true"', 'text="买入" selected="false"')
                    .replace(
                        'text="撤单" selected="false"',
                        'text="卖出" selected="true" bounds="[216,216][432,336]" /><node resource-id="com.hexin.plat.android:id/btn" text="撤单" selected="false"',
                    )
                    .replace('text="买 入"', 'text="卖 出"')
                )
            if self.screen == "buy":
                return super().dump_hierarchy(compressed).replace('<node resource-id="com.hexin.plat.android:id/btn" text="撤单"', '<node resource-id="com.hexin.plat.android:id/btn" text="卖出" selected="false" bounds="[216,216][432,336]" /><node resource-id="com.hexin.plat.android:id/btn" text="撤单"')
            if self.screen == "confirm":
                return f"""<hierarchy><node package="com.hexin.plat.android"><node text="委托确认" /><node text="证券代码 {self.form_code} 嘉实原油LOF" /><node text="委托价格 {self.form_price}" /><node text="委托数量 {self.form_qty}" /><node text="取消" clickable="true" bounds="[100,1200][400,1300]" /><node text="确认卖出" clickable="true" bounds="[600,1200][900,1300]" /></node></hierarchy>"""
            return super().dump_hierarchy(compressed)

    dev = _BiDirFakeDevice()
    executor = TonghuashunTradeExecutor(
        adb_serial="emulator-5556",
        u2_factory=lambda _s: dev,
        name_to_symbol=lambda n: "160723" if "嘉实原油" in n else None,
    )
    engine = AutoTradeEngine(
        auto_store=astore,
        research_store=rstore,
        broker=None,
        trade_executor=executor,
        symbol_lookup=lambda s: CATALOG.get(s),
        is_trading_day=lambda _d: True,
        now_fn=lambda: clock_dt["dt"],
        epoch_fn=lambda: clock_dt["ep"],
        quant_rule_store=qstore,
        order_book_fetcher=order_book_fetcher,
    )
    return {
        "rstore": rstore,
        "qstore": qstore,
        "astore": astore,
        "dev": dev,
        "engine": engine,
        "clock_dt": clock_dt,
        "seed_day_pts": _seed_day_pts,
        "brn_today": brn_today,
        "lof_today": lof_today,
    }


def test_v9_plus_aggressive_live_entry_exit_t0_cap_and_close_rebalance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("THS_QUANT_OFFLINE", "1")
    monkeypatch.setattr("level2_service.ths_trade.time.sleep", lambda _s: None)
    ctx = _setup_v9_plus_fixture(tmp_path / "v9p_auto.db")
    rstore, qstore, astore = ctx["rstore"], ctx["qstore"], ctx["astore"]
    dev, engine, clock_dt = ctx["dev"], ctx["engine"], ctx["clock_dt"]
    _seed_day_pts = ctx["seed_day_pts"]
    brn_today, lof_today = ctx["brn_today"], ctx["lof_today"]

    # 1. 10:05 Positive-T buy (A-tier 100% of ¥50k @ 1.980 -> 25200 shares)
    res1 = asyncio.run(engine.poll_once())
    assert res1["triggered"] == 1
    p1 = astore.list_pending_signals()[0]
    assert p1["side"] == "buy"
    assert p1["quantity"] == 25200
    assert ":v9-entry:buy" in p1["signal_key"]
    asyncio.run(engine.decide_signal(p1["id"], action="approve", dry_run=False))
    dev.order_open = False

    # 2. 10:15 160723 jumps to 2.060 (sp_fx > 0.28) -> stateful :v9-exit:sell sells 25100 shares (retaining 100 shares)
    _seed_day_pts("BRN0Y", "20261126", 70.0, brn_today + [("10:15", 72.1)], market="ICE")
    _seed_day_pts("160723", "20261126", 2.000, lof_today + [(f"10:{m:02d}", 2.060) for m in range(6, 16)])
    clock_dt["dt"] = datetime(2026, 11, 26, 10, 15, tzinfo=SHANGHAI_TZ)
    clock_dt["ep"] += 600.0
    res2 = asyncio.run(engine.poll_once())
    assert res2["triggered"] == 1
    p2 = astore.list_pending_signals()[0]
    assert p2["side"] == "sell"
    assert p2["quantity"] == 25100
    assert ":v9-exit:sell" in p2["signal_key"]
    asyncio.run(engine.decide_signal(p2["id"], action="approve", dry_run=False))
    dev.order_open = False

    # 3. 13:20 Second Positive-T buy after cooldown: cumulative buy today already ¥49,896,
    # 100 retained shares remain -> buys 25100 shares up to ¥50k cap.
    _seed_day_pts("BRN0Y", "20261126", 70.0, brn_today + [("10:15", 72.1), ("13:20", 72.5)], market="ICE")
    extra_pts = [(f"10:{m:02d}", 2.040) for m in range(16, 30)] + [("13:18", 1.985), ("13:19", 1.982), ("13:20", 1.980)]
    _seed_day_pts("160723", "20261126", 2.000, lof_today + [(f"10:{m:02d}", 2.060) for m in range(6, 16)] + extra_pts)
    clock_dt["dt"] = datetime(2026, 11, 26, 13, 20, tzinfo=SHANGHAI_TZ)
    clock_dt["ep"] += 11100.0
    res3 = asyncio.run(engine.poll_once())
    assert res3["triggered"] == 1
    p3 = astore.list_pending_signals()[0]
    assert p3["side"] == "buy"
    assert p3["quantity"] == 25100
    asyncio.run(engine.decide_signal(p3["id"], action="approve", dry_run=False))
    dev.order_open = False

    # 4. 14:53 Brent spikes to 89.0 (brn_pos >= 0.96 >= 0.90) -> 3-state close clears down to mandatory 100 shares
    _seed_day_pts("BRN0Y", "20261126", 70.0, brn_today + [("10:15", 72.1), ("13:20", 72.5), ("14:53", 89.0)], market="ICE")
    _seed_day_pts(
        "160723",
        "20261126",
        2.000,
        lof_today + [(f"10:{m:02d}", 2.060) for m in range(6, 16)] + extra_pts + [("14:53", 2.010)],
    )
    clock_dt["dt"] = datetime(2026, 11, 26, 14, 53, tzinfo=SHANGHAI_TZ)
    clock_dt["ep"] += 5580.0
    res4 = asyncio.run(engine.poll_once())
    assert res4["triggered"] == 1
    p4 = astore.list_pending_signals()[0]
    assert p4["side"] == "sell"
    assert p4["quantity"] == 25100
    assert ":v9-close:sell" in p4["signal_key"]
    asyncio.run(engine.decide_signal(p4["id"], action="approve", dry_run=False))
    assert astore.rolling_symbol_position("160723", trade_date="20261126", same_day_turnaround=True)["net_shares"] == 100

    rstore.close()
    qstore.close()
    astore.close()


def test_v9_plus_full_auto_100k_slippage_clamp_and_three_state_close(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("THS_QUANT_OFFLINE", "1")
    monkeypatch.setattr("level2_service.ths_trade.time.sleep", lambda _s: None)

    book_state = {
        "last_price": "1.980",
        "market": "36",
        "bids": [
            {"level": 1, "label": "买1", "price": "1.979", "volume_lots": 100},
            {"level": 2, "label": "买2", "price": "1.978", "volume_lots": 200},
            {"level": 3, "label": "买3", "price": "1.976", "volume_lots": 300},
        ],
        "asks": [
            {"level": 1, "label": "卖1", "price": "1.981", "volume_lots": 100},
            {"level": 2, "label": "卖2", "price": "1.982", "volume_lots": 200},
            {"level": 3, "label": "卖3", "price": "1.983", "volume_lots": 300},
        ],
    }
    ctx = _setup_v9_plus_fixture(
        tmp_path / "v9p_full_auto_100k.db",
        execution_mode="full_auto",
        daily_max_buy_amount=100000.0,
        pricing_mode="counterparty_book",
        slippage_ticks=3,
        order_book_level=3,
        order_book_fetcher=lambda _sym, _mkt=None: book_state,
    )
    rstore, qstore, astore = ctx["rstore"], ctx["qstore"], ctx["astore"]
    dev, engine, clock_dt = ctx["dev"], ctx["engine"], ctx["clock_dt"]
    _seed_day_pts = ctx["seed_day_pts"]
    brn_today, lof_today = ctx["brn_today"], ctx["lof_today"]

    # 1. 10:05 Full-Auto A-tier 100% buy: signal price 1.980 -> qty_override 50500 (¥99,990),
    # live Ask-3 is 1.983 -> 50500*1.983 = ¥100,141.50 > ¥100,000!
    # AutoTradeEngine must clamp qty down to 50400 (¥99,943.20 <= ¥100,000) and SUBMIT cleanly (NO RISK_BLOCKED).
    res1 = asyncio.run(engine.poll_once())
    assert res1["triggered"] == 1
    assert astore.list_pending_signals() == []
    rec1 = astore.list_recent_signals(limit=5)[0]
    assert rec1["status"] == "SUBMITTED"
    assert rec1["side"] == "buy"
    assert rec1["limit_price"] == "1.983"
    assert rec1["quantity"] == 50400
    assert rec1["estimated_amount"] == pytest.approx(99943.20)
    assert dev.form_qty == "50400"
    dev.order_open = False

    # 2. 10:15 Price spikes to 2.060 (Bid-3 = 2.057) -> Full-Auto autonomously sells 50,300 shares (retaining 100)
    book_state["last_price"] = "2.060"
    book_state["bids"][2]["price"] = "2.057"
    _seed_day_pts("BRN0Y", "20261126", 70.0, brn_today + [("10:15", 72.1)], market="ICE")
    _seed_day_pts("160723", "20261126", 2.000, lof_today + [(f"10:{m:02d}", 2.060) for m in range(6, 16)])
    clock_dt["dt"] = datetime(2026, 11, 26, 10, 15, tzinfo=SHANGHAI_TZ)
    clock_dt["ep"] += 600.0
    res2 = asyncio.run(engine.poll_once())
    assert res2["triggered"] == 1
    rec2 = astore.list_recent_signals(limit=5)[0]
    assert rec2["status"] == "SUBMITTED"
    assert rec2["side"] == "sell"
    assert rec2["quantity"] == 50300
    assert rec2["limit_price"] == "2.057"
    assert astore.rolling_symbol_position("160723", trade_date="20261126", same_day_turnaround=True)["net_shares"] == 100
    dev.order_open = False

    # 3. 14:53 BRN steady & deep discount (1.980 vs BRN 72.1) -> 100% overnight carry re-buy (+50,300 -> 50,400)
    # Test confirm-time Ask-3 slippage bump inside _run_trade_for_signal (1.983 -> 1.985)
    book_state["last_price"] = "1.980"
    book_state["asks"][2]["price"] = "1.983"
    _seed_day_pts("BRN0Y", "20261126", 70.0, brn_today + [("10:15", 72.1), ("14:53", 72.1)], market="ICE")
    _seed_day_pts(
        "160723",
        "20261126",
        2.000,
        lof_today + [(f"10:{m:02d}", 2.060) for m in range(6, 16)] + [("14:53", 1.980)],
    )
    clock_dt["dt"] = datetime(2026, 11, 26, 14, 53, tzinfo=SHANGHAI_TZ)
    clock_dt["ep"] += 16680.0
    res3 = asyncio.run(engine.poll_once())
    assert res3["triggered"] == 1
    rec3 = astore.list_recent_signals(limit=5)[0]
    assert rec3["status"] == "SUBMITTED"
    assert ":v9-close:buy" in rec3["signal_key"]
    assert rec3["quantity"] == 50300
    assert astore.rolling_symbol_position("160723", trade_date="20261126", same_day_turnaround=True)["net_shares"] == 50400

    # Verify read-before-write expire_overdue_signals skips DB write when no pending signals due,
    # get_status() includes today_quant_signals, and evaluate_live_snapshot() separates buy/sell conditions.
    assert astore.expire_overdue_signals(clock_dt["ep"]) == 0
    status = engine.get_status()
    assert len(status["today_quant_signals"]) == 3
    live_eval = engine.evaluate_live_snapshot("160723")
    assert live_eval["symbol"] == "160723"
    assert live_eval["data_ready"] is True
    assert len(live_eval["market_indicators"]) >= 5
    assert isinstance(live_eval["buy"]["conditions"], list) and len(live_eval["buy"]["conditions"]) >= 6
    assert isinstance(live_eval["sell"]["conditions"], list) and len(live_eval["sell"]["conditions"]) >= 5

    rstore.close()
    qstore.close()
    astore.close()


def test_update_capital_cap_to_150k_uses_current_base_position_as_numerator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("THS_QUANT_OFFLINE", "1")
    ctx = _setup_v9_plus_fixture(
        tmp_path / "cap_150k_numerator.db",
        execution_mode="full_auto",
        daily_max_buy_amount=67000.0,
    )
    rstore, qstore, astore = ctx["rstore"], ctx["qstore"], ctx["astore"]
    engine, _seed_day_pts, brn_today, _ = ctx["engine"], ctx["seed_day_pts"], ctx["brn_today"], ctx["lof_today"]

    # Seed latest 160723 bar at 2.167 and prior-day settled 15,700-share base holding
    _seed_day_pts("160723", "20261126", 2.109, [("09:31", 2.165), ("10:20", 2.167)])
    assert astore.claim_and_insert_signal(
        signal_key="160723:20261125:15:00:initial-base-sync:buy",
        trade_date="20261125",
        symbol="160723",
        name="嘉实原油",
        side="buy",
        signal_source="quant_rule",
        rule_id="ai-160723-v9-plus-aggressive",
        rule_title="实盘初始底仓同步 (15,700股)",
        rule_summary="既有持仓同步",
        minute="15:00",
        signal_price="2.109",
        limit_price="2.109",
        precision=3,
        quantity=15700,
        execution_mode="full_auto",
        is_simulation=False,
        status="FILLED_OR_CLOSED",
        now_str="2026-11-25T15:00:00+08:00",
    )

    app = create_app(
        admin_password_hash=PasswordHasher().hash("admin-pass-123"),
        admin_session_secret="b" * 32,
        symbol_lookup=lambda sym: CATALOG[sym],
        market_account_store=SQLiteMarketAccountStore(tmp_path / "market.db"),
        research_store=rstore,
        goodluck_internal_token=TOKEN,
        trade_executor=engine.trade_executor,
        auto_trade_engine=engine,
    )
    client = TestClient(app)

    payload = {
        "enabled": True,
        "signal_source": "quant_rule",
        "quant_rule_id": "ai-160723-v9-plus-aggressive",
        "execution_mode": "full_auto",
        "sizing_mode": "model_auto",
        "symbols": ["160723"],
        "order_quantity": 100,
        "pricing_mode": "counterparty_book",
        "order_book_level": 2,
        "slippage_ticks": 3,
        "max_slippage_pct": 1.5,
        "refresh_book_on_confirm": True,
        "auto_cancel_seconds": 30,
        "daily_max_buy_amount": 150000,
        "block_non_continuous_session": True,
    }
    res1 = client.put("/api/internal/goodluck/trade/strategy", headers=_headers(role="admin"), json=payload)
    assert res1.status_code == 200
    d1 = res1.json()["daily_usage"]
    expected_mv = round(15700 * 2.167, 2)
    assert d1["buy_amount"] == pytest.approx(expected_mv)
    assert d1["today_buy_amount"] == 0.0
    assert d1["base_position_shares"] == 15700
    assert d1["base_position_amount"] == pytest.approx(expected_mv)
    assert d1["position_source"] == "strategy_ledger"
    assert d1["daily_max_buy_amount"] == 150000.0
    assert d1["remaining_amount"] == pytest.approx(round(150000.0 - expected_mv, 2))

    # Populate cached broker account snapshot (15,700 shares) + post-capture fill (+300 shares)
    with engine.trade_executor._cache_lock:
        engine.trade_executor._last_account = {
            "device_serial": "emulator-5556",
            "broker_name": "国泰海通证券",
            "account_masked": "****9306",
            "logged_in": True,
            "metrics": {"available_cash": "116000.00"},
            "positions": [
                {
                    "symbol": "160723",
                    "name": "嘉实原油",
                    "market_value": "34021.90",
                    "shares": 15700,
                    "available_shares": 15700,
                    "current_price": "2.167",
                }
            ],
            "captured_at": "2026-11-26T10:15:00+08:00",
        }
        engine.trade_executor._last_account_at = 1_800_000_000.0

    # Signal placed after cached snapshot timestamp should reconcile delta (+300 -> 16,000 shares)
    assert astore.claim_and_insert_signal(
        signal_key="160723:20261126:10:18:v9-entry:buy",
        trade_date="20261126",
        symbol="160723",
        name="嘉实原油",
        side="buy",
        signal_source="quant_rule",
        rule_id="ai-160723-v9-plus-aggressive",
        rule_title="正T补仓",
        rule_summary="低吸补仓300股",
        minute="10:18",
        signal_price="2.167",
        limit_price="2.167",
        precision=3,
        quantity=300,
        execution_mode="full_auto",
        is_simulation=False,
        status="SUBMITTED",
        now_str="2026-11-26T10:18:00+08:00",
    )

    res2 = client.get("/api/internal/goodluck/trade/strategy", headers=_headers(role="admin"))
    assert res2.status_code == 200
    d2 = res2.json()["daily_usage"]
    expected_mv2 = round(16000 * 2.167, 2)
    assert d2["position_source"] == "broker_account"
    assert d2["base_position_shares"] == 16000
    assert d2["buy_amount"] == pytest.approx(expected_mv2)
    assert d2["today_buy_amount"] == pytest.approx(round(300 * 2.167, 2))
    assert d2["remaining_amount"] == pytest.approx(round(150000.0 - expected_mv2, 2))

    # Fixed qty mode keeps today_buy_amount as daily_usage.buy_amount
    res3 = client.put(
        "/api/internal/goodluck/trade/strategy",
        headers=_headers(role="admin"),
        json={**payload, "sizing_mode": "fixed_qty"},
    )
    assert res3.status_code == 200
    d3 = res3.json()["daily_usage"]
    assert d3["buy_amount"] == pytest.approx(round(300 * 2.167, 2))
    assert d3["base_position_shares"] == 16000

    del brn_today
    rstore.close()
    qstore.close()
    astore.close()


def test_multi_symbol_independent_profiles_and_broker_reconcile(tmp_path) -> None:
    rstore = SQLiteResearchStore(tmp_path / "research.db")
    qstore = SQLiteQuantRuleStore(tmp_path / "quant.db")
    astore = SQLiteAutoTradeStore(tmp_path / "auto_trade.db")

    class ReconcileFakeExecutor:
        adb_serial = "emulator-5556"

        def __init__(self) -> None:
            self.calls: list[float | None] = []
            self.account_payload: dict[str, Any] = {
                "device_serial": "emulator-5556",
                "broker_name": "国泰海通证券",
                "account_masked": "****18",
                "logged_in": True,
                "metrics": {
                    "total_assets": "210,000.00",
                    "available_cash": "120,000.00",
                    "market_value": "90,000.00",
                },
                "positions": [
                    {
                        "symbol": "160723",
                        "name": "嘉实原油LOF",
                        "market_value": "34,022.00",
                        "shares": 15700,
                        "available_shares": 15700,
                        "current_price": "2.167",
                    },
                    {
                        "symbol": "501018",
                        "name": "南方原油LOF",
                        "market_value": "20,000.00",
                        "shares": 10000,
                        "available_shares": 10000,
                        "current_price": "2.000",
                    },
                ],
                "captured_at": "2026-11-26T10:48:00+08:00",
            }

        def get_account_summary(self, *, max_age_seconds: float | None = None) -> dict[str, Any]:
            self.calls.append(max_age_seconds)
            return dict(self.account_payload)

        def _cached_account_snapshot(self, *_args, **_kwargs) -> dict[str, Any]:
            return dict(self.account_payload)

    catalog_ext = {
        **CATALOG,
        "501018": SymbolLookup(symbol="501018", name="南方原油LOF", market="36"),
        "512480": SymbolLookup(symbol="512480", name="半导体ETF", market="36"),
    }
    executor = ReconcileFakeExecutor()
    engine = AutoTradeEngine(
        auto_store=astore,
        research_store=rstore,
        broker=None,
        trade_executor=executor,
        quant_rule_store=qstore,
        symbol_lookup=lambda sym: catalog_ext[sym],
        now_fn=lambda: datetime(2026, 11, 26, 10, 48, tzinfo=SHANGHAI_TZ),
    )
    app = create_app(
        admin_password_hash=PasswordHasher().hash("admin-pass-123"),
        admin_session_secret="c" * 32,
        symbol_lookup=lambda sym: catalog_ext[sym],
        market_account_store=SQLiteMarketAccountStore(tmp_path / "market.db"),
        research_store=rstore,
        goodluck_internal_token=TOKEN,
        trade_executor=executor,
        auto_trade_engine=engine,
    )
    client = TestClient(app)

    # 1) Configure 160723 (cap 80k -> occupies 45,978 cash)
    r1 = client.put(
        "/api/internal/goodluck/trade/strategy/symbols/160723",
        headers=_headers(role="admin"),
        json={
            "enabled": True,
            "signal_source": "quant_rule",
            "quant_rule_id": "preset-macd-flow-baseline",
            "sizing_mode": "model_auto",
            "capital_cap": 80000,
            "reconcile_broker": True,
        },
    )
    assert r1.status_code == 200
    assert executor.calls[-1] == 0.0

    # 2) Independently add 501018 (cap 50k -> holds 20k -> occupies 30k cash) without touching 160723 rule/cap
    r2 = client.put(
        "/api/internal/goodluck/trade/strategy/symbols/501018",
        headers=_headers(role="admin"),
        json={
            "enabled": True,
            "signal_source": "intraday_macd_flow",
            "sizing_mode": "model_auto",
            "capital_cap": 50000,
            "reconcile_broker": True,
        },
    )
    assert r2.status_code == 200
    body2 = r2.json()
    assert body2["config"]["symbol_profiles"]["160723"]["capital_cap"] == 80000.0
    assert body2["config"]["symbol_profiles"]["160723"]["quant_rule_id"] == "preset-macd-flow-baseline"
    assert body2["config"]["symbol_profiles"]["501018"]["capital_cap"] == 50000.0
    assert body2["config"]["symbol_profiles"]["501018"]["signal_source"] == "intraday_macd_flow"
    ledger2 = body2["capital_ledger"]
    assert ledger2["available_cash"] == pytest.approx(120000.0)
    assert ledger2["total_occupied_cash"] == pytest.approx(75978.0)
    assert ledger2["unallocated_cash"] == pytest.approx(44022.0)
    assert ledger2["non_quant_holding_market_value"] == pytest.approx(35978.0)
    assert ledger2["conservation_ok"] is True

    # 3) Partial update of 501018 capital_cap only preserves signal_source without clearing
    r2_partial = client.put(
        "/api/internal/goodluck/trade/strategy/symbols/501018",
        headers=_headers(role="admin"),
        json={"enabled": True, "capital_cap": 50000, "reconcile_broker": True},
    )
    assert r2_partial.status_code == 200
    assert r2_partial.json()["config"]["symbol_profiles"]["501018"]["signal_source"] == "intraday_macd_flow"

    # 4) Reject cap lower than broker holding value
    r_low = client.put(
        "/api/internal/goodluck/trade/strategy/symbols/160723",
        headers=_headers(role="admin"),
        json={"enabled": True, "capital_cap": 30000, "reconcile_broker": True},
    )
    assert r_low.status_code == 409
    assert r_low.json()["detail"]["error_code"] == "TRADE_CAP_BELOW_HOLDING"

    # 5) Reject over-allocation for 512480 (60k > remaining 44,022 cash)
    r_over = client.put(
        "/api/internal/goodluck/trade/strategy/symbols/512480",
        headers=_headers(role="admin"),
        json={"enabled": True, "sizing_mode": "model_auto", "capital_cap": 60000, "reconcile_broker": True},
    )
    assert r_over.status_code == 409
    assert r_over.json()["detail"]["error_code"] == "TRADE_CASH_OVER_ALLOCATED"

    # 6) Save exact remaining allocatable cash (44,022) succeeds and brings unallocated cash to 0
    r_ok = client.put(
        "/api/internal/goodluck/trade/strategy/symbols/512480",
        headers=_headers(role="admin"),
        json={"enabled": True, "sizing_mode": "model_auto", "capital_cap": 44022, "reconcile_broker": True},
    )
    assert r_ok.status_code == 200
    assert r_ok.json()["capital_ledger"]["unallocated_cash"] == pytest.approx(0.0)
    assert r_ok.json()["account_snapshot"]["logged_in"] is True

    # 7) Pausing 512480 frees 44,022 cash back to the pool; deleting 512480 & 501018 leaves last symbol protected
    r_pause = client.put(
        "/api/internal/goodluck/trade/strategy/symbols/512480",
        headers=_headers(role="admin"),
        json={"enabled": False, "reconcile_broker": True},
    )
    assert r_pause.status_code == 200
    assert r_pause.json()["capital_ledger"]["unallocated_cash"] == pytest.approx(44022.0)

    # Verify evaluate_live_snapshot isolates strategy per symbol cleanly
    ev_160723 = engine.evaluate_live_snapshot("160723")
    ev_501018 = engine.evaluate_live_snapshot("501018")
    assert ev_160723["signal_source"] == "quant_rule"
    assert ev_501018["signal_source"] == "intraday_macd_flow"

    # Verify poll_once skips paused symbol (512480 disabled) while routing per-symbol configs independently
    observed_evals: list[tuple[str, str, float]] = []

    def _spy_eval(*, symbol: str, config: dict[str, Any], trade_date: str) -> list[dict[str, Any]]:
        observed_evals.append((symbol, str(config["signal_source"]), float(config["daily_max_buy_amount"])))
        return []

    engine._evaluate_symbol_candidates = _spy_eval  # type: ignore[method-assign]
    poll_res = asyncio.run(engine.poll_once(force_session=True))
    assert poll_res == {"triggered": 0}
    assert observed_evals == [
        ("160723", "quant_rule", 80000.0),
        ("501018", "intraday_macd_flow", 50000.0),
    ]

    del1 = client.delete("/api/internal/goodluck/trade/strategy/symbols/512480", headers=_headers(role="admin"))
    assert del1.status_code == 200
    del2 = client.delete("/api/internal/goodluck/trade/strategy/symbols/501018", headers=_headers(role="admin"))
    assert del2.status_code == 200
    del_last = client.delete("/api/internal/goodluck/trade/strategy/symbols/160723", headers=_headers(role="admin"))
    assert del_last.status_code == 422

    rstore.close()
    qstore.close()
    astore.close()


def test_periodic_broker_sync_external_close_reconciliation_and_cash_cap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("THS_QUANT_OFFLINE", "1")
    ctx = _setup_v9_plus_fixture(
        tmp_path / "sync_cash_reconcile.db",
        execution_mode="full_auto",
        daily_max_buy_amount=150000.0,
    )
    rstore, qstore, astore = ctx["rstore"], ctx["qstore"], ctx["astore"]
    _seed_day_pts, brn_today, lof_today = ctx["seed_day_pts"], ctx["brn_today"], ctx["lof_today"]

    # Build a clean stub executor exposing get_account_summary, place_order, keep_warm
    class _SyncFakeExecutor:
        adb_serial = "emulator-5556"

        def __init__(self) -> None:
            self.summary_calls: list[float] = []
            self.warm_calls: int = 0
            self.orders_placed: list[dict[str, Any]] = []
            self.cached_snap: dict[str, Any] | None = None
            self.live_snap: dict[str, Any] = {
                "device_serial": "emulator-5556",
                "broker_name": "国泰海通证券",
                "account_masked": "****18",
                "logged_in": True,
                "metrics": {
                    "total_assets": "171047.52",
                    "available_cash": "30000.00",
                    "market_value": "17198.30",
                },
                "positions": [
                    {
                        "symbol": "160723",
                        "name": "嘉实原油",
                        "market_value": "15642.00",
                        "shares": 7900,
                        "available_shares": 7900,
                        "current_price": "1.980",
                    }
                ],
                "captured_at": "2026-11-26T14:52:00+08:00",
            }

        def _cached_account_snapshot(self, max_age_seconds: float = 0.0, *, allow_stale: bool = False) -> dict[str, Any] | None:
            del max_age_seconds, allow_stale
            import copy as _copy
            return _copy.deepcopy(self.cached_snap) if self.cached_snap is not None else None

        def get_account_summary(self, *, max_age_seconds: float = 0.0) -> dict[str, Any]:
            import copy as _copy
            self.summary_calls.append(max_age_seconds)
            self.cached_snap = _copy.deepcopy(self.live_snap)
            return _copy.deepcopy(self.live_snap)

        def keep_warm(self, *, side: str = "buy") -> dict[str, Any]:
            del side
            self.warm_calls += 1
            return {"ok": True}

        def place_order(self, **kwargs: Any) -> dict[str, Any]:
            self.orders_placed.append(dict(kwargs))
            return {
                "accepted": True,
                "status": "SUBMITTED",
                "entrust_no": f"HT{len(self.orders_placed):04d}",
                "dialog_message": "委托已提交",
            }

    # Prior settled 15,700 base shares + today's 11:08 sell 7,800 (net expected 7,900)
    assert astore.claim_and_insert_signal(
        signal_key="160723:20261125:15:00:initial-base:buy",
        trade_date="20261125",
        symbol="160723",
        name="嘉实原油",
        side="buy",
        signal_source="quant_rule",
        rule_id="ai-160723-v9-plus-aggressive",
        rule_title="初始底仓同步",
        rule_summary="15700股",
        minute="15:00",
        signal_price="2.000",
        limit_price="2.000",
        precision=3,
        quantity=15700,
        execution_mode="full_auto",
        is_simulation=False,
        status="FILLED_OR_CLOSED",
        now_str="2026-11-25T15:00:00+08:00",
    )
    assert astore.claim_and_insert_signal(
        signal_key="160723:20261126:11:08:v9-entry:sell",
        trade_date="20261126",
        symbol="160723",
        name="嘉实原油",
        side="sell",
        signal_source="quant_rule",
        rule_id="ai-160723-v9-plus-aggressive",
        rule_title="V9+ 反T高抛",
        rule_summary="卖7800股",
        minute="11:08",
        signal_price="2.060",
        limit_price="2.060",
        precision=3,
        quantity=7800,
        execution_mode="full_auto",
        is_simulation=False,
        status="FILLED_OR_CLOSED",
        now_str="2026-11-26T11:08:00+08:00",
    )

    executor = _SyncFakeExecutor()
    clock_dt = {"dt": datetime(2026, 11, 26, 13, 15, tzinfo=SHANGHAI_TZ), "ep": 1_800_000_000.0}
    engine = AutoTradeEngine(
        auto_store=astore,
        research_store=rstore,
        broker=None,
        trade_executor=executor,
        symbol_lookup=lambda s: CATALOG.get(s),
        is_trading_day=lambda _d: True,
        now_fn=lambda: clock_dt["dt"],
        epoch_fn=lambda: clock_dt["ep"],
        quant_rule_store=qstore,
    )

    # Neutral midday bars (last 2 bars 13:14..13:15 neutral so 10:05 dip never sits in candidate_bars)
    _seed_day_pts("BRN0Y", "20261126", 70.0, brn_today + [("13:14", 70.5), ("13:15", 70.5)], market="ICE")
    _seed_day_pts("160723", "20261126", 2.000, lof_today + [("13:14", 2.015), ("13:15", 2.015)])

    async def _run() -> None:
        # 1) First poll with empty cache automatically syncs account summary + warms buy tab
        await engine.poll_once()
        assert len(executor.summary_calls) == 1
        assert executor.summary_calls[0] == 0.0
        assert executor.warm_calls == 1
        st1 = engine.get_status()
        assert st1["daily_usage"]["broker_available_cash"] == pytest.approx(30000.0)
        assert st1["daily_usage"]["account_synced_at"] == "2026-11-26T14:52:00+08:00"
        assert st1["daily_usage"]["base_position_shares"] == 7900
        snap1 = engine.evaluate_live_snapshot("160723")
        assert snap1["diagnostics"]["open_leg_side"] == "sell"
        assert snap1["diagnostics"]["open_leg_qty"] == 7800

        # 2) Simulate 45s passing -> idle periodic sync refreshes & sees manual external buyback (7,900 -> 15,700)
        executor.live_snap["positions"][0]["shares"] = 15700
        executor.live_snap["positions"][0]["available_shares"] = 15700
        executor.live_snap["metrics"]["available_cash"] = "30000.00"
        executor.live_snap["captured_at"] = "2026-11-26T14:53:00+08:00"
        clock_dt["ep"] += 50.0
        await engine.poll_once()
        if engine._bg_tasks:
            await asyncio.gather(*list(engine._bg_tasks), return_exceptions=True)
        assert len(executor.summary_calls) == 2
        assert executor.summary_calls[1] == 40.0
        snap2 = engine.evaluate_live_snapshot("160723")
        assert snap2["diagnostics"]["open_leg_side"] is None

        # 3) At 14:53 close rebalance (deep discount -> 100% of ¥150k, i.e. ~60,000 shares headroom),
        # broker_available_cash=¥30,000 constrains close buy quantity safely below available_cash!
        _seed_day_pts("BRN0Y", "20261126", 70.0, brn_today + [("13:14", 70.5), ("13:15", 70.5), ("14:53", 72.1)], market="ICE")
        _seed_day_pts("160723", "20261126", 2.000, lof_today + [("13:14", 2.015), ("13:15", 2.015), ("14:53", 1.980)])
        clock_dt["dt"] = datetime(2026, 11, 26, 14, 53, tzinfo=SHANGHAI_TZ)
        clock_dt["ep"] += 5880.0
        res_close = await engine.poll_once()
        assert res_close["triggered"] == 1
        assert len(executor.orders_placed) == 1
        max_afford = int((30000.0 * 0.9995) // (1.980 * 100)) * 100
        assert executor.orders_placed[0]["side"] == "buy"
        assert executor.orders_placed[0]["quantity"] <= max_afford
        assert executor.orders_placed[0]["quantity"] == max_afford
        # Cancel background delayed post-trade sync before test teardown
        for t in list(engine._bg_tasks):
            t.cancel()
        if engine._bg_tasks:
            await asyncio.gather(*list(engine._bg_tasks), return_exceptions=True)

        # 4) Full external sell-out (positions=[] + valid available_cash) yields 0 shares instead of stale strategy ledger!
        executor.live_snap["positions"] = []
        executor.live_snap["metrics"]["available_cash"] = "175000.00"
        executor.cached_snap = dict(executor.live_snap)
        # No post-capture signal should be applied
        executor.live_snap["captured_at"] = "2026-11-26T15:00:00+08:00"
        executor.cached_snap = dict(executor.live_snap)
        pos_cleared = engine._effective_symbol_position("160723", trade_date="20261126", same_day_turnaround=True)
        assert pos_cleared["source"] == "broker_account"
        assert pos_cleared["net_shares"] == 0

    asyncio.run(_run())
    del brn_today
    rstore.close()
    qstore.close()
    astore.close()


def test_mandatory_100_share_retention_and_push_reliability(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("THS_QUANT_OFFLINE", "1")
    monkeypatch.setattr("level2_service.ths_trade.time.sleep", lambda _s: None)
    from level2_service.v9_plus_engine import evaluate_v9_plus_live_decision

    ctx = _setup_v9_plus_fixture(
        tmp_path / "retain_100_push.db",
        execution_mode="full_auto",
        daily_max_buy_amount=100000.0,
    )
    rstore, qstore, astore = ctx["rstore"], ctx["qstore"], ctx["astore"]
    _seed_day_pts, brn_today, lof_today = ctx["seed_day_pts"], ctx["brn_today"], ctx["lof_today"]

    # Seed neutral warmup + strong afternoon reverse-T spread spike (sp_fx > 0.60 -> A-tier 100% sell)
    brn_bars = brn_today + [("13:54", 68.0), ("13:55", 68.0)]
    lof_bars = lof_today + [("13:54", 2.190), ("13:55", 2.191)]
    _seed_day_pts("BRN0Y", "20261126", 70.0, brn_bars, market="ICE")
    _seed_day_pts("160723", "20261126", 2.000, lof_bars)

    rule = qstore.get_rule("ai-160723-v9-plus-aggressive")
    ps = rule["position_sizing"]

    # 1) Holding 15,700 shares: V9+ A-tier 100% reverse-T sell clamps strictly to 15,600 (retaining 100 shares)
    dec_sell = evaluate_v9_plus_live_decision(
        rstore,
        symbol="160723",
        trade_date="20261126",
        now_hhmm="13:55",
        cap_limit=100000.0,
        rule_spec=rule,
        pos_sizing=ps,
        today_signals=[],
        rolling_shares=15700,
        allow_network=False,
        broker_available_cash=150000.0,
    )
    assert len(dec_sell["intraday"]) == 1
    assert dec_sell["intraday"][0]["side"] == "sell"
    assert dec_sell["intraday"][0]["qty_override"] == 15600
    assert "保留100股底仓" in dec_sell["intraday"][0]["rule_summary"]

    # 2) Holding exactly 100 shares: V9+ blocks any further intraday sell, and TonghuashunTradeExecutor
    # raises TRADE_RETAIN_MIN_SHARES on direct/manual sell of those last 100 shares!
    dec_floor = evaluate_v9_plus_live_decision(
        rstore,
        symbol="160723",
        trade_date="20261126",
        now_hhmm="13:55",
        cap_limit=100000.0,
        rule_spec=rule,
        pos_sizing=ps,
        today_signals=[],
        rolling_shares=100,
        allow_network=False,
        broker_available_cash=150000.0,
    )
    assert dec_floor["intraday"] == []

    class _RetainSellFakeDevice(_FakeU2Device):
        def click(self, x: int, y: int) -> None:
            if 216 <= x < 432 and 216 <= y <= 336:
                self.screen = "sell"
            else:
                super().click(x, y)

        def dump_hierarchy(self, compressed: bool = False) -> str:
            if self.screen == "sell":
                self.screen = "buy"
                try:
                    xml_buy = super().dump_hierarchy(compressed)
                finally:
                    self.screen = "sell"
                return (
                    xml_buy.replace('text="买入" selected="true"', 'text="买入" selected="false"')
                    .replace(
                        'text="撤单" selected="false"',
                        'text="卖出" selected="true" bounds="[216,216][432,336]" /><node resource-id="com.hexin.plat.android:id/btn" text="撤单" selected="false"',
                    )
                    .replace('text="可买15000股"', 'text="可卖100股"')
                    .replace('text="买 入"', 'text="卖 出"')
                )
            if self.screen == "buy":
                return super().dump_hierarchy(compressed).replace('<node resource-id="com.hexin.plat.android:id/btn" text="撤单"', '<node resource-id="com.hexin.plat.android:id/btn" text="卖出" selected="false" bounds="[216,216][432,336]" /><node resource-id="com.hexin.plat.android:id/btn" text="撤单"')
            return super().dump_hierarchy(compressed)

    guard_dev = _RetainSellFakeDevice()
    guard_exec = TonghuashunTradeExecutor(
        adb_serial="emulator-5556",
        u2_factory=lambda _s: guard_dev,
        name_to_symbol=lambda n: "160723" if "嘉实原油" in n else None,
    )
    with guard_exec._cache_lock:
        guard_exec._last_account = {
            "logged_in": True,
            "metrics": {"available_cash": "170000.00"},
            "positions": [{"symbol": "160723", "name": "嘉实原油", "shares": 100, "available_shares": 100}],
            "captured_at": "2026-11-26T13:55:00+08:00",
        }
        guard_exec._last_account_at = 1_800_000_000.0
        guard_exec._last_account_date = guard_exec._today_shanghai_compact()

    with pytest.raises(TradeExecutionError) as err_floor:
        guard_exec.place_order(symbol="160723", expected_name="嘉实原油", side="sell", price="2.190", quantity=100, precision=3)
    assert err_floor.value.error_code == "TRADE_RETAIN_MIN_SHARES"

    # 3) Holding 0 shares at 14:53 when Brent is >= 90% (wt == 0% clear-out): V9+ + AutoTradeEngine
    # automatically buys back 100 shares ([尾盘三态调仓·保底留仓100股]), AND push strips asterisks/slashes,
    # retries failed channel once, and emits confirmation push on 30s fill!
    class _PushRetryDispatcher:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, str | None]] = []
            self.fail_sc3_once = True

        def send(self, config: dict[str, Any], title: str, body: str, *, only_channel: str | None = None) -> dict[str, Any]:
            del config
            self.calls.append((title, body, only_channel))
            if only_channel is None and self.fail_sc3_once:
                self.fail_sc3_once = False
                return {"ok": False, "channels": {"bark": {"sent": 1}}, "errors": {"sc3_bot": "SC3_SEND_FAILED"}}
            return {"ok": True, "channels": {"sc3_bot": {"sent": 1}}, "errors": {}}

    class _PushTestExecutor:
        adb_serial = "emulator-5556"

        def __init__(self) -> None:
            self.acct = {
                "logged_in": True,
                "metrics": {"available_cash": "171171.15", "total_assets": "200196.15"},
                "positions": [],
                "captured_at": "2026-11-26T14:52:50+08:00",
            }
            self.placed: list[dict[str, Any]] = []

        def _cached_account_snapshot(self, *_a: Any, **_k: Any) -> dict[str, Any]:
            return dict(self.acct)

        def get_account_summary(self, *_a: Any, **_k: Any) -> dict[str, Any]:
            return dict(self.acct)

        def place_order(self, **kwargs: Any) -> dict[str, Any]:
            self.placed.append(dict(kwargs))
            return {
                "accepted": True,
                "status": "SUBMITTED",
                "entrust_no": "9306",
                "dialog_message": "系统信息 | 委托已提交，合同号为：****8378 | 确定 | **9306 | 持仓/可用 | 成本/现价",
            }

        def cancel_order(self, **_kwargs: Any) -> dict[str, Any]:
            raise TradeExecutionError("TRADE_ORDER_NOT_FOUND", "当前撤单列表中未找到匹配的可撤委托", 404)

    _seed_day_pts("BRN0Y", "20261126", 70.0, brn_bars + [("14:53", 89.0)], market="ICE")
    _seed_day_pts("160723", "20261126", 2.000, lof_bars + [("14:53", 2.190)])

    dispatcher = _PushRetryDispatcher()
    push_exec = _PushTestExecutor()
    clock_dt = {"dt": datetime(2026, 11, 26, 14, 53, tzinfo=SHANGHAI_TZ), "ep": 1_800_000_000.0}
    engine = AutoTradeEngine(
        auto_store=astore,
        research_store=rstore,
        broker=None,
        trade_executor=push_exec,
        push_dispatcher=dispatcher,
        symbol_lookup=lambda s: CATALOG.get(s),
        is_trading_day=lambda _d: True,
        now_fn=lambda: clock_dt["dt"],
        epoch_fn=lambda: clock_dt["ep"],
        quant_rule_store=qstore,
    )

    async def _run_floor_and_push() -> None:
        res_close = await engine.poll_once()
        assert res_close["triggered"] == 1
        assert len(push_exec.placed) == 1
        assert push_exec.placed[0]["side"] == "buy"
        assert push_exec.placed[0]["quantity"] == 100

        # Immediate submit push drops rule-name prefix, includes plain meaning+why, cleans '*', '/', and retries failed channel once
        submit_calls = [c for c in dispatcher.calls if "量化自动买入：嘉实原油 (160723)" == c[0]]
        assert len(submit_calls) == 2
        assert submit_calls[0][2] is None
        assert submit_calls[1][2] == "sc3_bot"
        body_txt = submit_calls[0][1]
        assert "V9+ 进取版" not in body_txt
        assert "BRN0Y汇调溢价三态量化" not in body_txt
        assert "量化规则" not in body_txt
        assert "委托：" in body_txt
        assert "含义：" in body_txt
        assert "触发：" in body_txt
        assert "*" not in body_txt
        assert "/" not in body_txt
        assert "尾号8378" in body_txt

        # After 30s cancel check -> TRADE_ORDER_NOT_FOUND -> normal FILLED_OR_CLOSED is silent (NO confirmation push!)
        count_before_fill_check = len(dispatcher.calls)
        clock_dt["ep"] += 35.0
        await engine.poll_once()
        assert len(dispatcher.calls) == count_before_fill_check

        # Timeout auto-cancel after 30s DOES emit timeout push!
        push_exec.cancel_order = lambda **_kw: {"ok": True, "dialog_message": "撤单请求已提交"}  # type: ignore[method-assign]
        timeout_sig = astore.claim_and_insert_signal(
            signal_key="160723:20261126:14:54:timeout-test:buy",
            trade_date="20261126",
            symbol="160723",
            name="嘉实原油",
            side="buy",
            signal_source="quant_rule",
            rule_id="ai-160723-v9-plus-aggressive",
            rule_title="V9+",
            rule_summary="超时测试",
            minute="14:54",
            signal_price="2.190",
            limit_price="2.190",
            precision=3,
            quantity=100,
            execution_mode="full_auto",
            is_simulation=False,
            status="EXECUTING",
            now_str="2026-11-26T14:54:00+08:00",
        )
        assert timeout_sig is not None
        astore.update_signal_execution(
            timeout_sig["id"],
            status="SUBMITTED",
            cancel_pending=True,
            cancel_due_at=clock_dt["ep"] - 1.0,
            now_str="2026-11-26T14:54:00+08:00",
        )
        await engine.poll_once()
        timeout_calls = [c for c in dispatcher.calls if "量化委托超时撤单" in c[0]]
        assert len(timeout_calls) == 1
        assert "挂单满30秒未成交" in timeout_calls[0][1]

        for t in list(engine._bg_tasks):
            t.cancel()
        if engine._bg_tasks:
            await asyncio.gather(*list(engine._bg_tasks), return_exceptions=True)

    asyncio.run(_run_floor_and_push())
    del brn_today
    rstore.close()
    qstore.close()
    astore.close()


def test_bark_partial_failure_retries_only_failed_group(tmp_path: Path) -> None:
    db_path = tmp_path / "bark_retry_scope.db"
    rstore = SQLiteResearchStore(db_path)
    astore = SQLiteAutoTradeStore(db_path)
    rstore.save_push_config(
        {
            "enabled": True,
            "bark_groups": [
                {"id": "bark-group-1", "name": "Bark 组 1", "base_url": "https://api.day.app", "device_key": "dummy-key-1"},
                {"id": "bark-group-2", "name": "A佬", "base_url": "https://api.day.app", "device_key": "dummy-key-2"},
                {"id": "bark-3", "name": "Bark 3", "base_url": "https://api.day.app", "device_key": "dummy-key-3"},
            ],
            "sc3_bot": {"enabled": False},
            "wecom": {"enabled": False},
        }
    )

    class _BarkPartialFailDispatcher:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        def send(
            self,
            config: dict[str, Any],
            title: str,
            body: str,
            *,
            only_channel: str | None = None,
        ) -> dict[str, Any]:
            group_ids = [str(g.get("id") or "") for g in (config.get("bark_groups") or [])]
            self.calls.append(
                {
                    "title": title,
                    "body": body,
                    "only_channel": only_channel,
                    "group_ids": group_ids,
                }
            )
            if only_channel is None:
                return {
                    "ok": False,
                    "channels": {"bark": {"sent": 2, "total": 3}},
                    "errors": {"bark:bark-group-2": "BARK_SEND_FAILED"},
                }
            return {
                "ok": True,
                "channels": {"bark": {"sent": len(group_ids), "total": len(group_ids)}},
                "errors": {},
            }

    dispatcher = _BarkPartialFailDispatcher()
    engine = AutoTradeEngine(
        auto_store=astore,
        research_store=rstore,
        broker=None,
        trade_executor=None,
        push_dispatcher=dispatcher,
    )

    engine._notify_external_best_effort(
        "量化自动买入：嘉实原油 (160723)",
        "委托：限价 ¥2.174 × 100股（约 ¥217.40） · 委托已提交，合同号为：尾号3827",
    )

    assert len(dispatcher.calls) == 2
    assert dispatcher.calls[0]["only_channel"] is None
    assert dispatcher.calls[0]["group_ids"] == ["bark-group-1", "bark-group-2", "bark-3"]
    assert dispatcher.calls[1]["only_channel"] == "bark"
    assert dispatcher.calls[1]["group_ids"] == ["bark-group-2"]

    stored_ids = [g["id"] for g in rstore.push_config(public=False)["bark_groups"]]
    assert stored_ids == ["bark-group-1", "bark-group-2", "bark-3"]

    rstore.close()
    astore.close()


def test_concurrent_decide_signal_atomic_cap_prevents_overbuy(tmp_path: Path) -> None:
    import time

    db_path = tmp_path / "research.db"
    rstore = SQLiteResearchStore(db_path)
    astore = SQLiteAutoTradeStore(db_path)
    astore.update_config(
        {
            "enabled": True,
            "sizing_mode": "fixed_qty",
            "order_quantity": 500,
            "daily_max_buy_amount": 1500.0,
            "pricing_mode": "fixed_ticks",
            "block_non_continuous_session": False,
        }
    )

    class _SlowExecutor:
        adb_serial = "emulator-5556"
        active_tab = "buy"

        def __init__(self) -> None:
            self.calls = 0

        def place_order(self, **_kwargs):
            self.calls += 1
            time.sleep(0.05)
            return {
                "accepted": True,
                "status": "SUBMITTED",
                "entrust_no": f"HT{self.calls:04d}",
                "dialog_message": "委托已提交",
                "timings_ms": {"total": 50},
            }

    executor = _SlowExecutor()
    engine = AutoTradeEngine(
        auto_store=astore,
        research_store=rstore,
        broker=None,
        trade_executor=executor,
        symbol_lookup=lambda s: CATALOG.get(s),
        is_trading_day=lambda _d: True,
        now_fn=lambda: datetime(2026, 11, 26, 10, 5, tzinfo=SHANGHAI_TZ),
        epoch_fn=lambda: 1_800_000_000.0,
    )

    sig1 = astore.claim_and_insert_signal(
        signal_key="160723:20261126:10:05:r1:buy",
        trade_date="20261126",
        symbol="160723",
        name="嘉实原油",
        side="buy",
        signal_source="intraday_macd_flow",
        rule_id="r1",
        rule_title="买入1",
        rule_summary="并发风控1",
        minute="10:05",
        signal_price="2.100",
        limit_price="2.100",
        precision=3,
        quantity=500,
        execution_mode="semi_auto",
        is_simulation=False,
        status="PENDING_CONFIRM",
        expires_at=1_800_000_300.0,
    )
    sig2 = astore.claim_and_insert_signal(
        signal_key="160723:20261126:10:06:r2:buy",
        trade_date="20261126",
        symbol="160723",
        name="嘉实原油",
        side="buy",
        signal_source="intraday_macd_flow",
        rule_id="r2",
        rule_title="买入2",
        rule_summary="并发风控2",
        minute="10:06",
        signal_price="2.100",
        limit_price="2.100",
        precision=3,
        quantity=500,
        execution_mode="semi_auto",
        is_simulation=False,
        status="PENDING_CONFIRM",
        expires_at=1_800_000_300.0,
    )
    assert sig1 is not None and sig2 is not None

    async def _race():
        return await asyncio.gather(
            engine.decide_signal(sig1["id"], action="approve"),
            engine.decide_signal(sig2["id"], action="approve"),
            return_exceptions=True,
        )

    outcomes = asyncio.run(_race())
    ok_results = [r for r in outcomes if isinstance(r, dict)]
    err_results = [r for r in outcomes if isinstance(r, TradeExecutionError)]

    assert executor.calls == 1
    assert len(ok_results) == 1
    assert ok_results[0]["signal"]["status"] == "SUBMITTED"
    assert len(err_results) == 1
    assert err_results[0].error_code == "TRADE_DAILY_LIMIT_EXCEEDED"
    rstore.close()
    astore.close()


def test_grey_state_order_reconciles_to_submitted_and_reconciling(tmp_path: Path) -> None:
    db_path = tmp_path / "research.db"
    rstore = SQLiteResearchStore(db_path)
    astore = SQLiteAutoTradeStore(db_path)
    astore.update_config({"enabled": True, "pricing_mode": "fixed_ticks", "block_non_continuous_session": False})

    state = {"offline_on_recheck": False}

    class _GreyExecutor:
        adb_serial = "emulator-5556"
        active_tab = "buy"

        def place_order(self, **_kwargs):
            return {
                "accepted": False,
                "status": "NO_DIALOG_OBSERVED",
                "dialog_message": "点击下单后未捕捉确认弹窗",
                "timings_ms": {},
            }

        def get_today_orders(self, **_kwargs):
            if state["offline_on_recheck"]:
                raise TradeExecutionError("TRADE_DEVICE_OFFLINE", "ADB断开", 503)
            return {
                "orders": [
                    {
                        "symbol": "160723",
                        "name": "嘉实原油",
                        "side": "buy",
                        "price": "2.100",
                        "quantity": 100,
                        "entrust_no": "HT888",
                        "status": "已报",
                    }
                ]
            }

    engine = AutoTradeEngine(
        auto_store=astore,
        research_store=rstore,
        broker=None,
        trade_executor=_GreyExecutor(),
        symbol_lookup=lambda s: CATALOG.get(s),
        is_trading_day=lambda _d: True,
        now_fn=lambda: datetime(2026, 11, 26, 10, 5, tzinfo=SHANGHAI_TZ),
        epoch_fn=lambda: 1_800_000_000.0,
    )

    sig_match = astore.claim_and_insert_signal(
        signal_key="160723:20261126:10:05:grey-match:buy",
        trade_date="20261126",
        symbol="160723",
        name="嘉实原油",
        side="buy",
        signal_source="intraday_macd_flow",
        rule_id="r1",
        rule_title="灰态确认",
        rule_summary="grey-match",
        minute="10:05",
        signal_price="2.100",
        limit_price="2.100",
        precision=3,
        quantity=100,
        execution_mode="semi_auto",
        is_simulation=False,
        status="PENDING_CONFIRM",
        expires_at=1_800_000_300.0,
    )
    assert sig_match is not None
    res_match = asyncio.run(engine.decide_signal(sig_match["id"], action="approve"))
    assert res_match["signal"]["status"] == "SUBMITTED"
    assert res_match["signal"]["entrust_no"] == "HT888"

    state["offline_on_recheck"] = True
    sig_reconcile = astore.claim_and_insert_signal(
        signal_key="160723:20261126:10:06:grey-rec:buy",
        trade_date="20261126",
        symbol="160723",
        name="嘉实原油",
        side="buy",
        signal_source="intraday_macd_flow",
        rule_id="r1",
        rule_title="灰态挂账",
        rule_summary="grey-reconcile",
        minute="10:06",
        signal_price="2.100",
        limit_price="2.100",
        precision=3,
        quantity=100,
        execution_mode="semi_auto",
        is_simulation=False,
        status="PENDING_CONFIRM",
        expires_at=1_800_000_300.0,
    )
    assert sig_reconcile is not None
    res_rec = asyncio.run(engine.decide_signal(sig_reconcile["id"], action="approve"))
    assert res_rec["signal"]["status"] == "RECONCILING"
    rstore.close()
    astore.close()


def test_two_tier_circuit_isolates_local_cash_error_from_global_login_required(tmp_path: Path) -> None:
    db_path = tmp_path / "research.db"
    rstore = SQLiteResearchStore(db_path)
    astore = SQLiteAutoTradeStore(db_path)
    astore.update_config({"enabled": True, "pricing_mode": "fixed_ticks", "block_non_continuous_session": False})

    err_mode = {"code": "TRADE_INSUFFICIENT_CASH", "msg": "可用资金不足"}

    class _ErrorExecutor:
        adb_serial = "emulator-5556"
        active_tab = "buy"

        def place_order(self, **_kwargs):
            raise TradeExecutionError(err_mode["code"], err_mode["msg"], 409)

    engine = AutoTradeEngine(
        auto_store=astore,
        research_store=rstore,
        broker=None,
        trade_executor=_ErrorExecutor(),
        symbol_lookup=lambda s: CATALOG.get(s),
        is_trading_day=lambda _d: True,
        now_fn=lambda: datetime(2026, 11, 26, 10, 5, tzinfo=SHANGHAI_TZ),
        epoch_fn=lambda: 1_800_000_000.0,
    )

    sig_local = astore.claim_and_insert_signal(
        signal_key="160723:20261126:10:05:local:buy",
        trade_date="20261126",
        symbol="160723",
        name="嘉实原油",
        side="buy",
        signal_source="intraday_macd_flow",
        rule_id="r1",
        rule_title="资金不足测试",
        rule_summary="local-reject",
        minute="10:05",
        signal_price="2.100",
        limit_price="2.100",
        precision=3,
        quantity=100,
        execution_mode="semi_auto",
        is_simulation=False,
        status="PENDING_CONFIRM",
        expires_at=1_800_000_300.0,
    )
    assert sig_local is not None
    with pytest.raises(TradeExecutionError) as exc_local:
        asyncio.run(engine.decide_signal(sig_local["id"], action="approve"))
    assert exc_local.value.error_code == "TRADE_INSUFFICIENT_CASH"
    status_local = engine.get_status()["config"]
    assert status_local["circuit_open"] is False
    assert "160723:buy" in status_local["symbol_cooldowns"]

    err_mode["code"] = "BROKER_LOGIN_REQUIRED"
    err_mode["msg"] = "券商需要重新登录"
    sig_global = astore.claim_and_insert_signal(
        signal_key="601872:20261126:10:06:global:buy",
        trade_date="20261126",
        symbol="601872",
        name="招商轮船",
        side="buy",
        signal_source="intraday_macd_flow",
        rule_id="r1",
        rule_title="登录过期测试",
        rule_summary="global-circuit",
        minute="10:06",
        signal_price="10.00",
        limit_price="10.00",
        precision=2,
        quantity=100,
        execution_mode="semi_auto",
        is_simulation=False,
        status="PENDING_CONFIRM",
        expires_at=1_800_000_300.0,
    )
    assert sig_global is not None
    with pytest.raises(TradeExecutionError) as exc_global:
        asyncio.run(engine.decide_signal(sig_global["id"], action="approve"))
    assert exc_global.value.error_code == "BROKER_LOGIN_REQUIRED"
    assert engine.get_status()["config"]["circuit_open"] is True
    rstore.close()
    astore.close()


def test_priority_trade_scheduler_preemption_and_tab_affinity() -> None:
    from level2_service.auto_trade import PriorityTradeScheduler

    async def _run(p0_side: str) -> list[str]:
        lock = asyncio.Lock()
        active_tab = {"val": "buy"}
        scheduler = PriorityTradeScheduler(exec_lock=lock, active_tab_getter=lambda: active_tab["val"])
        order: list[str] = []
        gate = asyncio.Event()

        async def _holder():
            async with scheduler.hold(PriorityTradeScheduler.P1_ORDER, side="buy"):
                gate.set()
                await asyncio.sleep(0.03)

        async def _worker(tag: str, prio: int, side: str | None):
            async with scheduler.hold(prio, side=side):
                order.append(tag)
                if side is not None:
                    active_tab["val"] = side

        h_task = asyncio.create_task(_holder())
        await gate.wait()
        tasks = [
            asyncio.create_task(_worker("P3", PriorityTradeScheduler.P3_BACKGROUND, None)),
            asyncio.create_task(_worker("P1:sell:1", PriorityTradeScheduler.P1_ORDER, "sell")),
            asyncio.create_task(_worker("P1:buy:1", PriorityTradeScheduler.P1_ORDER, "buy")),
            asyncio.create_task(_worker("P1:sell:2", PriorityTradeScheduler.P1_ORDER, "sell")),
            asyncio.create_task(_worker("P1:buy:2", PriorityTradeScheduler.P1_ORDER, "buy")),
            asyncio.create_task(_worker("P0", PriorityTradeScheduler.P0_CRITICAL, p0_side)),
        ]
        await asyncio.sleep(0.005)
        await asyncio.gather(h_task, *tasks)
        return order

    assert asyncio.run(_run("buy")) == ["P0", "P1:buy:1", "P1:buy:2", "P1:sell:1", "P1:sell:2", "P3"]
    assert asyncio.run(_run("sell")) == ["P0", "P1:sell:1", "P1:sell:2", "P1:buy:1", "P1:buy:2", "P3"]


def test_compute_ashare_price_cage_by_board_and_fund_exemption() -> None:
    from decimal import Decimal
    from level2_service.auto_trade import compute_ashare_price_cage

    ob_stock_10 = {"asks": [{"level": 1, "price": "10.00"}], "bids": [{"level": 1, "price": "9.99"}]}
    ob_stock_3 = {"asks": [{"level": 1, "price": "3.00"}], "bids": [{"level": 1, "price": "2.99"}]}

    assert compute_ashare_price_cage("513350", "17", "buy", ob_stock_10) == (None, None)
    assert compute_ashare_price_cage("160723", "36", "sell", ob_stock_3) == (None, None)

    star_limit, star_label = compute_ashare_price_cage("688001", "17", "buy", ob_stock_10)
    assert star_limit == Decimal("10.20")
    assert star_label == "双创2%价格笼子"

    main_limit, main_label = compute_ashare_price_cage("600036", "17", "buy", ob_stock_3)
    assert main_limit == Decimal("3.10")
    assert main_label == "主板2%/0.10元价格笼子"

    bse_limit, bse_label = compute_ashare_price_cage("920001", "BJ", "buy", ob_stock_10)
    assert bse_limit == Decimal("10.50")
    assert bse_label == "北交所5%价格笼子"


def test_single_rule_per_symbol_and_self_trade_precancel(tmp_path: Path) -> None:
    db_path = tmp_path / "research.db"
    rstore = SQLiteResearchStore(db_path)
    astore = SQLiteAutoTradeStore(db_path)
    legacy_cfg = astore.update_config(
        {
            "enabled": True,
            "symbols": ["160723"],
            "pricing_mode": "fixed_ticks",
            "block_non_continuous_session": False,
            "symbol_profiles": {
                "160723": {
                    "enabled": True,
                    "rule_bindings": [
                        {"signal_source": "quant_rule", "quant_rule_id": "rule-primary", "priority": 10},
                        {"signal_source": "intraday_macd_flow", "priority": 20},
                    ],
                }
            },
        }
    )
    prof_160723 = legacy_cfg["symbol_profiles"]["160723"]
    assert prof_160723["signal_source"] == "quant_rule"
    assert prof_160723["quant_rule_id"] == "rule-primary"
    assert "rule_bindings" not in prof_160723

    events: list[str] = []

    class _PrecancelExecutor:
        adb_serial = "emulator-5556"
        active_tab = "sell"

        def cancel_order(self, *, entrust_no=None, symbol=None, expected_name=None, side=None):
            del expected_name
            events.append(f"cancel:{symbol}:{side}:{entrust_no}")
            return {"ok": True}

        def place_order(self, *, symbol, side, **_kwargs):
            events.append(f"place:{symbol}:{side}")
            return {"accepted": True, "status": "SUBMITTED", "entrust_no": "HT-BUY-1", "dialog_message": "ok", "timings_ms": {}}

    engine = AutoTradeEngine(
        auto_store=astore,
        research_store=rstore,
        broker=None,
        trade_executor=_PrecancelExecutor(),
        symbol_lookup=lambda s: CATALOG.get(s),
        is_trading_day=lambda _d: True,
        now_fn=lambda: datetime(2026, 11, 26, 10, 5, tzinfo=SHANGHAI_TZ),
        epoch_fn=lambda: 1_800_000_000.0,
    )

    opp_sell = astore.claim_and_insert_signal(
        signal_key="160723:20261126:10:02:rs:sell",
        trade_date="20261126",
        symbol="160723",
        name="嘉实原油",
        side="sell",
        signal_source="intraday_macd_flow",
        rule_id="rs",
        rule_title="在途卖单",
        rule_summary="sell",
        minute="10:02",
        signal_price="2.120",
        limit_price="2.120",
        precision=3,
        quantity=100,
        execution_mode="full_auto",
        is_simulation=False,
        status="PENDING_CONFIRM",
    )
    assert opp_sell is not None
    astore.update_signal_execution(
        opp_sell["id"],
        status="SUBMITTED",
        entrust_no="HT-SELL-1",
        cancel_pending=True,
        cancel_due_at=1_800_000_030.0,
    )

    new_buy = astore.claim_and_insert_signal(
        signal_key="160723:20261126:10:05:rb:buy",
        trade_date="20261126",
        symbol="160723",
        name="嘉实原油",
        side="buy",
        signal_source="intraday_macd_flow",
        rule_id="rb",
        rule_title="买入触发防自成交",
        rule_summary="buy",
        minute="10:05",
        signal_price="2.100",
        limit_price="2.100",
        precision=3,
        quantity=100,
        execution_mode="semi_auto",
        is_simulation=False,
        status="PENDING_CONFIRM",
        expires_at=1_800_000_300.0,
    )
    assert new_buy is not None

    approved = asyncio.run(engine.decide_signal(new_buy["id"], action="approve"))
    assert approved["signal"]["status"] == "SUBMITTED"
    assert events == ["cancel:160723:sell:HT-SELL-1", "place:160723:buy"]
    assert astore.get_signal(opp_sell["id"])["status"] == "SUPERSEDED_CANCELLED"
    rstore.close()
    astore.close()


def test_standard_cash_pool_rolling_t0_recycles_net_cap_and_buy_entry_quota(tmp_path: Path) -> None:
    db_path = tmp_path / "std_t0_recycle.db"
    rstore = SQLiteResearchStore(db_path)
    qstore = SQLiteQuantRuleStore(db_path)
    astore = SQLiteAutoTradeStore(db_path)

    qstore.upsert_rule(
        {
            "id": "ext-std-t0-pool",
            "name": "标准资金池T+0测试",
            "source": "ai",
            "symbols": ["160723"],
            "buy_rule": {"title": "买", "joiner": "and", "conditions": [{"type": "macd_signal", "signal": "red"}]},
            "sell_rule": {"title": "卖", "joiner": "and", "conditions": [{"type": "macd_signal", "signal": "green"}]},
            "position_sizing": {
                "mode": "cash_pool_rolling",
                "engine_profile": "standard",
                "default_weight_pct": 100.0,
                "max_position_pct": 100.0,
                "max_daily_entries": 2,
                "allow_same_day_turnaround": "auto",
            },
        }
    )
    qstore.upsert_rule(
        {
            "id": "ext-std-t1-pool",
            "name": "标准资金池T+1对照",
            "source": "ai",
            "symbols": ["601872"],
            "buy_rule": {"title": "买", "joiner": "and", "conditions": [{"type": "macd_signal", "signal": "red"}]},
            "sell_rule": {"title": "卖", "joiner": "and", "conditions": [{"type": "macd_signal", "signal": "green"}]},
            "position_sizing": {
                "mode": "cash_pool_rolling",
                "engine_profile": "standard",
                "default_weight_pct": 100.0,
                "max_position_pct": 100.0,
                "max_daily_entries": 2,
                "allow_same_day_turnaround": "auto",
            },
        }
    )
    astore.update_config(
        {
            "enabled": True,
            "signal_source": "quant_rule",
            "quant_rule_id": "ext-std-t0-pool",
            "execution_mode": "semi_auto",
            "sizing_mode": "model_auto",
            "pricing_mode": "fixed_ticks",
            "slippage_ticks": 0,
            "symbols": ["160723"],
            "daily_max_buy_amount": 4000.0,
            "block_non_continuous_session": True,
        }
    )

    engine = AutoTradeEngine(
        auto_store=astore,
        research_store=rstore,
        broker=None,
        trade_executor=None,
        symbol_lookup=lambda s: CATALOG.get(s),
        is_trading_day=lambda _d: True,
        now_fn=lambda: datetime(2026, 11, 26, 10, 30, tzinfo=SHANGHAI_TZ),
        quant_rule_store=qstore,
    )
    cfg = astore.get_config()
    td = "20261126"

    # 1. T+0 160723 Round 1 Buy @ 2.000 -> 2000 shares
    q_b1, _, err_b1 = engine._resolve_signal_quantity(
        symbol="160723", side="buy", limit_price="2.000", config=cfg, candidate=None, trade_date=td
    )
    assert err_b1 is None and q_b1 == 2000
    assert astore.claim_and_insert_signal(
        signal_key="160723:20261126:09:40:ext-std-t0-pool:buy",
        trade_date=td, symbol="160723", name="嘉实原油", side="buy", signal_source="quant_rule",
        rule_id="ext-std-t0-pool", rule_title="R1买", rule_summary="b1", minute="09:40",
        signal_price="2.000", limit_price="2.000", precision=3, quantity=2000,
        execution_mode="semi_auto", is_simulation=False, status="FILLED_OR_CLOSED",
    ) is not None

    # Round 1 T+0 Sell @ 2.020 -> 1900 shares (reserving 100 floor)
    q_s1, _, err_s1 = engine._resolve_signal_quantity(
        symbol="160723", side="sell", limit_price="2.020", config=cfg, candidate=None, trade_date=td
    )
    assert err_s1 is None and q_s1 == 1900
    assert astore.claim_and_insert_signal(
        signal_key="160723:20261126:10:05:ext-std-t0-pool:sell",
        trade_date=td, symbol="160723", name="嘉实原油", side="sell", signal_source="quant_rule",
        rule_id="ext-std-t0-pool", rule_title="R1卖", rule_summary="s1", minute="10:05",
        signal_price="2.020", limit_price="2.020", precision=3, quantity=1900,
        execution_mode="semi_auto", is_simulation=False, status="FILLED_OR_CLOSED",
    ) is not None
    act = astore.today_symbol_activity(td, "160723")
    assert act["buy_entry_count"] == 1 and act["sell_entry_count"] == 1 and act["entry_count"] == 2

    # Round 2 Buy @ 2.000 SAME DAY: sold proceeds recycle cap (4000 - 100*2.0 = 3800 -> 1900 shares)!
    q_b2, _, err_b2 = engine._resolve_signal_quantity(
        symbol="160723", side="buy", limit_price="2.000", config=cfg, candidate=None, trade_date=td
    )
    assert err_b2 is None and q_b2 == 1900
    emitted = asyncio.run(
        engine._emit_trade_signal(
            symbol="160723",
            item={
                "rule_id": "ext-std-t0-pool",
                "rule_title": "R2买",
                "rule_summary": "r2",
                "side": "buy",
                "minute": "10:30",
                "price": "2.000",
                "name": "嘉实原油",
                "market": "36",
            },
            config=cfg,
            trade_date=td,
            now_epoch=1_800_000_000.0,
            local_iso="2026-11-26T10:30:00+08:00",
            sig_key="160723:20261126:10:30:ext-std-t0-pool:buy",
        )
    )
    assert emitted == 1
    b2_sig = next(s for s in astore.list_pending_signals() if s["minute"] == "10:30")
    assert b2_sig["quantity"] == 1900

    # Round 2 T+0 Sell + Round 3 Buy blocked by max_daily_entries=2 on buy entries
    assert astore.claim_and_insert_signal(
        signal_key="160723:20261126:10:45:ext-std-t0-pool:sell",
        trade_date=td, symbol="160723", name="嘉实原油", side="sell", signal_source="quant_rule",
        rule_id="ext-std-t0-pool", rule_title="R2卖", rule_summary="s2", minute="10:45",
        signal_price="2.030", limit_price="2.030", precision=3, quantity=1900,
        execution_mode="semi_auto", is_simulation=False, status="FILLED_OR_CLOSED",
    ) is not None
    q_b3, _, err_b3 = engine._resolve_signal_quantity(
        symbol="160723", side="buy", limit_price="2.000", config=cfg, candidate=None, trade_date=td
    )
    assert q_b3 == 0 and err_b3 == "已达模型单日最大买入次数(2)"

    # 2. Contrast: Ordinary A-share T+1 symbol (601872) keeps T+1 freeze & gross daily buy cap
    astore.update_config({"symbols": ["601872"], "quant_rule_id": "ext-std-t1-pool"})
    cfg_t1 = astore.get_config()
    # Overnight carry 200 shares + today's buy 200 shares @ 10.00
    assert astore.claim_and_insert_signal(
        signal_key="601872:20261125:10:00:ext-std-t1-pool:buy",
        trade_date="20261125", symbol="601872", name="招商轮船", side="buy", signal_source="quant_rule",
        rule_id="ext-std-t1-pool", rule_title="隔夜买", rule_summary="prev", minute="10:00",
        signal_price="10.00", limit_price="10.00", precision=2, quantity=200,
        execution_mode="semi_auto", is_simulation=False, status="FILLED_OR_CLOSED",
    ) is not None
    assert astore.claim_and_insert_signal(
        signal_key="601872:20261126:09:40:ext-std-t1-pool:buy",
        trade_date=td, symbol="601872", name="招商轮船", side="buy", signal_source="quant_rule",
        rule_id="ext-std-t1-pool", rule_title="今日买", rule_summary="b_t1", minute="09:40",
        signal_price="10.00", limit_price="10.00", precision=2, quantity=200,
        execution_mode="semi_auto", is_simulation=False, status="FILLED_OR_CLOSED",
    ) is not None
    # Only yesterday's 200 shares are sellable (T+1 freeze blocks today's 200 shares; leaving 100 floor -> 200 sellable -> min(200, 400-100)=200)
    q_s_t1, _, err_s_t1 = engine._resolve_signal_quantity(
        symbol="601872", side="sell", limit_price="10.10", config=cfg_t1, candidate=None, trade_date=td
    )
    assert err_s_t1 is None and q_s_t1 == 200
    assert astore.claim_and_insert_signal(
        signal_key="601872:20261126:10:00:ext-std-t1-pool:sell",
        trade_date=td, symbol="601872", name="招商轮船", side="sell", signal_source="quant_rule",
        rule_id="ext-std-t1-pool", rule_title="卖昨仓", rule_summary="s_t1", minute="10:00",
        signal_price="10.10", limit_price="10.10", precision=2, quantity=200,
        execution_mode="semi_auto", is_simulation=False, status="FILLED_OR_CLOSED",
    ) is not None
    # Even after selling 200 shares today, T+1 gross daily buy limit only leaves 4000 - 2000 = 2000 -> 200 shares (not 3000)
    q_b2_t1, _, err_b2_t1 = engine._resolve_signal_quantity(
        symbol="601872", side="buy", limit_price="10.00", config=cfg_t1, candidate=None, trade_date=td
    )
    assert err_b2_t1 is None and q_b2_t1 == 200

    qstore.close()
    astore.close()
    rstore.close()


def _seed_xop_macro_cache(
    rstore: SQLiteResearchStore,
    *,
    prev_us_date: str = "20261125",
    cur_us_date: str = "20261125",
    xop_prev: float = 160.0,
    xop_last: float = 165.0,
    nav_162411: float = 1.000,
) -> None:
    import json as _json
    import time as _time
    from datetime import timedelta as _td

    now_ts = _time.time()
    oil_rows = [
        ["20251101", int(datetime(2025, 11, 2, 6, 0, tzinfo=SHANGHAI_TZ).timestamp()), 60.0],
        ["20261101", int(datetime(2026, 11, 2, 6, 0, tzinfo=SHANGHAI_TZ).timestamp()), 100.0],
    ]
    xop_rows = [
        ["20251101", int(datetime(2025, 11, 2, 6, 0, tzinfo=SHANGHAI_TZ).timestamp()), 120.0],
        ["20251102", int(datetime(2025, 11, 3, 6, 0, tzinfo=SHANGHAI_TZ).timestamp()), 200.0],
    ]
    spy_rows = [["20251101", int(datetime(2025, 11, 2, 6, 0, tzinfo=SHANGHAI_TZ).timestamp()), 600.0]]
    iwm_rows = [["20251101", int(datetime(2025, 11, 2, 6, 0, tzinfo=SHANGHAI_TZ).timestamp()), 220.0]]
    xle_rows = [["20251101", int(datetime(2025, 11, 2, 6, 0, tzinfo=SHANGHAI_TZ).timestamp()), 90.0]]
    ng_rows = [["20251101", int(datetime(2025, 11, 2, 6, 0, tzinfo=SHANGHAI_TZ).timestamp()), 3.0]]
    cl_rows = [["20251101", int(datetime(2025, 11, 2, 6, 0, tzinfo=SHANGHAI_TZ).timestamp()), 68.0]]
    ho_rows = [["20251101", int(datetime(2025, 11, 2, 6, 0, tzinfo=SHANGHAI_TZ).timestamp()), 2.25]]
    nav_payload: dict[str, float] = {}
    for idx in range(18):
        us_d = (datetime(2026, 11, 5, tzinfo=SHANGHAI_TZ) + _td(days=idx)).strftime("%Y%m%d")
        ts = int((datetime.strptime(us_d, "%Y%m%d").replace(tzinfo=SHANGHAI_TZ) + _td(days=1, hours=6)).timestamp())
        x_val = xop_last if us_d == cur_us_date else xop_prev
        xop_rows.append([us_d, ts, x_val])
        spy_rows.append([us_d, ts, 600.0])
        iwm_rows.append([us_d, ts, 220.0])
        xle_rows.append([us_d, ts, 90.0])
        ng_rows.append([us_d, ts, 3.0])
        cl_rows.append([us_d, ts, 68.0])
        ho_rows.append([us_d, ts, 2.25])
        nav_payload[us_d] = nav_162411
    nav_payload[prev_us_date] = nav_162411
    nav_payload[cur_us_date] = nav_162411
    ts_fut = int(datetime(2026, 11, 26, 14, 52, tzinfo=SHANGHAI_TZ).timestamp())
    with rstore._lock, rstore._connection:
        rstore._connection.execute(
            "INSERT OR REPLACE INTO quant_macro_cache VALUES (?, ?, ?)",
            ("nav_162411", _json.dumps(nav_payload), now_ts),
        )
        rstore._connection.execute(
            "INSERT OR REPLACE INTO quant_macro_cache VALUES (?, ?, ?)",
            ("oil_daily_2y", _json.dumps(oil_rows), now_ts),
        )
        rstore._connection.execute(
            "INSERT OR REPLACE INTO quant_macro_cache VALUES (?, ?, ?)",
            ("xop_daily_2y", _json.dumps(xop_rows), now_ts),
        )
        rstore._connection.execute(
            "INSERT OR REPLACE INTO quant_macro_cache VALUES (?, ?, ?)",
            ("spy_daily_2y", _json.dumps(spy_rows), now_ts),
        )
        rstore._connection.execute(
            "INSERT OR REPLACE INTO quant_macro_cache VALUES (?, ?, ?)",
            ("iwm_daily_2y", _json.dumps(iwm_rows), now_ts),
        )
        rstore._connection.execute(
            "INSERT OR REPLACE INTO quant_macro_cache VALUES (?, ?, ?)",
            ("xle_daily_2y", _json.dumps(xle_rows), now_ts),
        )
        rstore._connection.execute(
            "INSERT OR REPLACE INTO quant_macro_cache VALUES (?, ?, ?)",
            ("ng_daily_2y", _json.dumps(ng_rows), now_ts),
        )
        rstore._connection.execute(
            "INSERT OR REPLACE INTO quant_macro_cache VALUES (?, ?, ?)",
            ("cl_daily_2y", _json.dumps(cl_rows), now_ts),
        )
        rstore._connection.execute(
            "INSERT OR REPLACE INTO quant_macro_cache VALUES (?, ?, ?)",
            ("ho_daily_2y", _json.dumps(ho_rows), now_ts),
        )
        for fkey, fpx in (("fut_1m_cl", 68.0), ("fut_1m_ho", 2.25), ("fut_1m_ng", 3.0), ("fut_1m_es", 6000.0)):
            rstore._connection.execute(
                "INSERT OR REPLACE INTO quant_macro_cache VALUES (?, ?, ?)",
                (fkey, _json.dumps({"ts": [ts_fut], "px": [fpx]}), now_ts),
            )
        rstore._connection.execute(
            "INSERT OR REPLACE INTO quant_macro_cache VALUES (?, ?, ?)",
            ("fx_usdcny_series", _json.dumps({"ts": [int(datetime(2026, 10, 1, 9, 0, tzinfo=SHANGHAI_TZ).timestamp())], "px": [6.70]}), now_ts),
        )


def test_route_d_shadow_preserves_static_execution_while_exposing_pool_telemetry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("THS_QUANT_OFFLINE", "1")
    monkeypatch.setattr("level2_service.ths_trade.time.sleep", lambda _s: None)
    ctx = _setup_v9_plus_fixture(
        tmp_path / "route_d_shadow.db",
        execution_mode="full_auto",
        daily_max_buy_amount=30000.0,
    )
    rstore, qstore, astore, engine = ctx["rstore"], ctx["qstore"], ctx["astore"], ctx["engine"]
    dev = ctx["dev"]
    astore.update_config({"capital_policy_mode": "route_d_shadow", "pool_policy": {"sigma_cap_pct": 4.20}})

    st = engine.get_status()
    assert st["capital_policy_mode"] == "route_d_shadow"
    assert st["capital_ledger"]["capital_policy_mode"] == "route_d_shadow"
    assert st["capital_ledger"]["pool_allocation"]["shadow_only"] is True
    assert "160723" in st["capital_ledger"]["pool_allocation"]["symbols"]
    assert st["daily_usage"]["daily_max_buy_amount"] == pytest.approx(30000.0)

    # Static execution remains strictly bounded by static cap (¥30,000 -> 15,100 shares @ 1.980)
    res = asyncio.run(engine.poll_once())
    assert res["triggered"] == 1
    sig = astore.list_recent_signals(limit=3)[0]
    assert sig["status"] == "SUBMITTED"
    assert sig["quantity"] == 15100
    assert dev.form_qty == "15100"

    # Static per-row TRADE_CAP_BELOW_HOLDING guard stays intact under route_d_shadow
    class _ShadowReconcileStub:
        adb_serial = "emulator-5556"
        snap = {
            "logged_in": True,
            "metrics": {"available_cash": "120000.00"},
            "positions": [{"symbol": "160723", "name": "嘉实原油", "shares": 25000, "market_value": "50000.00"}],
            "captured_at": "2026-11-26T10:15:00+08:00",
        }

        def _cached_account_snapshot(self, *_a: Any, **_k: Any) -> dict[str, Any]:
            return dict(self.snap)

        def get_account_summary(self, *_a: Any, **_k: Any) -> dict[str, Any]:
            return dict(self.snap)

    engine.trade_executor = _ShadowReconcileStub()
    with pytest.raises(TradeExecutionError) as err_low:
        engine.reconcile_and_update_config(
            {"capital_policy_mode": "route_d_shadow", "daily_max_buy_amount": 30000.0},
            reconcile_broker=True,
        )
    assert err_low.value.error_code == "TRADE_CAP_BELOW_HOLDING"

    rstore.close()
    qstore.close()
    astore.close()


def test_route_d_plus_reconcile_pool_conservation_and_two_wave_close_rebalance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("THS_QUANT_OFFLINE", "1")
    ctx = _setup_v9_plus_fixture(
        tmp_path / "route_d_two_wave.db",
        execution_mode="full_auto",
        daily_max_buy_amount=140000.0,
    )
    rstore, qstore, astore, clock_dt = ctx["rstore"], ctx["qstore"], ctx["astore"], ctx["clock_dt"]
    _seed_day_pts, brn_today, lof_today = ctx["seed_day_pts"], ctx["brn_today"], ctx["lof_today"]

    # Configure 162411 with xop_us_ep anchor & static capital_cap=20k (< ¥89k dynamic solo target!)
    _seed_xop_macro_cache(rstore, xop_prev=160.0, xop_last=166.0, nav_162411=1.020)
    xop_bars_prev = [("09:30", 0.990), ("14:53", 0.990), ("15:00", 0.990)]
    xop_bars_today = [("09:30", 0.990)] + [(t, 0.995) for t, _ in lof_today[:-6]] + [
        ("10:00", 1.000), ("10:05", 1.000), ("14:53", 1.000)
    ]
    _seed_day_pts("162411", "20261125", 0.990, xop_bars_prev, market="36")
    _seed_day_pts("162411", "20261126", 0.990, xop_bars_today, market="36")
    qstore.upsert_rule(
        {
            "id": "ai-162411-v9-xop-aggressive",
            "name": "162411 XOP 专版",
            "source": "ai",
            "symbols": ["162411", "BRN0Y"],
            "macd_settings": {"short": 10, "long": 20, "signal": 5, "marker_threshold": 0.0002},
            "buy_rule": {"title": "正T买", "joiner": "and", "conditions": [{"type": "brent_spread_pct", "direction": "lte", "value": -0.25}]},
            "sell_rule": {"title": "反T卖", "joiner": "and", "conditions": [{"type": "brent_spread_pct", "direction": "gte", "value": 0.42}]},
            "position_sizing": {
                "mode": "cash_pool_rolling",
                "engine_profile": "v9_plus_aggressive",
                "cross_market_anchor": "xop_us_ep",
                "max_daily_entries": 8,
                "cooldown_minutes": 4,
                "close_flatten_time": "14:53",
            },
        }
    )

    placed_orders: list[dict[str, Any]] = []
    reject_160723_sell = {"flag": False}

    class _PoolFakeExecutor:
        adb_serial = "emulator-5556"
        active_tab = "buy"

        def __init__(self) -> None:
            self.snap: dict[str, Any] = {
                "logged_in": True,
                "metrics": {"available_cash": "90000.00", "total_assets": "140000.00"},
                "positions": [
                    {"symbol": "162411", "name": "华宝油气", "shares": 50000, "available_shares": 50000, "market_value": "50000.00", "current_price": "1.000"},
                ],
                "captured_at": "2026-11-26T14:50:00+08:00",
            }

        def _cached_account_snapshot(self, *_a: Any, **_k: Any) -> dict[str, Any]:
            import copy as _copy
            return _copy.deepcopy(self.snap)

        def get_account_summary(self, *_a: Any, **_k: Any) -> dict[str, Any]:
            import copy as _copy
            return _copy.deepcopy(self.snap)

        def place_order(self, **kwargs: Any) -> dict[str, Any]:
            placed_orders.append({"symbol": kwargs["symbol"], "side": kwargs["side"], "qty": kwargs["quantity"], "price": kwargs["price"]})
            if reject_160723_sell["flag"] and kwargs["symbol"] == "160723" and kwargs["side"] == "sell":
                return {"accepted": False, "status": "REJECTED", "dialog_message": "柜台拒单测试"}
            return {"accepted": True, "status": "SUBMITTED", "entrust_no": f"HT{len(placed_orders):04d}", "dialog_message": "委托已提交"}

    exec_stub = _PoolFakeExecutor()
    cat = {**CATALOG, "162411": SymbolLookup(symbol="162411", name="华宝油气", market="36")}
    engine = AutoTradeEngine(
        auto_store=astore,
        research_store=rstore,
        broker=None,
        trade_executor=exec_stub,
        symbol_lookup=lambda s: cat.get(s),
        is_trading_day=lambda _d: True,
        now_fn=lambda: clock_dt["dt"],
        epoch_fn=lambda: clock_dt["ep"],
        quant_rule_store=qstore,
    )

    pool_patch = {
        "enabled": True,
        "signal_source": "quant_rule",
        "quant_rule_id": "ai-160723-v9-plus-aggressive",
        "execution_mode": "full_auto",
        "sizing_mode": "model_auto",
        "capital_policy_mode": "route_d_plus",
        "daily_max_buy_amount": 140000.0,
        "pool_policy": {"total_pool_cap": 140000.0, "sigma_cap_pct": 4.20},
        "symbols": ["160723", "162411"],
        "symbol_profiles": {
            "160723": {"enabled": True, "signal_source": "quant_rule", "quant_rule_id": "ai-160723-v9-plus-aggressive", "sizing_mode": "model_auto", "capital_cap": 60000.0},
            "162411": {"enabled": True, "signal_source": "quant_rule", "quant_rule_id": "ai-162411-v9-xop-aggressive", "sizing_mode": "model_auto", "capital_cap": 20000.0},
        },
    }
    # 1) 162411 holds ¥50k (> static 20k) within ¥140k pool -> reconcile SUCCEEDS in route_d_plus mode!
    st_ok = engine.reconcile_and_update_config(pool_patch, reconcile_broker=True)
    assert st_ok["capital_ledger"]["capital_policy_mode"] == "route_d_plus"
    assert st_ok["capital_ledger"]["conservation_ok"] is True
    assert st_ok["capital_ledger"]["pool_allocation"]["shadow_only"] is False

    # Reject only if total quant holding exceeds pool_cap (¥150k > ¥140k)
    exec_stub.snap["positions"] = [
        {"symbol": "160723", "name": "嘉实原油", "shares": 50000, "available_shares": 50000, "market_value": "100000.00", "current_price": "2.000"},
        {"symbol": "162411", "name": "华宝油气", "shares": 50000, "available_shares": 50000, "market_value": "50000.00", "current_price": "1.000"},
    ]
    with pytest.raises(TradeExecutionError) as err_pool:
        engine.reconcile_and_update_config({"pool_policy": {"total_pool_cap": 140000.0}}, reconcile_broker=True)
    assert err_pool.value.error_code == "TRADE_CAP_BELOW_HOLDING"

    # 2) At 14:53: BRN spikes to 89.0 (160723 target=0%, must keep 100 shares) while 162411 (xop_us_ep, discounted @ 1.000) is solo 100%!
    # Holding 50,000 shares of 160723 (¥100,000) + ¥40,000 cash -> Wave 1 sells 49,900 shares of 160723 BEFORE Wave 2 buys 162411 (~¥89.4k > static 20k and > pre-sell ¥40k cash)!
    _seed_day_pts("BRN0Y", "20261126", 70.0, brn_today + [("14:53", 89.0)], market="ICE")
    _seed_day_pts("160723", "20261126", 2.000, lof_today + [("14:53", 1.980)])
    exec_stub.snap = {
        "logged_in": True,
        "metrics": {"available_cash": "40000.00", "total_assets": "140000.00"},
        "positions": [
            {"symbol": "160723", "name": "嘉实原油", "shares": 50000, "available_shares": 50000, "market_value": "100000.00", "current_price": "2.000"},
        ],
        "captured_at": "2026-11-26T14:50:00+08:00",
    }
    clock_dt["dt"] = datetime(2026, 11, 26, 14, 53, tzinfo=SHANGHAI_TZ)
    clock_dt["ep"] += 17280.0

    res = asyncio.run(engine.poll_once())
    assert res["triggered"] == 2
    assert len(placed_orders) == 2
    # Wave 1 SELL MUST execute strictly before Wave 2 BUY
    assert placed_orders[0]["symbol"] == "160723" and placed_orders[0]["side"] == "sell"
    assert placed_orders[0]["qty"] == 49900
    assert placed_orders[1]["symbol"] == "162411" and placed_orders[1]["side"] == "buy"
    assert placed_orders[1]["qty"] > 60000  # uses freed cash well beyond static 20k and pre-sell 40k!

    # 3) If Wave 1 sell is REJECTED by broker, Wave 2 buy must clamp strictly to pre-existing ¥40,000 cash!
    placed_orders.clear()
    astore._connection.execute("DELETE FROM auto_trade_signals WHERE trade_date='20261126'")
    astore._connection.commit()
    reject_160723_sell["flag"] = True
    res_rej = asyncio.run(engine.poll_once())
    assert res_rej["triggered"] == 2
    assert len(placed_orders) == 2
    assert placed_orders[0]["symbol"] == "160723" and placed_orders[0]["side"] == "sell"
    assert placed_orders[1]["symbol"] == "162411" and placed_orders[1]["side"] == "buy"
    assert placed_orders[1]["qty"] * 1.000 <= 40000.0

    rstore.close()
    qstore.close()
    astore.close()


def test_route_d_plus_intraday_short_reserve_protects_cover_cash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("THS_QUANT_OFFLINE", "1")
    ctx = _setup_v9_plus_fixture(
        tmp_path / "route_d_short_reserve.db",
        execution_mode="full_auto",
        daily_max_buy_amount=100000.0,
    )
    rstore, qstore, astore, clock_dt = ctx["rstore"], ctx["qstore"], ctx["astore"], ctx["clock_dt"]
    _seed_day_pts, brn_today, _ = ctx["seed_day_pts"], ctx["brn_today"], ctx["lof_today"]

    _seed_day_pts("BRN0Y", "20261126", 70.0, brn_today, market="ICE")
    _seed_day_pts("160723", "20261126", 2.000, ctx["lof_today"] + [("10:12", 2.000)])
    # Seed 501018 bars matching 160723 fixture
    _seed_day_pts("501018", "20261125", 1.500, [("09:30", 1.500), ("15:00", 1.500)], market="17")
    qstore.upsert_rule(
        {
            "id": "ai-501018-v9-plus-aggressive",
            "name": "501018 V9+",
            "source": "ai",
            "symbols": ["501018", "BRN0Y"],
            "macd_settings": {"short": 10, "long": 20, "signal": 5, "marker_threshold": 0.0002},
            "buy_rule": {"title": "正T买", "joiner": "and", "conditions": [{"type": "brent_spread_pct", "direction": "lte", "value": -0.25}]},
            "sell_rule": {"title": "反T卖", "joiner": "and", "conditions": [{"type": "brent_spread_pct", "direction": "gte", "value": 0.42}]},
            "position_sizing": {
                "mode": "cash_pool_rolling",
                "engine_profile": "v9_plus_aggressive",
                "cross_market_anchor": "brent",
                "max_daily_entries": 8,
                "cooldown_minutes": 4,
                "close_flatten_time": "14:53",
            },
        }
    )

    placed_orders: list[dict[str, Any]] = []

    class _ReserveExecutor:
        adb_serial = "emulator-5556"
        active_tab = "buy"

        def __init__(self) -> None:
            # 160723 holds 30,000 remaining shares (¥60k) after selling 20,000 shares @ 2.000; cash=¥80k -> reserve=¥40,002 -> free_cash=¥39,998!
            self.snap = {
                "logged_in": True,
                "metrics": {"available_cash": "80000.00", "total_assets": "140000.00"},
                "positions": [
                    {"symbol": "160723", "name": "嘉实原油", "shares": 30000, "available_shares": 30000, "market_value": "60000.00", "current_price": "2.000"},
                ],
                "captured_at": "2026-11-26T10:12:00+08:00",
            }

        def _cached_account_snapshot(self, *_a: Any, **_k: Any) -> dict[str, Any]:
            return dict(self.snap)

        def get_account_summary(self, *_a: Any, **_k: Any) -> dict[str, Any]:
            return dict(self.snap)

        def place_order(self, **kwargs: Any) -> dict[str, Any]:
            placed_orders.append({"symbol": kwargs["symbol"], "side": kwargs["side"], "qty": kwargs["quantity"], "price": kwargs["price"]})
            return {"accepted": True, "status": "SUBMITTED", "entrust_no": f"HT{len(placed_orders):04d}", "dialog_message": "委托已提交"}

    # Prior reverse-T short leg on 160723 (sold 20,000 shares @ 2.000 at 10:10)
    assert astore.claim_and_insert_signal(
        signal_key="160723:20261126:10:10:v9-entry:sell",
        trade_date="20261126",
        symbol="160723",
        name="嘉实原油",
        side="sell",
        signal_source="quant_rule",
        rule_id="ai-160723-v9-plus-aggressive",
        rule_title="反T高抛",
        rule_summary="sell 20000",
        minute="10:10",
        signal_price="2.000",
        limit_price="2.000",
        precision=3,
        quantity=20000,
        execution_mode="full_auto",
        is_simulation=False,
        status="FILLED_OR_CLOSED",
        now_str="2026-11-26T10:10:00+08:00",
    )

    cat = {**CATALOG, "501018": SymbolLookup(symbol="501018", name="南方原油", market="17")}
    engine = AutoTradeEngine(
        auto_store=astore,
        research_store=rstore,
        broker=None,
        trade_executor=_ReserveExecutor(),
        symbol_lookup=lambda s: cat.get(s),
        is_trading_day=lambda _d: True,
        now_fn=lambda: clock_dt["dt"],
        epoch_fn=lambda: clock_dt["ep"],
        quant_rule_store=qstore,
    )
    astore.update_config(
        {
            "enabled": True,
            "signal_source": "quant_rule",
            "quant_rule_id": "ai-160723-v9-plus-aggressive",
            "execution_mode": "full_auto",
            "sizing_mode": "model_auto",
            "capital_policy_mode": "route_d_plus",
            "daily_max_buy_amount": 140000.0,
            "pool_policy": {"total_pool_cap": 140000.0},
            "symbols": ["160723", "501018"],
            "symbol_profiles": {
                "160723": {"enabled": True, "signal_source": "quant_rule", "quant_rule_id": "ai-160723-v9-plus-aggressive", "sizing_mode": "model_auto", "capital_cap": 60000.0},
                "501018": {"enabled": True, "signal_source": "quant_rule", "quant_rule_id": "ai-501018-v9-plus-aggressive", "sizing_mode": "model_auto", "capital_cap": 20000.0},
            },
        }
    )

    pool_sum = engine.get_status()["capital_ledger"]["pool_allocation"]
    assert pool_sum["total_short_reserve_cash"] == pytest.approx(40002.0)
    assert pool_sum["free_pool_cash"] == pytest.approx(39998.0)

    # 1) 501018 attempting a ¥60,000 buy must NEVER touch 160723's ¥40,002 short-cover reserve!
    cfg_root = astore.get_config()
    asyncio.run(
        engine._emit_trade_signal(
            symbol="501018",
            item={
                "rule_id": "ai-501018-v9-plus-aggressive",
                "rule_title": "501018买",
                "rule_summary": "test reserve lock",
                "side": "buy",
                "minute": "10:15",
                "price": "1.500",
                "name": "南方原油",
                "market": "17",
                "t0_net_cap_mode": True,
            },
            config=cfg_root,
            trade_date="20261126",
            now_epoch=clock_dt["ep"],
            local_iso="2026-11-26T10:15:00+08:00",
            sig_key="501018:20261126:10:15:v9-entry:buy",
            qty_override=40000,  # wants ¥60,000 > free ¥39,998!
        )
    )
    assert placed_orders[-1]["symbol"] == "501018"
    assert placed_orders[-1]["qty"] * 1.500 <= 39998.0  # Reserve ¥40,002 for 160723 stays untouched!

    # 2) Meanwhile 160723 covering its OWN short leg (:v9-exit:buy) CAN use the remaining + reserved cash cleanly!
    asyncio.run(
        engine._emit_trade_signal(
            symbol="160723",
            item={
                "rule_id": "ai-160723-v9-plus-aggressive",
                "rule_title": "反T回补",
                "rule_summary": "short cover",
                "side": "buy",
                "minute": "10:16",
                "price": "1.950",
                "name": "嘉实原油",
                "market": "36",
                "signal_tag": "v9-exit",
                "t0_net_cap_mode": True,
            },
            config=cfg_root,
            trade_date="20261126",
            now_epoch=clock_dt["ep"] + 60.0,
            local_iso="2026-11-26T10:16:00+08:00",
            sig_key="160723:20261126:10:16:v9-exit:buy",
            qty_override=20000,  # ¥39,000 <= ¥40,002+ remaining cash!
        )
    )
    assert placed_orders[-1]["symbol"] == "160723"
    assert placed_orders[-1]["qty"] == 20000

    del brn_today
    rstore.close()
    qstore.close()
    astore.close()


def test_bridge_put_strategy_accepts_capital_policy_mode_and_pool_policy(tmp_path: Path) -> None:
    db_path = tmp_path / "bridge_pool_cfg.db"
    rstore = SQLiteResearchStore(db_path)
    qstore = SQLiteQuantRuleStore(db_path)
    astore = SQLiteAutoTradeStore(db_path)
    engine = AutoTradeEngine(
        auto_store=astore,
        research_store=rstore,
        broker=None,
        trade_executor=None,
        symbol_lookup=lambda s: CATALOG.get(s),
        quant_rule_store=qstore,
    )
    app = create_app(
        admin_password_hash=PasswordHasher().hash("admin-pass-123"),
        admin_session_secret="b" * 32,
        symbol_lookup=lambda s: CATALOG.get(s),
        market_account_store=SQLiteMarketAccountStore(tmp_path / "market.db"),
        research_store=rstore,
        goodluck_internal_token=TOKEN,
        auto_trade_engine=engine,
    )
    client = TestClient(app)

    res = client.put(
        "/api/internal/goodluck/trade/strategy",
        headers=_headers(role="admin"),
        json={
            "enabled": True,
            "signal_source": "intraday_macd_flow",
            "execution_mode": "semi_auto",
            "sizing_mode": "model_auto",
            "symbols": ["160723"],
            "order_quantity": 100,
            "pricing_mode": "counterparty_book",
            "order_book_level": 3,
            "slippage_ticks": 3,
            "auto_cancel_seconds": 30,
            "daily_max_buy_amount": 140000.0,
            "block_non_continuous_session": True,
            "capital_policy_mode": "route_d_shadow",
            "pool_policy": {"sigma_cap_pct": 3.5, "equity_solo_anchors": ["xop_us_ep"]},
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body["config"]["capital_policy_mode"] == "route_d_shadow"
    assert body["config"]["pool_policy"]["sigma_cap_pct"] == pytest.approx(3.5)
    assert body["config"]["pool_policy"]["equity_solo_anchors"] == ["xop_us_ep"]
    assert body["capital_ledger"]["pool_allocation"]["shadow_only"] is True

    qstore.close()
    astore.close()
    rstore.close()


def test_evaluate_live_snapshot_offhours_falls_back_to_latest_close_and_computes_dynamic_pool_weights(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("THS_QUANT_OFFLINE", "1")
    # Fixture seeds bars for 2026-11-26; clock at 2026-11-27 01:25 (next day before market open)
    ctx = _setup_v9_plus_fixture(
        tmp_path / "offhours_fallback.db",
        execution_mode="semi_auto",
        daily_max_buy_amount=140000.0,
    )
    rstore, qstore, astore, engine = ctx["rstore"], ctx["qstore"], ctx["astore"], ctx["engine"]
    engine.now_fn = lambda: datetime(2026, 11, 27, 1, 25, 0, tzinfo=SHANGHAI_TZ)

    engine.reconcile_and_update_config(
        {
            "capital_policy_mode": "route_d_sharpe_shield",
            "pool_policy": {"total_pool_cap": 140000.0},
        },
        reconcile_broker=False,
    )

    # 1) Live evaluation should NOT error with "V9+ 日内布伦特/分时样本尚在初始化，请稍候重试"
    snap = engine.evaluate_live_snapshot("160723")
    assert snap["data_ready"] is True
    assert snap["is_historical_close_snapshot"] is True
    assert snap["snapshot_trade_date"] == "20261126"
    assert "初始化" not in str(snap.get("note") or "")
    assert "20261126" in str(snap.get("note") or "")

    # 2) Dynamic position weight & capital allocation scale with total_pool_cap
    st140 = engine.get_status()
    assert st140["capital_policy_mode"] == "route_d_sharpe_shield"
    assert st140["active_capital_policy"]["params"]["sigma_cap_pct"] == pytest.approx(2.40)
    assert len(st140["available_capital_policies"]) >= 5
    pa140 = st140["capital_ledger"]["pool_allocation"]
    assert pa140["policy_id"] == "route_d_sharpe_shield"
    assert pa140["pool_cap"] == pytest.approx(140000.0)
    row140 = pa140["symbols"]["160723"]
    assert row140["full_signal_position_pct"] > 0.0
    assert row140["full_signal_capital_amount"] == pytest.approx(
        round(140000.0 * row140["full_signal_position_pct"] / 100.0, 2)
    )
    assert row140["target_capital_amount"] == pytest.approx(
        round(140000.0 * row140["target_position_pct"] / 100.0, 2)
    )

    engine.reconcile_and_update_config({"pool_policy": {"total_pool_cap": 200000.0}}, reconcile_broker=False)
    st200 = engine.get_status()
    assert st200["daily_usage"]["daily_max_buy_amount"] == pytest.approx(200000.0)
    row200 = st200["capital_ledger"]["pool_allocation"]["symbols"]["160723"]
    assert row200["full_signal_position_pct"] == pytest.approx(row140["full_signal_position_pct"])
    assert row200["full_signal_capital_amount"] == pytest.approx(
        round(200000.0 * row200["full_signal_position_pct"] / 100.0, 2)
    )

    rstore.close()
    qstore.close()
    astore.close()




def test_morning_cold_start_base_catchup_respects_gap_gate_and_enables_reverse_t(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("THS_QUANT_OFFLINE", "1")
    ctx = _setup_v9_plus_fixture(
        tmp_path / "morning_catchup.db",
        execution_mode="full_auto",
        daily_max_buy_amount=140000.0,
    )
    rstore, qstore, astore, clock_dt = ctx["rstore"], ctx["qstore"], ctx["astore"], ctx["clock_dt"]
    _seed_day_pts = ctx["seed_day_pts"]

    # Clear 10:05 bars seeded by _setup_v9_plus_fixture on 20261126 so morning clock (09:33..09:36) sees fresh bars
    with rstore._lock:
        rstore._connection.execute("DELETE FROM research_points WHERE trade_date = ?", ("20261126",))
        rstore._connection.commit()

    # Morning series on 20261126 starting at 09:30; previous close (pre_close) = 2.000
    morning_lof = [("09:30", 2.000), ("09:31", 2.005), ("09:32", 2.008), ("09:33", 2.010), ("09:34", 2.010), ("09:35", 2.010)]
    morning_brn = [("09:30", 70.0), ("09:33", 70.3), ("09:35", 70.3)]
    _seed_day_pts("BRN0Y", "20261126", 70.0, morning_brn, market="ICE")
    _seed_day_pts("160723", "20261126", 2.000, morning_lof)

    placed_orders: list[dict[str, Any]] = []

    class _CatchupExecutor:
        adb_serial = "emulator-5556"
        active_tab = "buy"

        def __init__(self) -> None:
            self.held_shares = 0
            self.avail_cash = 140000.0

        def _make_snap(self) -> dict[str, Any]:
            mv = round(self.held_shares * 2.010, 2)
            positions = (
                [
                    {
                        "symbol": "160723",
                        "name": "嘉实原油",
                        "shares": self.held_shares,
                        "available_shares": self.held_shares,
                        "market_value": f"{mv:.2f}",
                        "current_price": "2.010",
                    }
                ]
                if self.held_shares > 0
                else []
            )
            return {
                "logged_in": True,
                "metrics": {
                    "available_cash": f"{self.avail_cash:.2f}",
                    "total_assets": f"{self.avail_cash + mv:.2f}",
                },
                "positions": positions,
                "captured_at": clock_dt["dt"].isoformat(timespec="seconds"),
            }

        def _cached_account_snapshot(self, *_a: Any, **_k: Any) -> dict[str, Any]:
            return self._make_snap()

        def get_account_summary(self, *_a: Any, **_k: Any) -> dict[str, Any]:
            return self._make_snap()

        def place_order(self, **kwargs: Any) -> dict[str, Any]:
            qty = int(kwargs["quantity"])
            px = float(kwargs["price"])
            side = str(kwargs["side"])
            if side == "buy":
                self.held_shares += qty
                self.avail_cash = round(self.avail_cash - qty * px, 2)
            else:
                self.held_shares = max(0, self.held_shares - qty)
                self.avail_cash = round(self.avail_cash + qty * px, 2)
            placed_orders.append({"symbol": kwargs["symbol"], "side": side, "qty": qty, "price": px})
            return {
                "accepted": True,
                "status": "SUBMITTED",
                "entrust_no": f"MC{len(placed_orders):04d}",
                "dialog_message": "委托已提交",
            }

    exec_obj = _CatchupExecutor()
    engine = AutoTradeEngine(
        auto_store=astore,
        research_store=rstore,
        broker=None,
        trade_executor=exec_obj,
        symbol_lookup=lambda s: CATALOG.get(s),
        is_trading_day=lambda _d: True,
        now_fn=lambda: clock_dt["dt"],
        epoch_fn=lambda: clock_dt["ep"],
        quant_rule_store=qstore,
    )
    astore.update_config(
        {
            "enabled": True,
            "signal_source": "quant_rule",
            "quant_rule_id": "ai-160723-v9-plus-aggressive",
            "execution_mode": "full_auto",
            "sizing_mode": "model_auto",
            "capital_policy_mode": "route_d_sharpe_shield",
            "daily_max_buy_amount": 140000.0,
            "pool_policy": {"total_pool_cap": 140000.0},
            "symbols": ["160723"],
            "symbol_profiles": {
                "160723": {
                    "enabled": True,
                    "signal_source": "quant_rule",
                    "quant_rule_id": "ai-160723-v9-plus-aggressive",
                    "sizing_mode": "model_auto",
                    "capital_cap": 140000.0,
                }
            },
        }
    )

    from level2_service.v9_plus_engine import evaluate_v9_plus_live_indicators

    real_ind_fn = evaluate_v9_plus_live_indicators
    probe_state = {"sp_fx": 0.55, "price": 2.010, "prem": 0.20, "wt_pct": 90.0}

    def _stub_indicators(store: Any, **kwargs: Any) -> dict[str, Any] | None:
        td = str(kwargs.get("trade_date") or "")
        if td < "20261126":
            return {
                "diagnostics": {
                    "price": 2.000,
                    "prem_pct": 0.10,
                    "sp_fx_pct": 0.05,
                    "close_target_weight_pct": 90.0,
                    "close_target_reason": "隔夜目标重仓90%",
                    "sell_s_sig": False,
                    "cross_market_anchor": "brent",
                }
            }
        return {
            "diagnostics": {
                "price": float(probe_state["price"]),
                "prem_pct": float(probe_state["prem"]),
                "sp_fx_pct": float(probe_state["sp_fx"]),
                "close_target_weight_pct": float(probe_state["wt_pct"]),
                "close_target_reason": "隔夜延续目标仓位",
                "sell_s_sig": False,
                "cross_market_anchor": "brent",
            }
        }

    with monkeypatch.context() as mp:
        mp.setattr("level2_service.auto_trade.evaluate_v9_plus_live_indicators", _stub_indicators)

        # 1) 09:33 sp_fx_pct = +0.55% (> +0.38%) -> blocked!
        clock_dt["dt"] = datetime(2026, 11, 26, 9, 33, tzinfo=SHANGHAI_TZ)
        clock_dt["ep"] += 60.0
        res_block_sp = asyncio.run(engine.poll_once())
        assert res_block_sp["triggered"] == 0
        assert placed_orders == []

        # 2) 09:34 gap_pct = +3.00% (2.060 vs 2.000 > +2.50%) -> blocked!
        probe_state.update({"sp_fx": 0.10, "price": 2.060})
        clock_dt["dt"] = datetime(2026, 11, 26, 9, 34, tzinfo=SHANGHAI_TZ)
        clock_dt["ep"] += 60.0
        res_block_gap = asyncio.run(engine.poll_once())
        assert res_block_gap["triggered"] == 0
        assert placed_orders == []

        # 3) 09:35 safe fair open (gap +0.50%, sp_fx +0.10%, prem +0.20%) -> emits 1 catch-up buy!
        probe_state.update({"sp_fx": 0.10, "price": 2.010})
        clock_dt["dt"] = datetime(2026, 11, 26, 9, 35, tzinfo=SHANGHAI_TZ)
        clock_dt["ep"] += 60.0
        res_buy = asyncio.run(engine.poll_once())
        assert res_buy["triggered"] == 1
        assert len(placed_orders) == 1
        assert placed_orders[0]["side"] == "buy"
        caught_qty = placed_orders[0]["qty"]
        assert caught_qty >= 100
        sigs_after_buy = astore.list_today_symbol_signals("20261126", "160723")
        assert any(":v9-init-base:buy" in str(s.get("signal_key") or "") for s in sigs_after_buy)
        row_title = astore._connection.execute(
            "SELECT rule_title FROM auto_trade_signals WHERE trade_date=? AND symbol=? AND signal_key LIKE '%:v9-init-base:%'",
            ("20261126", "160723"),
        ).fetchone()
        assert row_title is not None and "早盘补底仓" in str(row_title["rule_title"])
        assert astore.today_symbol_activity("20261126", "160723")["buy_entry_count"] == 0

        # 4) 09:36 next poll -> idempotent, zero duplicate buys!
        clock_dt["dt"] = datetime(2026, 11, 26, 9, 36, tzinfo=SHANGHAI_TZ)
        clock_dt["ep"] += 60.0
        res_idem = asyncio.run(engine.poll_once())
        assert res_idem["triggered"] == 0
        assert len(placed_orders) == 1

    # 5) 10:15 real V9+ engine runs intraday spike -> reverse-T sell triggers on caught-up base shares while retaining >= 100 floor shares!
    assert real_ind_fn is not None
    spike_lof = morning_lof + [(f"09:{m:02d}", 2.005) for m in range(36, 60)] + [(f"10:{m:02d}", 2.008) for m in range(0, 12)] + [
        ("10:12", 2.030),
        ("10:13", 2.045),
        ("10:14", 2.060),
        ("10:15", 2.055),
    ]
    _seed_day_pts("BRN0Y", "20261126", 70.0, morning_brn + [("10:15", 70.0)], market="ICE")
    _seed_day_pts("160723", "20261126", 2.000, spike_lof)
    clock_dt["dt"] = datetime(2026, 11, 26, 10, 15, tzinfo=SHANGHAI_TZ)
    clock_dt["ep"] += 2340.0
    res_sell = asyncio.run(engine.poll_once())
    assert res_sell["triggered"] == 1
    assert len(placed_orders) == 2
    assert placed_orders[-1]["side"] == "sell"
    assert placed_orders[-1]["qty"] <= caught_qty - 100
    assert exec_obj.held_shares >= 100

    rstore.close()
    qstore.close()
    astore.close()
