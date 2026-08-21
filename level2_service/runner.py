"""Safe Android UI runner and single-admin device-control primitives.

The runner uses only the public Android UI through ADB/uiautomator2. It never
knows, records, or sends application credentials or private THS protocol data.
"""

from __future__ import annotations

import base64
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Protocol

from .models import CaptureKind, CaptureStatus, TaskRecord, TaskStatus, utc_now
from .queue import TaskStore


RUNNER_STATES = frozenset({"BOOTING", "READY", "ADMIN_CONTROL", "NEEDS_ADMIN", "OFFLINE"})
APP_PACKAGE = "com.hexin.plat.android"
APP_ACTIVITY = "com.hexin.plat.android.LogoEmptyActivity"
TAB_LABELS = {
    CaptureKind.LARGE_ORDER_NET: "大单净量",
    CaptureKind.LARGE_ORDER_AMOUNT: "大单金额",
    CaptureKind.RETAIL_COUNT: "散户数量",
}


class NavigationError(RuntimeError):
    """A retryable public-UI navigation failure."""


class NeedsAdminError(RuntimeError):
    """Login, CAPTCHA, device verification, or entitlement requires a human."""


class DeviceBridge(Protocol):
    """Small, normalised boundary around a real Android device UI."""

    def launch_app(self, package: str, activity: str) -> None: ...
    def screenshot_png(self) -> bytes: ...
    def tap(self, x: float, y: float) -> None: ...
    def swipe(self, start_x: float, start_y: float, end_x: float, end_y: float) -> None: ...
    def key(self, key: str, action: str) -> None: ...
    def has_text(self, text: str) -> bool: ...
    def click_text(self, text: str) -> bool: ...
    def has_selector(self, selector: str) -> bool: ...
    def click_selector(self, selector: str) -> bool: ...
    def is_selected(self, selector: str) -> bool: ...
    def input_text(self, value: str) -> None: ...


class TemplateFallback(Protocol):
    def tap_template(self, name: str, bridge: DeviceBridge) -> bool: ...


class OpenCVTemplateFallback:
    """Optional visual fallback; cv2/numpy are imported only when used."""

    def __init__(self, templates: dict[str, Path] | None = None) -> None:
        self.templates = templates or {}

    def tap_template(self, name: str, bridge: DeviceBridge) -> bool:
        template = self.templates.get(name)
        if template is None or not template.is_file():
            return False
        try:
            import cv2  # type: ignore[import-not-found]
            import numpy as np  # type: ignore[import-not-found]
        except ImportError:
            return False
        screen = cv2.imdecode(np.frombuffer(bridge.screenshot_png(), np.uint8), cv2.IMREAD_COLOR)
        needle = cv2.imread(str(template), cv2.IMREAD_COLOR)
        if screen is None or needle is None:
            return False
        _, score, _, point = cv2.minMaxLoc(cv2.matchTemplate(screen, needle, cv2.TM_CCOEFF_NORMED))
        if score < 0.90:
            return False
        height, width = screen.shape[:2]
        bridge.tap((point[0] + needle.shape[1] / 2) / width, (point[1] + needle.shape[0] / 2) / height)
        return True


class ADBDeviceBridge:
    """Lazy ADB bridge; importing this module never requires ADB or uiautomator2."""

    def __init__(self, adb: str = "adb", serial: str | None = None, uiautomator_adapter: object | None = None) -> None:
        self.adb = adb
        self.serial = serial
        self._uiautomator_adapter = uiautomator_adapter

    def _run(self, *args: str) -> bytes:
        command = [self.adb]
        if self.serial:
            command.extend(["-s", self.serial])
        command.extend(args)
        return subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout

    def _shell(self, *args: str) -> bytes:
        return self._run("shell", *args)

    def launch_app(self, package: str, activity: str) -> None:
        self._shell("am", "start", "-n", f"{package}/{activity}")

    def screenshot_png(self) -> bytes:
        return self._run("exec-out", "screencap", "-p")

    def tap(self, x: float, y: float) -> None:
        self._shell("input", "tap", str(round(self._coordinate(x, 1080))), str(round(self._coordinate(y, 1920))))

    def swipe(self, start_x: float, start_y: float, end_x: float, end_y: float) -> None:
        self._shell("input", "swipe", str(round(self._coordinate(start_x, 1080))), str(round(self._coordinate(start_y, 1920))), str(round(self._coordinate(end_x, 1080))), str(round(self._coordinate(end_y, 1920))), "250")

    def key(self, key: str, action: str) -> None:
        if action == "down":
            self._shell("input", "keyevent", key)

    def has_text(self, text: str) -> bool:
        adapter = self._uiautomator()
        if adapter is None:
            return text in self._shell("uiautomator", "dump", "/dev/tty").decode("utf-8", "ignore")
        return bool(adapter(text=text).exists)

    def click_text(self, text: str) -> bool:
        adapter = self._uiautomator()
        if adapter is None:
            return False
        target = adapter(text=text)
        if not target.exists:
            return False
        target.click()
        return True

    def has_selector(self, selector: str) -> bool:
        adapter = self._uiautomator()
        return bool(adapter and adapter(resourceId=selector).exists)

    def click_selector(self, selector: str) -> bool:
        adapter = self._uiautomator()
        if adapter is None:
            return False
        target = adapter(resourceId=selector)
        if not target.exists:
            return False
        target.click()
        return True

    def is_selected(self, selector: str) -> bool:
        adapter = self._uiautomator()
        return bool(adapter and adapter(resourceId=selector, selected=True).exists)

    def input_text(self, value: str) -> None:
        self._shell("input", "text", value.replace(" ", "%s"))

    @staticmethod
    def _coordinate(value: float, limit: int) -> float:
        if not 0 <= value <= 1:
            raise ValueError("input coordinate must be normalised to 0..1")
        return value * limit

    def _uiautomator(self):
        if self._uiautomator_adapter is not None:
            return self._uiautomator_adapter
        try:
            import uiautomator2 as u2  # type: ignore[import-not-found]
        except ImportError:
            return None
        self._uiautomator_adapter = u2.connect(self.serial) if self.serial else u2.connect()
        return self._uiautomator_adapter


@dataclass
class FakeDeviceBridge:
    """Deterministic public-UI fake used by tests; it contains no credentials."""

    symbol: str
    visible_symbol: str | None = None
    selector_available: bool = True
    tab_activation: bool = True
    failures: list[Exception | None] = field(default_factory=list)
    screenshot: bytes = b"\x89PNG\r\n\x1a\nfake"
    inputs: list[tuple] = field(default_factory=list)
    visual_actions: list[str] = field(default_factory=list)
    capture_attempts: int = 0
    selected_tab: str | None = None

    def launch_app(self, package: str, activity: str) -> None:
        assert package == APP_PACKAGE and activity == APP_ACTIVITY

    def screenshot_png(self) -> bytes:
        self.capture_attempts += 1
        if self.failures:
            failure = self.failures.pop(0)
            if failure is not None:
                raise failure
        return self.screenshot

    def tap(self, x: float, y: float) -> None:
        self.inputs.append(("tap", x, y))

    def swipe(self, start_x: float, start_y: float, end_x: float, end_y: float) -> None:
        self.inputs.append(("swipe", start_x, start_y, end_x, end_y))

    def key(self, key: str, action: str) -> None:
        self.inputs.append(("key", key, action))

    def has_text(self, text: str) -> bool:
        return text in {self.visible_symbol or self.symbol, self.selected_tab, *TAB_LABELS.values()}

    def click_text(self, text: str) -> bool:
        if text in TAB_LABELS.values():
            if self.tab_activation:
                self.selected_tab = text
            return True
        return text == "搜索"

    def has_selector(self, selector: str) -> bool:
        return self.selector_available and selector in {"symbol_search", *(f"tab:{tab}" for tab in TAB_LABELS.values())}

    def click_selector(self, selector: str) -> bool:
        if not self.has_selector(selector):
            return False
        if selector.startswith("tab:") and self.tab_activation:
            self.selected_tab = selector.removeprefix("tab:")
        return True

    def is_selected(self, selector: str) -> bool:
        return selector == f"tab:{self.selected_tab}"

    def input_text(self, value: str) -> None:
        self.symbol = value

    def visual_tap(self, name: str) -> bool:
        self.visual_actions.append(name)
        if name.startswith("tab:") and self.tab_activation:
            self.selected_tab = name.removeprefix("tab:")
        return True


class FakeTemplateFallback:
    def tap_template(self, name: str, bridge: DeviceBridge) -> bool:
        action = getattr(bridge, "visual_tap", None)
        return bool(action and action(name))


class Level2Navigator:
    """Selector-first public UI recipe with exact symbol/tab verification."""

    def __init__(self, bridge: DeviceBridge, visual_fallback: TemplateFallback | None = None) -> None:
        self.bridge = bridge
        self.visual_fallback = visual_fallback or FakeTemplateFallback()

    def capture(self, symbol: str, kind: CaptureKind) -> bytes:
        normalized = symbol.strip().upper()
        self.bridge.launch_app(APP_PACKAGE, APP_ACTIVITY)
        self._raise_if_admin_needed()
        if not self.bridge.click_selector("symbol_search") and not self.visual_fallback.tap_template("search", self.bridge):
            raise NavigationError("symbol search selector unavailable")
        self.bridge.input_text(normalized)
        if not self.bridge.has_text(normalized):
            raise NavigationError("exact symbol not visible")
        label = TAB_LABELS[kind]
        selector = f"tab:{label}"
        if not self.bridge.click_selector(selector) and not self.bridge.click_text(label) and not self.visual_fallback.tap_template(selector, self.bridge):
            raise NavigationError("Level2 tab unavailable")
        self._raise_if_admin_needed()
        if not self.bridge.has_text(normalized):
            raise NavigationError("exact symbol not visible")
        if not self.bridge.has_text(label):
            raise NavigationError("exact Level2 tab not visible")
        if not self.bridge.is_selected(selector):
            raise NavigationError("exact Level2 tab is not active")
        image = self.bridge.screenshot_png()
        if not image.startswith(b"\x89PNG"):
            raise NavigationError("device did not return a PNG screenshot")
        return image

    def _raise_if_admin_needed(self) -> None:
        for text in ("登录", "验证码", "设备验证", "人机验证", "暂无权限", "开通"):
            if self.bridge.has_text(text):
                raise NeedsAdminError("admin interaction required")


@dataclass
class RunnerControl:
    state: str = "OFFLINE"
    last_heartbeat: datetime | None = None
    queue_paused: bool = False
    _lock_owner: str | None = None
    _sequence: int = 0
    _listeners: list[Callable[[dict], None]] = field(default_factory=list)
    _socket_disconnectors: dict[str, list[Callable[[], None]]] = field(default_factory=dict)

    def heartbeat(self, state: str = "READY") -> None:
        if state not in RUNNER_STATES:
            raise ValueError("unknown runner state")
        self.state = state
        self.last_heartbeat = utc_now()
        self._publish()

    def health(self) -> dict:
        return {"state": self._effective_state(), "last_heartbeat": self.last_heartbeat.isoformat() if self.last_heartbeat else None, "queue_paused": self.queue_paused}

    def lock(self, session_id: str) -> bool:
        if self._lock_owner not in (None, session_id):
            return False
        self._lock_owner = session_id
        self._publish()
        return True

    def release(self, session_id: str) -> bool:
        if self._lock_owner != session_id:
            return False
        self._lock_owner = None
        self._publish()
        return True

    def lock_state(self, session_id: str) -> dict[str, bool]:
        return {"locked": self._lock_owner == session_id}

    def authorizes_input(self, session_id: str) -> bool:
        return self._lock_owner == session_id

    def disconnect_session(self, session_id: str) -> None:
        """Invalidate a disconnected admin's lock and active device streams."""
        if self._lock_owner == session_id:
            self._lock_owner = None
        for disconnect in tuple(self._socket_disconnectors.get(session_id, ())):
            disconnect()
        self._publish()

    def register_socket(self, session_id: str, disconnect: Callable[[], None]) -> Callable[[], None]:
        self._socket_disconnectors.setdefault(session_id, []).append(disconnect)

        def unregister() -> None:
            sockets = self._socket_disconnectors.get(session_id)
            if sockets is None:
                return
            if disconnect in sockets:
                sockets.remove(disconnect)
            if not sockets:
                self._socket_disconnectors.pop(session_id, None)

        return unregister

    def pause_queue(self) -> None:
        self.queue_paused = True
        self._publish()

    def resume_queue(self) -> None:
        self.queue_paused = False
        self._publish()

    def status(self, session_id: str) -> dict:
        return {"type": "runner_status", "state": self._effective_state(), "locked": self.authorizes_input(session_id), "sequence": self._sequence}

    def subscribe(self, listener: Callable[[dict], None]) -> Callable[[], None]:
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener) if listener in self._listeners else None

    def _effective_state(self) -> str:
        return "ADMIN_CONTROL" if self._lock_owner is not None else self.state

    def _publish(self) -> None:
        self._sequence += 1
        payload = self.health()
        for listener in tuple(self._listeners):
            listener(payload)


class Level2Runner:
    """FIFO single task executor with bounded retries and partial capture support."""

    def __init__(self, store: TaskStore, navigator: Level2Navigator, capture_root: Path, control: RunnerControl) -> None:
        self.store = store
        self.navigator = navigator
        self.capture_root = capture_root.resolve()
        self.control = control

    def run_once(self) -> TaskRecord | None:
        if self.control.queue_paused:
            return None
        self.control.heartbeat("BOOTING")
        task = self.store.next_queued()
        if task is None:
            self.control.heartbeat("READY")
            return None
        self.control.heartbeat("READY")
        for kind in CaptureKind:
            if task.captures[kind].status == CaptureStatus.READY:
                continue
            try:
                image = self._capture_with_retry(task.symbol, kind)
            except NeedsAdminError:
                self.store.transition(task.task_id, TaskStatus.WAITING_ADMIN, error_code="WAITING_ADMIN")
                self.control.heartbeat("NEEDS_ADMIN")
                return self.store.get(task.task_id)
            except NavigationError:
                continue
            path = self.capture_root / task.task_id / f"{kind.value}.png"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(image)
            self.store.complete_capture(task.task_id, kind, str(path))
            task = self.store.get(task.task_id) or task
        task = self.store.get(task.task_id) or task
        if task.status == TaskStatus.RUNNING:
            task = self.store.transition(task.task_id, TaskStatus.FAILED, error_code="NAVIGATION_FAILED")
        self.control.heartbeat("READY")
        return task

    def run_forever(self, max_iterations: int | None = None) -> int:
        iterations = 0
        while max_iterations is None or iterations < max_iterations:
            self.run_once()
            iterations += 1
        return iterations

    def _capture_with_retry(self, symbol: str, kind: CaptureKind) -> bytes:
        last_error: NavigationError | None = None
        for _ in range(3):
            try:
                return self.navigator.capture(symbol, kind)
            except NeedsAdminError:
                raise
            except NavigationError as error:
                last_error = error
        raise last_error or NavigationError("navigation failed")


def jpeg_base64(png_or_jpeg: bytes) -> str:
    """Convert ADB PNG screenshots to JPEG using the declared Pillow dependency."""
    if png_or_jpeg.startswith(b"\xff\xd8"):
        return base64.b64encode(png_or_jpeg).decode("ascii")
    from io import BytesIO
    from PIL import Image

    try:
        with Image.open(BytesIO(png_or_jpeg)) as image:
            output = BytesIO()
            image.convert("RGB").save(output, format="JPEG", quality=80, optimize=True)
    except Exception as error:
        raise ValueError("unable to encode device screenshot as JPEG") from error
    return base64.b64encode(output.getvalue()).decode("ascii")
