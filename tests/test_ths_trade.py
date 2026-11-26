from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import xml.etree.ElementTree as ET

from argon2 import PasswordHasher
from fastapi.testclient import TestClient
import pytest

from level2_service.api import create_app
from level2_service.market_accounts import SQLiteMarketAccountStore
from level2_service.parsed_values import SymbolLookup
from level2_service.runner import RunnerControl
from level2_service.ths_trade import (
    TonghuashunTradeExecutor,
    TradeExecutionError,
    detect_auth_blocking,
    detect_modal_dialog,
    mask_sensitive_numbers,
    parse_account_metrics_xml,
    parse_form_readback,
    parse_holdings_xml,
    parse_orders_xml,
    parse_trades_xml,
)


TOKEN = "trade-bridge-secret-token"
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


def _holdings_xml() -> str:
    return """<hierarchy>
  <node package="com.hexin.plat.android" resource-id="com.hexin.plat.android:id/frame_layout">
    <node resource-id="com.hexin.plat.android:id/page_title_view" text="国泰海通证券" />
    <node resource-id="com.hexin.plat.android:id/account_info_view" text="**9306" />
    <node resource-id="com.hexin.plat.android:id/btn" text="买入" selected="false" bounds="[0,216][216,336]" />
    <node resource-id="com.hexin.plat.android:id/btn" text="卖出" selected="false" bounds="[216,216][432,336]" />
    <node resource-id="com.hexin.plat.android:id/btn" text="撤单" selected="false" bounds="[432,216][648,336]" />
    <node resource-id="com.hexin.plat.android:id/btn" text="持仓" selected="true" bounds="[648,216][864,336]" />
    <node resource-id="com.hexin.plat.android:id/btn" text="查询" selected="false" bounds="[864,216][1080,336]" />
    <node resource-id="com.hexin.plat.android:id/position_text" text="仓位 74.3%" />
    <node resource-id="com.hexin.plat.android:id/capital_cell_title" text="总资产" bounds="[48,482][174,544]" />
    <node resource-id="com.hexin.plat.android:id/capital_cell_value" text="128,708.93" bounds="[48,559][346,616]" />
    <node resource-id="com.hexin.plat.android:id/capital_cell_title" text="浮动盈亏" bounds="[376,482][544,544]" />
    <node resource-id="com.hexin.plat.android:id/capital_cell_value" text="-36,837.85" bounds="[376,559][674,616]" />
    <node resource-id="com.hexin.plat.android:id/capital_cell_title" text="总市值" bounds="[48,700][174,762]" />
    <node resource-id="com.hexin.plat.android:id/capital_cell_value" text="95,624.30" bounds="[48,777][346,834]" />
    <node resource-id="com.hexin.plat.android:id/capital_cell_title" text="可用" bounds="[376,700][460,762]" />
    <node resource-id="com.hexin.plat.android:id/capital_cell_value" text="33,084.63" bounds="[376,777][674,834]" />
    <node resource-id="com.hexin.plat.android:id/capital_cell_title" text="可取" bounds="[704,700][788,762]" />
    <node resource-id="com.hexin.plat.android:id/capital_cell_value" text="196.71" bounds="[704,777][1002,834]" />
    <node resource-id="com.hexin.plat.android:id/recyclerview_id">
      <node>
        <node text="嘉实原油" />
        <node text="33,111.30" />
        <node text="-5,533.31" />
        <node text="-14.318%" />
        <node text="15700" />
        <node text="15700" />
        <node text="2.461" />
        <node text="2.109" />
      </node>
      <node>
        <node text="查看已清仓股票" />
      </node>
    </node>
  </node>
</hierarchy>"""


def _buy_form_xml(code: str = "160723", price: str = "2.200", qty: str = "100") -> str:
    return f"""<hierarchy>
  <node package="com.hexin.plat.android" resource-id="com.hexin.plat.android:id/frame_layout">
    <node resource-id="com.hexin.plat.android:id/page_title_view" text="国泰海通证券" />
    <node resource-id="com.hexin.plat.android:id/account_info_view" text="**9306" />
    <node resource-id="com.hexin.plat.android:id/btn" text="买入" selected="true" bounds="[0,216][216,336]" />
    <node resource-id="com.hexin.plat.android:id/btn" text="卖出" selected="false" bounds="[216,216][432,336]" />
    <node resource-id="com.hexin.plat.android:id/btn" text="撤单" selected="false" bounds="[432,216][648,336]" />
    <node resource-id="com.hexin.plat.android:id/btn" text="持仓" selected="false" bounds="[648,216][864,336]" />
    <node resource-id="com.hexin.plat.android:id/btn" text="查询" selected="false" bounds="[864,216][1080,336]" />
    <node resource-id="com.hexin.plat.android:id/auto_stockcode" class="android.widget.EditText" text="{code}" bounds="[48,421][341,529]" />
    <node resource-id="com.hexin.plat.android:id/stockname" text="嘉实原油LOF" bounds="[341,421][702,529]" />
    <node resource-id="com.hexin.plat.android:id/stockprice">
      <node class="android.widget.EditText" text="{price}" bounds="[156,556][594,664]" />
    </node>
    <node resource-id="com.hexin.plat.android:id/dietingprice" text="1.898" />
    <node resource-id="com.hexin.plat.android:id/zhangtingprice" text="2.320" />
    <node resource-id="com.hexin.plat.android:id/stockvolume">
      <node class="android.widget.EditText" text="{qty}" bounds="[156,736][594,844]" />
    </node>
    <node resource-id="com.hexin.plat.android:id/couldbuy" text="可买15000股" />
    <node resource-id="com.hexin.plat.android:id/btn_transaction" clickable="true" bounds="[195,1007][702,1130]">
      <node text="买 入" clickable="true" bounds="[195,1007][702,1130]" />
    </node>
  </node>
</hierarchy>"""


def _confirm_dialog_xml(symbol: str = "160723", price: str = "2.200", qty: str = "100") -> str:
    return f"""<hierarchy>
  <node package="com.hexin.plat.android" resource-id="com.hexin.plat.android:id/frame_layout">
    <node resource-id="com.hexin.plat.android:id/page_title_view" text="国泰海通证券" />
  </node>
  <node package="com.hexin.plat.android">
    <node text="委托买入确认" />
    <node text="证券代码 {symbol} 嘉实原油LOF" />
    <node text="委托价格 {price}" />
    <node text="委托数量 {qty}" />
    <node text="取消" clickable="true" bounds="[100,1200][400,1300]" />
    <node text="确认买入" clickable="true" bounds="[600,1200][900,1300]" />
  </node>
</hierarchy>"""


def _receipt_dialog_xml(message: str = "委托已提交，合同编号: HT20261125001") -> str:
    return f"""<hierarchy>
  <node package="com.hexin.plat.android" resource-id="com.hexin.plat.android:id/frame_layout">
    <node resource-id="com.hexin.plat.android:id/page_title_view" text="国泰海通证券" />
  </node>
  <node package="com.hexin.plat.android">
    <node text="提示" />
    <node text="{message}" />
    <node text="确定" clickable="true" bounds="[300,1200][780,1300]" />
  </node>
</hierarchy>"""


def _orders_xml(status: str = "已报") -> str:
    return f"""<hierarchy>
  <node package="com.hexin.plat.android" resource-id="com.hexin.plat.android:id/frame_layout">
    <node resource-id="com.hexin.plat.android:id/page_title_view" text="国泰海通证券" />
    <node resource-id="com.hexin.plat.android:id/btn" text="当日委托" selected="true" bounds="[0,216][270,336]" />
    <node resource-id="com.hexin.plat.android:id/btn" text="当日成交" selected="false" bounds="[270,216][540,336]" />
    <node resource-id="com.hexin.plat.android:id/recyclerview_id">
      <node bounds="[0,446][1080,611]">
        <node text="嘉实原油" />
        <node text="16:45:00" />
        <node text="2.200" />
        <node text="0.000" />
        <node text="100" />
        <node text="0" />
        <node text="买入" />
        <node text="{status}" />
      </node>
    </node>
  </node>
</hierarchy>"""


def _cancel_tab_xml(*, has_order: bool) -> str:
    if not has_order:
        return """<hierarchy>
  <node package="com.hexin.plat.android" resource-id="com.hexin.plat.android:id/frame_layout">
    <node resource-id="com.hexin.plat.android:id/btn" text="买入" selected="false" bounds="[0,216][216,336]" />
    <node resource-id="com.hexin.plat.android:id/btn" text="撤单" selected="true" bounds="[432,216][648,336]" />
    <node resource-id="com.hexin.plat.android:id/btn" text="持仓" selected="false" bounds="[648,216][864,336]" />
    <node resource-id="com.hexin.plat.android:id/btn" text="查询" selected="false" bounds="[864,216][1080,336]" />
    <node resource-id="com.hexin.plat.android:id/empty_tips" text="当前无可撤委托单" />
  </node>
</hierarchy>"""
    return """<hierarchy>
  <node package="com.hexin.plat.android" resource-id="com.hexin.plat.android:id/frame_layout">
    <node resource-id="com.hexin.plat.android:id/btn" text="买入" selected="false" bounds="[0,216][216,336]" />
    <node resource-id="com.hexin.plat.android:id/btn" text="撤单" selected="true" bounds="[432,216][648,336]" />
    <node resource-id="com.hexin.plat.android:id/btn" text="持仓" selected="false" bounds="[648,216][864,336]" />
    <node resource-id="com.hexin.plat.android:id/btn" text="查询" selected="false" bounds="[864,216][1080,336]" />
    <node resource-id="com.hexin.plat.android:id/recyclerview_id" bounds="[0,446][1080,650]">
      <node bounds="[0,446][1080,611]">
        <node text="嘉实原油" />
        <node text="16:45:00" />
        <node text="2.200" />
        <node text="0.000" />
        <node text="100" />
        <node text="0" />
        <node text="买入" />
        <node text="已报" />
      </node>
    </node>
    <node resource-id="com.hexin.plat.android:id/cannot_chedan_title_text" text="今日成交单" bounds="[435,1010][645,1118]" />
  </node>
</hierarchy>"""


def _trades_xml() -> str:
    return """<hierarchy>
  <node package="com.hexin.plat.android" resource-id="com.hexin.plat.android:id/frame_layout">
    <node resource-id="com.hexin.plat.android:id/btn" text="当日委托" selected="false" bounds="[0,216][270,336]" />
    <node resource-id="com.hexin.plat.android:id/btn" text="当日成交" selected="true" bounds="[270,216][540,336]" />
    <node resource-id="com.hexin.plat.android:id/recyclerview_id">
      <node>
        <node resource-id="com.hexin.plat.android:id/stock_name" text="嘉实原油" />
        <node resource-id="com.hexin.plat.android:id/deal_time" text="14:47:12" />
        <node resource-id="com.hexin.plat.android:id/price" text="2.113" />
        <node resource-id="com.hexin.plat.android:id/amount" text="15600" />
        <node resource-id="com.hexin.plat.android:id/opt_type" text="买入" />
        <node resource-id="com.hexin.plat.android:id/deal_money" text="32962.800" />
      </node>
    </node>
  </node>
</hierarchy>"""


@dataclass
class _FakeWidget:
    device: "_FakeU2Device"
    selector: dict

    @property
    def exists(self) -> bool:
        return True

    def click(self) -> None:
        self.device.events.append(("widget_click", dict(self.selector)))

    def set_text(self, value: str) -> None:
        rid = self.selector.get("resourceId")
        self.device.events.append(("set_text", rid, value))
        if rid == "com.hexin.plat.android:id/auto_stockcode":
            self.device.form_code = value
        elif rid == "com.hexin.plat.android:id/stockprice":
            self.device.form_price = value
        elif rid == "com.hexin.plat.android:id/stockvolume":
            self.device.form_qty = value

    def child(self, **kwargs):
        merged = dict(self.selector)
        merged["child"] = kwargs
        return _FakeWidget(self.device, merged)


@dataclass
class _FakeU2Device:
    screen: str = "buy"
    form_code: str = "160723"
    form_price: str = "2.109"
    form_qty: str = "100"
    order_open: bool = False
    order_status: str = "已报"
    readback_override_qty: str | None = None
    events: list[tuple] = field(default_factory=list)
    info: dict = field(default_factory=lambda: {"displayWidth": 1080, "displayHeight": 1920})

    def __call__(self, **kwargs):
        return _FakeWidget(self, kwargs)

    def app_current(self) -> dict[str, str]:
        return {"package": "com.hexin.plat.android"}

    def click(self, x: int, y: int) -> None:
        self.events.append(("click", x, y))
        if self.screen == "buy" and 195 <= x <= 702 and 1007 <= y <= 1130:
            self.screen = "confirm"
        elif self.screen == "confirm":
            if 600 <= x <= 900:
                self.order_open = True
                self.order_status = "已报"
                self.screen = "receipt"
            else:
                self.screen = "buy"
        elif self.screen == "receipt":
            self.screen = "buy"
        elif self.screen == "cancel" and 0 <= x <= 1080 and 446 <= y <= 611:
            self.screen = "cancel_confirm"
        elif self.screen == "cancel_confirm" and 600 <= x <= 900:
            self.order_open = False
            self.order_status = "订单已撤销"
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

    def press(self, key: str) -> None:
        self.events.append(("press", key))
        if key == "back" and self.screen in {"orders", "trades"}:
            self.screen = "holdings"

    def swipe(self, *_args, **_kwargs) -> None:
        self.events.append(("swipe",))

    def dump_hierarchy(self, compressed: bool = False) -> str:
        if self.screen == "holdings":
            return _holdings_xml()
        if self.screen == "buy":
            qty = self.readback_override_qty if self.readback_override_qty is not None else self.form_qty
            return _buy_form_xml(self.form_code, self.form_price, qty)
        if self.screen == "confirm":
            return _confirm_dialog_xml(self.form_code, self.form_price, self.form_qty)
        if self.screen == "receipt":
            return _receipt_dialog_xml()
        if self.screen == "cancel":
            return _cancel_tab_xml(has_order=self.order_open)
        if self.screen == "cancel_confirm":
            return """<hierarchy>
              <node package="com.hexin.plat.android" resource-id="com.hexin.plat.android:id/frame_layout"><node text="国泰海通证券" /></node>
              <node package="com.hexin.plat.android"><node text="确认撤单？" /><node text="取消" clickable="true" bounds="[100,1200][400,1300]" /><node text="确定" clickable="true" bounds="[600,1200][900,1300]" /></node>
            </hierarchy>"""
        if self.screen == "query":
            return """<hierarchy>
              <node package="com.hexin.plat.android" resource-id="com.hexin.plat.android:id/frame_layout">
                <node resource-id="com.hexin.plat.android:id/btn" text="买入" bounds="[0,216][216,336]" />
                <node resource-id="com.hexin.plat.android:id/btn" text="持仓" bounds="[648,216][864,336]" />
                <node resource-id="com.hexin.plat.android:id/drwt" text="当日委托" bounds="[0,481][1080,625]" />
                <node resource-id="com.hexin.plat.android:id/drcj" text="当日成交" bounds="[0,337][1080,481]" />
              </node>
            </hierarchy>"""
        if self.screen == "orders":
            return _orders_xml(self.order_status)
        if self.screen == "trades":
            return _trades_xml()
        return _holdings_xml()


def test_refuses_protected_fund_emulator() -> None:
    with pytest.raises(ValueError):
        TonghuashunTradeExecutor(adb_serial="emulator-5554")
    with pytest.raises(ValueError):
        TonghuashunTradeExecutor(adb_serial="emulator-5559", prohibited_serial="emulator-5559")


def test_mask_sensitive_numbers_keeps_trade_values() -> None:
    raw = "资金帐户 123456789306 股东代码 A0192839306 代码 160723 价格 2.200 数量 100 金额 32962.800"
    masked = mask_sensitive_numbers(raw, keep_symbols={"160723"})
    assert "123456789306" not in masked
    assert "A0192839306" not in masked
    assert "****9306" in masked
    assert "160723" in masked
    assert "2.200" in masked
    assert "32962.800" in masked


def test_xml_parsers_extract_account_holdings_orders_trades_and_dialogs() -> None:
    acct = parse_account_metrics_xml(ET.fromstring(_holdings_xml()))
    assert acct["broker_name"] == "国泰海通证券"
    assert acct["metrics"]["total_assets"] == "128,708.93"
    assert acct["metrics"]["available_cash"] == "33,084.63"

    holdings = parse_holdings_xml(ET.fromstring(_holdings_xml()), name_to_symbol=lambda n: "160723" if "嘉实原油" in n else None)
    assert len(holdings) == 1
    assert holdings[0]["symbol"] == "160723"
    assert holdings[0]["available_shares"] == 15700

    orders = parse_orders_xml(ET.fromstring(_orders_xml("已报")), name_to_symbol=lambda n: "160723" if "嘉实原油" in n else None)
    assert orders[0]["cancellable"] is True
    assert orders[0]["quantity"] == 100

    trades = parse_trades_xml(ET.fromstring(_trades_xml()), name_to_symbol=lambda n: "160723" if "嘉实原油" in n else None)
    assert trades[0]["filled_price"] == "2.113"
    assert trades[0]["filled_quantity"] == 15600

    dialog = detect_modal_dialog(ET.fromstring(_confirm_dialog_xml()))
    assert dialog is not None
    assert dialog.kind == "confirm"
    assert dialog.confirm_button is not None

    auth_xml = ET.fromstring('<hierarchy><node package="com.hexin.plat.android" text="请输入交易密码" password="true" /></hierarchy>')
    assert detect_auth_blocking(auth_xml) is not None

    rb = parse_form_readback(ET.fromstring(_buy_form_xml()))
    assert rb["symbol"] == "160723"
    assert rb["price"] == "2.200"
    assert rb["quantity"] == "100"


def test_executor_blocks_on_readback_mismatch_and_supports_dry_run_and_auto_cancel(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("level2_service.ths_trade.time.sleep", lambda _s: None)

    fake_dev = _FakeU2Device(readback_override_qty="200")
    executor = TonghuashunTradeExecutor(
        adb_serial="emulator-5556",
        u2_factory=lambda _s: fake_dev,
        name_to_symbol=lambda n: "160723" if "嘉实原油" in n else None,
    )
    with pytest.raises(TradeExecutionError) as exc_info:
        executor.place_order(symbol="160723", expected_name="嘉实原油", side="buy", price="2.200", quantity=100, precision=3)
    assert exc_info.value.error_code == "TRADE_INPUT_READBACK_MISMATCH"
    assert ("click", 448, 1068) not in fake_dev.events

    # Now normal dry_run
    fake_dev.readback_override_qty = None
    dry = executor.place_order(
        symbol="160723",
        expected_name="嘉实原油",
        side="buy",
        price="2.200",
        quantity=100,
        precision=3,
        dry_run=True,
    )
    assert dry["status"] == "DRY_RUN_CONFIRMED"
    assert dry["submitted"] is False
    for key in ("total", "nav_ms", "fill_ms", "readback_ms", "submit_confirm_ms", "gateway_reply_ms", "reconcile_ms"):
        assert isinstance(dry["timings_ms"][key], int)

    # Live order + auto_cancel
    live = executor.place_order(
        symbol="160723",
        expected_name="嘉实原油",
        side="buy",
        price="2.200",
        quantity=100,
        precision=3,
        dry_run=False,
        auto_cancel=True,
    )
    assert live["submitted"] is True
    assert live["accepted"] is True
    assert live["entrust_no"] == "HT20261125001"
    assert live["auto_cancel_attempted"] is True
    assert live["auto_cancel_ok"] is True
    assert live["status"] == "AUTO_CANCELLED"
    assert live["orders"][0]["status"] == "订单已撤销"
    for key in ("total", "nav_ms", "fill_ms", "readback_ms", "submit_confirm_ms", "gateway_reply_ms", "reconcile_ms"):
        assert isinstance(live["timings_ms"][key], int)


def test_executor_respects_runner_lock() -> None:
    control = RunnerControl()
    assert control.lock("admin-session-1") is True
    executor = TonghuashunTradeExecutor(adb_serial="emulator-5556", runner_control=control, u2_factory=lambda _s: _FakeU2Device())
    with pytest.raises(TradeExecutionError) as exc:
        executor.get_account_summary()
    assert exc.value.error_code == "TRADE_DEVICE_BUSY"


def test_bridge_trade_routes_enforce_admin_and_validate_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("level2_service.ths_trade.time.sleep", lambda _s: None)
    fake_dev = _FakeU2Device()
    executor = TonghuashunTradeExecutor(
        adb_serial="emulator-5556",
        u2_factory=lambda _s: fake_dev,
        name_to_symbol=lambda n: "160723" if "嘉实原油" in n else None,
    )
    app = create_app(
        admin_password_hash=PasswordHasher().hash("admin-pass-123"),
        admin_session_secret="a" * 32,
        symbol_lookup=lambda sym: CATALOG[sym],
        market_account_store=SQLiteMarketAccountStore(tmp_path / "market.db"),
        goodluck_internal_token=TOKEN,
        trade_executor=executor,
    )
    client = TestClient(app)

    # Non-admin user rejected with 403
    assert client.get("/api/internal/goodluck/trade/account", headers=_headers(role="user")).status_code == 403
    assert client.post(
        "/api/internal/goodluck/trade/orders",
        headers=_headers(role="user"),
        json={"symbol": "160723", "side": "buy", "price": "2.200", "quantity": 100},
    ).status_code == 403

    # Admin succeeds
    acct_res = client.get("/api/internal/goodluck/trade/account", headers=_headers(role="admin"))
    assert acct_res.status_code == 200
    assert acct_res.json()["broker_name"] == "国泰海通证券"

    bad_qty = client.post(
        "/api/internal/goodluck/trade/orders",
        headers=_headers(role="admin"),
        json={"symbol": "160723", "side": "buy", "price": "2.200", "quantity": 150},
    )
    assert bad_qty.status_code == 422

    order_res = client.post(
        "/api/internal/goodluck/trade/orders",
        headers=_headers(role="admin"),
        json={"symbol": "160723", "side": "buy", "price": "2.2", "quantity": 100, "dry_run": False, "auto_cancel": True},
    )
    assert order_res.status_code == 200
    payload = order_res.json()
    assert payload["price"] == "2.200"
    assert payload["status"] == "AUTO_CANCELLED"


def test_detect_modal_dialog_handles_standalone_window_and_secondary_confirm() -> None:
    single_alert_xml = """<hierarchy>
  <node package="com.android.systemui" bounds="[0,0][1080,72]" />
  <node package="com.hexin.plat.android" bounds="[90,720][990,1200]">
    <node text="提示" />
    <node text="当前为清算时间，不允许委托" />
    <node text="确定" clickable="true" bounds="[360,1080][720,1170]" />
  </node>
</hierarchy>"""
    alert = detect_modal_dialog(ET.fromstring(single_alert_xml))
    assert alert is not None
    assert alert.kind == "alert"
    assert "当前为清算时间，不允许委托" in alert.message
    assert alert.ack_button == ("确定", (360, 1080, 720, 1170))

    secondary_confirm_xml = """<hierarchy>
  <node package="com.hexin.plat.android" bounds="[60,640][1020,1260]">
    <node text="交易风险提示" />
    <node text="当前非正常交易时间，是否继续提交委托？" />
    <node text="取消" clickable="true" bounds="[120,1120][480,1210]" />
    <node text="继续委托" clickable="true" bounds="[600,1120][960,1210]" />
  </node>
</hierarchy>"""
    sec = detect_modal_dialog(ET.fromstring(secondary_confirm_xml))
    assert sec is not None
    assert sec.kind == "confirm"
    assert sec.cancel_button == ("取消", (120, 1120, 480, 1210))
    assert sec.confirm_button == ("继续委托", (600, 1120, 960, 1210))


def test_place_order_classifies_gateway_closed_alert_as_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("level2_service.ths_trade.time.sleep", lambda _s: None)

    class _GatewayClosedDevice(_FakeU2Device):
        def click(self, x: int, y: int) -> None:
            super().click(x, y)
            if self.screen == "receipt":
                self.order_open = False

        def dump_hierarchy(self, compressed: bool = False) -> str:
            if self.screen == "receipt":
                return """<hierarchy>
  <node package="com.android.systemui" bounds="[0,0][1080,72]" />
  <node package="com.hexin.plat.android" bounds="[90,720][990,1200]">
    <node text="系统信息" />
    <node text="网关开关关闭[-99001101]" />
    <node text="确定" clickable="true" bounds="[360,1080][720,1170]" />
  </node>
</hierarchy>"""
            if self.screen == "orders":
                return """<hierarchy>
  <node package="com.hexin.plat.android" resource-id="com.hexin.plat.android:id/frame_layout">
    <node resource-id="com.hexin.plat.android:id/btn" text="当日委托" selected="true" bounds="[0,216][270,336]" />
    <node resource-id="com.hexin.plat.android:id/recyclerview_id" />
  </node>
</hierarchy>"""
            return super().dump_hierarchy(compressed=compressed)

    dev = _GatewayClosedDevice()
    executor = TonghuashunTradeExecutor(
        adb_serial="emulator-5556",
        u2_factory=lambda _s: dev,
        name_to_symbol=lambda n: "160723" if "嘉实原油" in n else None,
    )
    # Pre-populate cached trades so fast-reject preserves last known trades without leaving buy tab
    trades_res = executor.get_today_trades()
    assert len(trades_res["trades"]) == 1

    res = executor.place_order(
        symbol="160723",
        expected_name="嘉实原油",
        side="buy",
        price="2.200",
        quantity=100,
        precision=3,
        dry_run=False,
        auto_cancel=True,
    )
    assert res["status"] == "REJECTED"
    assert res["accepted"] is False
    assert "网关开关关闭" in res["dialog_message"]
    assert dev.screen == "buy"
    assert len(res["trades"]) == 1
    for key in ("total", "nav_ms", "fill_ms", "readback_ms", "submit_confirm_ms", "gateway_reply_ms", "reconcile_ms"):
        assert isinstance(res["timings_ms"][key], int)

    # 2nd warm call with identical symbol/price/qty skips all set_text events
    events_before = len(dev.events)
    res2 = executor.place_order(
        symbol="160723",
        expected_name="嘉实原油",
        side="buy",
        price="2.200",
        quantity=100,
        precision=3,
        dry_run=False,
        auto_cancel=True,
    )
    assert res2["status"] == "REJECTED"
    assert dev.screen == "buy"
    new_set_texts = [ev for ev in dev.events[events_before:] if ev[0] == "set_text"]
    assert new_set_texts == []


def test_compressed_parse_form_readback_handles_elided_wrappers() -> None:
    compressed_xml = """<hierarchy>
  <node package="com.hexin.plat.android">
    <node resource-id="com.hexin.plat.android:id/auto_stockcode" class="android.widget.EditText" text="160723" bounds="[48,421][341,529]" />
    <node resource-id="com.hexin.plat.android:id/stockname" text="嘉实原油LOF" bounds="[341,421][702,529]" />
    <node class="android.widget.EditText" text="2.205" bounds="[156,556][594,664]" />
    <node class="android.widget.EditText" text="300" bounds="[156,736][594,844]" />
    <node resource-id="com.hexin.plat.android:id/btn_transaction" text="买 入" clickable="true" bounds="[195,1007][702,1130]" />
  </node>
</hierarchy>"""
    rb = parse_form_readback(ET.fromstring(compressed_xml))
    assert rb["symbol"] == "160723"
    assert rb["name"] == "嘉实原油LOF"
    assert rb["price"] == "2.205"
    assert rb["quantity"] == "300"
    assert rb["has_submit_button"] is True


def test_fast_reconcile_skips_three_tab_switches_and_keeps_buy_screen_hot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("level2_service.ths_trade.time.sleep", lambda _s: None)

    class _TrackedDevice(_FakeU2Device):
        def __init__(self, **kwargs) -> None:
            super().__init__(**kwargs)
            self.dump_compressed_flags: list[bool] = []
            self.app_current_calls = 0

        def app_current(self) -> dict[str, str]:
            self.app_current_calls += 1
            return super().app_current()

        def dump_hierarchy(self, compressed: bool = False) -> str:
            self.dump_compressed_flags.append(compressed)
            return super().dump_hierarchy(compressed=compressed)

    dev = _TrackedDevice(screen="buy", form_code="160723", form_price="2.200", form_qty="100")
    executor = TonghuashunTradeExecutor(
        adb_serial="emulator-5556",
        u2_factory=lambda _s: dev,
        name_to_symbol=lambda n: "160723" if "嘉实原油" in n else None,
    )
    res = executor.place_order(
        symbol="160723",
        expected_name="嘉实原油",
        side="buy",
        price="2.200",
        quantity=100,
        precision=3,
        dry_run=False,
        auto_cancel=False,
    )
    assert res["status"] == "SUBMITTED"
    assert res["accepted"] is True
    assert res["entrust_no"] == "HT20261125001"
    assert res["reconcile_mode"] == "fast"
    assert res["prestaged"] is True
    assert res["timings_ms"]["prestaged_hit"] == 1
    assert res["matched_order"]["entrust_no"] == "HT20261125001"
    assert res["orders"][0]["entrust_no"] == "HT20261125001"
    assert dev.screen == "buy"
    assert dev.app_current_calls == 0
    assert all(flag is True for flag in dev.dump_compressed_flags)
    # Must NOT have clicked 撤单 or 查询 tabs
    tab_clicks = [
        (x, y)
        for ev, *rest in dev.events
        if ev == "click"
        for x, y in [(rest[0], rest[1])]
        if 216 <= y <= 336 and (432 <= x <= 648 or 864 <= x <= 1080)
    ]
    assert tab_clicks == []

    # Fallback: explicit reconcile_mode="full" performs full 撤单/当日委托/当日成交 reconcile
    full_res = executor.place_order(
        symbol="160723",
        expected_name="嘉实原油",
        side="buy",
        price="2.200",
        quantity=100,
        precision=3,
        dry_run=False,
        auto_cancel=False,
        reconcile_mode="full",
    )
    assert full_res["reconcile_mode"] == "full"
    assert full_res["status"] == "SUBMITTED"

    # Fallback: if no receipt dialog appeared after confirm, auto-fallback confirms order on 撤单/当日委托
    class _SilentReceiptDevice(_TrackedDevice):
        def click(self, x: int, y: int) -> None:
            super().click(x, y)
            if self.screen == "receipt":
                self.screen = "buy"

    silent_dev = _SilentReceiptDevice(screen="buy", form_code="160723", form_price="2.200", form_qty="100")
    silent_exec = TonghuashunTradeExecutor(
        adb_serial="emulator-5556",
        u2_factory=lambda _s: silent_dev,
        name_to_symbol=lambda n: "160723" if "嘉实原油" in n else None,
    )
    fallback_res = silent_exec.place_order(
        symbol="160723",
        expected_name="嘉实原油",
        side="buy",
        price="2.200",
        quantity=100,
        precision=3,
        dry_run=False,
    )
    assert fallback_res["reconcile_mode"] == "full"
    assert fallback_res["status"] == "SUBMITTED"
    assert fallback_res["accepted"] is True


def test_prestage_order_and_selective_override_fast_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("level2_service.ths_trade.time.sleep", lambda _s: None)
    fake_dev = _FakeU2Device(screen="holdings", form_code="600000", form_price="10.00", form_qty="100")
    executor = TonghuashunTradeExecutor(
        adb_serial="emulator-5556",
        u2_factory=lambda _s: fake_dev,
        name_to_symbol=lambda n: "160723" if "嘉实原油" in n else None,
    )
    pre = executor.prestage_order(
        symbol="160723",
        expected_name="嘉实原油",
        side="buy",
        price="2.200",
        quantity=100,
        precision=3,
    )
    assert pre["ok"] is True
    assert fake_dev.screen == "buy"
    assert fake_dev.form_code == "160723"
    assert fake_dev.form_price == "2.200"
    assert fake_dev.form_qty == "100"
    assert ("click", 448, 1068) not in fake_dev.events

    # Confirming identical order skips ALL set_text calls
    before_exact = len(fake_dev.events)
    res_exact = executor.place_order(
        symbol="160723",
        expected_name="嘉实原油",
        side="buy",
        price="2.200",
        quantity=100,
        precision=3,
        dry_run=True,
    )
    assert res_exact["prestaged"] is True
    assert res_exact["timings_ms"]["prestaged_hit"] == 1
    assert [ev for ev in fake_dev.events[before_exact:] if ev[0] == "set_text"] == []

    # Adjusting only price updates ONLY stockprice (skips code and qty)
    before_price_adj = len(fake_dev.events)
    res_adj = executor.place_order(
        symbol="160723",
        expected_name="嘉实原油",
        side="buy",
        price="2.203",
        quantity=100,
        precision=3,
        dry_run=True,
    )
    assert res_adj["prestaged"] is False
    adj_set_texts = [ev for ev in fake_dev.events[before_price_adj:] if ev[0] == "set_text"]
    assert adj_set_texts == [("set_text", "com.hexin.plat.android:id/stockprice", "2.203")]

    # HTTP bridge /trade/prestage checks admin auth
    app = create_app(
        admin_password_hash=PasswordHasher().hash("admin-pass-123"),
        admin_session_secret="a" * 32,
        symbol_lookup=lambda sym: CATALOG[sym],
        market_account_store=SQLiteMarketAccountStore(tmp_path / "market.db"),
        goodluck_internal_token=TOKEN,
        trade_executor=executor,
    )
    client = TestClient(app)
    assert client.post(
        "/api/internal/goodluck/trade/prestage",
        headers=_headers(role="user"),
        json={"symbol": "160723", "side": "buy", "price": "2.205", "quantity": 100},
    ).status_code == 403
    ok_res = client.post(
        "/api/internal/goodluck/trade/prestage",
        headers=_headers(role="admin"),
        json={"symbol": "160723", "side": "buy", "price": "2.205", "quantity": 100},
    )
    assert ok_res.status_code == 200
    assert ok_res.json()["ok"] is True
    assert fake_dev.form_price == "2.205"


def test_place_order_clicks_through_generic_lof_premium_warning_then_order_confirm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("level2_service.ths_trade.time.sleep", lambda _s: None)

    class _LofWarningChainDevice(_FakeU2Device):
        def __init__(self) -> None:
            super().__init__(screen="buy", form_code="160723", form_price="2.200", form_qty="200")
            self.wrong_symbol_confirm = False

        def click(self, x: int, y: int) -> None:
            self.events.append(("click", x, y))
            if self.screen == "buy" and 195 <= x <= 702 and 1007 <= y <= 1130:
                self.screen = "lof_risk"
                return
            if self.screen == "lof_risk":
                self.screen = "confirm" if 600 <= x <= 900 else "buy"
                return
            if self.screen == "confirm":
                if 600 <= x <= 900:
                    self.order_open = True
                    self.screen = "receipt"
                else:
                    self.screen = "buy"
                return
            super().click(x, y)

        def dump_hierarchy(self, compressed: bool = False) -> str:
            if self.screen == "lof_risk":
                return """<hierarchy>
  <node package="com.hexin.plat.android" resource-id="com.hexin.plat.android:id/frame_layout">
    <node resource-id="com.hexin.plat.android:id/page_title_view" text="国泰海通证券" />
  </node>
  <node package="com.hexin.plat.android">
    <node text="溢价风险提示" />
    <node text="该LOF基金二级市场溢价率较高，是否继续委托？" />
    <node text="取消" clickable="true" bounds="[100,1200][400,1300]" />
    <node text="继续买入" clickable="true" bounds="[600,1200][900,1300]" />
  </node>
</hierarchy>"""
            if self.screen == "confirm" and self.wrong_symbol_confirm:
                return """<hierarchy>
  <node package="com.hexin.plat.android" resource-id="com.hexin.plat.android:id/frame_layout">
    <node resource-id="com.hexin.plat.android:id/page_title_view" text="国泰海通证券" />
  </node>
  <node package="com.hexin.plat.android">
    <node text="委托买入确认" />
    <node text="证券代码 513050 中概互联" />
    <node text="取消" clickable="true" bounds="[100,1200][400,1300]" />
    <node text="确认买入" clickable="true" bounds="[600,1200][900,1300]" />
  </node>
</hierarchy>"""
            if self.screen == "receipt":
                return _receipt_dialog_xml("委托已提交，合同编号：HT998877")
            return super().dump_hierarchy(compressed=compressed)

    dev = _LofWarningChainDevice()
    executor = TonghuashunTradeExecutor(
        adb_serial="emulator-5556",
        u2_factory=lambda _s: dev,
        name_to_symbol=lambda n: "160723" if "嘉实原油" in n else None,
    )
    res = executor.place_order(
        symbol="160723",
        expected_name="嘉实原油",
        side="buy",
        price="2.200",
        quantity=200,
        precision=3,
        dry_run=False,
        auto_cancel=False,
    )
    assert res["accepted"] is True
    assert res["status"] == "SUBMITTED"
    assert res["entrust_no"] == "HT998877"
    assert "委托买入确认" in res["dialog_message"]
    assert "HT998877" in res["dialog_message"]

    # Wrong stock in secondary confirm dialog must still cancel safely and raise mismatch
    wrong_dev = _LofWarningChainDevice()
    wrong_dev.wrong_symbol_confirm = True
    wrong_exec = TonghuashunTradeExecutor(
        adb_serial="emulator-5556",
        u2_factory=lambda _s: wrong_dev,
        name_to_symbol=lambda n: "160723" if "嘉实原油" in n else None,
    )
    with pytest.raises(TradeExecutionError) as exc_info:
        wrong_exec.place_order(
            symbol="160723",
            expected_name="嘉实原油",
            side="buy",
            price="2.200",
            quantity=200,
            precision=3,
            dry_run=False,
            auto_cancel=False,
        )
    assert exc_info.value.error_code == "TRADE_INPUT_READBACK_MISMATCH"
    assert wrong_dev.screen == "buy"


def test_ensure_trade_workspace_scrolls_up_when_landing_page_scrolled_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("level2_service.ths_trade.time.sleep", lambda _s: None)

    class _ScrolledLandingDevice(_FakeU2Device):
        def __init__(self) -> None:
            super().__init__(screen="landing_bottom", form_code="160723", form_price="2.200", form_qty="100")

        def swipe(self, *_args, **_kwargs) -> None:
            super().swipe(*_args, **_kwargs)
            if self.screen == "landing_bottom":
                self.screen = "landing_top"

        def click(self, x: int, y: int) -> None:
            self.events.append(("click", x, y))
            if self.screen == "landing_top" and 80 <= x <= 200 and 500 <= y <= 620:
                self.screen = "buy"
                return
            super().click(x, y)

        def dump_hierarchy(self, compressed: bool = False) -> str:
            if self.screen == "landing_bottom":
                return """<hierarchy>
  <node package="com.hexin.plat.android" resource-id="com.hexin.plat.android:id/frame_layout">
    <node resource-id="com.hexin.plat.android:id/tab_a" text="A股" clickable="true" selected="false" bounds="[164,95][300,192]" />
    <node resource-id="com.hexin.plat.android:id/tab_mn" text="模拟" clickable="true" selected="false" bounds="[788,95][890,192]" />
    <node resource-id="com.hexin.plat.android:id/weituo_first_page" bounds="[0,216][1080,1766]">
      <node resource-id="com.hexin.plat.android:id/scroll_view" bounds="[0,216][1080,1766]">
        <node resource-id="com.hexin.plat.android:id/text" text="银证转账" bounds="[51,243][243,313]" />
        <node resource-id="com.hexin.plat.android:id/exit_btn" text="退出当前账户" clickable="true" bounds="[45,1514][1035,1646]" />
      </node>
    </node>
    <node resource-id="com.hexin.plat.android:id/title" text="交易" selected="true" bounds="[597,1855][663,1904]" />
  </node>
</hierarchy>"""
            if self.screen == "landing_top":
                return """<hierarchy>
  <node package="com.hexin.plat.android" resource-id="com.hexin.plat.android:id/frame_layout">
    <node resource-id="com.hexin.plat.android:id/tab_a" text="A股" clickable="true" selected="false" bounds="[164,95][300,192]" />
    <node resource-id="com.hexin.plat.android:id/weituo_first_page" bounds="[0,216][1080,1766]">
      <node resource-id="com.hexin.plat.android:id/menu_buy_text" text="买入" clickable="true" bounds="[80,500][200,620]" />
      <node resource-id="com.hexin.plat.android:id/menu_holdings_text" text="持仓" clickable="true" bounds="[700,500][820,620]" />
    </node>
    <node resource-id="com.hexin.plat.android:id/title" text="交易" selected="true" bounds="[597,1855][663,1904]" />
  </node>
</hierarchy>"""
            return super().dump_hierarchy(compressed=compressed)

    dev = _ScrolledLandingDevice()
    executor = TonghuashunTradeExecutor(
        adb_serial="emulator-5556",
        u2_factory=lambda _s: dev,
        name_to_symbol=lambda n: "160723" if "嘉实原油" in n else None,
    )
    pre = executor.prestage_order(
        symbol="160723",
        expected_name="嘉实原油",
        side="buy",
        price="2.200",
        quantity=100,
        precision=3,
    )
    assert pre["ok"] is True
    assert dev.screen == "buy"
    assert ("swipe",) in dev.events
    # Must NOT tap tab_a repeatedly when already on A-share weituo_first_page
    assert ("click", 232, 144) not in dev.events


def test_bridge_trade_device_lifecycle_start_status_and_shutdown(
    tmp_path: Path,
) -> None:
    from datetime import datetime, timezone
    from level2_service.device_lifecycle import (
        DeviceLifecycleAction,
        DeviceLifecycleOperation,
        DeviceLifecycleState,
        DeviceLifecycleStatus,
    )

    class _FakeLifecycle:
        def __init__(self) -> None:
            self.core_state = DeviceLifecycleState.STOPPED
            self.submitted: list[tuple[str, DeviceLifecycleAction]] = []
            self.adb_online = False

        def devices(self) -> tuple[DeviceLifecycleStatus, ...]:
            now = datetime(2026, 11, 25, 21, 50, tzinfo=timezone.utc)
            return (
                DeviceLifecycleStatus("core_metrics", self.core_state, "op-core-1", None, now),
                DeviceLifecycleStatus("main_fund_flow", DeviceLifecycleState.STOPPED, None, None, now),
            )

        def submit(self, role: str, action: DeviceLifecycleAction) -> DeviceLifecycleOperation:
            assert role == "core_metrics"
            self.submitted.append((role, action))
            now = datetime(2026, 11, 25, 21, 50, tzinfo=timezone.utc)
            if action == DeviceLifecycleAction.START_AND_LAUNCH_APP:
                self.core_state = DeviceLifecycleState.STARTING
            else:
                self.core_state = DeviceLifecycleState.STOPPING
                self.adb_online = False
            return DeviceLifecycleOperation("op-core-1", role, action, self.core_state, None, now)

        def operation(self, operation_id: str) -> DeviceLifecycleOperation:
            now = datetime(2026, 11, 25, 21, 50, tzinfo=timezone.utc)
            act = (
                DeviceLifecycleAction.START_AND_LAUNCH_APP
                if self.core_state in {DeviceLifecycleState.STARTING, DeviceLifecycleState.RUNNING}
                else DeviceLifecycleAction.SHUTDOWN
            )
            return DeviceLifecycleOperation(operation_id, "core_metrics", act, self.core_state, None, now)

    fake_lc = _FakeLifecycle()
    control = RunnerControl()
    fake_dev = _FakeU2Device()
    executor = TonghuashunTradeExecutor(
        adb_serial="emulator-5556",
        runner_control=control,
        u2_factory=lambda _s: fake_dev,
        name_to_symbol=lambda n: "160723" if "嘉实原油" in n else None,
    )
    app = create_app(
        admin_password_hash=PasswordHasher().hash("admin-pass-123"),
        admin_session_secret="a" * 32,
        symbol_lookup=lambda sym: CATALOG[sym],
        market_account_store=SQLiteMarketAccountStore(tmp_path / "market.db"),
        goodluck_internal_token=TOKEN,
        runner_control=control,
        device_lifecycle=fake_lc,
        device_health_probes={
            "core_metrics": lambda: {
                "adb": "ONLINE" if fake_lc.adb_online else "OFFLINE",
                "app": "ONLINE" if fake_lc.adb_online else "OFFLINE",
            }
        },
        trade_executor=executor,
    )
    client = TestClient(app)

    # Non-admin user blocked
    assert client.get("/api/internal/goodluck/trade/device", headers=_headers(role="user")).status_code == 403
    assert client.post(
        "/api/internal/goodluck/trade/device",
        headers=_headers(role="user"),
        json={"action": "start"},
    ).status_code == 403

    # Stopped initial status
    s0 = client.get("/api/internal/goodluck/trade/device", headers=_headers(role="admin"))
    assert s0.status_code == 200
    assert s0.json()["state"] == "STOPPED"
    assert s0.json()["ready"] is False
    assert s0.json()["role"] == "core_metrics"
    assert s0.json()["device_serial"] == "emulator-5556"

    # Admin clicks start -> only core_metrics woken, queue paused while STARTING
    s1 = client.post(
        "/api/internal/goodluck/trade/device",
        headers=_headers(role="admin"),
        json={"action": "start"},
    )
    assert s1.status_code == 200
    assert s1.json()["state"] == "STARTING"
    assert s1.json()["operation_id"] == "op-core-1"
    assert fake_lc.submitted == [("core_metrics", DeviceLifecycleAction.START_AND_LAUNCH_APP)]
    assert control.queue_paused is True

    # Emulate completion -> GET status reconciles lease & resumes queue
    fake_lc.core_state = DeviceLifecycleState.RUNNING
    fake_lc.adb_online = True
    s2 = client.get("/api/internal/goodluck/trade/device", headers=_headers(role="admin"))
    assert s2.status_code == 200
    assert s2.json()["state"] == "RUNNING"
    assert s2.json()["adb"] == "ONLINE"
    assert s2.json()["ready"] is True
    assert control.queue_paused is False

    # Populate cached u2 handle and verify shutdown clears it cleanly
    executor._device = fake_dev
    executor._last_verified_at = 123.0
    s3 = client.post(
        "/api/internal/goodluck/trade/device",
        headers=_headers(role="admin"),
        json={"action": "shutdown"},
    )
    assert s3.status_code == 200
    assert s3.json()["state"] == "STOPPING"
    assert fake_lc.submitted[-1] == ("core_metrics", DeviceLifecycleAction.SHUTDOWN)
    assert executor._device is None
    assert executor._last_verified_at == 0.0


def test_trade_account_orders_trades_ttl_cache_and_busy_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("level2_service.ths_trade.time.sleep", lambda _s: None)
    control = RunnerControl()
    fake_dev = _FakeU2Device()
    executor = TonghuashunTradeExecutor(
        adb_serial="emulator-5556",
        runner_control=control,
        u2_factory=lambda _s: fake_dev,
        name_to_symbol=lambda n: "160723" if "嘉实原油" in n else None,
    )
    app = create_app(
        admin_password_hash=PasswordHasher().hash("admin-pass-123"),
        admin_session_secret="a" * 32,
        symbol_lookup=lambda sym: CATALOG[sym],
        market_account_store=SQLiteMarketAccountStore(tmp_path / "market.db"),
        goodluck_internal_token=TOKEN,
        runner_control=control,
        trade_executor=executor,
    )
    client = TestClient(app)

    dump_calls = 0
    orig_dump = fake_dev.dump_hierarchy

    def _counting_dump(*args, **kwargs):
        nonlocal dump_calls
        dump_calls += 1
        return orig_dump(*args, **kwargs)

    fake_dev.dump_hierarchy = _counting_dump  # type: ignore[method-assign]

    # First read hits fake ADB; second read within TTL returns cached snapshot without ADB calls
    a1 = client.get("/api/internal/goodluck/trade/account", headers=_headers(role="admin"))
    assert a1.status_code == 200
    assert a1.json()["cached"] is False
    dumps_after_first = dump_calls
    assert dumps_after_first >= 1

    a2 = client.get("/api/internal/goodluck/trade/account", headers=_headers(role="admin"))
    assert a2.status_code == 200
    assert a2.json()["cached"] is True
    assert dump_calls == dumps_after_first

    # Simulate ADB lock held while expired TTL: non-blocking busy fallback returns stale_while_busy
    executor._last_account_at -= 120.0
    assert executor._lock.acquire(blocking=False)
    try:
        a_busy = client.get("/api/internal/goodluck/trade/account", headers=_headers(role="admin"))
        assert a_busy.status_code == 200
        assert a_busy.json()["cached"] is True
        assert a_busy.json()["stale_while_busy"] is True
    finally:
        executor._lock.release()

    # force=true bypasses cache and re-reads ADB
    a_force = client.get("/api/internal/goodluck/trade/account?force=true", headers=_headers(role="admin"))
    assert a_force.status_code == 200
    assert a_force.json()["cached"] is False
    assert dump_calls > dumps_after_first

    # Orders & trades also cache within TTL
    o1 = client.get("/api/internal/goodluck/trade/orders", headers=_headers(role="admin"))
    assert o1.status_code == 200 and o1.json()["cached"] is False
    dumps_ord = dump_calls
    o2 = client.get("/api/internal/goodluck/trade/orders", headers=_headers(role="admin"))
    assert o2.status_code == 200 and o2.json()["cached"] is True and dump_calls == dumps_ord

    t1 = client.get("/api/internal/goodluck/trade/trades", headers=_headers(role="admin"))
    assert t1.status_code == 200 and t1.json()["cached"] is False and len(t1.json()["trades"]) == 1
    dumps_trd = dump_calls
    t2 = client.get("/api/internal/goodluck/trade/trades", headers=_headers(role="admin"))
    assert t2.status_code == 200 and t2.json()["cached"] is True and dump_calls == dumps_trd


def test_trade_snapshot_invalidation_after_order_and_cross_day_isolation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("level2_service.ths_trade.time.sleep", lambda _s: None)
    control = RunnerControl()
    fake_dev = _FakeU2Device()
    executor = TonghuashunTradeExecutor(
        adb_serial="emulator-5556",
        runner_control=control,
        u2_factory=lambda _s: fake_dev,
        name_to_symbol=lambda n: "160723" if "嘉实原油" in n else None,
    )

    first = executor.get_today_trades(max_age_seconds=25.0)
    assert first["cached"] is False
    assert executor._last_trades_at > 0.0

    # 提交订单走 fast reconcile 后，必须清零当日成交与账户快照时间戳，即使占锁也不返回下单前旧成交
    executor.invalidate_snapshots(account=True, orders=True, trades=True)
    assert executor._last_trades_at == 0.0
    assert executor._last_account_at == 0.0
    assert executor._cached_trades_snapshot(25.0, allow_stale=True) is None

    # 跨日隔离：如果快照停留在上一交易日，即使 TTL 未过期也不应返回
    fresh_again = executor.get_today_trades(max_age_seconds=25.0)
    assert fresh_again["cached"] is False
    with executor._cache_lock:
        executor._last_trades_date = "20200101"
    assert executor._cached_trades_snapshot(25.0, allow_stale=True) is None
    reloaded = executor.get_today_trades(max_age_seconds=25.0)
    assert reloaded["cached"] is False


def test_dump_root_reconnects_once_preserving_compressed_true() -> None:
    calls: list[tuple[str, bool]] = []

    class _DevA:
        info = {"displayWidth": 1080, "displayHeight": 1920}

        def dump_hierarchy(self, *, compressed: bool = True) -> str:
            calls.append(("A", compressed))
            raise RuntimeError("atx-agent socket reset")

    class _DevB:
        info = {"displayWidth": 1080, "displayHeight": 1920}

        def dump_hierarchy(self, *, compressed: bool = True) -> str:
            calls.append(("B", compressed))
            return '<hierarchy><node package="com.hexin.plat.android" /></hierarchy>'

    connects = 0

    def _u2_factory(_serial: str):
        nonlocal connects
        connects += 1
        return _DevA() if connects == 1 else _DevB()

    executor = TonghuashunTradeExecutor(adb_serial="emulator-5556", u2_factory=_u2_factory)
    dev_a = executor._connect_device()
    root = executor._dump_root(dev_a, compressed=True)

    assert root.tag == "hierarchy"
    assert connects == 2
    assert calls == [("A", True), ("B", True)]


def test_guarded_device_session_yields_to_finishing_runner_task() -> None:
    from level2_service.runner import RunnerControl

    class _YieldingTaskStore:
        def __init__(self) -> None:
            self.checks = 0

        def has_running_task(self) -> bool:
            self.checks += 1
            return self.checks <= 2

    control = RunnerControl()
    task_store = _YieldingTaskStore()
    fake_dev = _FakeU2Device()
    executor = TonghuashunTradeExecutor(
        adb_serial="emulator-5556",
        runner_control=control,
        task_store=task_store,
        runner_yield_timeout_seconds=1.0,
        u2_factory=lambda _s: fake_dev,
    )

    with executor._guarded_device_session(blocking=True) as device:
        assert device is fake_dev
        assert control.queue_paused is True
        assert task_store.checks >= 3

    assert control.queue_paused is False


def test_keep_warm_refresh_gateway_and_probe_broker_session(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("level2_service.ths_trade.time.sleep", lambda _s: None)

    class _WorkspaceDevice:
        def __init__(self) -> None:
            self.info = {"displayWidth": 1080, "displayHeight": 1920}
            self.current_tab = "持仓"
            self.refresh_clicks = 0
            self.clicked_tabs: list[str] = []

        def app_current(self) -> dict[str, str]:
            return {"package": "com.hexin.plat.android"}

        def app_start(self, *_args, **_kwargs) -> None:
            pass

        def press(self, _key: str) -> None:
            pass

        def swipe(self, *_args, **_kwargs) -> None:
            pass

        def click(self, x: int, y: int) -> None:
            if 940 <= x <= 1030 and 90 <= y <= 170:
                self.refresh_clicks += 1
            elif y <= 250:
                if x < 200:
                    self.current_tab = "买入"
                elif x < 400:
                    self.current_tab = "卖出"
                elif x < 600:
                    self.current_tab = "撤单"
                elif x < 800:
                    self.current_tab = "持仓"
                else:
                    self.current_tab = "查询"
                self.clicked_tabs.append(self.current_tab)

        def dump_hierarchy(self, *, compressed: bool = True) -> str:
            del compressed
            tabs = "".join(
                f'<node package="com.hexin.plat.android" resource-id="com.hexin.plat.android:id/btn" '
                f'text="{label}" selected="{"true" if self.current_tab == label else "false"}" '
                f'bounds="[{idx * 200 + 10},180][{idx * 200 + 180},240]" />'
                for idx, label in enumerate(("买入", "卖出", "撤单", "持仓", "查询"))
            )
            return (
                "<hierarchy>"
                '<node package="com.hexin.plat.android" resource-id="com.hexin.plat.android:id/page_title_view" text="国泰海通证券" bounds="[40,80][400,140]" />'
                '<node package="com.hexin.plat.android" resource-id="com.hexin.plat.android:id/account_info_view" text="资金账号12345678" bounds="[40,145][400,175]" />'
                '<node package="com.hexin.plat.android" resource-id="com.hexin.plat.android:id/refresh_img" text="" bounds="[950,100][1020,160]" />'
                f"{tabs}"
                '<node package="com.hexin.plat.android" resource-id="com.hexin.plat.android:id/capital_cell_title" text="总资产" bounds="[20,300][200,340]" />'
                '<node package="com.hexin.plat.android" resource-id="com.hexin.plat.android:id/capital_cell_value" text="50000.00" bounds="[20,350][200,390]" />'
                '<node package="com.hexin.plat.android" resource-id="com.hexin.plat.android:id/capital_cell_title" text="可用" bounds="[240,300][420,340]" />'
                '<node package="com.hexin.plat.android" resource-id="com.hexin.plat.android:id/capital_cell_value" text="12000.00" bounds="[240,350][420,390]" />'
                "</hierarchy>"
            )

    dev = _WorkspaceDevice()
    executor = TonghuashunTradeExecutor(adb_serial="emulator-5556", u2_factory=lambda _s: dev)

    probe = executor.probe_broker_session(preferred_side="buy")
    assert probe["ok"] is True
    assert probe["status"] == "READY"
    assert probe["available_cash"] == "12000.00"
    assert probe["tab"] == "买入"
    assert dev.refresh_clicks == 1
    assert executor._last_gateway_touch_at > 0.0

    warm = executor.keep_warm(side="buy", refresh_gateway=True)
    assert warm["ok"] is True
    assert warm["tab"] == "买入"
    assert warm["gateway_refreshed"] is True
    assert dev.refresh_clicks == 2


