"""Server-side market-data transports that reuse a human-authenticated App session."""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import base64
import re
import socket
import struct
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from threading import Lock, RLock, Thread
from typing import Any, Callable, Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .app_sessions import SessionProvider
from .market_data import MarketSnapshot
from .models import FUND_FLOW_METRICS, FUND_FLOW_PERIODS, MetricKind
from .parsed_values import (
    DirectReadOutcome,
    DirectRequestError,
    empty_metric_values,
    market_code_for_symbol,
    sanitized_direct_error_code,
)


FUND_FLOW_URL = "https://dataq.10jqka.com.cn/fetch-data-server/fetch/v1/specific_data"
FUND_FLOW_SOURCE_ID = "sif-charge-indicator-capital"
CORE_BASE64_ALPHABET = (
    "aCcMeTKhxnwzmoPbsG4EvU8gyd02B3q6fIVWXYZjApRrDtuHkiLlN1O9F5S7JQ+/"
)
CORE_STANDARD_BASE64_ALPHABET = (
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
)
FUND_FLOW_INDEXES = (
    "charge_main_capital",
    "charge_main_listed_capital",
    "charge_main_grey_capital",
)

logger = logging.getLogger(__name__)


def _core_base64_tables(alphabet: str) -> tuple[dict[int, int], dict[int, int]]:
    if (
        len(alphabet) != 64
        or len(set(alphabet)) != 64
        or "=" in alphabet
        or any(ord(character) > 127 for character in alphabet)
    ):
        raise ValueError("core Base64 alphabet must contain 64 unique characters")
    encode = str.maketrans(
        CORE_STANDARD_BASE64_ALPHABET,
        alphabet,
    )
    decode = str.maketrans(
        alphabet,
        CORE_STANDARD_BASE64_ALPHABET,
    )
    return encode, decode


def encode_core_base64(payload: bytes, alphabet: str = CORE_BASE64_ALPHABET) -> bytes:
    encode, _ = _core_base64_tables(alphabet)
    return base64.b64encode(payload).decode("ascii").translate(encode).encode("ascii")


def decode_core_base64(
    payload: bytes | str, alphabet: str = CORE_BASE64_ALPHABET
) -> bytes:
    _, decode = _core_base64_tables(alphabet)
    text = payload.decode("ascii") if isinstance(payload, bytes) else payload
    try:
        return base64.b64decode(text.translate(decode), validate=True)
    except (ValueError, UnicodeEncodeError) as error:
        raise DirectRequestError(
            "DIRECT_PROTOCOL_RESPONSE_INVALID",
            "core Base64 payload is invalid",
        ) from error


def patch_core_packet_symbol(
    packet: bytes,
    original_symbol: str,
    replacement_symbol: str,
    *,
    alphabet: str = CORE_BASE64_ALPHABET,
) -> bytes:
    """Patch a six-digit symbol inside a captured final wire packet."""

    _validate_9528_outer_packet(packet)
    if len(packet) < 15:
        raise ValueError("core packet header is truncated")
    try:
        header_length = struct.unpack_from("<H", packet, 13)[0]
    except struct.error as error:
        raise ValueError("core packet header is truncated") from error
    body_start = 13 + header_length
    if header_length < 2 or body_start >= len(packet):
        raise ValueError("core packet header exceeds packet length")
    decoded = bytearray(decode_core_base64(packet[body_start:], alphabet))
    replacements = (
        (original_symbol.encode("ascii"), replacement_symbol.encode("ascii")),
        (
            original_symbol.encode("utf-16-be"),
            replacement_symbol.encode("utf-16-be"),
        ),
        (
            original_symbol.encode("utf-16-le"),
            replacement_symbol.encode("utf-16-le"),
        ),
    )
    changed = False
    for old, new in replacements:
        if old in decoded:
            decoded[:] = decoded.replace(old, new)
            changed = True
    if not changed:
        raise DirectRequestError(
            "DIRECT_PROTOCOL_REQUEST_INVALID",
            "core packet does not contain the template symbol",
        )
    encoded = encode_core_base64(bytes(decoded), alphabet)
    if len(encoded) != len(packet) - body_start:
        raise DirectRequestError(
            "DIRECT_PROTOCOL_REQUEST_INVALID",
            "core symbol patch changed packet length",
        )
    return packet[:body_start] + encoded


def _validate_9528_outer_packet(packet: bytes) -> None:
    if len(packet) < 14 or packet[:4] != b"\xfd" * 4:
        raise ValueError("core packet does not contain the expected wire prefix")
    if re.fullmatch(rb"[0-9A-Fa-f]{8}", packet[4:12]) is None:
        raise ValueError("core packet length field is invalid")
    try:
        declared_length = int(packet[4:12].decode("ascii"), 16)
    except (UnicodeDecodeError, ValueError):
        raise ValueError("core packet length field is invalid") from None
    if packet[12] != 0:
        raise ValueError("core packet separator is invalid")
    if declared_length != len(packet) - 13 or declared_length > 16 * 1024 * 1024:
        raise ValueError("core packet length does not match its payload")


class _CoreGovBitReader:
    """Read the MSB-first control stream used by the App's ``gov`` codec."""

    def __init__(self, payload: bytes) -> None:
        if len(payload) < 2:
            raise ValueError("core compressed payload is truncated")
        self.payload = payload
        self.position = 1
        self.control = payload[0]
        self.bits_remaining = 8

    def bit(self) -> int:
        value = 1 if self.control & 0x80 else 0
        self.control = (self.control << 1) & 0xFF
        self.bits_remaining -= 1
        if self.bits_remaining == 0:
            if self.position >= len(self.payload):
                raise ValueError("core compressed control stream is truncated")
            self.control = self.payload[self.position]
            self.position += 1
            self.bits_remaining = 8
        return value

    def byte(self) -> int:
        if self.position >= len(self.payload):
            raise ValueError("core compressed literal stream is truncated")
        value = self.payload[self.position]
        self.position += 1
        return value


def decode_core_gov(payload: bytes, output_length: int) -> bytes:
    """Decode the verified App ``gov.a`` column compressor."""

    if output_length <= 0 or output_length > 16 * 1024 * 1024:
        raise DirectRequestError("DIRECT_PROTOCOL_RESPONSE_INVALID")
    try:
        reader = _CoreGovBitReader(bytes(payload))
        output = bytearray([reader.byte()])

        max_output_length = output_length + 1

        def append_last(count: int, last: int) -> None:
            if count < 0 or len(output) + count > max_output_length:
                raise ValueError("core compressed output length is invalid")
            output.extend([last] * count)

        last = output[0]
        while len(output) < output_length:
            if reader.bit() == 0:
                if len(output) + 2 > max_output_length:
                    raise ValueError("core compressed output length is invalid")
                output.extend((reader.byte(), reader.byte()))
                last = output[-1]
                continue
            if reader.bit() == 0:
                last = reader.byte()
                output.append(last)
            append_last(1, last)
            if reader.bit() == 0:
                continue
            append_last(1, last)
            if reader.bit() == 0:
                if reader.bit() != 0:
                    append_last(1, last)
                continue
            append_last(2, last)
            if reader.bit() == 0:
                continue
            append_last(1, last)
            if reader.bit() == 0:
                if reader.bit() == 0:
                    if reader.bit() != 0:
                        append_last(1, last)
                elif reader.bit() == 0:
                    append_last(2, last)
                else:
                    append_last(3, last)
                continue
            append_last(4, last)
            if reader.bit() == 0:
                if reader.bit() != 0:
                    append_last(1, last)
                continue
            if reader.bit() == 0:
                append_last(2, last)
                continue
            append_last(3, last)
            while True:
                run_length = reader.byte()
                if run_length > 127:
                    run_length = ((run_length - 128) << 8) | reader.byte()
                append_last(run_length, last)
                if run_length != 32767:
                    break
        if len(output) not in {output_length, max_output_length}:
            raise ValueError("core compressed output length is invalid")
        return bytes(output[:output_length])
    except DirectRequestError:
        raise
    except (IndexError, ValueError, OverflowError):
        raise DirectRequestError("DIRECT_PROTOCOL_RESPONSE_INVALID") from None


def decode_core_snappy(payload: bytes) -> bytes:
    """Decode the raw Snappy block format used by mini-body type ``0x1000``."""

    try:
        data = bytes(payload)
        position = 0
        output_length = 0
        shift = 0
        while True:
            if position >= len(data) or shift > 28:
                raise ValueError("core Snappy length is truncated")
            value = data[position]
            position += 1
            output_length |= (value & 0x7F) << shift
            if value & 0x80 == 0:
                break
            shift += 7
        if output_length < 0 or output_length > 16 * 1024 * 1024:
            raise ValueError("core Snappy output length is invalid")

        output = bytearray()
        while position < len(data):
            tag = data[position]
            position += 1
            tag_type = tag & 0x03
            if tag_type == 0:
                length_code = tag >> 2
                if length_code < 60:
                    length = length_code + 1
                else:
                    length_bytes = length_code - 59
                    if length_bytes > 4 or position + length_bytes > len(data):
                        raise ValueError("core Snappy literal length is invalid")
                    length = (
                        int.from_bytes(
                            data[position : position + length_bytes],
                            "little",
                        )
                        + 1
                    )
                    position += length_bytes
                if (
                    position + length > len(data)
                    or len(output) + length > output_length
                ):
                    raise ValueError("core Snappy literal is truncated")
                output.extend(data[position : position + length])
                position += length
                continue

            if tag_type == 1:
                if position >= len(data):
                    raise ValueError("core Snappy copy offset is truncated")
                length = 4 + ((tag >> 2) & 0x07)
                offset = ((tag & 0xE0) << 3) | data[position]
                position += 1
            elif tag_type == 2:
                if position + 2 > len(data):
                    raise ValueError("core Snappy copy offset is truncated")
                length = 1 + (tag >> 2)
                offset = int.from_bytes(data[position : position + 2], "little")
                position += 2
            else:
                if position + 4 > len(data):
                    raise ValueError("core Snappy copy offset is truncated")
                length = 1 + (tag >> 2)
                offset = int.from_bytes(data[position : position + 4], "little")
                position += 4
            if (
                offset <= 0
                or offset > len(output)
                or len(output) + length > output_length
            ):
                raise ValueError("core Snappy copy is invalid")
            for _ in range(length):
                output.append(output[-offset])

        if len(output) != output_length:
            raise ValueError("core Snappy output length does not match")
        return bytes(output)
    except (IndexError, ValueError, OverflowError):
        raise DirectRequestError("DIRECT_PROTOCOL_RESPONSE_INVALID") from None


def _core_hxl_value(raw: int) -> Decimal | None:
    unsigned = raw & 0xFFFFFFFF
    if unsigned in {0x80000000, 0xFFFFFFFF}:
        return None
    if unsigned == 0:
        return Decimal(0)
    magnitude = Decimal(unsigned & 0x07FFFFFF)
    exponent = (unsigned >> 28) & 0x07
    scale = Decimal(10) ** exponent
    value = magnitude / scale if unsigned & 0x80000000 else magnitude * scale
    return -value if unsigned & 0x08000000 else value


def _core_format_number(
    value: Decimal | int | float | None,
    places: int,
    *,
    suffix: str = "",
    show_plus: bool = False,
) -> str | None:
    if value is None:
        return None
    try:
        decimal = value if isinstance(value, Decimal) else Decimal(str(value))
        rounded = decimal.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        return None
    if rounded == 0:
        rounded = abs(rounded)
    prefix = "+" if show_plus and rounded > 0 else ""
    return f"{prefix}{rounded:.{places}f}{suffix}"


def _core_time(value: object) -> str | None:
    text = str(value).strip() if value is not None else ""
    compact = text.replace(":", "")
    if compact.isdigit() and 3 <= len(compact) <= 4:
        padded = compact.zfill(4)
        hour, minute = int(padded[:2]), int(padded[2:])
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return f"{hour:02d}:{minute:02d}"
    try:
        packed = int(text)
    except ValueError:
        packed = 0
    if packed > 0xFFFF:
        year = ((packed >> 20) & 0xFFF) + 1900
        month = (packed >> 16) & 0x0F
        day = (packed >> 11) & 0x1F
        hour = (packed >> 6) & 0x1F
        minute = packed & 0x3F
        in_session = (
            9 * 60 + 30 <= hour * 60 + minute <= 11 * 60 + 30
            or 13 * 60 <= hour * 60 + minute <= 15 * 60
        )
        if 2000 <= year <= 2199 and in_session:
            try:
                datetime(year, month, day)
            except ValueError:
                pass
            else:
                return f"{hour:02d}:{minute:02d}"
    return text or None


@dataclass(frozen=True)
class _CoreCurve:
    symbol: str
    name: str
    ext: dict[int, object]
    data: dict[int, tuple[Decimal | int | None, ...]]


class Core9528CurveDecoder:
    """Decode verified ``cv3`` response frames into direct result values."""

    _RETAIL_DATA_IDS = (13, 18, 216, 218, 220, 222, 215, 217, 219, 221)

    def __init__(
        self,
        macdfs_params: tuple[int, int, int] = (12, 26, 9),
    ) -> None:
        params = tuple(int(value) for value in macdfs_params)
        if len(params) != 3 or any(value <= 0 or value > 1000 for value in params):
            raise DirectRequestError("DIRECT_PROTOCOL_RESPONSE_INVALID")
        self.macdfs_params = params

    def with_macdfs_params(
        self, macdfs_params: tuple[int, int, int]
    ) -> "Core9528CurveDecoder":
        return type(self)(macdfs_params=macdfs_params)

    def __call__(
        self, frames: list[bytes], symbol: str, market: str
    ) -> DirectReadOutcome:
        curves = [
            self._decode_frame(frame, symbol, market)
            for frame in frames
        ]
        curves = [curve for curve in curves if curve is not None]
        if not curves:
            raise DirectRequestError("DIRECT_PROTOCOL_RESPONSE_INVALID")

        values = empty_metric_values()
        intraday: dict[MetricKind, dict[str, Any]] = {}
        quote = next(
            (
                curve
                for curve in curves
                if {1, 10, 13, 19, 34312}.issubset(curve.data)
            ),
            None,
        )
        if quote is not None:
            values[MetricKind.STOCK_NAME] = quote.name or None
            price = self._ext_decimal(quote.ext.get(10))
            if price is None:
                price = self._last_decimal(quote.data.get(10))
            values[MetricKind.CURRENT_PRICE] = _core_format_number(price, 2)
            previous_close = self._ext_decimal(quote.ext.get(6))
            change = self._ext_decimal(quote.ext.get(34315))
            if change is None and price is not None and previous_close not in (None, Decimal(0)):
                change = (price / previous_close - Decimal(1)) * Decimal(100)
            values[MetricKind.CHANGE_PERCENT] = _core_format_number(
                change, 2, suffix="%"
            )
            values[MetricKind.TURNOVER_RATE] = _core_format_number(
                self._ext_decimal(quote.ext.get(34312)), 2, suffix="%"
            )
            macdfs = self._calculate_macdfs(quote, self.macdfs_params)
            values[MetricKind.MACDFS] = _core_format_number(
                self._last_decimal(macdfs),
                3,
                show_plus=True,
            )
            if macdfs is not None:
                macdfs_points = []
                for raw_time, value in zip(
                    quote.data.get(1, ()),
                    macdfs,
                    strict=True,
                ):
                    time_value = _core_time(raw_time)
                    if time_value is not None:
                        macdfs_points.append(
                            {
                                "time": time_value,
                                "value": _core_format_number(
                                    value,
                                    3,
                                    show_plus=True,
                                ),
                            }
                        )
                if macdfs_points:
                    intraday[MetricKind.MACDFS] = {
                        "unit": None,
                        "points": macdfs_points,
                    }

        if market != "151":
            for curve in curves:
                if 33007 in curve.data:
                    series = self._series(curve, 33007, places=2)
                    values[MetricKind.LARGE_ORDER_NET] = series["latest"]
                    intraday[MetricKind.LARGE_ORDER_NET] = series["intraday"]
                if 33015 in curve.data:
                    series = self._series(
                        curve,
                        33015,
                        places=1,
                        divisor=Decimal(10000),
                        unit="万",
                    )
                    values[MetricKind.LARGE_ORDER_AMOUNT] = series["latest"]
                    intraday[MetricKind.LARGE_ORDER_AMOUNT] = series["intraday"]
                if set(self._RETAIL_DATA_IDS).issubset(curve.data):
                    retail_values = self._calculate_retail(curve)
                    if retail_values is None:
                        continue
                    times = curve.data.get(1, ())
                    points = []
                    for raw_time, value in zip(times, retail_values, strict=True):
                        time_value = _core_time(raw_time)
                        if time_value is not None:
                            points.append(
                                {
                                    "time": time_value,
                                    "value": _core_format_number(value, 2),
                                }
                            )
                    if points:
                        values[MetricKind.RETAIL_COUNT] = points[-1]["value"]
                        intraday[MetricKind.RETAIL_COUNT] = {
                            "unit": None,
                            "points": points,
                        }

        return DirectReadOutcome(
            values=values,
            source_errors={"core_metrics": None, "main_fund_flow": None},
            intraday_series=intraday,
        )

    @classmethod
    def _decode_frame(
        cls,
        frame: bytes,
        symbol: str,
        market: str,
    ) -> _CoreCurve | None:
        try:
            _validate_9528_outer_packet(frame)
            if len(frame) < 13 + struct.calcsize("<HiiHiiiI"):
                raise ValueError("core response mini header is truncated")
            (
                head_length,
                _head_id,
                head_type,
                _page_id,
                data_length,
                _mini_frame_id,
                text_length,
                _session,
            ) = struct.unpack_from("<HiiHiiiI", frame, 13)
            if head_length < 24 or 13 + head_length > len(frame):
                raise ValueError("core response mini header is invalid")
            body = frame[13 + head_length :]
            if data_length != len(body) or text_length < 0:
                raise ValueError("core response mini header length is invalid")
            if head_type & 0xF0000000:
                raise ValueError("core encrypted mini body is unsupported")
            compression_type = head_type & 0xF000
            if compression_type == 0x1000:
                body = decode_core_snappy(body)
            elif compression_type == 0x3000:
                raise ValueError("core Zstd mini body is unsupported")
            if body[:3].lower() != b"cv3":
                if body[4:7].lower() == b"cv3":
                    body = body[4:]
                else:
                    return None
            if head_length < 32:
                raise ValueError("core curve mini header is invalid")
            if len(body) < 24:
                raise ValueError("core curve header is truncated")
            (
                name_raw,
                point_count,
                _curve_flags,
                first_index,
                extension_end,
                row_width,
                field_count,
            ) = struct.unpack_from("<6s i I i H H H", body, 0)
            if name_raw[:3].lower() != b"cv3" or point_count <= 0 or point_count > 1441:
                raise ValueError("core curve header is invalid")
            if (
                first_index < 0
                or row_width <= 0
                or row_width > 4096
                or field_count <= 0
                or field_count > 256
            ):
                raise ValueError("core curve dimensions are invalid")
            header_length = 24 + field_count * 4
            if header_length > extension_end or extension_end > len(body):
                raise ValueError("core curve extension bounds are invalid")

            descriptors: list[tuple[int, int, int]] = []
            expected_width = 0
            seen_ids: set[int] = set()
            for index in range(field_count):
                type_word, width, _aux = struct.unpack_from(
                    "<HBB", body, 24 + index * 4
                )
                data_id = type_word & 0x8FFF
                type_bits = type_word & 0x7000
                if data_id in seen_ids:
                    raise ValueError("core curve field ids are duplicated")
                expected_field_width = 2 if type_bits == 0x3000 else 4
                if (
                    type_bits not in {0x1000, 0x2000, 0x3000}
                    or width != expected_field_width
                ):
                    raise ValueError("core curve field type is unsupported")
                seen_ids.add(data_id)
                descriptors.append((data_id, type_bits, width))
                expected_width += width
            if expected_width != row_width:
                raise ValueError("core curve row width is invalid")

            ext = cls._parse_extensions(body[header_length:extension_end])
            raw_symbol = ext.get(4)
            raw_name = ext.get(55)
            if not isinstance(raw_symbol, str) or raw_symbol != symbol:
                raise ValueError("core curve response identity does not match")
            if not isinstance(raw_name, str) or not raw_name.strip():
                raise ValueError("core curve response name is missing")
            if market_code_for_symbol(raw_symbol) != market:
                raise ValueError("core curve response market does not match")

            remaining = body[extension_end:]
            expected_bytes = point_count * row_width

            def decode_data(data_segment: bytes) -> bytearray:
                if len(data_segment) >= 8:
                    compressed_length = int.from_bytes(data_segment[:4], "little")
                    declared_output = int.from_bytes(data_segment[4:8], "big")
                    compressed = data_segment[8:]
                    if (
                        declared_output != expected_bytes
                        or compressed_length
                        not in {len(compressed), len(compressed) + 4}
                    ):
                        raise ValueError("core curve compressed length is invalid")
                    column_bytes = decode_core_gov(compressed, expected_bytes)
                    decoded_rows = bytearray(expected_bytes)
                    for column in range(row_width):
                        for row in range(point_count):
                            decoded_rows[row * row_width + column] = column_bytes[
                                column * point_count + row
                            ]
                    return decoded_rows
                if len(data_segment) == expected_bytes:
                    return bytearray(data_segment)
                raise ValueError("core curve data is truncated")

            row_bytes = decode_data(remaining)

            data: dict[int, tuple[Decimal | int | None, ...]] = {}
            field_offset = 0
            for data_id, type_bits, width in descriptors:
                column_values: list[Decimal | int | None] = []
                for row in range(point_count):
                    offset = row * row_width + field_offset
                    raw = row_bytes[offset : offset + width]
                    if len(raw) != width:
                        raise ValueError("core curve row is truncated")
                    if type_bits == 0x1000:
                        column_values.append(
                            _core_hxl_value(int.from_bytes(raw, "little"))
                        )
                    elif type_bits == 0x3000:
                        column_values.append(
                            int.from_bytes(raw, "little", signed=True)
                        )
                    else:
                        column_values.append(
                            int.from_bytes(raw, "little", signed=type_bits == 0x2000)
                        )
                data[data_id] = tuple(column_values)
                field_offset += width
            return _CoreCurve(symbol=raw_symbol, name=raw_name, ext=ext, data=data)
        except DirectRequestError:
            raise
        except (KeyError, IndexError, TypeError, ValueError, struct.error, UnicodeDecodeError):
            raise DirectRequestError("DIRECT_PROTOCOL_RESPONSE_INVALID") from None

    @staticmethod
    def _parse_extensions(payload: bytes) -> dict[int, object]:
        if len(payload) < 2:
            raise ValueError("core curve extension header is truncated")
        position = 0
        count = int.from_bytes(payload[:2], "little", signed=True)
        position += 2
        if count < 0 or count > 1024:
            raise ValueError("core curve extension count is invalid")
        result: dict[int, object] = {}
        for _ in range(count):
            if position + 2 > len(payload):
                raise ValueError("core curve extension type is truncated")
            type_word = int.from_bytes(payload[position : position + 2], "little")
            position += 2
            data_id = type_word & 0x8FFF
            type_bits = type_word & 0x7000
            if type_bits == 0:
                if position + 2 > len(payload):
                    raise ValueError("core curve extension string length is truncated")
                length = int.from_bytes(payload[position : position + 2], "little")
                position += 2
                size = length * 2
                if position + size > len(payload):
                    raise ValueError("core curve extension string is truncated")
                result[data_id] = payload[position : position + size].decode("utf-16-le")
                position += size
            elif type_bits == 0x1000:
                if position + 4 > len(payload):
                    raise ValueError("core curve extension HXLONG is truncated")
                result[data_id] = _core_hxl_value(
                    int.from_bytes(payload[position : position + 4], "little")
                )
                position += 4
            elif type_bits == 0x2000:
                if position + 4 > len(payload):
                    raise ValueError("core curve extension int is truncated")
                result[data_id] = int.from_bytes(
                    payload[position : position + 4], "little", signed=True
                )
                position += 4
            elif type_bits == 0x3000:
                if position + 2 > len(payload):
                    raise ValueError("core curve extension short is truncated")
                result[data_id] = int.from_bytes(
                    payload[position : position + 2], "little", signed=True
                )
                position += 2
            elif type_bits == 0x5000:
                if position + 8 > len(payload):
                    raise ValueError("core curve extension double is truncated")
                position += 8
            elif type_bits == 0x6000:
                if position + 2 > len(payload):
                    raise ValueError("core curve extension array length is truncated")
                length = int.from_bytes(
                    payload[position : position + 2], "little", signed=True
                )
                position += 2
                if length < 0 or length > 100 or position + 4 * length > len(payload):
                    raise ValueError("core curve extension array is invalid")
                result[data_id] = tuple(
                    int.from_bytes(
                        payload[position + 4 * index : position + 4 * index + 4],
                        "little",
                        signed=True,
                    )
                    for index in range(length)
                )
                position += 4 * length
            elif type_bits == 0x7000:
                if position + 2 > len(payload):
                    raise ValueError("core curve extension struct length is truncated")
                length = int.from_bytes(
                    payload[position : position + 2], "little", signed=True
                )
                if length < 6 or position + length > len(payload):
                    raise ValueError("core curve extension struct is invalid")
                result[data_id] = None
                position += length
            else:
                raise ValueError("core curve extension type is unsupported")
        return result

    @staticmethod
    def _ext_decimal(value: object) -> Decimal | None:
        return value if isinstance(value, Decimal) else _decimal(value)

    @staticmethod
    def _last_decimal(
        values: tuple[Decimal | int | None, ...] | None,
    ) -> Decimal | None:
        if not values:
            return None
        for value in reversed(values):
            if value is not None:
                return value if isinstance(value, Decimal) else Decimal(value)
        return None

    @classmethod
    def _series(
        cls,
        curve: _CoreCurve,
        data_id: int,
        *,
        places: int,
        divisor: Decimal = Decimal(1),
        unit: str | None = None,
    ) -> dict[str, object]:
        times = curve.data.get(1, ())
        values = curve.data.get(data_id, ())
        points: list[dict[str, str | None]] = []
        for raw_time, raw_value in zip(times, values, strict=True):
            time_value = _core_time(raw_time)
            if time_value is None:
                continue
            number = raw_value if isinstance(raw_value, Decimal) else _decimal(raw_value)
            points.append(
                {
                    "time": time_value,
                    "value": _core_format_number(
                        number / divisor if number is not None else None,
                        places,
                    ),
                }
            )
        latest_number = cls._last_decimal(values)
        return {
            "latest": _core_format_number(
                latest_number / divisor if latest_number is not None else None,
                places,
                suffix=unit or "",
            ),
            "intraday": {"unit": unit, "points": points},
        }

    @classmethod
    def _calculate_retail(
        cls, curve: _CoreCurve
    ) -> tuple[Decimal | None, ...] | None:
        columns = [curve.data.get(data_id) for data_id in cls._RETAIL_DATA_IDS]
        if any(column is None or not column for column in columns):
            return None
        assert all(column is not None for column in columns)
        length = len(columns[0])
        if any(len(column) != length for column in columns):
            return None
        divisor = cls._ext_decimal(curve.ext.get(407))
        if divisor in (None, Decimal(0)):
            return None
        result: list[Decimal | None] = []
        for index in range(length):
            data = [column[index] for column in columns]
            if any(value is None for value in data):
                result.append(None)
                continue
            volume = data[0] if isinstance(data[0], Decimal) else Decimal(data[0])
            amount = data[1] if isinstance(data[1], Decimal) else Decimal(data[1])
            if volume == 0 or amount == 0:
                result.append(None)
                continue
            positive = sum(
                (
                    value if isinstance(value, Decimal) else Decimal(value)
                    for value in data[2:6]
                ),
                Decimal(0),
            )
            negative = sum(
                (
                    value if isinstance(value, Decimal) else Decimal(value)
                    for value in data[6:10]
                ),
                Decimal(0),
            )
            result.append(
                ((positive - negative) * Decimal(1000000))
                / (divisor / (volume / amount))
            )
        return tuple(result)

    @classmethod
    def _calculate_macdfs(
        cls,
        curve: _CoreCurve,
        params: tuple[int, int, int],
    ) -> tuple[Decimal | None, ...] | None:
        prices = curve.data.get(10)
        if not prices:
            return None
        fallback = cls._ext_decimal(curve.ext.get(6)) or Decimal(0)
        previous_price = fallback
        ema_short = fallback
        ema_long = fallback
        dea = Decimal(0)
        result: list[Decimal | None] = []

        def ema(value: Decimal, period: int, previous: Decimal) -> Decimal:
            return (
                value * Decimal(2) + Decimal(period - 1) * previous
            ) / Decimal(period + 1)

        for index, raw_price in enumerate(prices):
            price = (
                raw_price
                if isinstance(raw_price, Decimal)
                else _decimal(raw_price)
            )
            if price is None:
                price = previous_price if index > 0 else fallback
            if index == 0:
                ema_short = ema(price, params[0], price)
                ema_long = ema(price, params[1], price)
                diff = ema_short - ema_long
                dea = ema(diff, params[2], diff)
            else:
                ema_short = ema(price, params[0], ema_short)
                ema_long = ema(price, params[1], ema_long)
                diff = ema_short - ema_long
                dea = ema(diff, params[2], dea)
            result.append((diff - dea) * Decimal(2))
            previous_price = price
        return tuple(result)


def validate_core_auth_packet(packet: bytes) -> None:
    _validate_9528_outer_packet(packet)
    if len(packet) == 13:
        raise ValueError("core authentication packet is empty")


@dataclass(frozen=True)
class HttpResponse:
    status_code: int
    headers: Mapping[str, str]
    body: bytes


HttpRequester = Callable[[str, dict[str, str], bytes, float], HttpResponse]


def _urllib_request(
    url: str,
    headers: dict[str, str],
    body: bytes,
    timeout_seconds: float,
) -> HttpResponse:
    request = Request(url, data=body, headers=headers, method="POST")
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            payload = response.read()
            response_headers = dict(response.headers.items())
            status = response.status
    except HTTPError as error:
        payload = error.read()
        response_headers = dict(error.headers.items()) if error.headers else {}
        status = error.code
    except (OSError, URLError):
        raise DirectRequestError("DIRECT_FUND_FLOW_REQUEST_FAILED") from None
    if response_headers.get("Content-Encoding", "").lower() == "gzip":
        try:
            payload = gzip.decompress(payload)
        except OSError:
            raise DirectRequestError(
                "DIRECT_FUND_FLOW_RESPONSE_INVALID"
            ) from None
    return HttpResponse(status_code=status, headers=response_headers, body=payload)


def _decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return result if result.is_finite() else None


def _formatted(value: Decimal | None) -> str | None:
    if value is None:
        return None
    rounded = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if rounded == 0:
        rounded = abs(rounded)
    return f"{rounded:.2f}"


class FundFlowHttpClient:
    """Read the three fund-flow periods without invoking the App request manager."""

    def __init__(
        self,
        session_provider: SessionProvider,
        *,
        requester: HttpRequester = _urllib_request,
        timeout_seconds: float = 10.0,
        minimum_interval_seconds: float = 15.0,
        max_cache_entries: int = 512,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if minimum_interval_seconds < 0:
            raise ValueError("minimum_interval_seconds must not be negative")
        if not isinstance(max_cache_entries, int) or max_cache_entries <= 0:
            raise ValueError("max_cache_entries must be positive")
        self.session_provider = session_provider
        self.requester = requester
        self.timeout_seconds = timeout_seconds
        self.minimum_interval_seconds = minimum_interval_seconds
        self.max_cache_entries = max_cache_entries
        self.clock = clock
        self._cache: dict[str, tuple[DirectReadOutcome, float]] = {}
        self._lock = RLock()

    @staticmethod
    def _request_body(symbol: str, market: str, window: int) -> bytes:
        indexes = [
            {
                "index_id": index_id,
                "time_type": "DAY_1",
                "timestamp": "0",
                "attribute": {"win_size": str(window)},
                "req_uniq_id": f"id_{index}",
            }
            for index, index_id in enumerate(FUND_FLOW_INDEXES)
        ]
        return json.dumps(
            {
                "indexes": indexes,
                "code_selectors": {
                    "include": [
                        {
                            "type": "stock_code",
                            "values": [f"{market}:{symbol}"],
                        }
                    ]
                },
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")

    @staticmethod
    def _response_values(
        response: HttpResponse,
        symbol: str,
        market: str,
    ) -> dict[str, Decimal | None]:
        try:
            payload = json.loads(response.body.decode("utf-8"))
            if not isinstance(payload, dict) or int(payload.get("status_code")) != 0:
                raise ValueError("fund flow service returned a failure status")
            data = payload["data"]
            indexes = data["indexes"]
            rows = data["data"]
            if not isinstance(indexes, list) or not isinstance(rows, list):
                raise ValueError("fund flow response collections are invalid")
            matching = [
                row
                for row in rows
                if isinstance(row, dict) and row.get("code") == f"{market}:{symbol}"
            ]
            if len(matching) != 1:
                raise ValueError("fund flow response identity does not match")
            raw_values = matching[0].get("values")
            if not isinstance(raw_values, list):
                raise ValueError("fund flow response values are invalid")
            index_names = {
                position: item.get("index_id")
                for position, item in enumerate(indexes)
                if isinstance(item, dict) and item.get("index_id") in FUND_FLOW_INDEXES
            }
            result = {name: None for name in FUND_FLOW_INDEXES}
            for item in raw_values:
                if not isinstance(item, dict):
                    continue
                try:
                    position = int(item.get("idx"))
                except (TypeError, ValueError):
                    continue
                name = index_names.get(position)
                if name is not None:
                    result[name] = _decimal(item.get("value"))
            return result
        except (
            KeyError,
            TypeError,
            ValueError,
            UnicodeDecodeError,
            json.JSONDecodeError,
        ):
            raise DirectRequestError(
                "DIRECT_FUND_FLOW_RESPONSE_INVALID"
            ) from None

    def _read_uncached(self, symbol: str, market: str) -> DirectReadOutcome:
        session = self.session_provider.get("main_fund_flow")
        if session is None:
            raise DirectRequestError("DIRECT_SESSION_UNAVAILABLE")
        headers = {
            "Cookie": session.cookie,
            "Platform": session.platform,
            "Source-Id": FUND_FLOW_SOURCE_ID,
            "User-Agent": session.user_agent,
            "Content-Type": "application/json",
            "Accept-Encoding": "gzip",
        }
        values = empty_metric_values()
        unit_kinds = {period: unit for period, _label, unit in FUND_FLOW_PERIODS}
        for period, window in (("today", 1), ("three_day", 3), ("five_day", 5)):
            response = self.requester(
                FUND_FLOW_URL,
                headers,
                self._request_body(symbol, market, window),
                self.timeout_seconds,
            )
            if response.status_code in {401, 403}:
                self.session_provider.mark_error(
                    "main_fund_flow",
                    "DIRECT_SESSION_EXPIRED",
                )
                raise DirectRequestError("DIRECT_SESSION_EXPIRED")
            if 400 <= response.status_code < 500:
                raise DirectRequestError("DIRECT_HTTP_FORBIDDEN")
            if response.status_code < 200 or response.status_code >= 300:
                raise DirectRequestError("DIRECT_FUND_FLOW_REQUEST_FAILED")
            raw = self._response_values(response, symbol, market)
            present = [
                number.copy_abs() for number in raw.values() if number is not None
            ]
            divisor = (
                Decimal(100000000)
                if any(number >= Decimal(100000000) for number in present)
                else Decimal(10000)
            )
            values[unit_kinds[period]] = (
                "亿元" if divisor == Decimal(100000000) else "万元"
            )
            metrics = FUND_FLOW_METRICS[period]
            main = raw["charge_main_capital"]
            listed = raw["charge_main_listed_capital"]
            grey = raw["charge_main_grey_capital"]
            values[metrics["main_net_inflow"]] = _formatted(
                main / divisor if main is not None else None
            )
            values[metrics["main_visible_inflow"]] = _formatted(
                listed / divisor if listed is not None else None
            )
            values[metrics["main_hidden_inflow"]] = _formatted(
                grey / divisor if grey is not None else None
            )
            values[metrics["retail_inflow"]] = _formatted(
                -main / divisor if main is not None else None
            )
        return DirectReadOutcome(
            values=values,
            source_errors={"core_metrics": None, "main_fund_flow": None},
        )

    def read_direct(self, symbol: str) -> DirectReadOutcome:
        market = market_code_for_symbol(symbol)
        with self._lock:
            now = self.clock()
            cached = self._cache.get(symbol)
            if cached is not None and now - cached[1] < self.minimum_interval_seconds:
                return cached[0]
            outcome = self._read_uncached(symbol, market)
            self._cache[symbol] = (outcome, now)
            while len(self._cache) > self.max_cache_entries:
                self._cache.pop(next(iter(self._cache)))
            return outcome

    def read(self, symbol: str) -> dict[MetricKind, str | None]:
        return self.read_direct(symbol).values

    def read_market_snapshot(self, symbol: str, *, detail: bool) -> MarketSnapshot:
        outcome = self.read_direct(symbol)
        main_fund_flow: dict[str, object] = {}
        unit_kinds = {period: unit for period, _label, unit in FUND_FLOW_PERIODS}
        for period, _label, _unit in FUND_FLOW_PERIODS:
            metrics = FUND_FLOW_METRICS[period]
            main_fund_flow[period] = {
                "unit": outcome.values[unit_kinds[period]],
                **{name: outcome.values[kind] for name, kind in metrics.items()},
            }
        return MarketSnapshot(
            symbol=symbol,
            name=None,
            market=market_code_for_symbol(symbol),
            sequence=0,
            source_time=None,
            collected_at=datetime.now(timezone.utc),
            quote={},
            main_fund_flow=main_fund_flow,
            source_errors={"core_metrics": None, "main_fund_flow": None},
        )


class ShadowParsedValueSource:
    """Run a candidate transport for comparison while returning the primary result."""

    def __init__(self, primary: Any, candidate: Any, *, role: str) -> None:
        self.primary = primary
        self.candidate = candidate
        self.role = role

    @staticmethod
    def _values(result: object) -> Mapping[object, object]:
        values = getattr(result, "values", result)
        return values if isinstance(values, Mapping) else {}

    @staticmethod
    def _intraday(result: object) -> Mapping[object, object]:
        intraday = getattr(result, "intraday_series", {})
        return intraday if isinstance(intraday, Mapping) else {}

    @staticmethod
    def _intraday_signature(series: object) -> object:
        if not isinstance(series, Mapping):
            return series
        raw_points = series.get("points", ())
        points = [point for point in raw_points if isinstance(point, Mapping)]
        return (
            series.get("unit"),
            tuple(point.get("time") for point in points),
            points[-1].get("value") if points else None,
        )

    def _compare(self, primary: object, candidate: object) -> None:
        primary_values = self._values(primary)
        candidate_values = self._values(candidate)
        mismatches = {
                key.value if isinstance(key, MetricKind) else str(key)
                for key in set(primary_values) | set(candidate_values)
                if primary_values.get(key) != candidate_values.get(key)
        }
        primary_intraday = self._intraday(primary)
        candidate_intraday = self._intraday(candidate)
        mismatches.update(
            "intraday_series."
            + (key.value if isinstance(key, MetricKind) else str(key))
            for key in set(primary_intraday) | set(candidate_intraday)
            if self._intraday_signature(primary_intraday.get(key))
            != self._intraday_signature(candidate_intraday.get(key))
        )
        if mismatches:
            logger.warning(
                "direct transport shadow mismatch role=%s fields=%s",
                self.role,
                ",".join(sorted(mismatches)),
            )

    def read_direct(self, symbol: str):
        primary = self.primary.read_direct(symbol)
        try:
            candidate = self.candidate.read_direct(symbol)
        except DirectRequestError as error:
            logger.warning(
                "direct transport shadow failed role=%s error_code=%s",
                self.role,
                sanitized_direct_error_code(
                    error.error_code, "DIRECT_REQUEST_FAILED"
                ),
            )
        except Exception:
            logger.warning(
                "direct transport shadow failed role=%s "
                "error_code=DIRECT_REQUEST_FAILED",
                self.role,
            )
        else:
            self._compare(primary, candidate)
        return primary

    def read_market_snapshot(self, symbol: str, *, detail: bool) -> MarketSnapshot:
        primary = self.primary.read_market_snapshot(symbol, detail=detail)
        try:
            candidate = self.candidate.read_market_snapshot(symbol, detail=detail)
        except DirectRequestError as error:
            logger.warning(
                "direct transport shadow failed role=%s error_code=%s",
                self.role,
                sanitized_direct_error_code(
                    error.error_code, "DIRECT_REQUEST_FAILED"
                ),
            )
        except Exception:
            logger.warning(
                "direct transport shadow failed role=%s "
                "error_code=DIRECT_REQUEST_FAILED",
                self.role,
            )
        else:
            if primary.main_fund_flow != candidate.main_fund_flow:
                logger.warning(
                    "direct transport shadow mismatch role=%s fields=main_fund_flow",
                    self.role,
                )
        return primary

    def __getattr__(self, name: str) -> object:
        return getattr(self.primary, name)


CoreResponseDecoder = Callable[
    [list[bytes], str, str],
    DirectReadOutcome,
]


@dataclass(frozen=True)
class CoreRequestMaterial:
    host: str
    port: int
    auth_packet: bytes = field(repr=False)
    request_packets: tuple[bytes, ...] = field(repr=False)
    macdfs_params: tuple[int, int, int] | None
    timeout_seconds: float
    session_fingerprint: bytes = field(repr=False)


@dataclass
class WarmCoreConnection:
    connection: object = field(repr=False)
    session_fingerprint: bytes = field(repr=False)
    authenticated_at: float


class Core9528TemplateProtocol:
    """Send captured 9528 templates over a fresh connection.

    The final binary curve decoder is deliberately injected. This keeps the
    transport usable for protocol fixtures without treating undecoded bytes as
    market values.
    """

    def __init__(
        self,
        *,
        socket_factory: Callable[
            [tuple[str, int], float], object
        ] = socket.create_connection,
        response_decoder: CoreResponseDecoder | None = None,
        max_response_frames: int = 64,
        frame_idle_timeout_seconds: float = 0.5,
    ) -> None:
        if frame_idle_timeout_seconds <= 0:
            raise ValueError("frame_idle_timeout_seconds must be positive")
        self.socket_factory = socket_factory
        self.response_decoder = response_decoder
        self.max_response_frames = max_response_frames
        self.frame_idle_timeout_seconds = frame_idle_timeout_seconds

    def ensure_read_direct_supported(self) -> None:
        if self.response_decoder is None:
            raise DirectRequestError("DIRECT_PROTOCOL_RESPONSE_UNSUPPORTED")

    @staticmethod
    def ensure_market_snapshot_supported() -> None:
        raise DirectRequestError("DIRECT_PROTOCOL_RESPONSE_UNSUPPORTED")

    @staticmethod
    def _material_packets(
        material: object, symbol: str
    ) -> tuple[str, int, list[bytes], str]:
        core_material = getattr(material, "core_material", {})
        try:
            host = str(core_material["server_ip"])
            port = int(core_material["server_port"])
            auth = bytes.fromhex(str(core_material["auth_packet_hex"]))
            template_symbol = str(core_material["template_symbol"])
            raw_packets = json.loads(str(core_material["request_packets_hex"]))
            alphabet = str(core_material["base64_alphabet"])
            _core_base64_tables(alphabet)
            validate_core_auth_packet(auth)
            if not isinstance(raw_packets, list) or not raw_packets:
                raise ValueError("core request packet list is empty")
            packets = [
                patch_core_packet_symbol(
                    bytes.fromhex(str(packet)),
                    template_symbol,
                    symbol,
                    alphabet=alphabet,
                )
                for packet in raw_packets
            ]
        except (DirectRequestError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            raise DirectRequestError("DIRECT_PROTOCOL_HANDSHAKE_FAILED") from None
        if not auth or not packets:
            raise DirectRequestError("DIRECT_PROTOCOL_HANDSHAKE_FAILED")
        return host, port, [auth, *packets], alphabet

    @staticmethod
    def _material_macdfs_params(
        material: object,
    ) -> tuple[int, int, int] | None:
        core_material = getattr(material, "core_material", {})
        raw_value = core_material.get("macdfs_params", "")
        raw_params = "" if raw_value is None else str(raw_value).strip()
        if not raw_params:
            return None
        try:
            parsed = json.loads(raw_params)
        except json.JSONDecodeError:
            raise DirectRequestError("DIRECT_PROTOCOL_HANDSHAKE_FAILED") from None
        if (
            not isinstance(parsed, list)
            or len(parsed) != 3
            or any(
                isinstance(value, bool)
                or not isinstance(value, int)
                or value <= 0
                or value > 1000
                for value in parsed
            )
        ):
            raise DirectRequestError("DIRECT_PROTOCOL_HANDSHAKE_FAILED")
        return parsed[0], parsed[1], parsed[2]

    @staticmethod
    def _session_fingerprint(material: object) -> bytes:
        core_material = getattr(material, "core_material", {})
        updated_at = getattr(material, "updated_at", None)
        try:
            normalized = json.dumps(
                {
                    "updated_at": (
                        updated_at.isoformat()
                        if callable(getattr(updated_at, "isoformat", None))
                        else str(updated_at)
                    ),
                    "core_material": dict(core_material),
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError):
            raise DirectRequestError("DIRECT_PROTOCOL_HANDSHAKE_FAILED") from None
        return hashlib.sha256(normalized).digest()

    def prepare(self, material: object, symbol: str) -> CoreRequestMaterial:
        self.ensure_read_direct_supported()
        host, port, packets, _alphabet = self._material_packets(material, symbol)
        macdfs_params = self._material_macdfs_params(material)
        if macdfs_params is None and callable(
            getattr(self.response_decoder, "with_macdfs_params", None)
        ):
            raise DirectRequestError("DIRECT_PROTOCOL_HANDSHAKE_FAILED")
        try:
            timeout_seconds = float(getattr(material, "timeout_seconds", 10.0))
        except (TypeError, ValueError):
            raise DirectRequestError("DIRECT_PROTOCOL_HANDSHAKE_FAILED") from None
        if timeout_seconds <= 0:
            raise DirectRequestError("DIRECT_PROTOCOL_HANDSHAKE_FAILED")
        return CoreRequestMaterial(
            host=host,
            port=port,
            auth_packet=packets[0],
            request_packets=tuple(packets[1:]),
            macdfs_params=macdfs_params,
            timeout_seconds=timeout_seconds,
            session_fingerprint=self._session_fingerprint(material),
        )

    @staticmethod
    def close_connection(warm: WarmCoreConnection) -> None:
        try:
            warm.connection.close()
        except Exception:
            pass

    def authenticate(self, prepared: CoreRequestMaterial) -> WarmCoreConnection:
        connection = None
        try:
            connection = self.socket_factory(
                (prepared.host, prepared.port),
                prepared.timeout_seconds,
            )
            connection.settimeout(prepared.timeout_seconds)
            connection.sendall(prepared.auth_packet)
            frames = self._read_frames(
                connection,
                prepared.timeout_seconds,
                inter_frame_timeout_seconds=self.frame_idle_timeout_seconds,
            )
            if not frames:
                raise DirectRequestError("DIRECT_PROTOCOL_RESPONSE_TIMEOUT")
            return WarmCoreConnection(
                connection=connection,
                session_fingerprint=prepared.session_fingerprint,
                authenticated_at=time.monotonic(),
            )
        except DirectRequestError as error:
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass
            raise DirectRequestError(
                sanitized_direct_error_code(
                    error.error_code,
                    "DIRECT_PROTOCOL_HANDSHAKE_FAILED",
                )
            ) from None
        except Exception:
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass
            raise DirectRequestError("DIRECT_PROTOCOL_HANDSHAKE_FAILED") from None

    @staticmethod
    def _read_exact(
        connection: object,
        length: int,
        deadline: float,
        *,
        max_wait_seconds: float | None = None,
    ) -> bytes:
        chunks: list[bytes] = []
        received = 0
        while received < length:
            remaining_seconds = deadline - time.monotonic()
            if remaining_seconds <= 0:
                raise DirectRequestError("DIRECT_PROTOCOL_RESPONSE_TIMEOUT")
            socket_timeout_seconds = remaining_seconds
            if max_wait_seconds is not None:
                socket_timeout_seconds = min(
                    socket_timeout_seconds,
                    max_wait_seconds,
                )
            settimeout = getattr(connection, "settimeout", None)
            if callable(settimeout):
                settimeout(socket_timeout_seconds)
            try:
                chunk = connection.recv(length - received)
            except (TimeoutError, OSError):
                if received:
                    raise DirectRequestError(
                        "DIRECT_PROTOCOL_RESPONSE_INVALID"
                    ) from None
                raise DirectRequestError(
                    "DIRECT_PROTOCOL_RESPONSE_TIMEOUT"
                ) from None
            if not chunk:
                raise DirectRequestError("DIRECT_PROTOCOL_RESPONSE_INVALID")
            chunks.append(bytes(chunk))
            received += len(chunk)
        return b"".join(chunks)

    @staticmethod
    def _is_curve_frame(frame: bytes) -> bool:
        """Recognize a ``cv3`` payload without decoding its fields."""

        payload = frame[13:]
        if len(payload) < 10:
            return False
        head_length = int.from_bytes(payload[:2], "little")
        if head_length < 24 or head_length > len(payload):
            return False
        body = payload[head_length:]
        head_type = int.from_bytes(payload[6:10], "little", signed=True)
        if head_type & 0xF0000000:
            return False
        if head_type & 0xF000 == 0x1000:
            try:
                body = decode_core_snappy(body)
            except DirectRequestError:
                return False
        elif head_type & 0xF000 == 0x3000:
            return False
        return body[:3].lower() == b"cv3" or body[4:7].lower() == b"cv3"

    def _read_frames(
        self,
        connection: object,
        timeout_seconds: float,
        *,
        deadline: float | None = None,
        stop_after_curves: int | None = None,
        inter_frame_timeout_seconds: float | None = None,
    ) -> list[bytes]:
        if stop_after_curves is not None and stop_after_curves <= 0:
            raise ValueError("stop_after_curves must be positive")
        if (
            inter_frame_timeout_seconds is not None
            and inter_frame_timeout_seconds <= 0
        ):
            raise ValueError("inter_frame_timeout_seconds must be positive")
        if deadline is None:
            deadline = time.monotonic() + timeout_seconds
        frames: list[bytes] = []
        curve_frames = 0
        for _ in range(self.max_response_frames):
            try:
                header = self._read_exact(
                    connection,
                    13,
                    deadline,
                    max_wait_seconds=(
                        inter_frame_timeout_seconds if frames else None
                    ),
                )
            except DirectRequestError as error:
                if (
                    frames
                    and error.error_code == "DIRECT_PROTOCOL_RESPONSE_TIMEOUT"
                ):
                    break
                raise
            if header[:4] != b"\xfd" * 4:
                raise DirectRequestError("DIRECT_PROTOCOL_RESPONSE_INVALID")
            if (
                re.fullmatch(rb"[0-9A-Fa-f]{8}", header[4:12]) is None
                or header[12] != 0
            ):
                raise DirectRequestError("DIRECT_PROTOCOL_RESPONSE_INVALID")
            try:
                length = int(header[4:12].decode("ascii"), 16)
            except (UnicodeDecodeError, ValueError):
                raise DirectRequestError("DIRECT_PROTOCOL_RESPONSE_INVALID") from None
            if length < 0 or length > 16 * 1024 * 1024:
                raise DirectRequestError("DIRECT_PROTOCOL_RESPONSE_INVALID")
            try:
                body = self._read_exact(connection, length, deadline)
            except DirectRequestError as error:
                # A complete header commits us to reading this frame.  Even
                # an empty read after it is a truncated response, not idle.
                if error.error_code == "DIRECT_PROTOCOL_RESPONSE_TIMEOUT":
                    raise DirectRequestError(
                        "DIRECT_PROTOCOL_RESPONSE_INVALID"
                    ) from None
                raise
            frames.append(header + body)
            if stop_after_curves is not None and self._is_curve_frame(frames[-1]):
                curve_frames += 1
                if curve_frames >= stop_after_curves:
                    break
        return frames

    def read_authenticated(
        self,
        warm: WarmCoreConnection,
        prepared: CoreRequestMaterial,
        symbol: str,
        market: str,
    ) -> DirectReadOutcome:
        if warm.session_fingerprint != prepared.session_fingerprint:
            self.close_connection(warm)
            raise DirectRequestError("DIRECT_PROTOCOL_HANDSHAKE_FAILED")
        try:
            frames: list[bytes] = []
            deadline = time.monotonic() + prepared.timeout_seconds
            # Captured request templates are emitted as adjacent pairs by the
            # App (a request descriptor followed by its 6001 payload).  Keep
            # each pair together and stop after its first curve response before
            # moving on to the next indicator.  A final unpaired template is
            # still sent as a single request for compatibility with captures.
            seen_batches: set[tuple[bytes, ...]] = set()
            for batch_start in range(0, len(prepared.request_packets), 2):
                batch = tuple(
                    prepared.request_packets[batch_start : batch_start + 2]
                )
                if batch in seen_batches:
                    continue
                seen_batches.add(batch)
                for packet in batch:
                    warm.connection.sendall(packet)
                frames.extend(
                    self._read_frames(
                        warm.connection,
                        prepared.timeout_seconds,
                        deadline=deadline,
                        stop_after_curves=1,
                    )
                )
            assert self.response_decoder is not None
            response_decoder = self.response_decoder
            if prepared.macdfs_params is not None:
                bind_macdfs_params = getattr(
                    response_decoder,
                    "with_macdfs_params",
                    None,
                )
                if callable(bind_macdfs_params):
                    response_decoder = bind_macdfs_params(
                        prepared.macdfs_params
                    )
            try:
                return response_decoder(frames, symbol, market)
            except DirectRequestError as error:
                raise DirectRequestError(
                    sanitized_direct_error_code(
                        error.error_code, "DIRECT_PROTOCOL_RESPONSE_INVALID"
                    )
                ) from None
            except Exception:
                raise DirectRequestError("DIRECT_PROTOCOL_RESPONSE_INVALID") from None
        except DirectRequestError as error:
            raise DirectRequestError(
                sanitized_direct_error_code(
                    error.error_code, "DIRECT_PROTOCOL_HANDSHAKE_FAILED"
                )
            ) from None
        except Exception:
            raise DirectRequestError("DIRECT_PROTOCOL_HANDSHAKE_FAILED") from None
        finally:
            self.close_connection(warm)

    def read_direct(
        self, material: object, symbol: str, market: str
    ) -> DirectReadOutcome:
        prepared = self.prepare(material, symbol)
        warm = self.authenticate(prepared)
        return self.read_authenticated(warm, prepared, symbol, market)

    def read_market_snapshot(
        self,
        material: object,
        symbol: str,
        market: str,
        *,
        detail: bool,
    ) -> MarketSnapshot:
        self.ensure_market_snapshot_supported()
        raise AssertionError("unreachable")


class Core9528Protocol(Protocol):
    def read_direct(
        self,
        session: object,
        symbol: str,
        market: str,
    ) -> DirectReadOutcome: ...

    def read_market_snapshot(
        self,
        session: object,
        symbol: str,
        market: str,
        *,
        detail: bool,
    ) -> MarketSnapshot: ...


def _start_warm_background(task: Callable[[], None]) -> None:
    Thread(target=task, name="ths-core-warm", daemon=True).start()


class Core9528WarmPool:
    """Hold one authenticated connection that may be consumed exactly once."""

    def __init__(
        self,
        protocol: object,
        *,
        max_idle_seconds: float = 25.0,
        clock: Callable[[], float] = time.monotonic,
        start_background: Callable[[Callable[[], None]], None] = (
            _start_warm_background
        ),
    ) -> None:
        if max_idle_seconds <= 0:
            raise ValueError("max_idle_seconds must be positive")
        self.protocol = protocol
        self.max_idle_seconds = max_idle_seconds
        self.clock = clock
        self.start_background = start_background
        self._lock = Lock()
        self._ready: WarmCoreConnection | None = None
        self._session_fingerprint: bytes | None = None
        self._refilling = False
        self._closed = False
        self._generation = 0

    @property
    def ready_count(self) -> int:
        with self._lock:
            return int(self._ready is not None)

    def close_connection(self, warm: WarmCoreConnection) -> None:
        close_connection = getattr(self.protocol, "close_connection", None)
        if callable(close_connection):
            try:
                close_connection(warm)
            except Exception:
                pass
            return
        try:
            warm.connection.close()
        except Exception:
            pass

    @staticmethod
    def _template_symbol(session: object) -> str:
        core_material = getattr(session, "core_material", {})
        value = str(core_material.get("template_symbol", "")).strip()
        return value if len(value) == 6 and value.isdigit() else "600000"

    def _prepare(self, session: object, symbol: str) -> CoreRequestMaterial:
        prepare = getattr(self.protocol, "prepare", None)
        if not callable(prepare):
            raise DirectRequestError("DIRECT_PROTOCOL_HANDSHAKE_FAILED")
        return prepare(session, symbol)

    def _schedule_refill(
        self,
        prepared: CoreRequestMaterial,
        *,
        expected_generation: int | None = None,
    ) -> bool:
        stale: WarmCoreConnection | None = None
        with self._lock:
            if self._closed:
                return False
            if expected_generation is not None and (
                expected_generation != self._generation
                or self._session_fingerprint
                != prepared.session_fingerprint
            ):
                return False
            if self._session_fingerprint != prepared.session_fingerprint:
                stale = self._ready
                self._ready = None
                self._session_fingerprint = prepared.session_fingerprint
                self._generation += 1
                self._refilling = False
            if self._ready is not None or self._refilling:
                should_start = False
            else:
                self._refilling = True
                should_start = True
                generation = self._generation
        if stale is not None:
            self.close_connection(stale)
        if not should_start:
            return True
        try:
            self.start_background(
                lambda: self._refill(prepared, generation)
            )
        except Exception:
            with self._lock:
                self._refilling = False
            return False
        return True

    def _refill(
        self,
        prepared: CoreRequestMaterial,
        generation: int,
    ) -> None:
        warm: WarmCoreConnection | None = None
        try:
            authenticate = getattr(self.protocol, "authenticate", None)
            if not callable(authenticate):
                return
            warm = authenticate(prepared)
        except Exception:
            warm = None
        stale: WarmCoreConnection | None = None
        with self._lock:
            if generation == self._generation:
                self._refilling = False
                if (
                    warm is not None
                    and not self._closed
                    and self._session_fingerprint
                    == warm.session_fingerprint
                    and self._ready is None
                ):
                    self._ready = warm
                    warm = None
                elif self._ready is not None and self._closed:
                    stale = self._ready
                    self._ready = None
        if warm is not None:
            self.close_connection(warm)
        if stale is not None:
            self.close_connection(stale)

    def prewarm(self, session: object, symbol: str | None = None) -> None:
        prepared = self._prepare(
            session,
            symbol or self._template_symbol(session),
        )
        self._schedule_refill(prepared)

    def replenish(self, prepared: CoreRequestMaterial) -> None:
        self._schedule_refill(prepared)

    def acquire(
        self,
        session: object,
        symbol: str,
    ) -> tuple[CoreRequestMaterial, WarmCoreConnection]:
        prepared = self._prepare(session, symbol)
        stale: WarmCoreConnection | None = None
        with self._lock:
            if self._closed:
                raise DirectRequestError("DIRECT_PROTOCOL_HANDSHAKE_FAILED")
            if self._session_fingerprint != prepared.session_fingerprint:
                stale = self._ready
                self._ready = None
                self._session_fingerprint = prepared.session_fingerprint
                self._generation += 1
                self._refilling = False
            generation = self._generation
            warm = self._ready
            self._ready = None
            if (
                warm is not None
                and self.clock() - warm.authenticated_at
                > self.max_idle_seconds
            ):
                if stale is None:
                    stale = warm
                else:
                    self.close_connection(warm)
                warm = None
        if stale is not None:
            self.close_connection(stale)
        if warm is None:
            authenticate = getattr(self.protocol, "authenticate", None)
            if not callable(authenticate):
                raise DirectRequestError("DIRECT_PROTOCOL_HANDSHAKE_FAILED")
            warm = authenticate(prepared)
        with self._lock:
            valid = (
                not self._closed
                and generation == self._generation
                and self._session_fingerprint
                == prepared.session_fingerprint
            )
        if not valid:
            self.close_connection(warm)
            raise DirectRequestError("DIRECT_PROTOCOL_HANDSHAKE_FAILED")
        return prepared, warm

    def invalidate(self) -> None:
        with self._lock:
            stale = self._ready
            self._ready = None
            self._session_fingerprint = None
            self._generation += 1
            self._refilling = False
        if stale is not None:
            self.close_connection(stale)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            stale = self._ready
            self._ready = None
            self._session_fingerprint = None
            self._generation += 1
            self._refilling = False
        if stale is not None:
            self.close_connection(stale)


class Core9528Client:
    """Standalone core transport with an explicit protocol-research stage gate."""

    def __init__(
        self,
        session_provider: SessionProvider,
        *,
        protocol: Core9528Protocol | None = None,
        warm_pool: Core9528WarmPool | None = None,
    ) -> None:
        self.session_provider = session_provider
        self.protocol = protocol
        self._request_lock = RLock()
        supports_warm_pool = all(
            callable(getattr(protocol, name, None))
            for name in ("prepare", "authenticate", "read_authenticated")
        )
        self.warm_pool = (
            warm_pool
            if warm_pool is not None
            else Core9528WarmPool(protocol)
            if protocol is not None and supports_warm_pool
            else None
        )

    def _session(self):
        session = self.session_provider.get("core_metrics")
        if session is None:
            raise DirectRequestError("DIRECT_SESSION_UNAVAILABLE")
        if self.protocol is None:
            self.session_provider.mark_error(
                "core_metrics",
                "DIRECT_PROTOCOL_HANDSHAKE_FAILED",
            )
            raise DirectRequestError("DIRECT_PROTOCOL_HANDSHAKE_FAILED")
        return session

    def read_direct(self, symbol: str) -> DirectReadOutcome:
        market = market_code_for_symbol(symbol)
        gate = getattr(self.protocol, "ensure_read_direct_supported", None)
        if callable(gate):
            gate()
        with self._request_lock:
            session = self._session()
            assert self.protocol is not None
            if self.warm_pool is not None:
                prepared, warm = self.warm_pool.acquire(session, symbol)
                try:
                    read_authenticated = getattr(
                        self.protocol,
                        "read_authenticated",
                        None,
                    )
                    if not callable(read_authenticated):
                        self.warm_pool.close_connection(warm)
                        raise DirectRequestError(
                            "DIRECT_PROTOCOL_HANDSHAKE_FAILED"
                        )
                    return read_authenticated(
                        warm,
                        prepared,
                        symbol,
                        market,
                    )
                finally:
                    self.warm_pool.replenish(prepared)
            return self.protocol.read_direct(session, symbol, market)

    def prewarm(self, symbol: str | None = None) -> None:
        if self.warm_pool is None:
            return
        if not self._request_lock.acquire(blocking=False):
            return
        try:
            session = self._session()
            self.warm_pool.prewarm(session, symbol)
        finally:
            self._request_lock.release()

    def invalidate(self) -> None:
        with self._request_lock:
            if self.warm_pool is not None:
                self.warm_pool.invalidate()

    def close(self) -> None:
        with self._request_lock:
            if self.warm_pool is not None:
                self.warm_pool.close()

    def read(self, symbol: str) -> dict[MetricKind, str | None]:
        return self.read_direct(symbol).values

    def read_market_snapshot(self, symbol: str, *, detail: bool) -> MarketSnapshot:
        market = market_code_for_symbol(symbol)
        gate = getattr(self.protocol, "ensure_market_snapshot_supported", None)
        if callable(gate):
            gate()
        with self._request_lock:
            session = self._session()
            assert self.protocol is not None
            return self.protocol.read_market_snapshot(
                session,
                symbol,
                market,
                detail=detail,
            )
