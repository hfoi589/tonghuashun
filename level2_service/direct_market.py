"""Server-side market-data transports that reuse a human-authenticated App session."""

from __future__ import annotations

import gzip
import json
import logging
import base64
import socket
import struct
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from threading import RLock
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
    try:
        declared_length = int(packet[4:12].decode("ascii"), 16)
    except (UnicodeDecodeError, ValueError):
        raise ValueError("core packet length field is invalid") from None
    if packet[12] != 0:
        raise ValueError("core packet separator is invalid")
    if declared_length != len(packet) - 13 or declared_length > 16 * 1024 * 1024:
        raise ValueError("core packet length does not match its payload")


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
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if minimum_interval_seconds < 0:
            raise ValueError("minimum_interval_seconds must not be negative")
        self.session_provider = session_provider
        self.requester = requester
        self.timeout_seconds = timeout_seconds
        self.minimum_interval_seconds = minimum_interval_seconds
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

    def _compare(self, primary: object, candidate: object) -> None:
        primary_values = self._values(primary)
        candidate_values = self._values(candidate)
        mismatches = sorted(
            (
                key.value if isinstance(key, MetricKind) else str(key)
                for key in set(primary_values) | set(candidate_values)
                if primary_values.get(key) != candidate_values.get(key)
            )
        )
        if mismatches:
            logger.warning(
                "direct transport shadow mismatch role=%s fields=%s",
                self.role,
                ",".join(mismatches),
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
    ) -> None:
        self.socket_factory = socket_factory
        self.response_decoder = response_decoder
        self.max_response_frames = max_response_frames

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
    def _read_exact(connection: object, length: int, deadline: float) -> bytes:
        chunks: list[bytes] = []
        received = 0
        while received < length:
            if time.monotonic() >= deadline:
                raise DirectRequestError("DIRECT_PROTOCOL_RESPONSE_TIMEOUT")
            chunk = connection.recv(length - received)
            if not chunk:
                raise DirectRequestError("DIRECT_PROTOCOL_RESPONSE_INVALID")
            chunks.append(bytes(chunk))
            received += len(chunk)
        return b"".join(chunks)

    def _read_frames(self, connection: object, timeout_seconds: float) -> list[bytes]:
        deadline = time.monotonic() + timeout_seconds
        frames: list[bytes] = []
        for _ in range(self.max_response_frames):
            try:
                header = self._read_exact(connection, 13, deadline)
            except DirectRequestError:
                if frames:
                    break
                raise
            if header[:4] != b"\xfd" * 4:
                raise DirectRequestError("DIRECT_PROTOCOL_RESPONSE_INVALID")
            try:
                length = int(header[4:12].decode("ascii"), 16)
            except (UnicodeDecodeError, ValueError):
                raise DirectRequestError("DIRECT_PROTOCOL_RESPONSE_INVALID") from None
            if length < 0 or length > 16 * 1024 * 1024:
                raise DirectRequestError("DIRECT_PROTOCOL_RESPONSE_INVALID")
            frames.append(header + self._read_exact(connection, length, deadline))
        return frames

    def read_direct(
        self, material: object, symbol: str, market: str
    ) -> DirectReadOutcome:
        self.ensure_read_direct_supported()
        host, port, packets, _alphabet = self._material_packets(material, symbol)
        timeout_seconds = float(getattr(material, "timeout_seconds", 10.0))
        connection = None
        try:
            connection = self.socket_factory((host, port), timeout_seconds)
            connection.settimeout(timeout_seconds)
            for packet in packets:
                connection.sendall(packet)
            frames = self._read_frames(connection, timeout_seconds)
            if not frames:
                raise DirectRequestError("DIRECT_PROTOCOL_RESPONSE_TIMEOUT")
            assert self.response_decoder is not None
            try:
                return self.response_decoder(frames, symbol, market)
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
            if connection is not None:
                try:
                    connection.close()
                except OSError:
                    pass

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


class Core9528Client:
    """Standalone core transport with an explicit protocol-research stage gate."""

    def __init__(
        self,
        session_provider: SessionProvider,
        *,
        protocol: Core9528Protocol | None = None,
    ) -> None:
        self.session_provider = session_provider
        self.protocol = protocol

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
        session = self._session()
        assert self.protocol is not None
        return self.protocol.read_direct(session, symbol, market)

    def read(self, symbol: str) -> dict[MetricKind, str | None]:
        return self.read_direct(symbol).values

    def read_market_snapshot(self, symbol: str, *, detail: bool) -> MarketSnapshot:
        market = market_code_for_symbol(symbol)
        gate = getattr(self.protocol, "ensure_market_snapshot_supported", None)
        if callable(gate):
            gate()
        session = self._session()
        assert self.protocol is not None
        return self.protocol.read_market_snapshot(
            session,
            symbol,
            market,
            detail=detail,
        )
