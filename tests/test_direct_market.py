from __future__ import annotations

import json
import struct
import time
import traceback
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from threading import Thread
from types import SimpleNamespace
from typing import Callable

import pytest

from level2_service.app_sessions import AccountSessionBundle
from level2_service.direct_market import (
    CORE_BASE64_ALPHABET,
    Core9528Client,
    Core9528CurveDecoder,
    Core9528TemplateProtocol,
    Core9528WarmPool,
    CoreRequestMaterial,
    WarmCoreConnection,
    decode_core_base64,
    decode_core_gov,
    decode_core_snappy,
    encode_core_base64,
    FundFlowHttpClient,
    HttpResponse,
    patch_core_packet_symbol,
    ShadowParsedValueSource,
    _core_hxl_value,
)
from level2_service.models import FUND_FLOW_METRICS, FUND_FLOW_PERIODS, MetricKind
from level2_service.parsed_values import DirectReadOutcome, DirectRequestError


class StaticSessionProvider:
    def __init__(self, session: AccountSessionBundle | None) -> None:
        self.session = session
        self.errors: list[tuple[str, str]] = []

    def get(self, role: str) -> AccountSessionBundle | None:
        if self.session is not None:
            assert role == self.session.role
        return self.session

    def mark_error(self, role: str, error_code: str) -> None:
        self.errors.append((role, error_code))


def session() -> AccountSessionBundle:
    return AccountSessionBundle(
        role="main_fund_flow",
        cookie="user=private; sess_tk=private-ticket",
        user_agent="app-user-agent",
        platform="android",
        updated_at=datetime(2026, 8, 26, 8, 0, tzinfo=timezone.utc),
    )


def response_for(window: int) -> HttpResponse:
    values = {
        1: ("123456789", "100000000", "23456789"),
        3: ("19715000", "-36389000", "56104000"),
        5: ("0", "0", "0"),
    }[window]
    payload = {
        "data": {
            "indexes": [
                {"index_id": "charge_main_capital", "req_uniq_id": "id_0"},
                {"index_id": "charge_main_listed_capital", "req_uniq_id": "id_1"},
                {"index_id": "charge_main_grey_capital", "req_uniq_id": "id_2"},
            ],
            "data": [
                {
                    "code": "17:601872",
                    "values": [
                        {"idx": 0, "value": values[0]},
                        {"idx": 1, "value": values[1]},
                        {"idx": 2, "value": values[2]},
                    ],
                }
            ],
            "total": 1,
        },
        "status_code": 0,
        "status_msg": "success",
    }
    return HttpResponse(
        status_code=200,
        headers={"content-type": "application/json"},
        body=json.dumps(payload).encode(),
    )


def framed_9528(payload: bytes, *, separator: bytes = b"\x00") -> bytes:
    return b"\xfd" * 4 + f"{len(payload):08x}".encode("ascii") + separator + payload


def core_request_packet(symbol: str = "600519") -> bytes:
    body = f"[frame]\r\nid=6001\r\nstockcode={symbol}\r\n".encode("utf-16-be")
    header = struct.pack("<HiiHiiiI", 76, 1, 262144, 65283, 0, 6001, len(body), 0)
    header += b"\x00" * (76 - len(header))
    return framed_9528(header + encode_core_base64(body, CORE_BASE64_ALPHABET))


def _hxl_raw(value: float | int) -> int:
    number = float(value)
    if number == 0:
        return 0
    sign = 1 if number < 0 else 0
    magnitude = abs(number)
    for exponent in range(7):
        candidates = (
            (round(magnitude * (10**exponent)), 1),
            (round(magnitude / (10**exponent)), 0),
        )
        for scaled, divide in candidates:
            decoded = scaled / (10**exponent) if divide else scaled * (10**exponent)
            if scaled <= 0x07FFFFFF and abs(decoded - magnitude) < 1e-9:
                return scaled | (sign << 27) | (exponent << 28) | (divide << 31)
    raise AssertionError("synthetic HXLONG value cannot be represented")


def _literal_gov_stream(payload: bytes) -> bytes:
    assert payload
    stream = bytearray([0, payload[0]])
    remaining = payload[1:]
    # The vendor loop reloads its control byte after seven consumed bits,
    # before reading the literals for the eighth operation.  That makes the
    # first literal group 14 bytes, followed by 16-byte groups.
    stream.extend(remaining[:14])
    for index in range(14, len(remaining), 16):
        stream.append(0)
        chunk = remaining[index : index + 16]
        stream.extend(chunk)
        if len(chunk) == 1:
            stream.append(0)
    stream.append(0)
    return bytes(stream)


def _snappy_literal_stream(payload: bytes) -> bytes:
    assert payload
    output = bytearray()
    remaining_length = len(payload)
    while remaining_length >= 0x80:
        output.append((remaining_length & 0x7F) | 0x80)
        remaining_length >>= 7
    output.append(remaining_length)
    length_minus_one = len(payload) - 1
    if length_minus_one < 60:
        output.append(length_minus_one << 2)
    else:
        length_bytes = max(1, (length_minus_one.bit_length() + 7) // 8)
        output.append((59 + length_bytes) << 2)
        output.extend(length_minus_one.to_bytes(length_bytes, "little"))
    output.extend(payload)
    return bytes(output)


def _core_curve_frame(
    symbol: str,
    name: str,
    fields: list[tuple[int, str]],
    rows: list[list[float | int]],
    *,
    ext_values: dict[int, tuple[str, float | int]] | None = None,
) -> bytes:
    encoded_ext: list[bytes] = []
    all_ext: dict[int, tuple[str, object]] = {
        4: ("string", symbol),
        55: ("string", name),
    }
    all_ext.update(ext_values or {})
    for data_id, (kind, value) in all_ext.items():
        if kind == "string":
            text = str(value)
            encoded_ext.append(
                struct.pack("<HH", data_id, len(text))
                + text.encode("utf-16-le")
            )
        elif kind == "hxl":
            encoded_ext.append(
                struct.pack("<HI", 0x1000 | data_id, _hxl_raw(value))
            )
        else:
            raise AssertionError(f"unsupported synthetic extension kind: {kind}")
    extension = struct.pack("<H", len(encoded_ext)) + b"".join(encoded_ext)
    descriptor_bytes = bytearray()
    row_width = 0
    for data_id, kind in fields:
        type_bits = {"int": 0x2000, "hxl": 0x1000, "short": 0x3000}[kind]
        width = 2 if kind == "short" else 4
        descriptor_bytes.extend(struct.pack("<HBB", type_bits | data_id, width, 4))
        row_width += width
    header_length = 24 + len(descriptor_bytes)
    extension_end = header_length + len(extension)
    cv_header = struct.pack(
        "<6s i I i H H H",
        b"cv3.0\x00",
        len(rows),
        0xFFFFFFFF,
        0,
        extension_end,
        row_width,
        len(fields),
    ) + descriptor_bytes
    row_bytes = bytearray()
    for row in rows:
        for (_data_id, kind), value in zip(fields, row, strict=True):
            if kind == "int":
                row_bytes.extend(struct.pack("<i", int(value)))
            elif kind == "short":
                row_bytes.extend(struct.pack("<h", int(value)))
            else:
                row_bytes.extend(struct.pack("<I", _hxl_raw(value)))
    column_bytes = bytearray()
    for column in range(row_width):
        for row in range(len(rows)):
            column_bytes.append(row_bytes[row * row_width + column])
    compressed = _literal_gov_stream(bytes(column_bytes))
    body = (
        cv_header
        + extension
        + struct.pack("<I", len(compressed))
        + struct.pack(">I", len(column_bytes))
        + compressed
    )
    mini = struct.pack(
        "<HiiHiiiI",
        32,
        2200,
        0x100,
        0,
        len(body),
        1,
        0,
        0,
    ) + b"\x00" * 4
    payload = mini + body
    return b"\xfd" * 4 + f"{len(payload):08x}".encode() + b"\x00" + payload


def _snappy_compress_core_frame(frame: bytes) -> bytes:
    payload = bytearray(frame[13:])
    mini_length = int.from_bytes(payload[:2], "little")
    body = bytes(payload[mini_length:])
    compressed = _snappy_literal_stream(body)
    type_word = int.from_bytes(payload[6:10], "little", signed=True)
    payload[6:10] = (type_word | 0x1000).to_bytes(4, "little", signed=True)
    payload[12:16] = len(compressed).to_bytes(4, "little", signed=True)
    compressed_payload = bytes(payload[:mini_length]) + compressed
    return (
        b"\xfd" * 4
        + f"{len(compressed_payload):08x}".encode("ascii")
        + b"\x00"
        + compressed_payload
    )


def test_fund_client_builds_three_exact_requests_and_formats_values() -> None:
    requests: list[tuple[str, dict[str, str], dict[str, object], float]] = []

    def request(
        url: str, headers: dict[str, str], body: bytes, timeout: float
    ) -> HttpResponse:
        payload = json.loads(body)
        requests.append((url, headers, payload, timeout))
        window = int(payload["indexes"][0]["attribute"]["win_size"])
        return response_for(window)

    client = FundFlowHttpClient(
        StaticSessionProvider(session()),
        requester=request,
        timeout_seconds=9,
    )

    outcome = client.read_direct("601872")

    assert len(requests) == 3
    for window, (url, headers, payload, timeout) in zip(
        (1, 3, 5), requests, strict=True
    ):
        assert (
            url
            == "https://dataq.10jqka.com.cn/fetch-data-server/fetch/v1/specific_data"
        )
        assert headers == {
            "Cookie": "user=private; sess_tk=private-ticket",
            "Platform": "android",
            "Source-Id": "sif-charge-indicator-capital",
            "User-Agent": "app-user-agent",
            "Content-Type": "application/json",
            "Accept-Encoding": "gzip",
        }
        assert timeout == 9
        assert [item["index_id"] for item in payload["indexes"]] == [
            "charge_main_capital",
            "charge_main_listed_capital",
            "charge_main_grey_capital",
        ]
        assert {item["attribute"]["win_size"] for item in payload["indexes"]} == {
            str(window)
        }
        assert payload["code_selectors"] == {
            "include": [{"type": "stock_code", "values": ["17:601872"]}]
        }

    today = FUND_FLOW_METRICS["today"]
    three_day = FUND_FLOW_METRICS["three_day"]
    five_day = FUND_FLOW_METRICS["five_day"]
    units = {period: unit for period, _label, unit in FUND_FLOW_PERIODS}
    assert outcome.values[units["today"]] == "亿元"
    assert outcome.values[today["main_net_inflow"]] == "1.23"
    assert outcome.values[today["main_visible_inflow"]] == "1.00"
    assert outcome.values[today["main_hidden_inflow"]] == "0.23"
    assert outcome.values[today["retail_inflow"]] == "-1.23"
    assert outcome.values[units["three_day"]] == "万元"
    assert outcome.values[three_day["main_net_inflow"]] == "1971.50"
    assert outcome.values[three_day["main_visible_inflow"]] == "-3638.90"
    assert outcome.values[three_day["main_hidden_inflow"]] == "5610.40"
    assert outcome.values[three_day["retail_inflow"]] == "-1971.50"
    assert outcome.values[units["five_day"]] == "万元"
    assert outcome.values[five_day["main_net_inflow"]] == "0.00"
    assert all(
        outcome.values[kind] is None
        for kind in (
            MetricKind.STOCK_NAME,
            MetricKind.CURRENT_PRICE,
            MetricKind.CHANGE_PERCENT,
            MetricKind.TURNOVER_RATE,
            MetricKind.LARGE_ORDER_NET,
            MetricKind.LARGE_ORDER_AMOUNT,
            MetricKind.RETAIL_COUNT,
            MetricKind.MACDFS,
        )
    )


@pytest.mark.parametrize("status_code", [401, 403])
def test_fund_client_marks_http_auth_failures_as_expired(status_code: int) -> None:
    provider = StaticSessionProvider(session())
    client = FundFlowHttpClient(
        provider,
        requester=lambda *_args: HttpResponse(status_code, {}, b"forbidden"),
    )

    with pytest.raises(DirectRequestError) as captured:
        client.read_direct("601872")

    assert captured.value.error_code == "DIRECT_SESSION_EXPIRED"
    assert provider.errors == [("main_fund_flow", "DIRECT_SESSION_EXPIRED")]


def test_fund_client_requires_a_stored_session() -> None:
    client = FundFlowHttpClient(
        StaticSessionProvider(None), requester=lambda *_args: None
    )

    with pytest.raises(DirectRequestError) as captured:
        client.read_direct("601872")

    assert captured.value.error_code == "DIRECT_SESSION_UNAVAILABLE"


def test_fund_client_rejects_a_response_for_another_symbol() -> None:
    wrong = response_for(1)
    payload = json.loads(wrong.body)
    payload["data"]["data"][0]["code"] = "17:600000"
    wrong = replace(wrong, body=json.dumps(payload).encode())
    client = FundFlowHttpClient(
        StaticSessionProvider(session()),
        requester=lambda *_args: wrong,
    )

    with pytest.raises(DirectRequestError) as captured:
        client.read_direct("601872")

    assert captured.value.error_code == "DIRECT_FUND_FLOW_RESPONSE_INVALID"


def test_fund_client_reuses_a_symbol_result_for_fifteen_seconds() -> None:
    calls = 0
    times = iter((100.0, 110.0, 116.0))

    def request(
        _url: str, _headers: dict[str, str], body: bytes, _timeout: float
    ) -> HttpResponse:
        nonlocal calls
        calls += 1
        return response_for(
            int(json.loads(body)["indexes"][0]["attribute"]["win_size"])
        )

    client = FundFlowHttpClient(
        StaticSessionProvider(session()),
        requester=request,
        clock=lambda: next(times),
    )

    first = client.read_direct("601872")
    second = client.read_direct("601872")
    third = client.read_direct("601872")

    assert second is first
    assert third is not first
    assert calls == 6


def test_shadow_source_returns_primary_and_logs_only_mismatched_field_names(
    caplog,
) -> None:
    primary_values = {kind: None for kind in MetricKind}
    primary_values[MetricKind.MAIN_FLOW_TODAY_NET] = "1.23"
    candidate_values = dict(primary_values)
    candidate_values[MetricKind.MAIN_FLOW_TODAY_NET] = "9.99"

    class Source:
        def __init__(self, values):
            self.outcome = type("Outcome", (), {"values": values})()

        def read_direct(self, _symbol: str):
            return self.outcome

    source = ShadowParsedValueSource(
        Source(primary_values),
        Source(candidate_values),
        role="main_fund_flow",
    )

    result = source.read_direct("601872")

    assert result.values[MetricKind.MAIN_FLOW_TODAY_NET] == "1.23"
    assert "MAIN_FLOW_TODAY_NET" in caplog.text


def test_shadow_source_logs_intraday_curve_mismatches(caplog) -> None:
    values = {kind: None for kind in MetricKind}

    class Source:
        def __init__(self, point_value: str) -> None:
            self.outcome = type(
                "Outcome",
                (),
                {
                    "values": values,
                    "intraday_series": {
                        MetricKind.LARGE_ORDER_NET: {
                            "unit": None,
                            "points": [{"time": "09:30", "value": point_value}],
                        }
                    },
                },
            )()

        def read_direct(self, _symbol: str):
            return self.outcome

    source = ShadowParsedValueSource(
        Source("1.00"),
        Source("2.00"),
        role="core_metrics",
    )

    source.read_direct("601872")

    assert "intraday_series.LARGE_ORDER_NET" in caplog.text


def test_shadow_source_ignores_revised_intraday_history_when_shape_and_latest_match(
    caplog,
) -> None:
    values = {kind: None for kind in MetricKind}

    class Source:
        def __init__(self, first_value: str) -> None:
            self.outcome = type(
                "Outcome",
                (),
                {
                    "values": values,
                    "intraday_series": {
                        MetricKind.LARGE_ORDER_AMOUNT: {
                            "unit": "万",
                            "points": [
                                {"time": "09:30", "value": first_value},
                                {"time": "09:31", "value": "2.0"},
                            ],
                        }
                    },
                },
            )()

        def read_direct(self, _symbol: str):
            return self.outcome

    source = ShadowParsedValueSource(
        Source("1.0"),
        Source("9.0"),
        role="core_metrics",
    )

    source.read_direct("601872")

    assert "intraday_series.LARGE_ORDER_AMOUNT" not in caplog.text
    assert "1.23" not in caplog.text
    assert "9.99" not in caplog.text


def test_shadow_source_logs_only_role_and_validated_error_code(caplog) -> None:
    secret = "synthetic-shadow-secret-marker"

    class Primary:
        def read_direct(self, _symbol: str):
            return {MetricKind.STOCK_NAME: "招商轮船"}

    class Candidate:
        def read_direct(self, _symbol: str):
            raise DirectRequestError("secret=cookie-marker", secret)

    result = ShadowParsedValueSource(
        Primary(), Candidate(), role="core_metrics"
    ).read_direct("601872")

    assert result[MetricKind.STOCK_NAME] == "招商轮船"
    assert "role=core_metrics" in caplog.text
    assert "error_code=DIRECT_REQUEST_FAILED" in caplog.text
    assert secret not in caplog.text
    assert "cookie-marker" not in caplog.text


def test_core_client_exposes_the_protocol_stage_gate_without_app_fallback() -> None:
    provider = StaticSessionProvider(
        replace(session(), role="core_metrics", core_material={"session_id": "opaque"})
    )
    client = Core9528Client(provider)

    with pytest.raises(DirectRequestError) as captured:
        client.read_direct("601872")

    assert captured.value.error_code == "DIRECT_PROTOCOL_HANDSHAKE_FAILED"
    assert provider.errors == [("core_metrics", "DIRECT_PROTOCOL_HANDSHAKE_FAILED")]


def test_core_client_passes_the_encrypted_session_bundle_to_a_protocol_driver() -> None:
    provider = StaticSessionProvider(
        replace(
            session(),
            role="core_metrics",
            core_material={"auth_packet_hex": "deadbeef"},
        )
    )
    seen: list[tuple[str, str, str]] = []
    values = {kind: None for kind in MetricKind}
    values[MetricKind.STOCK_NAME] = "招商轮船"

    class Protocol:
        def read_direct(self, material, symbol: str, market: str):
            assert material.core_material["auth_packet_hex"] == "deadbeef"
            seen.append((symbol, market, material.role))
            return type("Outcome", (), {"values": values})()

    client = Core9528Client(provider, protocol=Protocol())

    result = client.read_direct("601872")

    assert result.values[MetricKind.STOCK_NAME] == "招商轮船"
    assert seen == [("601872", "17", "core_metrics")]


def test_core_client_reads_the_session_inside_the_lifecycle_lock() -> None:
    old_session = replace(
        session(),
        role="core_metrics",
        core_material={"marker": "old"},
    )
    new_session = replace(
        old_session,
        core_material={"marker": "new"},
    )

    class Provider:
        def __init__(self) -> None:
            self.session = old_session

        def get(self, role: str):
            assert role == "core_metrics"
            return self.session

        def mark_error(self, _role: str, _error_code: str) -> None:
            pass

    seen: list[str] = []
    values = {kind: None for kind in MetricKind}

    class Protocol:
        @staticmethod
        def read_direct(material, _symbol: str, _market: str):
            seen.append(material.core_material["marker"])
            return DirectReadOutcome(
                values=values,
                source_errors={"core_metrics": None, "main_fund_flow": None},
            )

    provider = Provider()
    client = Core9528Client(provider, protocol=Protocol())
    client._request_lock.acquire()
    thread = Thread(target=lambda: client.read_direct("601872"))
    thread.start()
    time.sleep(0.05)
    provider.session = new_session
    client.invalidate()
    client._request_lock.release()
    thread.join(timeout=1)

    assert thread.is_alive() is False
    assert seen == ["new"]


def test_core_client_prewarm_does_not_block_behind_an_active_request() -> None:
    provider = StaticSessionProvider(
        replace(session(), role="core_metrics", core_material={"marker": "one"})
    )

    class Pool:
        def __init__(self) -> None:
            self.calls = 0

        def prewarm(self, _session, _symbol=None) -> None:
            self.calls += 1

        def invalidate(self) -> None:
            pass

        def close(self) -> None:
            pass

    class Protocol:
        @staticmethod
        def read_direct(_session, _symbol, _market):
            raise AssertionError("not used")

    pool = Pool()
    client = Core9528Client(provider, protocol=Protocol(), warm_pool=pool)
    finished = Thread(target=lambda: client.prewarm("601872"))
    client._request_lock.acquire()
    finished.start()
    finished.join(timeout=0.1)
    blocked = finished.is_alive()
    client._request_lock.release()
    finished.join(timeout=1)

    assert blocked is False
    assert pool.calls == 0


def test_core_base64_round_trips_with_the_app_alphabet() -> None:
    payload = bytes(range(256))

    encoded = encode_core_base64(payload)

    assert decode_core_base64(encoded) == payload


def test_core_packet_symbol_patch_preserves_wire_length() -> None:
    wire = core_request_packet()

    patched = patch_core_packet_symbol(wire, "600519", "601872")

    assert len(patched) == len(wire)
    assert decode_core_base64(patched[13 + 76 :]).decode("utf-16-be") == (
        "[frame]\r\nid=6001\r\nstockcode=601872\r\n"
    )


def test_core_template_protocol_sends_auth_then_patched_packets_to_decoder() -> None:
    packet = core_request_packet()
    auth = framed_9528(b"auth")
    response = framed_9528(b"response")
    business = _core_curve_frame(
        "601872",
        "测试股票",
        [(1, "int"), (33007, "hxl")],
        [[930, 0.1]],
    )

    class Socket:
        def __init__(self) -> None:
            self.sent: list[bytes] = []
            self.closed = False
            self.reads: list[bytes | Exception] = [
                response[:13],
                response[13:],
                TimeoutError("synthetic auth idle"),
            ]

        def settimeout(self, _timeout: float) -> None:
            pass

        def sendall(self, value: bytes) -> None:
            self.sent.append(value)
            if len(self.sent) == 2:
                self.reads.extend([business[:13], business[13:]])

        def recv(self, size: int) -> bytes:
            value = self.reads.pop(0)
            if isinstance(value, Exception):
                raise value
            return value[:size]

        def close(self) -> None:
            self.closed = True

    socket = Socket()
    material = replace(
        session(),
        role="core_metrics",
        core_material={
            "server_ip": "127.0.0.1",
            "server_port": "9528",
            "auth_packet_hex": auth.hex(),
            "base64_alphabet": CORE_BASE64_ALPHABET,
            "template_symbol": "600519",
            "request_packets_hex": json.dumps([packet.hex()]),
        },
    )
    values = {kind: None for kind in MetricKind}
    values[MetricKind.STOCK_NAME] = "招商轮船"

    class Decoder:
        def __init__(self) -> None:
            self.frames = None

        def __call__(self, frames, _symbol: str, _market: str):
            self.frames = frames
            return type("Outcome", (), {"values": values})()

    decoder = Decoder()
    protocol = Core9528TemplateProtocol(
        socket_factory=lambda _address, _timeout: socket,
        response_decoder=decoder,
    )

    outcome = protocol.read_direct(material, "601872", "17")

    assert outcome.values[MetricKind.STOCK_NAME] == "招商轮船"
    assert socket.sent[0] == auth
    assert socket.sent[1] != packet
    assert (
        decode_core_base64(socket.sent[1][13 + 76 :])
        .decode("utf-16-be")
        .endswith("stockcode=601872\r\n")
    )
    assert decoder.frames
    assert socket.closed is True


def test_core_template_protocol_passes_macdfs_parameters_to_decoder() -> None:
    packet = core_request_packet()
    auth = framed_9528(b"auth")
    response = framed_9528(b"response")
    expected_params = (10, 20, 5)
    business = _core_curve_frame(
        "601872",
        "测试股票",
        [(1, "int"), (33007, "hxl")],
        [[930, 0.1]],
    )

    class Socket:
        def __init__(self) -> None:
            self.sent = 0
            self.reads: list[bytes | Exception] = [
                response[:13],
                response[13:],
                TimeoutError("synthetic auth idle"),
            ]

        def settimeout(self, _timeout: float) -> None:
            pass

        def sendall(self, _value: bytes) -> None:
            self.sent += 1
            if self.sent == 2:
                self.reads.extend([business[:13], business[13:]])

        def recv(self, size: int) -> bytes:
            value = self.reads.pop(0)
            if isinstance(value, Exception):
                raise value
            return value[:size]

        def close(self) -> None:
            pass

    material = replace(
        session(),
        role="core_metrics",
        core_material={
            "server_ip": "127.0.0.1",
            "server_port": "9528",
            "auth_packet_hex": auth.hex(),
            "base64_alphabet": CORE_BASE64_ALPHABET,
            "template_symbol": "600519",
            "request_packets_hex": json.dumps([packet.hex()]),
            "macdfs_params": json.dumps(list(expected_params)),
        },
    )
    values = {kind: None for kind in MetricKind}

    class Decoder:
        def __init__(self) -> None:
            self.params = None

        def with_macdfs_params(self, params: tuple[int, int, int]) -> "Decoder":
            self.params = params
            return self

        def __call__(self, _frames, _symbol: str, _market: str):
            assert self.params == expected_params
            return type("Outcome", (), {"values": values})()

    decoder = Decoder()
    outcome = Core9528TemplateProtocol(
        socket_factory=lambda _address, _timeout: Socket(),
        response_decoder=decoder,
    ).read_direct(material, "601872", "17")

    assert outcome.values[MetricKind.STOCK_NAME] is None


def test_core_template_protocol_requires_macdfs_parameters_for_curve_decoder() -> None:
    packet = core_request_packet()
    auth = framed_9528(b"auth")
    socket_called = False

    def socket_factory(_address, _timeout):
        nonlocal socket_called
        socket_called = True
        raise AssertionError("missing MACDFS parameters must fail before socket use")

    material = replace(
        session(),
        role="core_metrics",
        core_material={
            "server_ip": "127.0.0.1",
            "server_port": "9528",
            "auth_packet_hex": auth.hex(),
            "base64_alphabet": CORE_BASE64_ALPHABET,
            "template_symbol": "600519",
            "request_packets_hex": json.dumps([packet.hex()]),
        },
    )
    protocol = Core9528TemplateProtocol(
        socket_factory=socket_factory,
        response_decoder=Core9528CurveDecoder(),
    )

    with pytest.raises(DirectRequestError) as caught:
        protocol.read_direct(material, "601872", "17")

    assert caught.value.error_code == "DIRECT_PROTOCOL_HANDSHAKE_FAILED"
    assert socket_called is False


def test_core_template_protocol_waits_for_auth_response_before_request() -> None:
    packet = core_request_packet()
    auth = framed_9528(b"auth")
    business = _core_curve_frame(
        "601872",
        "测试股票",
        [(1, "int"), (33007, "hxl")],
        [[930, 0.1]],
    )

    class Socket:
        def __init__(self) -> None:
            self.sent: list[bytes] = []
            self.recv_calls = 0
            self.reads = [
                auth[:13],
                auth[13:],
                TimeoutError("synthetic idle timeout"),
                business[:13],
                business[13:],
                TimeoutError("synthetic idle timeout"),
            ]

        def settimeout(self, _timeout: float) -> None:
            pass

        def sendall(self, value: bytes) -> None:
            if len(self.sent) > 0:
                assert self.recv_calls > 0
            self.sent.append(value)

        def recv(self, size: int) -> bytes:
            self.recv_calls += 1
            if not self.reads:
                raise TimeoutError("synthetic idle timeout")
            value = self.reads.pop(0)
            if isinstance(value, Exception):
                raise value
            return value[:size]

        def close(self) -> None:
            pass

    material = replace(
        session(),
        role="core_metrics",
        core_material={
            "server_ip": "127.0.0.1",
            "server_port": "9528",
            "auth_packet_hex": auth.hex(),
            "base64_alphabet": CORE_BASE64_ALPHABET,
            "template_symbol": "600519",
            "request_packets_hex": json.dumps([packet.hex()]),
        },
    )
    values = {kind: None for kind in MetricKind}
    values[MetricKind.STOCK_NAME] = "测试股票"
    socket = Socket()
    decoder = lambda frames, _symbol, _market: (
        type("Outcome", (), {"values": values})()
    )

    outcome = Core9528TemplateProtocol(
        socket_factory=lambda _address, _timeout: socket,
        response_decoder=decoder,
    ).read_direct(material, "601872", "17")

    assert outcome.values[MetricKind.STOCK_NAME] == "测试股票"
    assert socket.sent[0] == auth
    assert socket.sent[1] != packet


def test_core_template_protocol_treats_socket_timeout_after_frame_as_idle() -> None:
    packet = core_request_packet()
    auth = framed_9528(b"auth")
    business = _core_curve_frame(
        "601872",
        "测试股票",
        [(1, "int"), (33007, "hxl")],
        [[930, 0.1]],
    )

    class Socket:
        def __init__(self) -> None:
            self.sent: list[bytes] = []
            self.timeouts: list[float] = []
            self.phase = "auth"
            self.reads: list[bytes | Exception] = [
                auth[:13],
                auth[13:],
                TimeoutError("synthetic auth idle"),
            ]

        def settimeout(self, timeout: float) -> None:
            self.timeouts.append(timeout)

        def sendall(self, value: bytes) -> None:
            self.sent.append(value)
            if len(self.sent) == 2:
                self.phase = "business"
                self.reads.extend([business[:13], business[13:]])

        def recv(self, size: int) -> bytes:
            value = self.reads.pop(0)
            if isinstance(value, Exception):
                raise value
            return value[:size]

        def close(self) -> None:
            pass

    material = replace(
        session(),
        role="core_metrics",
        core_material={
            "server_ip": "127.0.0.1",
            "server_port": "9528",
            "auth_packet_hex": auth.hex(),
            "base64_alphabet": CORE_BASE64_ALPHABET,
            "template_symbol": "600519",
            "request_packets_hex": json.dumps([packet.hex()]),
        },
    )
    values = {kind: None for kind in MetricKind}
    values[MetricKind.STOCK_NAME] = "测试股票"
    socket = Socket()

    outcome = Core9528TemplateProtocol(
        socket_factory=lambda _address, _timeout: socket,
        response_decoder=lambda _frames, _symbol, _market: type(
            "Outcome", (), {"values": values}
        )(),
    ).read_direct(material, "601872", "17")

    assert outcome.values[MetricKind.STOCK_NAME] == "测试股票"
    assert len(socket.sent) == 2
    assert any(timeout == pytest.approx(0.5) for timeout in socket.timeouts)


def test_core_template_protocol_separates_authentication_from_business_read() -> None:
    packet = core_request_packet()
    auth = framed_9528(b"auth")
    business = _core_curve_frame(
        "601872",
        "测试股票",
        [(1, "int"), (33007, "hxl")],
        [[930, 0.1]],
    )

    class Socket:
        def __init__(self) -> None:
            self.sent: list[bytes] = []
            self.reads: list[bytes | Exception] = [
                auth[:13],
                auth[13:],
                TimeoutError("synthetic auth idle"),
            ]
            self.closed = False

        def settimeout(self, _timeout: float) -> None:
            pass

        def sendall(self, value: bytes) -> None:
            self.sent.append(value)
            if len(self.sent) == 2:
                self.reads.extend([business[:13], business[13:]])

        def recv(self, size: int) -> bytes:
            value = self.reads.pop(0)
            if isinstance(value, Exception):
                raise value
            return value[:size]

        def close(self) -> None:
            self.closed = True

    material = replace(
        session(),
        role="core_metrics",
        core_material={
            "server_ip": "127.0.0.1",
            "server_port": "9528",
            "auth_packet_hex": auth.hex(),
            "base64_alphabet": CORE_BASE64_ALPHABET,
            "template_symbol": "600519",
            "request_packets_hex": json.dumps([packet.hex()]),
        },
    )
    values = {kind: None for kind in MetricKind}
    values[MetricKind.STOCK_NAME] = "测试股票"
    socket = Socket()
    protocol = Core9528TemplateProtocol(
        socket_factory=lambda _address, _timeout: socket,
        response_decoder=lambda _frames, _symbol, _market: DirectReadOutcome(
            values=values,
            source_errors={"core_metrics": None, "main_fund_flow": None},
        ),
    )

    prepared = protocol.prepare(material, "601872")
    warm = protocol.authenticate(prepared)

    assert socket.sent == [auth]
    assert socket.closed is False

    outcome = protocol.read_authenticated(warm, prepared, "601872", "17")

    assert outcome.values[MetricKind.STOCK_NAME] == "测试股票"
    assert len(socket.sent) == 2
    assert socket.sent[1] != auth
    assert socket.closed is True


def test_core_warm_pool_consumes_a_ready_connection_only_once() -> None:
    sockets = [SimpleNamespace(closed=False), SimpleNamespace(closed=False)]
    authenticated: list[WarmCoreConnection] = []

    class Protocol:
        def prepare(self, _session, symbol: str) -> CoreRequestMaterial:
            return CoreRequestMaterial(
                host="127.0.0.1",
                port=9528,
                auth_packet=b"auth",
                request_packets=(symbol.encode(),),
                macdfs_params=(12, 26, 9),
                timeout_seconds=10,
                session_fingerprint=b"session-one",
            )

        def authenticate(self, prepared: CoreRequestMaterial) -> WarmCoreConnection:
            warm = WarmCoreConnection(
                connection=sockets[len(authenticated)],
                session_fingerprint=prepared.session_fingerprint,
                authenticated_at=10,
            )
            authenticated.append(warm)
            return warm

        @staticmethod
        def close_connection(warm: WarmCoreConnection) -> None:
            warm.connection.closed = True

    pending_refills: list[Callable[[], None]] = []
    pool = Core9528WarmPool(
        Protocol(),
        clock=lambda: 20,
        start_background=pending_refills.append,
    )
    pool.prewarm(session(), "601872")
    assert len(pending_refills) == 1
    pending_refills.pop()()
    assert pool.ready_count == 1

    prepared, first = pool.acquire(session(), "601872")
    assert prepared.session_fingerprint == b"session-one"
    assert first is authenticated[0]
    assert pool.ready_count == 0
    assert pending_refills == []

    pool.replenish(prepared)
    assert len(pending_refills) == 1

    pending_refills.pop()()
    _prepared, second = pool.acquire(session(), "601872")

    assert second is authenticated[1]
    assert second is not first
    pool.close_connection(first)
    pool.close_connection(second)
    assert sockets[0].closed is True
    assert sockets[1].closed is True


def test_core_warm_pool_restarts_an_inflight_refill_after_session_change() -> None:
    sockets: list[SimpleNamespace] = []

    class Protocol:
        def prepare(self, current_session, symbol: str) -> CoreRequestMaterial:
            fingerprint = current_session.core_material["fingerprint"].encode()
            return CoreRequestMaterial(
                host="127.0.0.1",
                port=9528,
                auth_packet=b"auth",
                request_packets=(symbol.encode(),),
                macdfs_params=(12, 26, 9),
                timeout_seconds=10,
                session_fingerprint=fingerprint,
            )

        def authenticate(self, prepared: CoreRequestMaterial) -> WarmCoreConnection:
            socket = SimpleNamespace(closed=False)
            sockets.append(socket)
            return WarmCoreConnection(
                connection=socket,
                session_fingerprint=prepared.session_fingerprint,
                authenticated_at=0,
            )

        @staticmethod
        def close_connection(warm: WarmCoreConnection) -> None:
            warm.connection.closed = True

    pending_refills: list[Callable[[], None]] = []
    first_session = replace(
        session(),
        role="core_metrics",
        core_material={"fingerprint": "first", "template_symbol": "600519"},
    )
    second_session = replace(
        first_session,
        core_material={"fingerprint": "second", "template_symbol": "600519"},
    )
    pool = Core9528WarmPool(
        Protocol(),
        start_background=pending_refills.append,
    )

    pool.prewarm(first_session)
    pool.invalidate()
    pool.prewarm(second_session)

    assert len(pending_refills) == 2
    pending_refills.pop(0)()
    assert sockets[0].closed is True
    pending_refills.pop(0)()
    assert pool.ready_count == 1


def test_core_warm_pool_rejects_sync_auth_completed_after_invalidation() -> None:
    sockets: list[SimpleNamespace] = []
    pool_ref: dict[str, Core9528WarmPool] = {}

    class Protocol:
        @staticmethod
        def prepare(_session, symbol: str) -> CoreRequestMaterial:
            return CoreRequestMaterial(
                host="127.0.0.1",
                port=9528,
                auth_packet=b"auth",
                request_packets=(symbol.encode(),),
                macdfs_params=(12, 26, 9),
                timeout_seconds=10,
                session_fingerprint=b"session-one",
            )

        def authenticate(self, prepared: CoreRequestMaterial) -> WarmCoreConnection:
            socket = SimpleNamespace(closed=False)
            sockets.append(socket)
            pool_ref["pool"].invalidate()
            return WarmCoreConnection(
                connection=socket,
                session_fingerprint=prepared.session_fingerprint,
                authenticated_at=0,
            )

        @staticmethod
        def close_connection(warm: WarmCoreConnection) -> None:
            warm.connection.closed = True

    pending_refills: list[Callable[[], None]] = []
    pool = Core9528WarmPool(
        Protocol(),
        start_background=pending_refills.append,
    )
    pool_ref["pool"] = pool

    with pytest.raises(DirectRequestError) as caught:
        pool.acquire(session(), "601872")

    assert caught.value.error_code == "DIRECT_PROTOCOL_HANDSHAKE_FAILED"
    assert sockets[0].closed is True
    assert pending_refills == []


def test_core_warm_pool_discards_a_connection_past_its_idle_limit() -> None:
    now = [0.0]
    sockets: list[SimpleNamespace] = []

    class Protocol:
        @staticmethod
        def prepare(_session, symbol: str) -> CoreRequestMaterial:
            return CoreRequestMaterial(
                host="127.0.0.1",
                port=9528,
                auth_packet=b"auth",
                request_packets=(symbol.encode(),),
                macdfs_params=(12, 26, 9),
                timeout_seconds=10,
                session_fingerprint=b"session-one",
            )

        def authenticate(self, prepared: CoreRequestMaterial) -> WarmCoreConnection:
            socket = SimpleNamespace(closed=False)
            sockets.append(socket)
            return WarmCoreConnection(
                connection=socket,
                session_fingerprint=prepared.session_fingerprint,
                authenticated_at=now[0],
            )

        @staticmethod
        def close_connection(warm: WarmCoreConnection) -> None:
            warm.connection.closed = True

    pending_refills: list[Callable[[], None]] = []
    pool = Core9528WarmPool(
        Protocol(),
        max_idle_seconds=25,
        clock=lambda: now[0],
        start_background=pending_refills.append,
    )
    pool.prewarm(session(), "601872")
    pending_refills.pop()()
    assert pool.ready_count == 1

    now[0] = 26
    _prepared, warm = pool.acquire(session(), "601872")

    assert sockets[0].closed is True
    assert warm.connection is sockets[1]


def test_core_template_protocol_uses_short_idle_wait_only_after_a_frame() -> None:
    response = framed_9528(b"response")

    class Socket:
        def __init__(self) -> None:
            self.timeouts: list[float] = []
            self.reads: list[bytes | Exception] = [
                response[:13],
                response[13:],
                TimeoutError("synthetic idle timeout"),
            ]

        def settimeout(self, timeout: float) -> None:
            self.timeouts.append(timeout)

        def recv(self, size: int) -> bytes:
            value = self.reads.pop(0)
            if isinstance(value, Exception):
                raise value
            return value[:size]

    socket = Socket()
    protocol = Core9528TemplateProtocol(
        frame_idle_timeout_seconds=0.5,
    )
    frames = protocol._read_frames(
        socket,
        10,
        inter_frame_timeout_seconds=protocol.frame_idle_timeout_seconds,
    )

    assert frames == [response]
    assert socket.timeouts[0] > 9
    assert socket.timeouts[-1] == pytest.approx(0.5)


def test_core_template_protocol_caps_socket_wait_to_the_remaining_deadline() -> None:
    class Socket:
        def __init__(self) -> None:
            self.timeouts: list[float] = []

        def settimeout(self, timeout: float) -> None:
            self.timeouts.append(timeout)

        def recv(self, _size: int) -> bytes:
            raise TimeoutError("synthetic timeout")

    socket = Socket()
    deadline = time.monotonic() + 0.25

    with pytest.raises(DirectRequestError) as caught:
        Core9528TemplateProtocol._read_exact(socket, 13, deadline)

    assert caught.value.error_code == "DIRECT_PROTOCOL_RESPONSE_TIMEOUT"
    assert len(socket.timeouts) == 1
    assert 0 < socket.timeouts[0] <= 0.25


def test_core_template_protocol_preserves_a_request_batch_timeout() -> None:
    packet = core_request_packet()
    auth = framed_9528(b"auth")

    class Socket:
        def __init__(self) -> None:
            self.sent: list[bytes] = []
            self.reads: list[bytes | Exception] = [
                auth[:13],
                auth[13:],
                TimeoutError("synthetic auth idle"),
            ]

        def settimeout(self, _timeout: float) -> None:
            pass

        def sendall(self, value: bytes) -> None:
            self.sent.append(value)

        def recv(self, size: int) -> bytes:
            if not self.reads:
                raise TimeoutError("synthetic request timeout")
            value = self.reads.pop(0)
            if isinstance(value, Exception):
                raise value
            return value[:size]

        def close(self) -> None:
            pass

    material = replace(
        session(),
        role="core_metrics",
        core_material={
            "server_ip": "127.0.0.1",
            "server_port": "9528",
            "auth_packet_hex": auth.hex(),
            "base64_alphabet": CORE_BASE64_ALPHABET,
            "template_symbol": "600519",
            "request_packets_hex": json.dumps([packet.hex()]),
            "macdfs_params": json.dumps([10, 20, 5]),
        },
    )

    with pytest.raises(DirectRequestError) as caught:
        Core9528TemplateProtocol(
            socket_factory=lambda _address, _timeout: Socket(),
            response_decoder=Core9528CurveDecoder(),
        ).read_direct(material, "601872", "17")

    assert caught.value.error_code == "DIRECT_PROTOCOL_RESPONSE_TIMEOUT"


def test_core_template_protocol_rejects_a_partial_next_frame() -> None:
    complete = framed_9528(b"complete")
    partial = framed_9528(b"partial")[:15]

    class Socket:
        def __init__(self) -> None:
            self.reads = [complete[:13], complete[13:], partial[:13], partial[13:]]

        def recv(self, size: int) -> bytes:
            if self.reads:
                return self.reads.pop(0)[:size]
            raise TimeoutError("synthetic idle timeout")

    with pytest.raises(DirectRequestError) as caught:
        Core9528TemplateProtocol()._read_frames(Socket(), 1)
    assert caught.value.error_code == "DIRECT_PROTOCOL_RESPONSE_INVALID"


def test_core_template_protocol_rejects_a_header_without_its_body() -> None:
    header = framed_9528(b"body")[:13]

    class Socket:
        def __init__(self) -> None:
            self.reads = [header]

        def recv(self, size: int) -> bytes:
            if self.reads:
                return self.reads.pop(0)[:size]
            raise TimeoutError("synthetic body timeout")

    with pytest.raises(DirectRequestError) as caught:
        Core9528TemplateProtocol()._read_frames(Socket(), 1)
    assert caught.value.error_code == "DIRECT_PROTOCOL_RESPONSE_INVALID"


def test_core_template_protocol_can_stop_after_a_curve_before_later_heartbeats() -> None:
    curve = _core_curve_frame(
        "601872",
        "测试股票",
        [(1, "int"), (33007, "hxl")],
        [[930, 0.1]],
    )
    partial_heartbeat = framed_9528(b"heartbeat")[:15]

    class Socket:
        def __init__(self) -> None:
            self.buffer = curve + partial_heartbeat

        def recv(self, size: int) -> bytes:
            if self.buffer:
                value, self.buffer = self.buffer[:size], self.buffer[size:]
                return value
            raise TimeoutError("synthetic idle timeout")

    frames = Core9528TemplateProtocol()._read_frames(
        Socket(), 1, stop_after_curves=1
    )

    assert frames == [curve]


def test_core_template_protocol_can_stop_after_a_snappy_curve() -> None:
    curve = _snappy_compress_core_frame(
        _core_curve_frame(
            "601872",
            "测试股票",
            [(1, "int"), (33007, "hxl")],
            [[930, 0.1]],
        )
    )
    partial_heartbeat = framed_9528(b"heartbeat")[:15]

    class Socket:
        def __init__(self) -> None:
            self.buffer = curve + partial_heartbeat

        def recv(self, size: int) -> bytes:
            if self.buffer:
                value, self.buffer = self.buffer[:size], self.buffer[size:]
                return value
            raise TimeoutError("synthetic idle timeout")

    frames = Core9528TemplateProtocol()._read_frames(
        Socket(), 1, stop_after_curves=1
    )

    assert frames == [curve]


def test_core_template_protocol_sends_the_captured_request_batch_after_auth() -> None:
    packet_one = core_request_packet()
    packet_two = core_request_packet().replace(b"600519", b"600518")
    auth = framed_9528(b"auth")
    business = framed_9528(b"business")

    class Socket:
        def __init__(self) -> None:
            self.sent: list[bytes] = []
            self.recv_calls = 0
            self.reads: list[bytes | Exception] = [
                auth[:13],
                auth[13:],
                TimeoutError("synthetic idle timeout"),
            ]

        def settimeout(self, _timeout: float) -> None:
            pass

        def sendall(self, value: bytes) -> None:
            self.sent.append(value)
            if len(self.sent) == 3:
                assert self.recv_calls == 3
                self.reads.extend(
                    [
                        business[:13],
                        business[13:],
                        TimeoutError("synthetic idle timeout"),
                    ]
                )

        def recv(self, size: int) -> bytes:
            self.recv_calls += 1
            if not self.reads:
                raise TimeoutError("synthetic idle timeout")
            value = self.reads.pop(0)
            if isinstance(value, Exception):
                raise value
            return value[:size]

        def close(self) -> None:
            pass

    material = replace(
        session(),
        role="core_metrics",
        core_material={
            "server_ip": "127.0.0.1",
            "server_port": "9528",
            "auth_packet_hex": auth.hex(),
            "base64_alphabet": CORE_BASE64_ALPHABET,
            "template_symbol": "600519",
            "request_packets_hex": json.dumps([packet_one.hex(), packet_two.hex()]),
        },
    )
    values = {kind: None for kind in MetricKind}
    values[MetricKind.STOCK_NAME] = "测试股票"
    socket = Socket()

    outcome = Core9528TemplateProtocol(
        socket_factory=lambda _address, _timeout: socket,
        response_decoder=lambda _frames, _symbol, _market: type(
            "Outcome", (), {"values": values}
        )(),
    ).read_direct(material, "601872", "17")

    assert outcome.values[MetricKind.STOCK_NAME] == "测试股票"
    assert len(socket.sent) == 3


def test_core_template_protocol_skips_duplicate_captured_request_pairs() -> None:
    packet = core_request_packet()
    auth = framed_9528(b"auth")
    business = framed_9528(b"business")

    class Socket:
        def __init__(self) -> None:
            self.sent: list[bytes] = []
            self.reads: list[bytes | Exception] = [
                auth[:13],
                auth[13:],
                TimeoutError("synthetic idle timeout"),
            ]

        def settimeout(self, _timeout: float) -> None:
            pass

        def sendall(self, value: bytes) -> None:
            self.sent.append(value)
            if len(self.sent) == 3:
                self.reads.extend(
                    [
                        business[:13],
                        business[13:],
                        TimeoutError("synthetic idle timeout"),
                    ]
                )

        def recv(self, size: int) -> bytes:
            if self.reads:
                value = self.reads.pop(0)
                if isinstance(value, Exception):
                    raise value
                return value[:size]
            raise TimeoutError("synthetic idle timeout")

        def close(self) -> None:
            pass

    material = replace(
        session(),
        role="core_metrics",
        core_material={
            "server_ip": "127.0.0.1",
            "server_port": "9528",
            "auth_packet_hex": auth.hex(),
            "base64_alphabet": CORE_BASE64_ALPHABET,
            "template_symbol": "600519",
            "request_packets_hex": json.dumps([packet.hex(), packet.hex(), packet.hex(), packet.hex()]),
        },
    )
    values = {kind: None for kind in MetricKind}
    values[MetricKind.STOCK_NAME] = "测试股票"
    socket = Socket()

    outcome = Core9528TemplateProtocol(
        socket_factory=lambda _address, _timeout: socket,
        response_decoder=lambda _frames, _symbol, _market: type(
            "Outcome", (), {"values": values}
        )(),
    ).read_direct(material, "601872", "17")

    assert outcome.values[MetricKind.STOCK_NAME] == "测试股票"
    assert len(socket.sent) == 3


@pytest.mark.parametrize(
    "mutate",
    [
        lambda packet: b"BAD!" + packet[4:],
        lambda packet: packet[:4] + b"00000001" + packet[12:],
        lambda packet: packet[:12] + b"!" + packet[13:],
        lambda packet: packet[:13] + b"\xff\xff" + packet[15:],
        lambda packet: packet[:-1] + b"!",
        lambda packet: core_request_packet("601872"),
    ],
)
def test_core_request_packet_rejects_malformed_outer_framing_or_template_symbol(
    mutate,
) -> None:
    with pytest.raises((ValueError, DirectRequestError)):
        patch_core_packet_symbol(
            mutate(core_request_packet()),
            "600519",
            "601872",
            alphabet=CORE_BASE64_ALPHABET,
        )


def test_core_template_protocol_requires_the_captured_base64_alphabet() -> None:
    material = replace(
        session(),
        role="core_metrics",
        core_material={
            "server_ip": "127.0.0.1",
            "server_port": "9528",
            "auth_packet_hex": framed_9528(b"auth").hex(),
            "template_symbol": "600519",
            "request_packets_hex": json.dumps([core_request_packet().hex()]),
        },
    )

    with pytest.raises(DirectRequestError) as caught:
        Core9528TemplateProtocol._material_packets(material, "601872")

    assert caught.value.error_code == "DIRECT_PROTOCOL_HANDSHAKE_FAILED"


@pytest.mark.parametrize(
    "auth",
    [
        b"BAD!00000004\x00auth",
        b"\xfd" * 4 + b"00000001\x00auth",
        b"\xfd" * 4 + b"00000004!auth",
    ],
)
def test_core_template_protocol_rejects_malformed_auth_outer_framing(auth: bytes) -> None:
    material = replace(
        session(),
        role="core_metrics",
        core_material={
            "server_ip": "127.0.0.1",
            "server_port": "9528",
            "auth_packet_hex": auth.hex(),
            "base64_alphabet": CORE_BASE64_ALPHABET,
            "template_symbol": "600519",
            "request_packets_hex": json.dumps([core_request_packet().hex()]),
        },
    )

    with pytest.raises(DirectRequestError) as caught:
        Core9528TemplateProtocol._material_packets(material, "601872")

    assert caught.value.error_code == "DIRECT_PROTOCOL_HANDSHAKE_FAILED"


@pytest.mark.parametrize("length_field", [b"+0000004", b" 0000004"])
def test_core_template_protocol_rejects_non_hex_length_field(length_field: bytes) -> None:
    auth = b"\xfd" * 4 + length_field + b"\x00auth"
    material = replace(
        session(),
        role="core_metrics",
        core_material={
            "server_ip": "127.0.0.1",
            "server_port": "9528",
            "auth_packet_hex": auth.hex(),
            "base64_alphabet": CORE_BASE64_ALPHABET,
            "template_symbol": "600519",
            "request_packets_hex": json.dumps([core_request_packet().hex()]),
        },
    )

    with pytest.raises(DirectRequestError) as caught:
        Core9528TemplateProtocol._material_packets(material, "601872")

    assert caught.value.error_code == "DIRECT_PROTOCOL_HANDSHAKE_FAILED"


@pytest.mark.parametrize("length_field", [b"+0000008", b" 0000008"])
def test_core_template_protocol_rejects_non_hex_response_length_field(
    length_field: bytes,
) -> None:
    response = b"\xfd" * 4 + length_field + b"\x00" + b"response"

    class Socket:
        def __init__(self) -> None:
            self.reads = [response[:13], response[13:]]

        def recv(self, size: int) -> bytes:
            if not self.reads:
                return b""
            value = self.reads.pop(0)
            return value[:size]

    with pytest.raises(DirectRequestError) as caught:
        Core9528TemplateProtocol()._read_frames(Socket(), 1)

    assert caught.value.error_code == "DIRECT_PROTOCOL_RESPONSE_INVALID"


@pytest.mark.parametrize(
    ("failure", "expected_code"),
    [
        (
            DirectRequestError(
                "DIRECT_PROTOCOL_RESPONSE_INVALID",
                "synthetic-decoder-direct-secret-marker",
            ),
            "DIRECT_PROTOCOL_RESPONSE_INVALID",
        ),
        (
            RuntimeError("synthetic-decoder-generic-secret-marker"),
            "DIRECT_PROTOCOL_RESPONSE_INVALID",
        ),
    ],
)
def test_core_decoder_boundary_rebuilds_errors_without_secret_tracebacks(
    failure: Exception,
    expected_code: str,
) -> None:
    response = framed_9528(b"response")
    business = _core_curve_frame(
        "601872",
        "测试股票",
        [(1, "int"), (33007, "hxl")],
        [[930, 0.1]],
    )

    class Socket:
        def __init__(self) -> None:
            self.sent = 0
            self.reads: list[bytes | Exception] = [
                response[:13],
                response[13:],
                TimeoutError("synthetic auth idle"),
            ]

        def settimeout(self, _timeout: float) -> None: pass
        def sendall(self, _value: bytes) -> None:
            self.sent += 1
            if self.sent == 2:
                self.reads.extend([business[:13], business[13:]])
        def recv(self, size: int) -> bytes:
            value = self.reads.pop(0)
            if isinstance(value, Exception):
                raise value
            return value[:size]
        def close(self) -> None: pass

    material = replace(
        session(),
        role="core_metrics",
        core_material={
            "server_ip": "127.0.0.1",
            "server_port": "9528",
            "auth_packet_hex": framed_9528(b"auth").hex(),
            "base64_alphabet": CORE_BASE64_ALPHABET,
            "template_symbol": "600519",
            "request_packets_hex": json.dumps([core_request_packet().hex()]),
        },
    )
    protocol = Core9528TemplateProtocol(
        socket_factory=lambda *_args: Socket(),
        response_decoder=lambda *_args: (_ for _ in ()).throw(failure),
    )

    with pytest.raises(DirectRequestError) as caught:
        protocol.read_direct(material, "601872", "17")

    rendered = "".join(
        traceback.format_exception(
            type(caught.value), caught.value, caught.value.__traceback__
        )
    )
    assert caught.value.error_code == expected_code
    assert "synthetic-decoder-direct-secret-marker" not in rendered
    assert "synthetic-decoder-generic-secret-marker" not in rendered


@pytest.mark.parametrize("failure_type", [OSError, RuntimeError])
def test_core_socket_boundary_hides_secret_exception_details(failure_type) -> None:
    secret = "synthetic-socket-secret-marker"
    material = replace(
        session(),
        role="core_metrics",
        core_material={
            "server_ip": "127.0.0.1",
            "server_port": "9528",
            "auth_packet_hex": framed_9528(b"auth").hex(),
            "base64_alphabet": CORE_BASE64_ALPHABET,
            "template_symbol": "600519",
            "request_packets_hex": json.dumps([core_request_packet().hex()]),
        },
    )
    protocol = Core9528TemplateProtocol(
        socket_factory=lambda *_args: (_ for _ in ()).throw(failure_type(secret)),
        response_decoder=lambda *_args: None,
    )

    with pytest.raises(DirectRequestError) as caught:
        protocol.read_direct(material, "601872", "17")

    rendered = "".join(
        traceback.format_exception(
            type(caught.value), caught.value, caught.value.__traceback__
        )
    )
    assert caught.value.error_code == "DIRECT_PROTOCOL_HANDSHAKE_FAILED"
    assert secret not in rendered


def test_core_gov_codec_decodes_a_sanitized_literal_fixture() -> None:
    payload = bytes(range(1, 34))

    assert decode_core_gov(_literal_gov_stream(payload), len(payload)) == payload


@pytest.mark.parametrize(
    ("compressed", "expected"),
    [
        (bytes.fromhex("051068656c6c6f"), b"hello"),
        (bytes.fromhex("09086162630903"), b"abcabcabc"),
        (bytes.fromhex("080c616263640e0400"), b"abcdabcd"),
        (bytes.fromhex("080c616263640f04000000"), b"abcdabcd"),
        (bytes.fromhex("46f045") + b"x" * 70, b"x" * 70),
    ],
)
def test_core_snappy_decodes_literal_and_copy_tags(
    compressed: bytes, expected: bytes
) -> None:
    assert decode_core_snappy(compressed) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (0x00000000, 0),
        (0x2000007B, 12300),
        (0xA000007B, Decimal("1.23")),
        (0x2800007B, -12300),
        (0xA800007B, Decimal("-1.23")),
        (0x80000000, None),
        (0xFFFFFFFF, None),
    ],
)
def test_core_hxl_value_handles_scale_sign_and_missing_sentinels(
    raw: int, expected: float | int | None
) -> None:
    value = _core_hxl_value(raw)

    if expected is None:
        assert value is None
    else:
        assert value == expected


def test_core_curve_decoder_extracts_focus_metrics_and_intraday_points() -> None:
    symbol = "601872"
    quote = _core_curve_frame(
        symbol,
        "测试股票",
        [(1, "int"), (10, "hxl"), (13, "hxl"), (19, "hxl"), (34312, "hxl")],
        [[930, 12.34, 100, 1000, 0.55], [931, 12.35, 200, 2000, 0.56]],
        ext_values={
            6: ("hxl", 12.00),
            10: ("hxl", 12.35),
            34312: ("hxl", 0.56),
            34315: ("hxl", 1.23),
            407: ("hxl", 1_000_000),
        },
    )
    large_net = _core_curve_frame(
        symbol,
        "测试股票",
        [(1, "int"), (33007, "hxl")],
        [[930, 0.1], [931, -0.013053]],
    )
    large_amount = _core_curve_frame(
        symbol,
        "测试股票",
        [(1, "int"), (33015, "hxl")],
        [[930, 1_000_000], [931, -211_165_010]],
    )
    retail = _core_curve_frame(
        symbol,
        "测试股票",
        [
            (1, "int"),
            (215, "hxl"),
            (216, "hxl"),
            (217, "hxl"),
            (218, "hxl"),
            (219, "hxl"),
            (220, "hxl"),
            (221, "hxl"),
            (222, "hxl"),
            (13, "hxl"),
            (18, "hxl"),
        ],
        [
            [930, 0, 1, 0, 1, 0, 1, 2.732, 1, 100, 10],
            [931, 0, 1, 0, 1, 0, 1, 2.732, 1, 100, 10],
        ],
        ext_values={407: ("hxl", 1_000_000)},
    )

    outcome = Core9528CurveDecoder()([quote, large_net, large_amount, retail], symbol, "17")

    assert outcome.values[MetricKind.STOCK_NAME] == "测试股票"
    assert outcome.values[MetricKind.CURRENT_PRICE] == "12.35"
    assert outcome.values[MetricKind.CHANGE_PERCENT] == "1.23%"
    assert outcome.values[MetricKind.TURNOVER_RATE] == "0.56%"
    assert outcome.values[MetricKind.LARGE_ORDER_NET] == "-0.01"
    assert outcome.values[MetricKind.LARGE_ORDER_AMOUNT] == "-21116.5万"
    assert outcome.values[MetricKind.RETAIL_COUNT] == "12.68"
    assert outcome.values[MetricKind.MACDFS] == "+0.001"
    assert outcome.intraday_series[MetricKind.LARGE_ORDER_NET]["points"][-1]["value"] == "-0.01"
    assert outcome.intraday_series[MetricKind.LARGE_ORDER_AMOUNT]["points"][-1]["value"] == "-21116.5"
    assert outcome.intraday_series[MetricKind.RETAIL_COUNT]["points"][-1]["value"] == "12.68"
    assert outcome.intraday_series[MetricKind.MACDFS]["points"][-1]["value"] == "+0.001"


def test_core_curve_decoder_decompresses_a_snappy_mini_body() -> None:
    frame = _core_curve_frame(
        "601872",
        "测试股票",
        [(1, "int"), (33007, "hxl")],
        [[930, 0.1], [931, -0.013053]],
    )
    compressed_frame = _snappy_compress_core_frame(frame)

    outcome = Core9528CurveDecoder()([compressed_frame], "601872", "17")

    assert outcome.values[MetricKind.LARGE_ORDER_NET] == "-0.01"


def test_core_curve_decoder_rejects_unimplemented_encryption_flags() -> None:
    frame = bytearray(
        _core_curve_frame(
            "601872",
            "测试股票",
            [(1, "int"), (33007, "hxl")],
            [[930, 0.1]],
        )
    )
    type_offset = 13 + 6
    type_word = int.from_bytes(frame[type_offset : type_offset + 4], "little")
    frame[type_offset : type_offset + 4] = (
        type_word | 0x10000000
    ).to_bytes(4, "little")

    with pytest.raises(DirectRequestError) as caught:
        Core9528CurveDecoder()([bytes(frame)], "601872", "17")

    assert caught.value.error_code == "DIRECT_PROTOCOL_RESPONSE_INVALID"


def test_core_curve_decoder_uses_captured_macdfs_parameters() -> None:
    quote = _core_curve_frame(
        "601872",
        "测试股票",
        [(1, "int"), (10, "hxl"), (13, "hxl"), (19, "hxl"), (34312, "hxl")],
        [
            [930, 10, 100, 1000, 0.55],
            [931, 11, 200, 2000, 0.56],
            [932, 12, 300, 3000, 0.57],
        ],
        ext_values={6: ("hxl", 9)},
    )

    outcome = Core9528CurveDecoder(macdfs_params=(10, 20, 5))(
        [quote], "601872", "17"
    )

    assert outcome.values[MetricKind.MACDFS] == "+0.276"


def test_core_curve_decoder_decodes_two_byte_short_columns() -> None:
    frame = _core_curve_frame(
        "601872",
        "测试股票",
        [(1, "int"), (216, "short")],
        [[930, -12], [931, 34]],
    )

    curve = Core9528CurveDecoder._decode_frame(frame, "601872", "17")

    assert curve is not None
    assert curve.data[216] == (-12, 34)


@pytest.mark.parametrize(
    "ext_values",
    [
        {},
        {407: ("hxl", 0)},
    ],
)
def test_core_curve_decoder_keeps_retail_count_missing_without_valid_407(
    ext_values: dict[int, tuple[str, float | int]]
) -> None:
    frame = _core_curve_frame(
        "601872",
        "测试股票",
        [
            (1, "int"),
            (215, "hxl"),
            (216, "hxl"),
            (217, "hxl"),
            (218, "hxl"),
            (219, "hxl"),
            (220, "hxl"),
            (221, "hxl"),
            (222, "hxl"),
            (13, "hxl"),
            (18, "hxl"),
        ],
        [[930, 1, 0, 1, 0, 1, 0, 1, 0, 100, 10]],
        ext_values=ext_values,
    )

    outcome = Core9528CurveDecoder()([frame], "601872", "17")

    assert outcome.values[MetricKind.RETAIL_COUNT] is None
    assert MetricKind.RETAIL_COUNT not in outcome.intraday_series


def test_core_curve_decoder_keeps_zero_volume_or_amount_points_missing() -> None:
    frame = _core_curve_frame(
        "601872",
        "测试股票",
        [
            (1, "int"),
            (215, "hxl"),
            (216, "hxl"),
            (217, "hxl"),
            (218, "hxl"),
            (219, "hxl"),
            (220, "hxl"),
            (221, "hxl"),
            (222, "hxl"),
            (13, "hxl"),
            (18, "hxl"),
        ],
        [
            [930, 1, 0, 1, 0, 1, 0, 1, 0, 0, 10],
            [931, 1, 0, 1, 0, 1, 0, 1, 0, 100, 10],
        ],
        ext_values={407: ("hxl", 1_000_000)},
    )

    outcome = Core9528CurveDecoder()([frame], "601872", "17")
    points = outcome.intraday_series[MetricKind.RETAIL_COUNT]["points"]

    assert points[0]["value"] is None
    assert points[1]["value"] == outcome.values[MetricKind.RETAIL_COUNT]


def test_core_curve_decoder_keeps_beijing_level2_metrics_missing() -> None:
    symbol = "920002"
    quote = _core_curve_frame(
        symbol,
        "测试北交所",
        [(1, "int"), (10, "hxl"), (13, "hxl"), (19, "hxl"), (34312, "hxl")],
        [[930, 52.02, 100, 1000, 1.65]],
        ext_values={6: ("hxl", 51.37)},
    )
    large_net = _core_curve_frame(
        symbol,
        "测试北交所",
        [(1, "int"), (33007, "hxl")],
        [[930, 0]],
    )
    large_amount = _core_curve_frame(
        symbol,
        "测试北交所",
        [(1, "int"), (33015, "hxl")],
        [[930, 0]],
    )

    outcome = Core9528CurveDecoder()(
        [quote, large_net, large_amount],
        symbol,
        "151",
    )

    assert outcome.values[MetricKind.LARGE_ORDER_NET] is None
    assert outcome.values[MetricKind.LARGE_ORDER_AMOUNT] is None
    assert outcome.values[MetricKind.RETAIL_COUNT] is None
    assert MetricKind.LARGE_ORDER_NET not in outcome.intraday_series
    assert MetricKind.LARGE_ORDER_AMOUNT not in outcome.intraday_series
    assert MetricKind.RETAIL_COUNT not in outcome.intraday_series


def test_core_curve_decoder_rejects_truncated_or_identity_mismatched_frames() -> None:
    frame = _core_curve_frame(
        "601872",
        "测试股票",
        [(1, "int"), (33007, "hxl")],
        [[930, 0.1]],
    )
    decoder = Core9528CurveDecoder()

    with pytest.raises(DirectRequestError) as truncated:
        decoder([frame[:-1]], "601872", "17")
    assert truncated.value.error_code == "DIRECT_PROTOCOL_RESPONSE_INVALID"

    wrong_identity = bytearray(frame)
    wrong_identity[45 + 24 + 2 : 45 + 24 + 2 + 12] = "600519".encode("utf-16-le")
    with pytest.raises(DirectRequestError) as mismatched:
        decoder([bytes(wrong_identity)], "601872", "17")
    assert mismatched.value.error_code == "DIRECT_PROTOCOL_RESPONSE_INVALID"


def test_core_curve_decoder_rejects_a_compressed_length_that_is_not_exact() -> None:
    frame = _core_curve_frame(
        "601872",
        "测试股票",
        [(1, "int"), (33007, "hxl")],
        [[930, 0.1], [931, -0.013053]],
    )
    body_start = 45
    extension_end = struct.unpack_from("<H", frame, body_start + 18)[0]
    compressed_length_offset = body_start + extension_end
    compressed_length = struct.unpack_from("<I", frame, compressed_length_offset)[0]
    malformed = bytearray(frame)
    struct.pack_into("<I", malformed, compressed_length_offset, compressed_length - 1)

    with pytest.raises(DirectRequestError) as caught:
        Core9528CurveDecoder()([bytes(malformed)], "601872", "17")
    assert caught.value.error_code == "DIRECT_PROTOCOL_RESPONSE_INVALID"
