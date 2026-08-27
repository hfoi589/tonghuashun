from __future__ import annotations

import json
import struct
import traceback
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from level2_service.app_sessions import AccountSessionBundle
from level2_service.direct_market import (
    CORE_BASE64_ALPHABET,
    Core9528Client,
    Core9528TemplateProtocol,
    decode_core_base64,
    encode_core_base64,
    FundFlowHttpClient,
    HttpResponse,
    patch_core_packet_symbol,
    ShadowParsedValueSource,
)
from level2_service.models import FUND_FLOW_METRICS, FUND_FLOW_PERIODS, MetricKind
from level2_service.parsed_values import DirectRequestError


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

    class Socket:
        def __init__(self) -> None:
            self.sent: list[bytes] = []
            self.closed = False
            self.reads = [response[:13], response[13:]]

        def settimeout(self, _timeout: float) -> None:
            pass

        def sendall(self, value: bytes) -> None:
            self.sent.append(value)

        def recv(self, size: int) -> bytes:
            if not self.reads:
                return b""
            value = self.reads.pop(0)
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

    class Socket:
        def __init__(self) -> None:
            self.reads = [response[:13], response[13:]]

        def settimeout(self, _timeout: float) -> None: pass
        def sendall(self, _value: bytes) -> None: pass
        def recv(self, size: int) -> bytes:
            value = self.reads.pop(0) if self.reads else b""
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
