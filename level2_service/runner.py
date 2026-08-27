"""Safe Android UI runner and single-admin device-control primitives.

The runner uses only the public Android UI through ADB/uiautomator2. It never
knows, records, or sends application credentials or private THS protocol data.
"""

from __future__ import annotations

import base64
from contextlib import contextmanager
from io import BytesIO
import json
import os
import re
import subprocess
import tempfile
from threading import RLock
import time
from xml.etree import ElementTree
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterator, Protocol
from zoneinfo import ZoneInfo

from .models import CaptureKind, MetricKind, TaskRecord, TaskStatus, utc_now
from .parsed_values import DirectReadOutcome, DirectRequestError, ParsedValueSource, UnsupportedMarketError, market_code_for_symbol
from .queue import TaskStore


RUNNER_STATES = frozenset({"BOOTING", "READY", "ADMIN_CONTROL", "NEEDS_ADMIN", "OFFLINE"})
APP_PACKAGE = "com.hexin.plat.android"
APP_ACTIVITY = "com.hexin.plat.android.LogoEmptyActivity"
TAB_LABELS = {
    CaptureKind.LARGE_ORDER_NET: "大单净量",
    CaptureKind.LARGE_ORDER_AMOUNT: "大单金额",
    CaptureKind.RETAIL_COUNT: "散户数量",
}
CHART_CAPTURE_ORDER = (
    CaptureKind.RETAIL_COUNT,
    CaptureKind.LARGE_ORDER_NET,
    CaptureKind.LARGE_ORDER_AMOUNT,
)
CHART_SCROLL_SWIPE = (0.5, 0.85, 0.5, 0.47)
CHART_SCROLL_SETTLE_SECONDS = 0.7
MAX_CHART_FRAMES = 6
SCREEN_SIZE = (1080, 1920)
FIXED_HEADER_HEIGHT = 215
FIXED_FOOTER_HEIGHT = 154
HOME_MARKER_SELECTOR = "com.hexin.plat.android:id/firstpagenavi"
HOME_SEARCH_SELECTOR = "com.hexin.plat.android:id/first_page_search_layout_container"
SEARCH_INPUT_SELECTOR = "com.hexin.plat.android:id/search_input"
SEARCH_RESULT_CODE_SELECTOR = "com.hexin.plat.android:id/stock_code"
STOCK_TITLE_SELECTOR = "com.hexin.plat.android:id/navi_title_text"
MAX_HOME_BACK_PRESSES = 8
ADMIN_BLOCKING_TEXTS = frozenset({"登录", "验证码", "设备验证", "人机验证", "暂无权限", "开通"})
MARKET_LABELS = {
    "17": "沪A",
    "20": "沪基",
    "33": "深A",
    "36": "深基",
    "151": "京A",
}
BEIJING_TIMEZONE = ZoneInfo("Asia/Shanghai")
ANDROID_KEY_NAMES = {
    "Enter": "KEYCODE_ENTER",
    "Backspace": "KEYCODE_DEL",
    "Delete": "KEYCODE_FORWARD_DEL",
    "Tab": "KEYCODE_TAB",
    "Escape": "KEYCODE_ESCAPE",
    " ": "KEYCODE_SPACE",
    "ArrowUp": "KEYCODE_DPAD_UP",
    "ArrowDown": "KEYCODE_DPAD_DOWN",
    "ArrowLeft": "KEYCODE_DPAD_LEFT",
    "ArrowRight": "KEYCODE_DPAD_RIGHT",
    "Home": "KEYCODE_MOVE_HOME",
    "End": "KEYCODE_MOVE_END",
    "PageUp": "KEYCODE_PAGE_UP",
    "PageDown": "KEYCODE_PAGE_DOWN",
    "Shift": "KEYCODE_SHIFT_LEFT",
    "Control": "KEYCODE_CTRL_LEFT",
    "Alt": "KEYCODE_ALT_LEFT",
    "Meta": "KEYCODE_META_LEFT",
    "CapsLock": "KEYCODE_CAPS_LOCK",
    "Insert": "KEYCODE_INSERT",
    ",": "KEYCODE_COMMA",
    ".": "KEYCODE_PERIOD",
    "/": "KEYCODE_SLASH",
    "-": "KEYCODE_MINUS",
    "=": "KEYCODE_EQUALS",
    "[": "KEYCODE_LEFT_BRACKET",
    "]": "KEYCODE_RIGHT_BRACKET",
    "\\": "KEYCODE_BACKSLASH",
    ";": "KEYCODE_SEMICOLON",
    "'": "KEYCODE_APOSTROPHE",
}


class NavigationError(RuntimeError):
    """A retryable public-UI navigation failure."""


class NeedsAdminError(RuntimeError):
    """Login, CAPTCHA, device verification, or entitlement requires a human."""


class DailyCheckState:
    """Persist the most recent successful admin-gate check by Beijing date."""

    def __init__(self, path: Path, *, clock: Callable[[], datetime] = utc_now) -> None:
        self.path = path.expanduser().resolve()
        self.clock = clock

    def passed_today(self) -> bool:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
            return False
        return payload.get("passed_date") == self._today()

    def mark_passed(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.", dir=self.path.parent, text=True
        )
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as temporary:
                descriptor = -1
                json.dump({"passed_date": self._today()}, temporary, ensure_ascii=False)
                temporary.write("\n")
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_name, self.path)
            os.chmod(self.path, 0o600)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass

    def _today(self) -> str:
        return self.clock().astimezone(BEIJING_TIMEZONE).date().isoformat()


def _decode_chart_content(frame: bytes):
    from PIL import Image

    try:
        with Image.open(BytesIO(frame)) as image:
            decoded = image.convert("RGB").copy()
    except Exception as error:
        raise NavigationError("device did not return a readable PNG screenshot") from error
    if decoded.size != SCREEN_SIZE:
        raise NavigationError("device screenshot must be 1080x1920")
    return decoded.crop((0, FIXED_HEADER_HEIGHT, SCREEN_SIZE[0], SCREEN_SIZE[1] - FIXED_FOOTER_HEIGHT))


def _chart_scroll_offset(previous, current) -> int:
    """Return the vertical page movement, or zero when the page has stopped."""
    from PIL import Image, ImageChops, ImageStat

    sample_width = 180
    sample_height = previous.height // 4
    alignment_left = round(previous.width * 0.70)
    alignment_right = previous.width - 12
    previous_sample = previous.crop((alignment_left, 0, alignment_right, previous.height)).convert("L").resize(
        (sample_width, sample_height), Image.Resampling.BILINEAR
    )
    current_sample = current.crop((alignment_left, 0, alignment_right, current.height)).convert("L").resize(
        (sample_width, sample_height), Image.Resampling.BILINEAR
    )

    def difference(offset: int) -> float:
        height = sample_height - offset
        delta = ImageChops.difference(
            previous_sample.crop((0, offset, sample_width, sample_height)),
            current_sample.crop((0, 0, sample_width, height)),
        )
        return ImageStat.Stat(delta).mean[0] / 255

    # Real stock-page swipes move a little over 80% of the chart viewport.
    # Keeping this at 75% makes the matcher choose a shorter repeated quote-row
    # alignment, so the next frame overwrites the bottom of the amount chart.
    # A 10% overlap still leaves enough pixels for a reliable match.
    maximum = round(sample_height * 0.90)
    score, offset = min((difference(candidate), candidate) for candidate in range(maximum + 1))
    if score > 0.24:
        raise NavigationError("unable to align scrolled chart screenshots")
    coarse_offset = round(offset * previous.height / sample_height)
    previous_rows = previous.crop((alignment_left, 0, alignment_right, previous.height)).convert("L").resize(
        (sample_width, previous.height), Image.Resampling.BILINEAR
    )
    current_rows = current.crop((alignment_left, 0, alignment_right, current.height)).convert("L").resize(
        (sample_width, current.height), Image.Resampling.BILINEAR
    )

    def refined_difference(candidate: int) -> float:
        height = previous.height - candidate
        delta = ImageChops.difference(
            previous_rows.crop((0, candidate, sample_width, previous.height)),
            current_rows.crop((0, 0, sample_width, height)),
        )
        return ImageStat.Stat(delta).mean[0] / 255

    refined_score, refined_offset = min(
        (refined_difference(candidate), candidate)
        for candidate in range(max(0, coarse_offset - 8), min(previous.height - 1, coarse_offset + 8) + 1)
    )
    if refined_score > 0.24:
        raise NavigationError("unable to align scrolled chart screenshots")
    return 0 if refined_offset <= 8 else refined_offset


def stitch_long_capture(frames: tuple[bytes, ...]) -> bytes:
    """Join scrolled 1080x1920 frames while keeping fixed chrome only once."""
    from PIL import Image

    if not frames:
        raise NavigationError("no screenshots were captured")
    decoded: list[Image.Image] = []
    contents = []
    for frame in frames:
        content = _decode_chart_content(frame)
        contents.append(content)
        screen = Image.new("RGB", SCREEN_SIZE)
        screen.paste(content, (0, FIXED_HEADER_HEIGHT))
        with Image.open(BytesIO(frame)) as source:
            screen.paste(source.convert("RGB").crop((0, 0, SCREEN_SIZE[0], FIXED_HEADER_HEIGHT)), (0, 0))
            screen.paste(
                source.convert("RGB").crop((0, SCREEN_SIZE[1] - FIXED_FOOTER_HEIGHT, *SCREEN_SIZE)),
                (0, SCREEN_SIZE[1] - FIXED_FOOTER_HEIGHT),
            )
        decoded.append(screen)

    content_bottom = SCREEN_SIZE[1] - FIXED_FOOTER_HEIGHT
    offsets = [_chart_scroll_offset(previous, current) for previous, current in zip(contents, contents[1:])]
    if any(offset == 0 for offset in offsets):
        raise NavigationError("chart page did not scroll")
    document_height = contents[0].height + sum(offsets)
    document = Image.new("RGB", (SCREEN_SIZE[0], document_height))
    document.paste(contents[0], (0, 0))
    cursor = 0
    for content, offset in zip(contents[1:], offsets):
        cursor += offset
        # Android renders off-screen chart Views lazily. Prefer the newer
        # screenshot across the whole overlap so a chart that appeared only
        # after scrolling is not discarded as if it were duplicate pixels.
        document.paste(content, (0, cursor))

    result = Image.new(
        "RGB",
        (SCREEN_SIZE[0], FIXED_HEADER_HEIGHT + document.height + FIXED_FOOTER_HEIGHT),
    )
    result.paste(decoded[0].crop((0, 0, SCREEN_SIZE[0], FIXED_HEADER_HEIGHT)), (0, 0))
    result.paste(document, (0, FIXED_HEADER_HEIGHT))
    result.paste(
        decoded[-1].crop((0, content_bottom, SCREEN_SIZE[0], SCREEN_SIZE[1])),
        (0, FIXED_HEADER_HEIGHT + document.height),
    )
    output = BytesIO()
    result.save(output, format="PNG", optimize=True)
    return output.getvalue()


def long_capture_has_net_heading(
    image: bytes,
    *,
    ocr: Callable[[bytes], str] | None = None,
) -> bool:
    """Reject a stitched image when OCR cannot find the large-order net heading."""
    ocr_input = image
    if ocr is None:
        from PIL import Image
        import numpy as np

        with Image.open(BytesIO(image)) as source:
            heading_column = source.convert("RGB").crop((0, 0, min(280, source.width), source.height))
        pixels = np.asarray(heading_column).copy()
        channel_max = pixels.max(axis=2)
        neutral_dark_text = (
            (channel_max < 175)
            & ((channel_max - pixels.min(axis=2)) < 28)
        )
        pixels[~neutral_dark_text] = 255
        prepared = Image.fromarray(pixels, mode="RGB").resize(
            (heading_column.width * 3, heading_column.height * 3),
            Image.Resampling.NEAREST,
        )
        encoded = BytesIO()
        prepared.save(encoded, format="PNG", optimize=True)
        ocr_input = encoded.getvalue()

        def recognize(raw: bytes) -> str:
            process = subprocess.run(
                ["tesseract", "stdin", "stdout", "-l", "chi_sim+eng", "--psm", "6"],
                input=raw,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
            )
            return process.stdout.decode("utf-8", "ignore")

        ocr = recognize
    try:
        recognized = ocr(ocr_input)
        # Fund pages use a different Level-2 heading from ordinary stock
        # pages.  This is still only a structural check; all task values are
        # read from the App-internal direct interface.
        return "净量" in recognized or "大单占比" in recognized
    except Exception:
        return False


class IndicatorValueReader:
    """Read the three visible chart values from calibrated 1080x1920 crops."""

    _REGIONS = {
        CaptureKind.RETAIL_COUNT: (0, (365, 1620, 515, 1690)),
        CaptureKind.LARGE_ORDER_NET: (2, (335, 865, 475, 940)),
        CaptureKind.LARGE_ORDER_AMOUNT: (2, (300, 1135, 545, 1235)),
    }
    _THRESHOLDS = {
        CaptureKind.RETAIL_COUNT: 220,
        CaptureKind.LARGE_ORDER_NET: None,
        CaptureKind.LARGE_ORDER_AMOUNT: 220,
    }
    _LABELS = {
        CaptureKind.RETAIL_COUNT: r"散户数(?:量)?",
        CaptureKind.LARGE_ORDER_NET: r"(?:大单)?净量",
        CaptureKind.LARGE_ORDER_AMOUNT: r"(?:大单)?金额",
    }

    def __init__(self, ocr: Callable[[bytes, CaptureKind], str] | None = None) -> None:
        self.ocr = ocr or self._tesseract

    def read(
        self,
        frames: tuple[bytes, ...],
        kinds: set[CaptureKind] | None = None,
    ) -> dict[CaptureKind, str | None]:
        from PIL import Image

        requested = set(CaptureKind) if kinds is None else set(kinds)
        if not requested:
            return {}
        if len(frames) < 3:
            raise NavigationError("three chart screenshots are required")
        decoded: list[Image.Image] = []
        try:
            for frame in frames:
                with Image.open(BytesIO(frame)) as image:
                    decoded.append(image.convert("RGB").copy())
        except Exception as error:
            raise NavigationError("device did not return a readable PNG screenshot") from error
        if any(image.size != SCREEN_SIZE for image in decoded):
            raise NavigationError("device screenshot must be 1080x1920")

        values: dict[CaptureKind, str | None] = {}
        if CaptureKind.RETAIL_COUNT in requested:
            retail_index, retail_default = self._REGIONS[CaptureKind.RETAIL_COUNT]
            values[CaptureKind.RETAIL_COUNT] = self._read_region(
                decoded[retail_index], CaptureKind.RETAIL_COUNT, retail_default
            )

        large_kinds = tuple(
            kind
            for kind in (CaptureKind.LARGE_ORDER_NET, CaptureKind.LARGE_ORDER_AMOUNT)
            if kind in requested
        )
        if not large_kinds:
            return values
        if len(large_kinds) == 1:
            kind = large_kinds[0]
            for image in decoded:
                region = self._located_region(image, kind)
                if region is None:
                    continue
                value = self._read_region(image, kind, region)
                if value is not None:
                    values[kind] = value
                    return values
            frame_index, default_region = self._REGIONS[kind]
            values[kind] = self._read_region(decoded[frame_index], kind, default_region)
            return values
        best_large_values = {kind: None for kind in large_kinds}
        found_chart_pair = False
        for image in decoded:
            regions = {
                kind: self._located_region(image, kind)
                for kind in large_kinds
            }
            if any(region is None for region in regions.values()):
                continue
            net_region = regions[CaptureKind.LARGE_ORDER_NET]
            amount_region = regions[CaptureKind.LARGE_ORDER_AMOUNT]
            assert net_region is not None and amount_region is not None
            if not 180 <= amount_region[1] - net_region[1] <= 380:
                continue
            found_chart_pair = True
            candidate_values = {
                kind: self._read_region(image, kind, regions[kind])
                for kind in large_kinds
            }
            if sum(value is not None for value in candidate_values.values()) > sum(
                value is not None for value in best_large_values.values()
            ):
                best_large_values = candidate_values
            if all(candidate_values.values()):
                break

        if not found_chart_pair:
            best_large_values = {
                kind: self._read_region(decoded[frame_index], kind, default_region)
                for kind, (frame_index, default_region) in self._REGIONS.items()
                if kind in large_kinds
            }
        values.update(best_large_values)
        return values

    def _read_region(self, image, kind: CaptureKind, region: tuple[int, int, int, int]) -> str | None:
        from PIL import Image

        crop = image.crop(region).resize(
            ((region[2] - region[0]) * 4, (region[3] - region[1]) * 4),
            Image.Resampling.LANCZOS,
        )
        threshold = self._THRESHOLDS[kind]
        if threshold is not None:
            crop = crop.convert("L").point(lambda pixel: 0 if pixel < threshold else 255)
        output = BytesIO()
        crop.save(output, format="PNG")
        try:
            text = self.ocr(output.getvalue(), kind)
        except Exception:
            text = ""
        return self._validated_value(kind, text)

    @classmethod
    def _tracked_region(cls, image, kind: CaptureKind, default_region: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
        if kind == CaptureKind.RETAIL_COUNT:
            return default_region
        return cls._located_region(image, kind) or default_region

    @staticmethod
    def _located_region(image, kind: CaptureKind) -> tuple[int, int, int, int] | None:
        search_left, search_top, search_right, search_bottom = (280, FIXED_HEADER_HEIGHT, 560, SCREEN_SIZE[1] - FIXED_FOOTER_HEIGHT)
        rows = [0] * (search_bottom - search_top)
        pixels = image.crop((search_left, search_top, search_right, search_bottom)).getdata()
        width = search_right - search_left
        for index, (red, green, blue) in enumerate(pixels):
            if kind == CaptureKind.LARGE_ORDER_NET:
                matches = green > 80 and green * 100 > red * 125 and green * 100 > blue * 125
            else:
                matches = red > 180 and 80 < green < 210 and blue < 120 and red * 100 > green * 120
            if matches:
                rows[index // width] += 1
        window_height = 70
        window = sum(rows[:window_height])
        best_score = window
        best_start = 0
        for start in range(1, len(rows) - window_height + 1):
            window += rows[start + window_height - 1] - rows[start - 1]
            if window > best_score:
                best_score = window
                best_start = start
        minimum_score = 150 if kind == CaptureKind.LARGE_ORDER_NET else 400
        if best_score < minimum_score:
            return None
        peak = search_top + best_start
        if kind == CaptureKind.LARGE_ORDER_NET:
            top = peak + 20
            return (335, top, 475, top + 75)
        top = peak - 32
        return (300, top, 545, top + 100)

    @classmethod
    def _validated_value(cls, kind: CaptureKind, text: str) -> str | None:
        normalized = re.sub(r"\s+", "", text).replace("−", "-").replace("—", "-").replace("：", ":")
        unit = r"(?:万|亿)" if kind == CaptureKind.LARGE_ORDER_AMOUNT else ""
        number = rf"[+-]?\d{{1,12}}(?:\.\d{{1,4}})?{unit}"
        labeled = re.search(rf"{cls._LABELS[kind]}[^+\-\d]*({number})", normalized)
        if labeled:
            return labeled.group(1)
        standalone = re.fullmatch(rf"[^+\-\d]*({number})[^\d万亿]*", normalized)
        return standalone.group(1) if standalone else None

    @staticmethod
    def _tesseract(crop: bytes, _kind: CaptureKind) -> str:
        page_segmentation = "6" if _kind == CaptureKind.LARGE_ORDER_AMOUNT else "7"
        process = subprocess.run(
            [
                "tesseract",
                "stdin",
                "stdout",
                "-l",
                "chi_sim+eng",
                "--psm",
                page_segmentation,
                "-c",
                "tessedit_char_whitelist=+-0123456789.万亿",
            ],
            input=crop,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
        return process.stdout.decode("utf-8", "ignore")


class DeviceBridge(Protocol):
    """Small, normalised boundary around a real Android device UI."""

    def launch_app(self, package: str, activity: str) -> None: ...
    def screenshot_png(self) -> bytes: ...
    def tap(self, x: float, y: float) -> None: ...
    def swipe(self, start_x: float, start_y: float, end_x: float, end_y: float) -> None: ...
    def wait_for_scroll_settle(self, timeout: float) -> None: ...
    def key(self, key: str, action: str) -> None: ...
    def press_back(self) -> None: ...
    def has_text(self, text: str) -> bool: ...
    def visible_texts(self) -> frozenset[str]: ...
    def exact_text_count(self, text: str) -> int: ...
    def click_text(self, text: str) -> bool: ...
    def has_selector(self, selector: str) -> bool: ...
    def click_selector(self, selector: str) -> bool: ...
    def wait_for_selector(self, selector: str, timeout: float) -> bool: ...
    def wait_for_selector_text(self, selector: str, text: str, timeout: float) -> bool: ...
    def replace_text(self, selector: str, value: str) -> bool: ...
    def exact_selector_text_count(self, selector: str, text: str) -> int: ...
    def click_selector_text(self, selector: str, text: str) -> bool: ...
    def is_selected(self, selector: str) -> bool: ...
    def input_text(self, value: str) -> None: ...
    def is_online(self) -> bool: ...
    def app_running(self, package: str = APP_PACKAGE) -> bool: ...


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

    def __init__(
        self,
        adb: str = "adb",
        serial: str | None = None,
        uiautomator_adapter: object | None = None,
        environment: dict[str, str] | None = None,
        command_timeout_seconds: float = 10.0,
    ) -> None:
        if command_timeout_seconds <= 0:
            raise ValueError("ADB command timeout must be positive")
        self.adb = adb
        self.serial = serial
        self._uiautomator_adapter = uiautomator_adapter
        self.environment = environment
        self.command_timeout_seconds = command_timeout_seconds

    def _run(self, *args: str) -> bytes:
        command = [self.adb]
        if self.serial:
            command.extend(["-s", self.serial])
        command.extend(args)
        try:
            return subprocess.run(
                command,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=self.environment or os.environ.copy(),
                timeout=self.command_timeout_seconds,
            ).stdout
        except subprocess.TimeoutExpired as error:
            raise NavigationError("ADB command timed out") from error

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

    def wait_for_scroll_settle(self, timeout: float) -> None:
        time.sleep(timeout)

    def key(self, key: str, action: str) -> None:
        if action == "down":
            self._shell("input", "keyevent", _android_keycode(key))

    def press_back(self) -> None:
        self._shell("input", "keyevent", "KEYCODE_BACK")

    def has_text(self, text: str) -> bool:
        adapter = self._uiautomator()
        if adapter is None:
            return text in self._shell("uiautomator", "dump", "/dev/tty").decode("utf-8", "ignore")
        return bool(adapter(text=text).exists)

    def visible_texts(self) -> frozenset[str]:
        """Return all visible UI texts from one hierarchy dump."""
        adapter = self._uiautomator()
        raw = (
            adapter.dump_hierarchy(compressed=True)
            if adapter is not None
            else self._shell("uiautomator", "dump", "/dev/tty").decode("utf-8", "ignore")
        )
        start = raw.find("<hierarchy")
        end_marker = "</hierarchy>"
        end = raw.find(end_marker, start) if start >= 0 else -1
        if start < 0 or end < 0:
            raise NavigationError("device UI hierarchy is unavailable")
        try:
            root = ElementTree.fromstring(raw[start : end + len(end_marker)])
        except (ElementTree.ParseError, ValueError) as error:
            raise NavigationError("device UI hierarchy is unavailable") from error
        return frozenset(
            text
            for node in root.iter()
            if (text := node.attrib.get("text", "").strip())
        )

    def exact_text_count(self, text: str) -> int:
        adapter = self._uiautomator()
        if adapter is None:
            raw = self._shell("uiautomator", "dump", "/dev/tty").decode("utf-8", "ignore")
            start = raw.find("<hierarchy")
            end_marker = "</hierarchy>"
            end = raw.find(end_marker, start) if start >= 0 else -1
            if start < 0 or end < 0:
                return 0
            try:
                root = ElementTree.fromstring(raw[start : end + len(end_marker)])
            except (ElementTree.ParseError, ValueError):
                return 0
            return sum(node.attrib.get("text") == text for node in root.iter())
        selector = adapter(text=text)
        count = getattr(selector, "count", None)
        if callable(count):
            count = count()
        if isinstance(count, int):
            return count
        return 1 if selector.exists else 0

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
        def click(adapter: object) -> bool:
            target = adapter(resourceId=selector)
            if not target.exists:
                return False
            target.click()
            return True

        return bool(self._run_uiautomator_action(click))

    def wait_for_selector(self, selector: str, timeout: float) -> bool:
        adapter = self._uiautomator()
        return bool(adapter and adapter(resourceId=selector).wait(timeout=timeout))

    def wait_for_selector_text(self, selector: str, text: str, timeout: float) -> bool:
        adapter = self._uiautomator()
        return bool(adapter and adapter(resourceId=selector, text=text).wait(timeout=timeout))

    def replace_text(self, selector: str, value: str) -> bool:
        adapter = self._uiautomator()
        if adapter is None:
            return False
        target = adapter(resourceId=selector)
        if not target.exists:
            return False
        target.set_text(value)
        return True

    def exact_selector_text_count(self, selector: str, text: str) -> int:
        adapter = self._uiautomator()
        if adapter is None:
            return 0
        target = adapter(resourceId=selector, text=text)
        count = getattr(target, "count", None)
        if callable(count):
            count = count()
        if isinstance(count, int):
            return count
        return 1 if target.exists else 0

    def click_selector_text(self, selector: str, text: str) -> bool:
        def click(adapter: object) -> bool:
            target = adapter(resourceId=selector, text=text)
            if not target.exists:
                return False
            target.click()
            return True

        return bool(self._run_uiautomator_action(click))

    def click_search_result(self, selector: str, text: str, market_label: str) -> bool:
        """Tap the exact-code result whose card carries the expected market label."""
        adapter = self._uiautomator()
        if adapter is None:
            return False
        try:
            raw = adapter.dump_hierarchy(compressed=True)
            root = ElementTree.fromstring(raw)
        except (ElementTree.ParseError, TypeError, ValueError, AttributeError):
            return False

        parent_map: dict[int, list[ElementTree.Element]] = {}

        def collect(node: ElementTree.Element, ancestors: list[ElementTree.Element]) -> None:
            parent_map[id(node)] = ancestors
            for child in node:
                collect(child, [*ancestors, node])

        collect(root, [])
        bounds_pattern = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")
        for candidate in root.iter():
            if candidate.attrib.get("resource-id") != selector or candidate.attrib.get("text") != text:
                continue
            label_container = next(
                (
                    ancestor
                    for ancestor in reversed(parent_map.get(id(candidate), []))
                    if ancestor.attrib.get("resource-id", "").endswith("stock_code_label")
                ),
                None,
            )
            if label_container is None:
                continue
            if not any(node.attrib.get("text") == market_label for node in label_container.iter()):
                continue
            match = bounds_pattern.fullmatch(label_container.attrib.get("bounds", ""))
            if match is None:
                continue
            left, top, right, bottom = (int(value) for value in match.groups())
            self.tap((left + right) / 2 / SCREEN_SIZE[0], (top + bottom) / 2 / SCREEN_SIZE[1])
            return True
        return False

    def is_selected(self, selector: str) -> bool:
        adapter = self._uiautomator()
        return bool(adapter and adapter(resourceId=selector, selected=True).exists)

    def input_text(self, value: str) -> None:
        self._shell("input", "text", value.replace(" ", "%s"))

    def is_online(self) -> bool:
        try:
            return self._run("get-state").strip() == b"device"
        except (OSError, subprocess.SubprocessError):
            return False

    def app_running(self, package: str = APP_PACKAGE) -> bool:
        if not self.is_online():
            return False
        try:
            return bool(self._shell("pidof", package).strip())
        except (OSError, subprocess.SubprocessError):
            return False

    @staticmethod
    def _coordinate(value: float, limit: int) -> float:
        if not 0 <= value <= 1:
            raise ValueError("input coordinate must be normalised to 0..1")
        return value * limit

    def _uiautomator(self):
        if self._uiautomator_adapter is not None:
            return self._uiautomator_adapter
        if self.serial is None:
            # Never let uiautomator2 guess when two account devices may be attached.
            return None
        try:
            import uiautomator2 as u2  # type: ignore[import-not-found]
        except ImportError:
            return None
        self._configure_uiautomator_adb()
        self._uiautomator_adapter = u2.connect(self.serial)
        return self._uiautomator_adapter

    def _run_uiautomator_action(self, action: Callable[[object], object]) -> object | None:
        for attempt in range(2):
            try:
                adapter = self._uiautomator()
                return action(adapter) if adapter is not None else None
            except Exception as error:
                if attempt == 0 and self._is_stale_uiautomator_error(error):
                    self._uiautomator_adapter = None
                    continue
                raise
        return None

    @staticmethod
    def _is_stale_uiautomator_error(error: Exception) -> bool:
        return "StaleObjectException" in f"{type(error).__name__}: {error}"

    def _configure_uiautomator_adb(self) -> None:
        """Make adbutils use the same remote ADB server as the adb CLI."""
        environment = self.environment or os.environ
        socket = environment.get("ADB_SERVER_SOCKET", "")
        if not socket.startswith("tcp:"):
            return
        endpoint = socket.removeprefix("tcp:")
        host, separator, port_text = endpoint.rpartition(":")
        if not separator or not host or not port_text.isdigit():
            return
        try:
            import adbutils  # type: ignore[import-not-found]
        except ImportError:
            return
        adbutils.adb = adbutils.AdbClient(host=host, port=int(port_text))


@dataclass
class FakeDeviceBridge:
    """Deterministic public-UI fake used by tests; it contains no credentials."""

    symbol: str
    visible_symbol: str | None = None
    exact_symbol_matches: int = 1
    selector_available: bool = True
    tab_activation: bool = True
    failures: list[Exception | None] = field(default_factory=list)
    screenshot: bytes = b"\x89PNG\r\n\x1a\nfake"
    inputs: list[tuple] = field(default_factory=list)
    visual_actions: list[str] = field(default_factory=list)
    capture_attempts: int = 0
    selected_tab: str | None = None
    home_ready: bool = True
    search_open: bool = False
    stock_open: bool = False
    online: bool = True

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

    def wait_for_scroll_settle(self, _timeout: float) -> None:
        return None

    def key(self, key: str, action: str) -> None:
        self.inputs.append(("key", key, action))

    def press_back(self) -> None:
        self.inputs.append(("back",))
        self.home_ready = True
        self.search_open = False
        self.stock_open = False

    def has_text(self, text: str) -> bool:
        if text == "买卖队列":
            swipe_count = sum(action[0] == "swipe" for action in self.inputs)
            return swipe_count >= 2
        return text in {self.visible_symbol or self.symbol, self.selected_tab, *TAB_LABELS.values()}

    def visible_texts(self) -> frozenset[str]:
        texts = {self.visible_symbol or self.symbol, self.selected_tab, *TAB_LABELS.values()}
        return frozenset(text for text in texts if text)

    def exact_text_count(self, text: str) -> int:
        if self.visible_symbol is not None:
            return int(text == self.visible_symbol)
        if text == self.symbol:
            return self.exact_symbol_matches
        return int(self.has_text(text))

    def click_text(self, text: str) -> bool:
        if text in TAB_LABELS.values():
            if self.tab_activation:
                self.selected_tab = text
            return True
        if text == "搜索":
            self.search_open = True
            return True
        return False

    def has_selector(self, selector: str) -> bool:
        if selector == HOME_MARKER_SELECTOR:
            return self.home_ready
        if selector == HOME_SEARCH_SELECTOR:
            return self.selector_available and self.home_ready
        if selector == SEARCH_INPUT_SELECTOR:
            return self.search_open
        if selector == STOCK_TITLE_SELECTOR:
            return self.stock_open
        return self.selector_available and selector in {*(f"tab:{tab}" for tab in TAB_LABELS.values())}

    def click_selector(self, selector: str) -> bool:
        if not self.has_selector(selector):
            return False
        if selector == HOME_SEARCH_SELECTOR:
            self.search_open = True
            return True
        if selector.startswith("tab:") and self.tab_activation:
            self.selected_tab = selector.removeprefix("tab:")
        return True

    def wait_for_selector(self, selector: str, _timeout: float) -> bool:
        return self.has_selector(selector)

    def wait_for_selector_text(self, selector: str, text: str, _timeout: float) -> bool:
        return self.exact_selector_text_count(selector, text) > 0

    def replace_text(self, selector: str, value: str) -> bool:
        if selector != SEARCH_INPUT_SELECTOR or not self.search_open:
            return False
        self.symbol = value
        return True

    def exact_selector_text_count(self, selector: str, text: str) -> int:
        if selector != SEARCH_RESULT_CODE_SELECTOR or not self.search_open:
            return 0
        if self.visible_symbol is not None:
            return int(text == self.visible_symbol)
        return self.exact_symbol_matches if text == self.symbol else 0

    def click_selector_text(self, selector: str, text: str) -> bool:
        if self.exact_selector_text_count(selector, text) != 1:
            return False
        self.stock_open = True
        return True

    def is_selected(self, selector: str) -> bool:
        return selector == f"tab:{self.selected_tab}"

    def input_text(self, value: str) -> None:
        self.symbol = value

    def is_online(self) -> bool:
        return self.online

    def app_running(self, _package: str = APP_PACKAGE) -> bool:
        return self.online

    def visual_tap(self, name: str) -> bool:
        self.visual_actions.append(name)
        if name == "search":
            self.search_open = True
        if name.startswith("tab:") and self.tab_activation:
            self.selected_tab = name.removeprefix("tab:")
        return True


class FakeTemplateFallback:
    def tap_template(self, name: str, bridge: DeviceBridge) -> bool:
        action = getattr(bridge, "visual_tap", None)
        return bool(action and action(name))


class Level2Navigator:
    """Open one exact stock once, then capture its vertically stacked charts."""

    def __init__(self, bridge: DeviceBridge, visual_fallback: TemplateFallback | None = None) -> None:
        self.bridge = bridge
        self.visual_fallback = visual_fallback or FakeTemplateFallback()

    def open_stock(self, symbol: str) -> None:
        normalized = symbol.strip().upper()
        self.bridge.launch_app(APP_PACKAGE, APP_ACTIVITY)
        # am start returns before the App has rebuilt its home hierarchy.  Do
        # not send back presses into that transition; on the real APK this
        # can leave the activity outside the home screen and make all three
        # navigation attempts fail.
        self.bridge.wait_for_selector(HOME_MARKER_SELECTOR, 5)
        self._return_to_home()
        if not self.bridge.click_selector(HOME_SEARCH_SELECTOR) and not self.bridge.click_text("搜索") and not self.visual_fallback.tap_template("search", self.bridge):
            raise NavigationError("symbol search selector unavailable")
        if not self.bridge.wait_for_selector(SEARCH_INPUT_SELECTOR, 5):
            raise NavigationError("symbol search input unavailable")
        if not self.bridge.replace_text(SEARCH_INPUT_SELECTOR, normalized):
            raise NavigationError("symbol search input unavailable")
        if not self.bridge.wait_for_selector_text(SEARCH_RESULT_CODE_SELECTOR, normalized, 10):
            raise NavigationError("exact symbol result unavailable")
        exact_count = self.bridge.exact_selector_text_count(SEARCH_RESULT_CODE_SELECTOR, normalized)
        market_label = None
        if normalized.isdigit() and len(normalized) == 6:
            try:
                market_label = MARKET_LABELS.get(market_code_for_symbol(normalized))
            except UnsupportedMarketError:
                market_label = None
        if exact_count > 1 and market_label is not None:
            click_market_result = getattr(self.bridge, "click_search_result", None)
            clicked = callable(click_market_result) and bool(
                click_market_result(SEARCH_RESULT_CODE_SELECTOR, normalized, market_label)
            )
        else:
            if exact_count != 1:
                raise NavigationError("exact symbol is not unique")
            clicked = self.bridge.click_selector_text(SEARCH_RESULT_CODE_SELECTOR, normalized)
        if not clicked:
            raise NavigationError("exact symbol result unavailable")
        if not self.bridge.wait_for_selector(STOCK_TITLE_SELECTOR, 20):
            raise NavigationError("stock page did not open")

    def admin_blocking_texts(self) -> frozenset[str]:
        return ADMIN_BLOCKING_TEXTS.intersection(self.bridge.visible_texts())

    def capture_indicators(self, kinds: set[CaptureKind] | None = None) -> dict[CaptureKind, bytes]:
        """Capture the three configured indicators while scrolling the four charts."""
        requested = set(CHART_CAPTURE_ORDER) if kinds is None else set(kinds)
        images: dict[CaptureKind, bytes] = {}
        for index, kind in enumerate(CHART_CAPTURE_ORDER):
            if kind in requested:
                try:
                    image = self.bridge.screenshot_png()
                    if not image.startswith(b"\x89PNG"):
                        raise NavigationError("device did not return a PNG screenshot")
                except NavigationError:
                    pass
                else:
                    images[kind] = image
            if index < len(CHART_CAPTURE_ORDER) - 1:
                self.bridge.swipe(*CHART_SCROLL_SWIPE)
        return images

    def capture_chart_frames(self) -> tuple[bytes, ...]:
        """Capture unique chart screens until a swipe no longer moves the page."""
        first = self.bridge.screenshot_png()
        if not first.startswith(b"\x89PNG"):
            raise NavigationError("device did not return a PNG screenshot")
        frames = [first]
        previous_content = _decode_chart_content(first)
        for _ in range(MAX_CHART_FRAMES - 1):
            self.bridge.swipe(*CHART_SCROLL_SWIPE)
            self.bridge.wait_for_scroll_settle(CHART_SCROLL_SETTLE_SECONDS)
            image = self.bridge.screenshot_png()
            if not image.startswith(b"\x89PNG"):
                raise NavigationError("device did not return a PNG screenshot")
            current_content = _decode_chart_content(image)
            if _chart_scroll_offset(previous_content, current_content) == 0:
                return tuple(frames)
            frames.append(image)
            previous_content = current_content
        raise NavigationError("chart page did not stop scrolling")

    def capture_all(self, symbol: str, kinds: set[CaptureKind] | None = None) -> dict[CaptureKind, bytes]:
        self.open_stock(symbol)
        return self.capture_indicators(kinds)

    def capture(self, symbol: str, kind: CaptureKind) -> bytes:
        """Compatibility wrapper for callers that still request one capture."""
        images = self.capture_all(symbol, {kind})
        if kind not in images:
            raise NavigationError(f"{TAB_LABELS[kind]} chart unavailable")
        return images[kind]

    def device_online(self) -> bool:
        try:
            return self.bridge.is_online()
        except Exception:
            return False

    def _return_to_home(self) -> None:
        for attempt in range(MAX_HOME_BACK_PRESSES + 1):
            if self.bridge.has_selector(HOME_MARKER_SELECTOR):
                return
            if attempt == MAX_HOME_BACK_PRESSES:
                break
            self.bridge.press_back()
            if self.bridge.wait_for_selector(HOME_MARKER_SELECTOR, 1):
                return
        raise NavigationError("app home page unavailable")

    def _exact_text_count(self, text: str) -> int:
        counter = getattr(self.bridge, "exact_text_count", None)
        if callable(counter):
            return int(counter(text))
        return int(self.bridge.has_text(text))


class RunnerMaintenanceError(RuntimeError):
    """A fixed failure raised while atomically entering device maintenance."""

    def __init__(self, error_code: str) -> None:
        super().__init__(error_code)
        self.error_code = error_code


@dataclass
class RunnerControl:
    state: str = "OFFLINE"
    last_heartbeat: datetime | None = None
    queue_paused: bool = False
    _lock_owner: str | None = None
    _sequence: int = 0
    _listeners: list[Callable[[dict], None]] = field(default_factory=list)
    _socket_disconnectors: dict[str, list[Callable[[], None]]] = field(default_factory=dict)
    _maintenance_gate: RLock = field(
        default_factory=RLock,
        repr=False,
        compare=False,
    )

    def heartbeat(self, state: str = "READY") -> None:
        with self._maintenance_gate:
            if state not in RUNNER_STATES:
                raise ValueError("unknown runner state")
            self.state = state
            self.last_heartbeat = utc_now()
            self._publish()

    def health(self) -> dict:
        with self._maintenance_gate:
            return {"state": self._effective_state(), "last_heartbeat": self.last_heartbeat.isoformat() if self.last_heartbeat else None, "queue_paused": self.queue_paused}

    def lock(self, session_id: str) -> bool:
        with self._maintenance_gate:
            if self._lock_owner not in (None, session_id):
                # A lock acquired without a live device stream is stale: the
                # browser that owned it can no longer send input, so allow the
                # next authenticated administrator to recover the device.
                if self._socket_disconnectors.get(self._lock_owner):
                    return False
                self._lock_owner = None
            self._lock_owner = session_id
            # Device takeover must stop the worker from claiming any new task.
            # Releasing the lock deliberately does not resume automation; the
            # administrator must make that decision explicitly via the queue API.
            self.queue_paused = True
            self._publish()
            return True

    def release(self, session_id: str) -> bool:
        with self._maintenance_gate:
            if self._lock_owner != session_id:
                return False
            self._lock_owner = None
            self._publish()
            return True

    def lock_state(self, session_id: str) -> dict[str, bool]:
        with self._maintenance_gate:
            return {"locked": self._lock_owner == session_id}

    def authorizes_input(self, session_id: str) -> bool:
        with self._maintenance_gate:
            return self._lock_owner == session_id

    def disconnect_session(self, session_id: str) -> None:
        """Invalidate a disconnected admin's lock and active device streams."""
        with self._maintenance_gate:
            if self._lock_owner == session_id:
                self._lock_owner = None
            for disconnect in tuple(self._socket_disconnectors.get(session_id, ())):
                disconnect()
            self._publish()

    def register_socket(self, session_id: str, disconnect: Callable[[], None]) -> Callable[[], None]:
        with self._maintenance_gate:
            self._socket_disconnectors.setdefault(session_id, []).append(disconnect)

        def unregister() -> None:
            with self._maintenance_gate:
                sockets = self._socket_disconnectors.get(session_id)
                if sockets is None:
                    return
                if disconnect in sockets:
                    sockets.remove(disconnect)
                if not sockets:
                    self._socket_disconnectors.pop(session_id, None)
                    if self._lock_owner == session_id:
                        self._lock_owner = None
                        self._publish()

        return unregister

    def pause_queue(self) -> None:
        with self._maintenance_gate:
            self.queue_paused = True
            self._publish()

    def resume_queue(self) -> bool:
        with self._maintenance_gate:
            if self._lock_owner is not None:
                return False
            self.queue_paused = False
            self._publish()
            return True

    def claim_next_task(self, store: TaskStore) -> TaskRecord | None:
        """Atomically recheck pause state and claim under the maintenance gate."""
        with self._maintenance_gate:
            if self.queue_paused:
                return None
            self.heartbeat("BOOTING")
            return store.next_queued()

    @contextmanager
    def maintenance(
        self,
        session_id: str,
        store: TaskStore,
    ) -> Iterator[None]:
        """Hold device ownership and idle guarantees through one host action."""
        with self._maintenance_gate:
            if self._lock_owner != session_id:
                raise RunnerMaintenanceError("DEVICE_LIFECYCLE_LOCK_REQUIRED")
            if not self.queue_paused or store.has_running_task():
                raise RunnerMaintenanceError("DEVICE_LIFECYCLE_BUSY")
            yield

    def status(self, session_id: str) -> dict:
        with self._maintenance_gate:
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

    def __init__(
        self,
        store: TaskStore,
        navigator: Level2Navigator,
        capture_root: Path,
        control: RunnerControl,
        *,
        value_reader: IndicatorValueReader | None = None,
        parsed_value_source: ParsedValueSource | None = None,
        daily_check_state: DailyCheckState | None = None,
        stitcher: Callable[[tuple[bytes, ...]], bytes] = stitch_long_capture,
        long_capture_validator: Callable[[bytes], bool] | None = None,
    ) -> None:
        self.store = store
        self.navigator = navigator
        self.capture_root = capture_root.resolve()
        self.control = control
        self.value_reader = value_reader or IndicatorValueReader()
        self.parsed_value_source = parsed_value_source
        self.daily_check_state = daily_check_state
        self.stitcher = stitcher
        self.long_capture_validator = long_capture_validator

    def run_once(self) -> TaskRecord | None:
        task = self.control.claim_next_task(self.store)
        if task is None and self.control.queue_paused:
            if self.control.state == "OFFLINE":
                self.control.heartbeat("READY" if self.device_online() else "OFFLINE")
            return None
        if task is None:
            self.control.heartbeat("READY")
            return None
        self.control.heartbeat("READY")
        source_errors: dict[str, str | None] | None = None
        intraday_series: dict[MetricKind, dict[str, object]] | None = None
        try:
            frames: tuple[bytes, ...] | None = None
            long_capture: bytes | None = None
            self._run_daily_admin_check()
            direct_reader = getattr(self.parsed_value_source, "read_direct", None)
            if not callable(direct_reader):
                raise DirectRequestError(
                    "DIRECT_REQUEST_UNAVAILABLE",
                    "App interface value source is not configured",
                )
            direct_result = direct_reader(task.symbol)
            if isinstance(direct_result, DirectReadOutcome):
                values = direct_result.values
                source_errors = direct_result.source_errors
                intraday_series = direct_result.intraday_series
            else:
                values = direct_result
            if task.include_long_capture:
                for capture_attempt in range(2):
                    self._open_stock_with_retry(task.symbol)
                    frames = self.navigator.capture_chart_frames()
                    long_capture = self.stitcher(frames)
                    if self.long_capture_validator is None or self.long_capture_validator(long_capture):
                        break
                    if capture_attempt == 1:
                        raise NavigationError("long capture is missing the large-order net heading")
                assert frames is not None and long_capture is not None
        except NeedsAdminError:
            self.store.transition(task.task_id, TaskStatus.WAITING_ADMIN, error_code="WAITING_ADMIN")
            self.control.pause_queue()
            self.control.heartbeat("NEEDS_ADMIN")
            return self.store.get(task.task_id)
        except NavigationError:
            failed = self.store.get(task.task_id) or task
            if failed.status in {TaskStatus.RUNNING, TaskStatus.PARTIAL}:
                failed = self.store.transition(task.task_id, TaskStatus.FAILED, error_code="NAVIGATION_FAILED")
            self.control.heartbeat("READY")
            return failed
        except UnsupportedMarketError:
            failed = self.store.get(task.task_id) or task
            if failed.status in {TaskStatus.RUNNING, TaskStatus.PARTIAL}:
                failed = self.store.transition(
                    task.task_id, TaskStatus.FAILED, error_code="UNSUPPORTED_MARKET"
                )
            self.control.heartbeat("READY")
            return failed
        except DirectRequestError as error:
            failed = self.store.get(task.task_id) or task
            if failed.status in {TaskStatus.RUNNING, TaskStatus.PARTIAL}:
                failed = self.store.transition(
                    task.task_id,
                    TaskStatus.FAILED,
                    error_code=error.error_code,
                    source_errors={"core_metrics": error.error_code},
                )
            self.control.heartbeat("READY" if self.device_online() else "OFFLINE")
            return failed
        except Exception:
            return self._fail_capture(task)
        try:
            path: Path | None = None
            if task.include_long_capture:
                assert long_capture is not None
                path = self.capture_root / task.task_id / "LONG.png"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(long_capture)
            task = self.store.complete_result(
                task.task_id,
                values,
                str(path) if path is not None else None,
                source_errors=source_errors,
                intraday_series=intraday_series,
            )
        except Exception:
            failed = self.store.get(task.task_id) or task
            if failed.status in {TaskStatus.RUNNING, TaskStatus.PARTIAL}:
                failed = self.store.transition(task.task_id, TaskStatus.FAILED, error_code="CAPTURE_STORAGE_FAILED")
            self.control.heartbeat("READY")
            return failed
        self.control.heartbeat("READY")
        return task

    def run_forever(self, max_iterations: int | None = None) -> int:
        iterations = 0
        while max_iterations is None or iterations < max_iterations:
            self.run_once()
            iterations += 1
        return iterations

    def device_online(self) -> bool:
        return self.navigator.device_online()

    def _open_stock_with_retry(self, symbol: str) -> None:
        last_error: NavigationError | None = None
        for _ in range(3):
            try:
                self.navigator.open_stock(symbol)
            except NeedsAdminError:
                raise
            except NavigationError as error:
                last_error = error
                continue
            return
        raise last_error or NavigationError("navigation failed")

    def _run_daily_admin_check(self) -> None:
        state = self.daily_check_state
        if state is None or state.passed_today():
            return
        if self.navigator.admin_blocking_texts():
            raise NeedsAdminError("admin interaction required")
        state.mark_passed()

    def _fail_capture(self, task: TaskRecord) -> TaskRecord:
        online = self.device_online()
        failed = self.store.get(task.task_id) or task
        if failed.status in {TaskStatus.RUNNING, TaskStatus.PARTIAL}:
            error_code = "NAVIGATION_FAILED" if online else "DEVICE_OFFLINE"
            failed = self.store.transition(task.task_id, TaskStatus.FAILED, error_code=error_code)
        self.control.heartbeat("READY" if online else "OFFLINE")
        return failed


def _android_keycode(key: str) -> str:
    if key in ANDROID_KEY_NAMES:
        return ANDROID_KEY_NAMES[key]
    if key.startswith("KEYCODE_"):
        return key.upper()
    if len(key) == 1 and key.isalpha():
        return f"KEYCODE_{key.upper()}"
    if len(key) == 1 and key.isdigit():
        return f"KEYCODE_{key}"
    return key


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
