from __future__ import annotations

from io import BytesIO
from random import Random
import sys
import types
from pathlib import Path
from time import monotonic

from PIL import Image
import pytest

from level2_service.models import CaptureKind, CaptureStatus, MetricKind, TaskRecord, TaskStatus
from level2_service.parsed_values import DirectReadOutcome
from level2_service.queue import InMemoryStreams
from level2_service.runner import (
    ADBDeviceBridge,
    FakeDeviceBridge,
    Level2Navigator,
    Level2Runner,
    NavigationError,
    NeedsAdminError,
    RunnerControl,
    SEARCH_RESULT_CODE_SELECTOR,
    long_capture_has_net_heading,
)


TEST_VALUES = {
    CaptureKind.LARGE_ORDER_NET: "-0.02",
    CaptureKind.LARGE_ORDER_AMOUNT: "-2802.6万",
    CaptureKind.RETAIL_COUNT: "21.23",
}

PARSED_VALUES = {
    MetricKind.STOCK_NAME: "招商轮船",
    MetricKind.CURRENT_PRICE: "19.78",
    MetricKind.CHANGE_PERCENT: "7.15%",
    MetricKind.TURNOVER_RATE: "2.40%",
    MetricKind.RETAIL_COUNT: "21.23",
    MetricKind.LARGE_ORDER_NET: "-0.02",
    MetricKind.LARGE_ORDER_AMOUNT: "-2802.6万",
    MetricKind.MACDFS: "+0.012",
}


def legacy_values(values):
    return {kind: values[kind] for kind in PARSED_VALUES}


def _successful_runner(store, navigator, capture_root: Path, control: RunnerControl | None = None) -> Level2Runner:
    viewport_height = 1920 - 215 - 154
    random = Random(33007)
    row_strip = Image.frombytes("RGB", (1, viewport_height + 450), random.randbytes((viewport_height + 450) * 3))
    document = row_strip.resize((1080, viewport_height + 450))

    def screen(offset: int) -> bytes:
        image = Image.new("RGB", (1080, 1920), "white")
        image.paste(document.crop((0, offset, 1080, offset + viewport_height)), (0, 215))
        output = BytesIO()
        image.save(output, format="PNG")
        return output.getvalue()

    frames = iter((screen(0), screen(450), screen(450)))

    def screenshot_png() -> bytes:
        navigator.bridge.capture_attempts += 1
        return next(frames)

    navigator.bridge.screenshot_png = screenshot_png  # type: ignore[method-assign]
    return Level2Runner(
        store,
        navigator,
        capture_root,
        control or RunnerControl(),
        value_reader=types.SimpleNamespace(read=lambda _frames, _kinds=None: TEST_VALUES),
        parsed_value_source=types.SimpleNamespace(
            read=lambda _symbol: dict(PARSED_VALUES),
            read_direct=lambda _symbol: dict(PARSED_VALUES),
        ),
        stitcher=lambda frames: b"\x89PNG\r\n\x1a\nlong:" + b"|".join(frames),
    )


def test_adb_exact_text_count_rejects_unparseable_ui_dump() -> None:
    """A malformed dump is not evidence of one exact symbol match."""
    bridge = ADBDeviceBridge()
    bridge._shell = lambda *_args: b"UI dump failed: SZ.000001"  # type: ignore[method-assign]

    assert bridge.exact_text_count("SZ.000001") == 0


def test_adb_exact_text_count_parses_ui_dump_with_diagnostic_prefix_and_suffix() -> None:
    bridge = ADBDeviceBridge()
    bridge._uiautomator = lambda: None  # type: ignore[method-assign]
    bridge._shell = lambda *_args: (
        b"UI dump requested\n"
        b"<hierarchy><node text=\"SZ.000001\" /></hierarchy>\n"
        b"UI dump complete\n"
    )  # type: ignore[method-assign]

    assert bridge.exact_text_count("SZ.000001") == 1


def test_adb_visible_texts_uses_one_hierarchy_dump_for_all_admin_markers() -> None:
    """Six separate selector queries would restore the avoidable daily delay."""
    class HierarchyAdapter:
        def __init__(self) -> None:
            self.dumps = 0

        def dump_hierarchy(self, **_kwargs) -> str:
            self.dumps += 1
            return (
                '<hierarchy><node text="登录" />'
                '<node text="验证码" /><node text="普通行情" /></hierarchy>'
            )

    adapter = HierarchyAdapter()
    bridge = ADBDeviceBridge(uiautomator_adapter=adapter)

    assert bridge.visible_texts() == frozenset({"登录", "验证码", "普通行情"})
    assert adapter.dumps == 1


def test_adb_command_timeout_prevents_a_capture_from_hanging_the_worker() -> None:
    """A wedged host ADB connection must return control to the task state machine."""
    bridge = ADBDeviceBridge(adb=sys.executable, command_timeout_seconds=0.05)
    started = monotonic()

    with pytest.raises(NavigationError, match="ADB command timed out"):
        bridge._run("-c", "import time; time.sleep(2)")

    assert monotonic() - started < 0.5


def test_adb_key_maps_browser_key_names_to_android_keycodes() -> None:
    bridge = ADBDeviceBridge()
    calls: list[tuple[str, ...]] = []
    bridge._shell = lambda *args: calls.append(args)  # type: ignore[method-assign]

    bridge.key("a", "down")
    bridge.key("Enter", "down")
    bridge.key("a", "up")

    assert calls == [
        ("input", "keyevent", "KEYCODE_A"),
        ("input", "keyevent", "KEYCODE_ENTER"),
    ]


def test_uiautomator_uses_configured_adb_server_socket(monkeypatch) -> None:
    calls: dict[str, object] = {}

    class FakeAdbClient:
        def __init__(self, **kwargs) -> None:
            calls["adb_client"] = kwargs

    fake_adbutils = types.SimpleNamespace(AdbClient=FakeAdbClient)
    fake_uiautomator2 = types.SimpleNamespace(
        connect=lambda serial: calls.setdefault("serial", serial) or object()
    )
    monkeypatch.setitem(sys.modules, "adbutils", fake_adbutils)
    monkeypatch.setitem(sys.modules, "uiautomator2", fake_uiautomator2)

    bridge = ADBDeviceBridge(
        serial="emulator-5554",
        environment={"ADB_SERVER_SOCKET": "tcp:host.docker.internal:5037"},
    )

    bridge._uiautomator()

    assert calls["adb_client"] == {"host": "host.docker.internal", "port": 5037}
    assert calls["serial"] == "emulator-5554"


def test_adb_click_selector_reconnects_after_a_stale_uiautomator_object() -> None:
    class StaleObjectException(Exception):
        pass

    class Target:
        exists = True

        def __init__(self, stale: bool) -> None:
            self.stale = stale
            self.clicked = False

        def click(self) -> None:
            if self.stale:
                raise StaleObjectException("StaleObjectException")
            self.clicked = True

    class Adapter:
        def __init__(self, target: Target) -> None:
            self.target = target

        def __call__(self, **_selector):
            return self.target

    class RetryingBridge(ADBDeviceBridge):
        def __init__(self) -> None:
            super().__init__()
            self.adapters = iter((Adapter(Target(True)), Adapter(Target(False))))

        def _uiautomator(self):
            return next(self.adapters)

    assert RetryingBridge().click_selector("search") is True


def test_adb_clicks_the_fund_search_result_with_the_expected_market_label() -> None:
    class Adapter:
        def dump_hierarchy(self, **_kwargs) -> str:
            return (
                '<hierarchy>'
                '<node class="android.widget.LinearLayout" resource-id="com.hexin.plat.android:id/stock_code_label" bounds="[48,304][252,353]">'
                '<node resource-id="com.hexin.plat.android:id/label" text="深基" />'
                '<node resource-id="com.hexin.plat.android:id/stock_code" text="160723" bounds="[132,304][252,353]" />'
                '</node>'
                '<node class="android.widget.LinearLayout" resource-id="com.hexin.plat.android:id/stock_code_label" bounds="[48,791][378,844]">'
                '<node resource-id="com.hexin.plat.android:id/label" text="基金" />'
                '<node resource-id="com.hexin.plat.android:id/stock_code" text="160723" bounds="[132,794][252,843]" />'
                '</node>'
                '</hierarchy>'
            )

    bridge = ADBDeviceBridge(uiautomator_adapter=Adapter())
    taps: list[tuple[float, float]] = []
    bridge.tap = lambda x, y: taps.append((x, y))  # type: ignore[method-assign]

    assert bridge.click_search_result(SEARCH_RESULT_CODE_SELECTOR, "160723", "深基") is True
    assert taps == [(150 / 1080, 328.5 / 1920)]


def test_navigator_filters_a_bond_collision_by_the_expected_stock_market_label() -> None:
    class MarketAwareBridge(FakeDeviceBridge):
        market_clicks: list[tuple[str, str, str]]

        def __init__(self) -> None:
            super().__init__(symbol="002412", exact_symbol_matches=2)
            self.market_clicks = []

        def click_search_result(self, selector: str, text: str, market_label: str) -> bool:
            self.market_clicks.append((selector, text, market_label))
            if market_label != "深A":
                return False
            self.stock_open = True
            return True

    bridge = MarketAwareBridge()

    Level2Navigator(bridge).open_stock("002412")

    assert bridge.market_clicks == [(SEARCH_RESULT_CODE_SELECTOR, "002412", "深A")]


def test_lock_can_take_over_when_previous_owner_has_no_active_socket() -> None:
    control = RunnerControl()

    assert control.lock("old-session") is True
    assert control.lock("new-session") is True
    assert control.lock_state("new-session") == {"locked": True}


def test_navigator_uses_visual_fallback_when_selector_is_missing() -> None:
    """A skin change must not make a verified Level2 tab unreachable."""
    class NoTextBridge(FakeDeviceBridge):
        def click_text(self, text: str) -> bool:
            return False if text == "搜索" else super().click_text(text)

    bridge = NoTextBridge(symbol="SZ.000001", selector_available=False)
    navigator = Level2Navigator(bridge)

    image = navigator.capture("SZ.000001", CaptureKind.LARGE_ORDER_NET)

    assert image.startswith(b"\x89PNG")
    assert bridge.visual_actions == ["search"]


def test_navigator_waits_for_home_after_launch_before_pressing_back() -> None:
    """A slow App launch must not receive back presses during its transition."""
    class DelayedHomeBridge(FakeDeviceBridge):
        def launch_app(self, package: str, activity: str) -> None:
            super().launch_app(package, activity)
            self.home_ready = False

        def wait_for_selector(self, selector: str, timeout: float) -> bool:
            if selector == "com.hexin.plat.android:id/firstpagenavi":
                self.home_ready = True
            return super().wait_for_selector(selector, timeout)

        def press_back(self) -> None:
            raise AssertionError("back pressed before the App home page was ready")

    bridge = DelayedHomeBridge(symbol="SZ.000001")

    Level2Navigator(bridge).open_stock("SZ.000001")


def test_navigator_allows_slow_stock_page_to_finish_loading() -> None:
    """A cold stock page may need more than the initial ten-second wait."""
    class SlowStockPageBridge(FakeDeviceBridge):
        def wait_for_selector(self, selector: str, timeout: float) -> bool:
            if selector == "com.hexin.plat.android:id/navi_title_text" and timeout < 20:
                return False
            return super().wait_for_selector(selector, timeout)

    bridge = SlowStockPageBridge(symbol="SZ.000001")

    Level2Navigator(bridge).open_stock("SZ.000001")


def test_navigator_uses_search_text_before_visual_fallback() -> None:
    class NoVisualFallback:
        def tap_template(self, _name: str, _bridge: FakeDeviceBridge) -> bool:
            return False

    class TextSearchBridge(FakeDeviceBridge):
        text_actions: list[str]

        def click_text(self, text: str) -> bool:
            self.text_actions.append(text)
            return super().click_text(text)

    bridge = TextSearchBridge(symbol="SZ.000001", selector_available=False)
    bridge.text_actions = []

    image = Level2Navigator(bridge, NoVisualFallback()).capture("SZ.000001", CaptureKind.LARGE_ORDER_NET)

    assert image.startswith(b"\x89PNG")
    assert bridge.text_actions[0] == "搜索"


def test_navigator_returns_home_and_opens_the_unique_first_stock_result() -> None:
    home_marker = "com.hexin.plat.android:id/firstpagenavi"
    home_search = "com.hexin.plat.android:id/first_page_search_layout_container"
    search_input = "com.hexin.plat.android:id/search_input"
    stock_code = "com.hexin.plat.android:id/stock_code"
    stock_title = "com.hexin.plat.android:id/navi_title_text"

    class SearchFlowBridge(FakeDeviceBridge):
        def __init__(self) -> None:
            super().__init__(symbol="OLD")
            self.back_steps_remaining = 2
            self.search_open = False
            self.stock_open = False
            self.actions: list[tuple] = []

        def has_selector(self, selector: str) -> bool:
            if selector == home_marker:
                return self.back_steps_remaining == 0
            if selector == search_input:
                return self.search_open
            if selector == stock_title:
                return self.stock_open
            return super().has_selector(selector)

        def press_back(self) -> None:
            self.actions.append(("back",))
            self.back_steps_remaining = max(0, self.back_steps_remaining - 1)

        def wait_for_selector(self, selector: str, _timeout: float) -> bool:
            self.actions.append(("wait_selector", selector))
            return self.has_selector(selector)

        def click_selector(self, selector: str) -> bool:
            self.actions.append(("click_selector", selector))
            if selector == home_search and self.back_steps_remaining == 0:
                self.search_open = True
                return True
            return super().click_selector(selector)

        def replace_text(self, selector: str, value: str) -> bool:
            self.actions.append(("replace_text", selector, value))
            if selector != search_input or not self.search_open:
                return False
            self.symbol = value
            return True

        def wait_for_selector_text(self, selector: str, text: str, _timeout: float) -> bool:
            self.actions.append(("wait_selector_text", selector, text))
            return selector == stock_code and text == self.symbol

        def exact_selector_text_count(self, selector: str, text: str) -> int:
            self.actions.append(("count_selector_text", selector, text))
            return self.exact_symbol_matches if selector == stock_code and text == self.symbol else 0

        def click_selector_text(self, selector: str, text: str) -> bool:
            self.actions.append(("click_selector_text", selector, text))
            if selector != stock_code or text != self.symbol:
                return False
            self.stock_open = True
            return True

    bridge = SearchFlowBridge()

    image = Level2Navigator(bridge).capture("601872", CaptureKind.LARGE_ORDER_NET)

    assert image.startswith(b"\x89PNG")
    assert bridge.back_steps_remaining == 0
    assert bridge.stock_open is True
    assert ("replace_text", search_input, "601872") in bridge.actions
    assert ("click_selector_text", stock_code, "601872") in bridge.actions
    assert bridge.actions.index(("back",)) < bridge.actions.index(("replace_text", search_input, "601872"))


def test_runner_ignores_early_level2_marker_and_scrolls_until_page_stops(tmp_path: Path) -> None:
    """The Level-2 section begins one screen before the real page bottom."""
    viewport_height = 1920 - 215 - 154
    offsets = (0, 450, 900, 1350, 1350)
    document_height = viewport_height + offsets[-1]
    random = Random(601975)
    row_strip = Image.frombytes("RGB", (1, document_height), random.randbytes(document_height * 3))
    document = row_strip.resize((1080, document_height))

    def screen(offset: int) -> bytes:
        image = Image.new("RGB", (1080, 1920), "white")
        image.paste(document.crop((0, offset, 1080, offset + viewport_height)), (0, 215))
        output = BytesIO()
        image.save(output, format="PNG")
        return output.getvalue()

    source_frames = tuple(screen(offset) for offset in offsets)

    class RecordingChartBridge(FakeDeviceBridge):
        def __init__(self) -> None:
            super().__init__(symbol="OLD")
            self.search_values: list[str] = []
            self.settle_waits: list[float] = []
            self.frames = iter(source_frames)

        def replace_text(self, selector: str, value: str) -> bool:
            self.search_values.append(value)
            return super().replace_text(selector, value)

        def screenshot_png(self) -> bytes:
            self.capture_attempts += 1
            return next(self.frames)

        def has_text(self, text: str) -> bool:
            if text == "买卖队列":
                return len([action for action in self.inputs if action[0] == "swipe"]) >= 2
            return super().has_text(text)

        def wait_for_scroll_settle(self, timeout: float) -> None:
            self.settle_waits.append(timeout)

    store = InMemoryStreams()
    store.enqueue(TaskRecord(task_id="one-search-task", symbol="601872"))
    bridge = RecordingChartBridge()
    values = PARSED_VALUES
    reader = types.SimpleNamespace(read=lambda _frames, _kinds=None: TEST_VALUES)
    runner = Level2Runner(
        store,
        Level2Navigator(bridge),
        tmp_path,
        RunnerControl(),
        value_reader=reader,
        parsed_value_source=types.SimpleNamespace(
            read=lambda _symbol: dict(PARSED_VALUES),
            read_direct=lambda _symbol: dict(PARSED_VALUES),
        ),
        stitcher=lambda frames: b"\x89PNG\r\n\x1a\nlong:" + b"|".join(frames),
    )

    task = runner.run_once()

    assert task is not None and task.status == TaskStatus.COMPLETED
    assert bridge.search_values == ["601872"]
    assert bridge.inputs == [("swipe", 0.5, 0.85, 0.5, 0.47)] * 4
    assert len(bridge.settle_waits) == 4
    assert (tmp_path / "one-search-task" / "LONG.png").read_bytes() == (
        b"\x89PNG\r\n\x1a\nlong:" + b"|".join(source_frames[:-1])
    )
    assert list((tmp_path / "one-search-task").iterdir()) == [tmp_path / "one-search-task" / "LONG.png"]
    assert legacy_values(task.values) == values


def test_long_capture_net_heading_validation_rejects_the_reported_missing_title() -> None:
    recognize = lambda _image: "散户数量 MACDFS 大单金额 金额:9091.9万 买卖队列"

    assert long_capture_has_net_heading(b"problem-image", ocr=recognize) is False


def test_long_capture_net_heading_validation_accepts_the_fund_level2_title() -> None:
    recognize = lambda _image: "分时量 大单占比(手)"

    assert long_capture_has_net_heading(b"fund-image", ocr=recognize) is True


def test_long_capture_net_heading_validation_accepts_the_visible_net_value_row() -> None:
    recognize = lambda _image: "散户数量 MACDFS 净量:0.49 大单金额 买卖队列"

    assert long_capture_has_net_heading(b"complete-image", ocr=recognize) is True


def test_long_capture_net_heading_default_ocr_isolates_the_left_neutral_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = Image.new("RGB", (1080, 1920), "white")
    source.putpixel((100, 100), (70, 75, 80))
    source.putpixel((120, 100), (170, 20, 20))
    source.putpixel((400, 100), (50, 50, 50))
    encoded = BytesIO()
    source.save(encoded, format="PNG")
    invocation: dict[str, object] = {}

    def run_tesseract(command, *, input, stdout, stderr, check):  # noqa: ANN001
        invocation.update(
            command=command,
            input=input,
            stdout=stdout,
            stderr=stderr,
            check=check,
        )
        return types.SimpleNamespace(stdout="大单净量".encode(), stderr=b"")

    monkeypatch.setattr("level2_service.runner.subprocess.run", run_tesseract)

    assert long_capture_has_net_heading(encoded.getvalue()) is True

    command = invocation["command"]
    assert command[-2:] == ["--psm", "6"]
    with Image.open(BytesIO(invocation["input"])) as prepared:  # type: ignore[arg-type]
        prepared = prepared.convert("RGB")
        assert prepared.size == (840, 5760)
        assert prepared.getpixel((301, 301)) == (70, 75, 80)
        assert prepared.getpixel((361, 301)) == (255, 255, 255)


def test_runner_recaptures_instead_of_publishing_a_long_image_without_net_heading(tmp_path: Path) -> None:
    class RetryingNavigator:
        def __init__(self) -> None:
            self.open_attempts = 0
            self.capture_attempts = 0

        def open_stock(self, _symbol: str) -> None:
            self.open_attempts += 1

        def capture_chart_frames(self) -> tuple[bytes, ...]:
            self.capture_attempts += 1
            return (f"frames-{self.capture_attempts}".encode(),)

        def device_online(self) -> bool:
            return True

    store = InMemoryStreams()
    store.enqueue(TaskRecord(task_id="heading-retry-task", symbol="601975"))
    navigator = RetryingNavigator()
    runner = Level2Runner(
        store,
        navigator,  # type: ignore[arg-type]
        tmp_path,
        RunnerControl(),
        value_reader=types.SimpleNamespace(read=lambda _frames, _kinds=None: TEST_VALUES),
        parsed_value_source=types.SimpleNamespace(
            read=lambda _symbol: dict(PARSED_VALUES),
            read_direct=lambda _symbol: dict(PARSED_VALUES),
        ),
        stitcher=lambda frames: b"missing-net" if frames[0] == b"frames-1" else b"complete-long-image",
        long_capture_validator=lambda image: image == b"complete-long-image",
    )

    task = runner.run_once()

    assert task is not None and task.status == TaskStatus.COMPLETED
    assert navigator.open_attempts == 2
    assert navigator.capture_attempts == 2
    assert (tmp_path / "heading-retry-task" / "LONG.png").read_bytes() == b"complete-long-image"


def test_long_capture_runner_uses_only_interface_values_and_leaves_missing_values_unfilled(tmp_path: Path) -> None:
    """A long screenshot must never authorize OCR to replace missing interface data."""
    store = InMemoryStreams()
    store.enqueue(TaskRecord(task_id="background-first", symbol="601872"))
    runner = _successful_runner(store, Level2Navigator(FakeDeviceBridge(symbol="601872")), tmp_path)
    background = dict(PARSED_VALUES)
    background[MetricKind.RETAIL_COUNT] = None

    def forbid_runtime_snapshot(_symbol: str):
        raise AssertionError("task values must use the direct App interface")

    runner.parsed_value_source = types.SimpleNamespace(
        read=forbid_runtime_snapshot,
        read_direct=lambda _symbol: background,
    )
    runner.value_reader = types.SimpleNamespace(
        read=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("task values must never run OCR")
        )
    )

    task = runner.run_once()

    assert task is not None and task.status == TaskStatus.PARTIAL
    assert task.error_code == "VALUE_RECOGNITION_FAILED"
    assert legacy_values(task.values) == background
    assert task.value_sources[MetricKind.RETAIL_COUNT] is None
    assert all(
        task.value_sources[kind].value == "INTERFACE"
        for kind in PARSED_VALUES
        if kind != MetricKind.RETAIL_COUNT
    )
    assert task.long_capture.status == CaptureStatus.READY


def test_data_only_runner_reads_parsed_values_without_capturing_or_running_ocr(tmp_path: Path) -> None:
    """Disabling the long image must skip every screenshot, stitch and OCR operation."""
    class DataOnlyNavigator:
        def __init__(self) -> None:
            self.opened: list[str] = []

        def open_stock(self, symbol: str) -> None:
            self.opened.append(symbol)
            raise AssertionError("data-only tasks must not open or switch the stock page")

        def admin_blocking_texts(self) -> frozenset[str]:
            return frozenset()

        def capture_chart_frames(self) -> tuple[bytes, ...]:
            raise AssertionError("data-only tasks must not capture frames")

        def device_online(self) -> bool:
            return True

    class ForbiddenReader:
        def read(self, *_args, **_kwargs):
            raise AssertionError("data-only tasks must not run OCR")

    store = InMemoryStreams()
    store.enqueue(TaskRecord(
        task_id="data-only-task",
        symbol="601872",
        include_long_capture=False,
    ))
    navigator = DataOnlyNavigator()
    runner = Level2Runner(
        store,
        navigator,  # type: ignore[arg-type]
        tmp_path,
        RunnerControl(),
        value_reader=ForbiddenReader(),  # type: ignore[arg-type]
        parsed_value_source=types.SimpleNamespace(read_direct=lambda _symbol: dict(PARSED_VALUES)),
        stitcher=lambda _frames: (_ for _ in ()).throw(AssertionError("must not stitch")),
        long_capture_validator=lambda _image: (_ for _ in ()).throw(AssertionError("must not validate")),
    )

    task = runner.run_once()

    assert task is not None and task.status == TaskStatus.COMPLETED
    assert navigator.opened == []
    assert legacy_values(task.values) == PARSED_VALUES
    assert task.long_capture.status == CaptureStatus.SKIPPED
    assert task.long_capture.path is None
    assert not (tmp_path / "data-only-task").exists()


def test_data_only_runner_never_uses_ocr_for_missing_interface_values(tmp_path: Path) -> None:
    """A partial interface response must stay partial without opening the App UI."""
    store = InMemoryStreams()
    store.enqueue(TaskRecord(
        task_id="partial-data-only-task",
        symbol="601872",
        include_long_capture=False,
    ))
    values = dict(PARSED_VALUES)
    values[MetricKind.LARGE_ORDER_NET] = None
    bridge = FakeDeviceBridge(symbol="601872")
    runner = _successful_runner(
        store,
        Level2Navigator(bridge),
        tmp_path,
    )
    runner.parsed_value_source = types.SimpleNamespace(read_direct=lambda _symbol: values)
    runner.value_reader = types.SimpleNamespace(
        read=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("task values must never run OCR")
        )
    )
    runner.stitcher = lambda _frames: (_ for _ in ()).throw(AssertionError("must not stitch"))
    runner.long_capture_validator = lambda _image: (_ for _ in ()).throw(AssertionError("must not validate"))

    task = runner.run_once()

    assert task is not None and task.status == TaskStatus.PARTIAL
    assert task.error_code == "VALUE_RECOGNITION_FAILED"
    assert legacy_values(task.values) == values
    assert task.value_sources[MetricKind.LARGE_ORDER_NET] is None
    assert all(
        task.value_sources[kind].value == "INTERFACE"
        for kind in PARSED_VALUES
        if kind != MetricKind.LARGE_ORDER_NET
    )
    assert bridge.inputs == []
    assert task.long_capture.status == CaptureStatus.SKIPPED
    assert task.long_capture.path is None
    assert not (tmp_path / "partial-data-only-task").exists()


def test_data_only_runner_publishes_a_nonfatal_fund_source_error_with_core_values(tmp_path: Path) -> None:
    store = InMemoryStreams()
    store.enqueue(TaskRecord(
        task_id="fund-source-error",
        symbol="600938",
        include_long_capture=False,
    ))
    runner = _successful_runner(store, Level2Navigator(FakeDeviceBridge(symbol="600938")), tmp_path)
    runner.parsed_value_source = types.SimpleNamespace(
        read_direct=lambda _symbol: DirectReadOutcome(
            values=dict(PARSED_VALUES),
            source_errors={
                "core_metrics": None,
                "main_fund_flow": "DIRECT_FUND_FLOW_TIMEOUT",
            },
            intraday_series={
                MetricKind.RETAIL_COUNT: {
                    "unit": None,
                    "points": [{"time": "09:31", "value": "21.23"}],
                }
            },
        )
    )

    task = runner.run_once()

    assert task is not None and task.status == TaskStatus.PARTIAL
    assert task.error_code == "DIRECT_FUND_FLOW_TIMEOUT"
    assert legacy_values(task.values) == PARSED_VALUES
    assert task.source_errors["main_fund_flow"] == "DIRECT_FUND_FLOW_TIMEOUT"
    assert task.intraday_series[MetricKind.RETAIL_COUNT]["points"] == [
        {"time": "09:31", "value": "21.23"}
    ]


def test_data_only_runner_preserves_interface_error_without_ocr_fallback(tmp_path: Path) -> None:
    store = InMemoryStreams()
    store.enqueue(TaskRecord(
        task_id="failed-interface-data-only-task",
        symbol="601872",
        include_long_capture=False,
    ))
    bridge = FakeDeviceBridge(symbol="601872")
    runner = _successful_runner(
        store,
        Level2Navigator(bridge),
        tmp_path,
    )

    def fail_direct(_symbol: str):
        from level2_service.parsed_values import DirectRequestError

        raise DirectRequestError("DIRECT_MANAGER_UNAVAILABLE")

    runner.parsed_value_source = types.SimpleNamespace(read_direct=fail_direct)
    runner.value_reader = types.SimpleNamespace(
        read=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("task values must never run OCR")
        )
    )
    runner.stitcher = lambda _frames: (_ for _ in ()).throw(AssertionError("must not stitch"))

    task = runner.run_once()

    assert task is not None and task.status == TaskStatus.FAILED
    assert task.error_code == "DIRECT_MANAGER_UNAVAILABLE"
    assert task.source_errors["core_metrics"] == "DIRECT_MANAGER_UNAVAILABLE"
    assert all(value is None for value in task.values.values())
    assert all(source is None for source in task.value_sources.values())
    assert bridge.inputs == []
    assert task.long_capture.status == CaptureStatus.SKIPPED
    assert not (tmp_path / "failed-interface-data-only-task").exists()


def test_online_ui_failure_is_not_reported_as_device_offline(tmp_path: Path) -> None:
    """A uiautomator/UI exception while adb is healthy must not poison runner health."""
    store = InMemoryStreams()
    store.enqueue(TaskRecord(task_id="ui-failure-task", symbol="601872"))
    bridge = FakeDeviceBridge(symbol="601872", failures=[RuntimeError("uiautomator rpc failed")])
    bridge.online = True
    control = RunnerControl()

    task = Level2Runner(
        store,
        Level2Navigator(bridge),
        tmp_path,
        control,
        parsed_value_source=types.SimpleNamespace(
            read_direct=lambda _symbol: dict(PARSED_VALUES)
        ),
    ).run_once()

    assert task is not None and task.status == TaskStatus.FAILED
    assert task.error_code == "NAVIGATION_FAILED"
    assert control.state == "READY"


def test_device_transport_failure_is_the_only_capture_error_marked_offline(tmp_path: Path) -> None:
    store = InMemoryStreams()
    store.enqueue(TaskRecord(task_id="transport-failure-task", symbol="601872"))
    bridge = FakeDeviceBridge(symbol="601872", failures=[RuntimeError("transport closed")])
    bridge.online = False
    control = RunnerControl()

    task = Level2Runner(
        store,
        Level2Navigator(bridge),
        tmp_path,
        control,
        parsed_value_source=types.SimpleNamespace(
            read_direct=lambda _symbol: dict(PARSED_VALUES)
        ),
    ).run_once()

    assert task is not None and task.status == TaskStatus.FAILED
    assert task.error_code == "DEVICE_OFFLINE"
    assert control.state == "OFFLINE"


def test_navigator_requires_exact_symbol_and_tab_before_capturing() -> None:
    """A similarly named stock or tab must never be published as this task's result."""
    bridge = FakeDeviceBridge(symbol="SZ.000001", visible_symbol="SZ.000002")

    try:
        Level2Navigator(bridge).capture("SZ.000001", CaptureKind.RETAIL_COUNT)
    except NavigationError as error:
        assert "symbol" in str(error)
    else:
        raise AssertionError("mismatched symbol was captured")


def test_navigator_rejects_ambiguous_exact_symbol_results() -> None:
    bridge = FakeDeviceBridge(symbol="SZ.000001", exact_symbol_matches=2)

    try:
        Level2Navigator(bridge).capture("SZ.000001", CaptureKind.LARGE_ORDER_NET)
    except NavigationError as error:
        assert "unique" in str(error)
    else:
        raise AssertionError("ambiguous symbol result was captured")


def test_runner_retries_transient_navigation_up_to_three_attempts(tmp_path: Path) -> None:
    """Only failures before stock entry may repeat the symbol search."""
    class TransientSearchBridge(FakeDeviceBridge):
        search_attempts = 0

        def wait_for_selector_text(self, selector: str, text: str, timeout: float) -> bool:
            self.search_attempts += 1
            if self.search_attempts < 3:
                return False
            return super().wait_for_selector_text(selector, text, timeout)

    store = InMemoryStreams()
    store.enqueue(TaskRecord(task_id="retry-task", symbol="SZ.000001"))
    bridge = TransientSearchBridge(symbol="SZ.000001")
    runner = _successful_runner(store, Level2Navigator(bridge), tmp_path)

    task = runner.run_once()

    assert task is not None
    assert task.status == TaskStatus.COMPLETED
    assert bridge.search_attempts == 3
    assert bridge.capture_attempts == 3


def test_runner_marks_login_requirement_waiting_for_admin(tmp_path: Path) -> None:
    """Login/CAPTCHA/device gates must wait for a human instead of being bypassed."""
    store = InMemoryStreams()
    store.enqueue(TaskRecord(task_id="admin-task", symbol="SZ.000001"))
    store.enqueue(TaskRecord(task_id="later-task", symbol="SZ.000002"))
    bridge = FakeDeviceBridge(symbol="SZ.000001", failures=[NeedsAdminError("login required")])
    runner = Level2Runner(
        store,
        Level2Navigator(bridge),
        tmp_path,
        RunnerControl(),
        parsed_value_source=types.SimpleNamespace(
            read_direct=lambda _symbol: dict(PARSED_VALUES)
        ),
    )

    task = runner.run_once()

    assert task is not None
    assert task.status == TaskStatus.WAITING_ADMIN
    assert task.error_code == "WAITING_ADMIN"
    assert runner.control.queue_paused is True
    assert runner.run_once() is None
    assert store.get("later-task").status == TaskStatus.QUEUED


def test_runner_does_not_leave_a_claimed_task_running_when_device_fails(tmp_path: Path) -> None:
    store = InMemoryStreams()
    store.enqueue(TaskRecord(task_id="offline-task", symbol="SZ.000001"))
    control = RunnerControl()
    runner = Level2Runner(
        store,
        Level2Navigator(FakeDeviceBridge(symbol="SZ.000001", failures=[RuntimeError("adb offline")], online=False)),
        tmp_path,
        control,
        parsed_value_source=types.SimpleNamespace(
            read_direct=lambda _symbol: dict(PARSED_VALUES)
        ),
    )

    task = runner.run_once()

    assert task is not None
    assert task.status == TaskStatus.FAILED
    assert task.error_code == "DEVICE_OFFLINE"
    assert control.state == "OFFLINE"


def test_waiting_admin_task_can_be_requeued_and_run_after_intervention(tmp_path: Path) -> None:
    """A human-cleared login gate must not strand the claimed FIFO task forever."""
    store = InMemoryStreams()
    store.enqueue(TaskRecord(task_id="recover-task", symbol="SZ.000001"))
    blocked = Level2Runner(
        store,
        Level2Navigator(
            FakeDeviceBridge(symbol="SZ.000001", failures=[NeedsAdminError("login")])
        ),
        tmp_path,
        RunnerControl(),
        parsed_value_source=types.SimpleNamespace(
            read_direct=lambda _symbol: dict(PARSED_VALUES)
        ),
    ).run_once()

    assert blocked is not None and blocked.status == TaskStatus.WAITING_ADMIN
    assert store.requeue_waiting("recover-task").status == TaskStatus.QUEUED
    recovered = _successful_runner(store, Level2Navigator(FakeDeviceBridge(symbol="SZ.000001")), tmp_path).run_once()

    assert recovered is not None and recovered.status == TaskStatus.COMPLETED


def test_runner_does_not_publish_a_long_capture_when_one_source_frame_fails(tmp_path: Path) -> None:
    """A missing middle frame cannot produce a truthful continuous long screenshot."""
    store = InMemoryStreams()
    store.enqueue(TaskRecord(task_id="partial-task", symbol="SZ.000001"))
    bridge = FakeDeviceBridge(symbol="SZ.000001", failures=[None, NavigationError("bad chart"), None])
    runner = Level2Runner(
        store,
        Level2Navigator(bridge),
        tmp_path,
        RunnerControl(),
        parsed_value_source=types.SimpleNamespace(
            read_direct=lambda _symbol: dict(PARSED_VALUES)
        ),
    )

    task = runner.run_once()

    assert task is not None
    assert task.status == TaskStatus.FAILED
    assert task.error_code == "NAVIGATION_FAILED"
    assert task.long_capture.path is None


def test_runner_control_pauses_queue_and_only_lock_owner_can_forward_input() -> None:
    """Queue and remote-device control belong to one authenticated admin session."""
    control = RunnerControl()
    control.heartbeat("READY")
    control.pause_queue()

    assert control.health()["state"] == "READY"
    assert control.health()["queue_paused"] is True
    assert control.lock("owner") is True
    assert control.authorizes_input("owner") is True
    assert control.authorizes_input("other") is False
    assert control.status("owner")["state"] == "ADMIN_CONTROL"
    assert control.release("owner") is True
    assert control.status("owner")["state"] == "READY"
