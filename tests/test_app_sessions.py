from __future__ import annotations

import base64
import json
import logging
import shutil
import struct
import subprocess
import sys
import traceback
import types
from datetime import datetime, timezone
from pathlib import Path

import pytest

from level2_service.app_sessions import (
    AccountSessionBundle,
    CoreAccountSessionRefresher,
    EncryptedFileSessionProvider,
    FundAccountSessionRefresher,
    _FRIDA_CORE_SESSION_CAPTURE_SCRIPT,
    capture_core_session_material,
    capture_fund_http_session,
)
from level2_service.direct_market import CORE_BASE64_ALPHABET, encode_core_base64
from level2_service.parsed_values import DirectRequestError, FridaParsedValueSource


def encryption_key() -> str:
    return base64.urlsafe_b64encode(b"session-encryption-key-material!").decode("ascii")


def bundle() -> AccountSessionBundle:
    return AccountSessionBundle(
        role="main_fund_flow",
        cookie="user=secret-user; sess_tk=secret-ticket",
        user_agent="private-app-user-agent",
        platform="android",
        updated_at=datetime(2026, 8, 26, 8, 0, tzinfo=timezone.utc),
    )


def core_template_packet_hex(symbol: str = "600519") -> str:
    body = f"[frame]\r\nid=6001\r\nstockcode={symbol}\r\n".encode("utf-16-be")
    header = struct.pack("<HiiHiiiI", 76, 1, 262144, 65283, 0, 6001, len(body), 0)
    header += b"\x00" * (76 - len(header))
    payload = header + encode_core_base64(body)
    packet = b"\xfd" * 4 + f"{len(payload):08x}".encode("ascii") + b"\x00"
    return (packet + payload).hex()


def core_auth_packet_hex(payload: bytes = b"synthetic-auth") -> str:
    return (
        b"\xfd" * 4
        + f"{len(payload):08x}".encode("ascii")
        + b"\x00"
        + payload
    ).hex()


def test_encrypted_session_provider_round_trips_without_plaintext(
    tmp_path: Path,
) -> None:
    provider = EncryptedFileSessionProvider(tmp_path / "sessions", encryption_key())

    provider.put(bundle())

    restored = provider.get("main_fund_flow")
    assert restored == bundle()
    stored = tmp_path / "sessions" / "main_fund_flow.session"
    raw = stored.read_bytes()
    assert b"secret-user" not in raw
    assert b"secret-ticket" not in raw
    assert b"private-app-user-agent" not in raw
    assert stored.stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "sessions").stat().st_mode & 0o777 == 0o700


def test_session_status_never_serializes_credentials(tmp_path: Path) -> None:
    provider = EncryptedFileSessionProvider(tmp_path / "sessions", encryption_key())
    provider.put(bundle())

    status = provider.status("main_fund_flow")

    assert status.role == "main_fund_flow"
    assert status.state == "READY"
    assert status.updated_at == bundle().updated_at
    assert status.error_code is None
    serialized = json.dumps(status.as_public())
    assert "secret-user" not in serialized
    assert "secret-ticket" not in serialized
    assert "private-app-user-agent" not in serialized
    assert set(status.as_public()) == {"role", "state", "updated_at", "error_code"}


def test_session_provider_exposes_missing_and_error_states(tmp_path: Path) -> None:
    provider = EncryptedFileSessionProvider(tmp_path / "sessions", encryption_key())

    assert provider.get("core_metrics") is None
    assert provider.status("core_metrics").as_public() == {
        "role": "core_metrics",
        "state": "MISSING",
        "updated_at": None,
        "error_code": None,
    }

    provider.mark_error("core_metrics", "DIRECT_PROTOCOL_HANDSHAKE_FAILED")

    assert provider.status("core_metrics").as_public() == {
        "role": "core_metrics",
        "state": "ERROR",
        "updated_at": None,
        "error_code": "DIRECT_PROTOCOL_HANDSHAKE_FAILED",
    }


def test_session_provider_rejects_invalid_roles_and_keys(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="THS_SESSION_ENCRYPTION_KEY"):
        EncryptedFileSessionProvider(tmp_path / "sessions", "not-a-fernet-key")

    provider = EncryptedFileSessionProvider(tmp_path / "sessions", encryption_key())
    with pytest.raises(ValueError, match="unknown account role"):
        provider.get("other")


def test_fund_session_refresher_builds_a_secret_safe_bundle() -> None:
    refresher = FundAccountSessionRefresher(
        lambda: {
            "cookie": "user=private; sess_tk=private-ticket",
            "user_agent": "app-user-agent",
            "platform": "android",
        },
        now=lambda: datetime(2026, 8, 26, 10, 0, tzinfo=timezone.utc),
    )

    refreshed = refresher("main_fund_flow")

    assert refreshed.cookie == "user=private; sess_tk=private-ticket"
    assert refreshed.user_agent == "app-user-agent"
    assert refreshed.platform == "android"
    assert refreshed.updated_at == datetime(2026, 8, 26, 10, 0, tzinfo=timezone.utc)
    assert "private-ticket" not in repr(refreshed)
    assert "app-user-agent" not in repr(refreshed)


@pytest.mark.parametrize(
    "captured",
    [
        {},
        {"cookie": "", "user_agent": "app-user-agent", "platform": "android"},
        {"cookie": "user=private", "user_agent": "", "platform": "android"},
    ],
)
def test_fund_session_refresher_rejects_incomplete_captures(
    captured: dict[str, str],
) -> None:
    refresher = FundAccountSessionRefresher(lambda: captured)

    with pytest.raises(DirectRequestError) as error:
        refresher("main_fund_flow")

    assert error.value.error_code == "DIRECT_SESSION_UNAVAILABLE"


def test_fund_session_refresher_rejects_the_core_role() -> None:
    refresher = FundAccountSessionRefresher(lambda: {})

    with pytest.raises(ValueError, match="main_fund_flow"):
        refresher("core_metrics")


def test_core_session_refresher_preserves_complete_opaque_material_without_exposure(
    tmp_path: Path,
    caplog,
) -> None:
    synthetic_auth = core_auth_packet_hex()
    synthetic_request = core_template_packet_hex()
    refresher = CoreAccountSessionRefresher(
        lambda: {
            "server_ip": "60.204.184.46",
            "server_port": "9528",
            "auth_packet_hex": synthetic_auth,
            "base64_alphabet": CORE_BASE64_ALPHABET,
            "template_symbol": "600519",
            "request_packets_hex": json.dumps([synthetic_request]),
        },
        now=lambda: datetime(2026, 8, 26, 10, 30, tzinfo=timezone.utc),
    )

    refreshed = refresher("core_metrics")
    provider = EncryptedFileSessionProvider(tmp_path / "sessions", encryption_key())
    provider.put(refreshed)
    public_status = json.dumps(provider.status("core_metrics").as_public())
    stored = (tmp_path / "sessions" / "core_metrics.session").read_bytes()

    assert refreshed.role == "core_metrics"
    assert refreshed.core_material["server_ip"] == "60.204.184.46"
    assert refreshed.core_material["server_port"] == "9528"
    assert refreshed.core_material["auth_packet_hex"] == synthetic_auth
    assert refreshed.core_material["base64_alphabet"].endswith("+/")
    assert refreshed.core_material["template_symbol"] == "600519"
    assert json.loads(refreshed.core_material["request_packets_hex"]) == [
        synthetic_request
    ]
    for secret in (synthetic_auth, synthetic_request):
        assert secret not in repr(refreshed)
        assert secret not in public_status
        assert secret not in caplog.text
        assert secret.encode("ascii") not in stored


def test_core_session_refresher_rejects_missing_protocol_material() -> None:
    refresher = CoreAccountSessionRefresher(lambda: {"server_ip": "60.204.184.46"})

    with pytest.raises(DirectRequestError) as error:
        refresher("core_metrics")

    assert error.value.error_code == "DIRECT_SESSION_UNAVAILABLE"


def test_core_session_refresher_rejects_a_missing_captured_base64_alphabet() -> None:
    refresher = CoreAccountSessionRefresher(
        lambda: {
            "server_ip": "60.204.184.46",
            "server_port": "9528",
            "auth_packet_hex": core_auth_packet_hex(),
            "template_symbol": "600519",
            "request_packets_hex": json.dumps([core_template_packet_hex()]),
        }
    )

    with pytest.raises(DirectRequestError) as error:
        refresher("core_metrics")

    assert error.value.error_code == "DIRECT_PROTOCOL_HANDSHAKE_FAILED"


def test_session_provider_revalidates_core_material_before_persistence(
    tmp_path: Path,
) -> None:
    provider = EncryptedFileSessionProvider(tmp_path / "sessions", encryption_key())
    unsafe = AccountSessionBundle(
        role="core_metrics",
        cookie="",
        user_agent="",
        platform="android",
        updated_at=datetime(2026, 8, 26, 10, 30, tzinfo=timezone.utc),
        core_material={
            "server_ip": "60.204.184.46",
            "server_port": "9528",
            "auth_packet_hex": core_auth_packet_hex(),
            "template_symbol": "600519",
            "request_packets_hex": json.dumps([core_template_packet_hex()]),
        },
    )

    with pytest.raises(DirectRequestError) as caught:
        provider.put(unsafe)

    assert caught.value.error_code == "DIRECT_PROTOCOL_HANDSHAKE_FAILED"
    assert not (tmp_path / "sessions" / "core_metrics.session").exists()


@pytest.mark.parametrize(
    ("auth_packet_hex", "request_packets_hex"),
    [
        ("00" + core_auth_packet_hex()[2:], json.dumps([core_template_packet_hex()])),
        (
            (b"\xfd" * 4 + b"00000001\x00auth").hex(),
            json.dumps([core_template_packet_hex()]),
        ),
        (
            core_auth_packet_hex(),
            json.dumps(["00" + core_template_packet_hex()[2:]]),
        ),
        (
            core_auth_packet_hex(),
            json.dumps(
                [
                    (
                        b"\xfd" * 4
                        + b"00000001\x00"
                        + bytes.fromhex(core_template_packet_hex())[13:]
                    ).hex()
                ]
            ),
        ),
    ],
)
def test_core_session_refresher_rejects_malformed_outer_9528_framing(
    auth_packet_hex: str,
    request_packets_hex: str,
) -> None:
    refresher = CoreAccountSessionRefresher(
        lambda: {
            "server_ip": "60.204.184.46",
            "server_port": "9528",
            "auth_packet_hex": auth_packet_hex,
            "base64_alphabet": CORE_BASE64_ALPHABET,
            "template_symbol": "600519",
            "request_packets_hex": request_packets_hex,
        }
    )

    with pytest.raises(DirectRequestError) as caught:
        refresher("core_metrics")

    assert caught.value.error_code == "DIRECT_PROTOCOL_HANDSHAKE_FAILED"


@pytest.mark.parametrize(
    "request_packets_hex",
    ["[]", "{}", '[""]', '["not-hex"]'],
)
def test_core_session_refresher_rejects_unverified_template_packets(
    request_packets_hex: str,
) -> None:
    refresher = CoreAccountSessionRefresher(
        lambda: {
            "server_ip": "60.204.184.46",
            "server_port": "9528",
            "auth_packet_hex": "a1b2c3d4",
            "base64_alphabet": (
                "abcdefghijklmnopqrstuvwxyz0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ+/"
            ),
            "template_symbol": "600519",
            "request_packets_hex": request_packets_hex,
        }
    )

    with pytest.raises(DirectRequestError) as error:
        refresher("core_metrics")

    assert error.value.error_code == "DIRECT_PROTOCOL_HANDSHAKE_FAILED"


@pytest.mark.parametrize(
    "request_packets_hex",
    [
        json.dumps(["fdfdfdfd0102"]),
        json.dumps([core_template_packet_hex("601872")]),
    ],
)
def test_core_session_refresher_rejects_malformed_or_unrelated_template_packets(
    request_packets_hex: str,
) -> None:
    refresher = CoreAccountSessionRefresher(
        lambda: {
            "server_ip": "60.204.184.46",
            "server_port": "9528",
            "auth_packet_hex": "a1b2c3d4",
            "base64_alphabet": CORE_BASE64_ALPHABET,
            "template_symbol": "600519",
            "request_packets_hex": request_packets_hex,
        }
    )

    with pytest.raises(DirectRequestError) as error:
        refresher("core_metrics")

    assert error.value.error_code == "DIRECT_PROTOCOL_HANDSHAKE_FAILED"


def test_core_session_refresher_sanitizes_secret_bearing_capture_exception(
    caplog,
) -> None:
    synthetic_secret = "synthetic-capture-secret-marker"

    def fail_capture() -> dict[str, str]:
        raise RuntimeError(f"RPC failed with {synthetic_secret}")

    refresher = CoreAccountSessionRefresher(fail_capture)

    with pytest.raises(DirectRequestError) as caught:
        refresher("core_metrics")

    logging.getLogger("tests.core-session").error(
        "core refresh failed",
        exc_info=(type(caught.value), caught.value, caught.value.__traceback__),
    )
    assert caught.value.error_code == "DIRECT_SESSION_UNAVAILABLE"
    assert synthetic_secret not in str(caught.value)
    assert synthetic_secret not in repr(caught.value)
    assert synthetic_secret not in caplog.text


@pytest.mark.parametrize(
    "failure",
    [
        DirectRequestError(
            "DIRECT_REQUEST_TIMEOUT",
            "synthetic-fund-direct-secret-marker",
        ),
        RuntimeError("synthetic-fund-generic-secret-marker"),
    ],
)
def test_fund_session_refresher_rebuilds_capture_errors_without_secret_tracebacks(
    failure: Exception,
) -> None:
    def fail_capture() -> dict[str, str]:
        raise failure

    with pytest.raises(DirectRequestError) as caught:
        FundAccountSessionRefresher(fail_capture)("main_fund_flow")

    rendered = "".join(
        traceback.format_exception(
            type(caught.value), caught.value, caught.value.__traceback__
        )
    )
    assert caught.value.error_code == (
        "DIRECT_REQUEST_TIMEOUT"
        if isinstance(failure, DirectRequestError)
        else "DIRECT_SESSION_UNAVAILABLE"
    )
    assert "synthetic-fund-direct-secret-marker" not in rendered
    assert "synthetic-fund-generic-secret-marker" not in rendered


def test_fund_session_refresher_rejects_an_untrusted_error_code() -> None:
    def fail_capture() -> dict[str, str]:
        raise DirectRequestError("secret=cookie-marker", "synthetic-secret-marker")

    with pytest.raises(DirectRequestError) as caught:
        FundAccountSessionRefresher(fail_capture)("main_fund_flow")

    assert caught.value.error_code == "DIRECT_SESSION_UNAVAILABLE"


def test_fund_session_capture_hooks_the_final_request_and_cleans_up(
    monkeypatch,
) -> None:
    calls: dict[str, object] = {}

    class FakeScript:
        def on(self, event: str, callback) -> None:
            assert event == "message"
            calls["callback"] = callback

        def load(self) -> None:
            calls["loaded"] = True
            calls["callback"](
                {
                    "type": "send",
                    "payload": {
                        "cookie": "user=private; sess_tk=private-ticket",
                        "user_agent": "app-user-agent",
                        "platform": "android",
                    },
                },
                None,
            )

        def unload(self) -> None:
            calls["unloaded"] = True

    class FakeSession:
        def create_script(self, source: str) -> FakeScript:
            calls["script_source"] = source
            return FakeScript()

        def detach(self) -> None:
            calls["detached"] = True

    class FakeDevice:
        def enumerate_applications(self):
            return [
                types.SimpleNamespace(identifier="com.hexin.plat.android", pid=9374)
            ]

        def attach(self, pid: int) -> FakeSession:
            calls["pid"] = pid
            return FakeSession()

    class FakeManager:
        def add_remote_device(self, endpoint: str) -> FakeDevice:
            calls["endpoint"] = endpoint
            return FakeDevice()

    monkeypatch.setitem(
        sys.modules,
        "frida",
        types.SimpleNamespace(get_device_manager=lambda: FakeManager()),
    )

    def trigger(endpoint: str, package: str, timeout: float, symbol: str, market: str):
        calls["trigger"] = (endpoint, package, timeout, symbol, market)
        return {"fund_flows": []}

    monkeypatch.setattr(
        FridaParsedValueSource,
        "_read_fund_runtime",
        staticmethod(trigger),
    )

    captured = capture_fund_http_session(
        "127.0.0.1:27042",
        timeout_seconds=3.5,
    )

    assert captured == {
        "cookie": "user=private; sess_tk=private-ticket",
        "user_agent": "app-user-agent",
        "platform": "android",
    }
    assert calls["endpoint"] == "127.0.0.1:27042"
    assert calls["pid"] == 9374
    assert calls["trigger"] == (
        "127.0.0.1:27042",
        "com.hexin.plat.android",
        3.5,
        "601872",
        "17",
    )
    assert "CallServerInterceptor" in calls["script_source"]
    assert calls["loaded"] is True
    assert calls["unloaded"] is True
    assert calls["detached"] is True


def test_fund_session_capture_rebuilds_trigger_error_without_secret_traceback(
    monkeypatch,
) -> None:
    secret = "synthetic-fund-trigger-secret-marker"

    class FakeScript:
        def on(self, _event: str, _callback) -> None: pass
        def load(self) -> None: pass
        def unload(self) -> None: pass

    class FakeSession:
        def create_script(self, _source: str) -> FakeScript: return FakeScript()
        def detach(self) -> None: pass

    class FakeDevice:
        def enumerate_applications(self):
            return [types.SimpleNamespace(identifier="com.hexin.plat.android", pid=9374)]
        def attach(self, _pid: int) -> FakeSession: return FakeSession()

    class FakeManager:
        def add_remote_device(self, _endpoint: str) -> FakeDevice: return FakeDevice()

    monkeypatch.setitem(
        sys.modules,
        "frida",
        types.SimpleNamespace(get_device_manager=lambda: FakeManager()),
    )
    monkeypatch.setattr(
        FridaParsedValueSource,
        "_read_fund_runtime",
        staticmethod(
            lambda *_args: (_ for _ in ()).throw(
                DirectRequestError("DIRECT_REQUEST_TIMEOUT", secret)
            )
        ),
    )

    with pytest.raises(DirectRequestError) as caught:
        capture_fund_http_session("127.0.0.1:27042", timeout_seconds=0)

    rendered = "".join(
        traceback.format_exception(
            type(caught.value), caught.value, caught.value.__traceback__
        )
    )
    assert caught.value.error_code == "DIRECT_REQUEST_TIMEOUT"
    assert secret not in rendered


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_core_capture_script_records_template_write_after_auth_write() -> None:
    harness = f"""
const transform = {{ call: () => null, implementation: null }};
const socketSend = {{ call: () => null, implementation: null }};
const write = {{ call: () => null, implementation: null }};
globalThis.rpc = {{ exports: {{}} }};
globalThis.Java = {{
  perform: (callback) => callback(),
  use: (name) => {{
    if (name === 'nsv') return {{ I: {{ overload: () => transform }} }};
    if (name === 'com.hexin.plat.android.net.Socket') {{
      return {{ send: {{ overload: () => socketSend }} }};
    }}
    if (name === 'java.net.SocketOutputStream') {{
      return {{ write: {{ overload: () => write }} }};
    }}
    return {{}};
  }}
}};
new Function({json.dumps(_FRIDA_CORE_SESSION_CAPTURE_SCRIPT)})();
const signed = (values) => values.map((value) => value > 127 ? value - 256 : value);
const authWrite = signed([253, 253, 253, 253, ...Array(20).fill(1)]);
const templateWrite = signed([253, 253, 253, 253, ...Array(24).fill(2)]);
socketSend.implementation([], 100, 0);
write.implementation(authWrite, 0, authWrite.length);
rpc.exports.arm('600519');
transform.implementation([54, 48, 48, 53, 49, 57], 700, 0, 0);
socketSend.implementation([], 700, 0);
write.implementation(templateWrite, 0, templateWrite.length);
process.stdout.write(JSON.stringify({{ packetCount: rpc.exports.packets().length }}));
"""

    completed = subprocess.run(
        ["node", "-e", harness],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {"packetCount": 1}


def test_core_session_capture_collects_complete_template_material(monkeypatch) -> None:
    calls: dict[str, object] = {}
    synthetic_request = core_template_packet_hex()

    class FakeExports:
        def capture(self, timeout: int) -> dict[str, str]:
            calls["timeout"] = timeout
            return {
                "server_ip": "60.204.184.46",
                "server_port": "9528",
                "auth_packet_hex": core_auth_packet_hex(),
                "base64_alphabet": CORE_BASE64_ALPHABET,
            }

        def arm(self, symbol: str) -> bool:
            calls["armed_symbol"] = symbol
            return True

        def packets(self) -> list[str]:
            return [synthetic_request]

    class FakeScript:
        exports_sync = FakeExports()

        def load(self) -> None:
            calls["loaded"] = True

        def unload(self) -> None:
            calls["unloaded"] = True

    class FakeSession:
        def create_script(self, source: str) -> FakeScript:
            calls["script_source"] = source
            return FakeScript()

        def detach(self) -> None:
            calls["detached"] = True

    class FakeDevice:
        def enumerate_applications(self):
            return [
                types.SimpleNamespace(identifier="com.hexin.plat.android", pid=14186)
            ]

        def attach(self, pid: int) -> FakeSession:
            calls["pid"] = pid
            return FakeSession()

    class FakeManager:
        def add_remote_device(self, endpoint: str) -> FakeDevice:
            calls["endpoint"] = endpoint
            return FakeDevice()

    monkeypatch.setitem(
        sys.modules,
        "frida",
        types.SimpleNamespace(get_device_manager=lambda: FakeManager()),
    )
    monkeypatch.setattr(
        FridaParsedValueSource,
        "_read_core_runtime",
        staticmethod(
            lambda endpoint, package, timeout, symbol, market: calls.update(
                trigger=(endpoint, package, timeout, symbol, market)
            )
        ),
    )

    captured = capture_core_session_material(
        "127.0.0.1:27043",
        timeout_seconds=3.5,
    )

    assert captured["server_port"] == "9528"
    assert captured["auth_packet_hex"] == core_auth_packet_hex()
    assert captured["template_symbol"] == "600519"
    assert json.loads(captured["request_packets_hex"]) == [synthetic_request]
    assert calls["timeout"] == 3500
    assert calls["endpoint"] == "127.0.0.1:27043"
    assert calls["pid"] == 14186
    assert calls["armed_symbol"] == "600519"
    assert calls["trigger"] == (
        "127.0.0.1:27043",
        "com.hexin.plat.android",
        3.5,
        "600519",
        "17",
    )
    assert "sendAuthRequest" in calls["script_source"]
    assert calls["loaded"] is True
    assert calls["unloaded"] is True
    assert calls["detached"] is True


def test_core_session_capture_preserves_explicit_template_request_error(
    monkeypatch,
) -> None:
    calls: dict[str, object] = {}

    class FakeExports:
        def capture(self, _timeout: int) -> dict[str, str]:
            return {
                "server_ip": "60.204.184.46",
                "server_port": "9528",
                "auth_packet_hex": core_auth_packet_hex(),
                "base64_alphabet": CORE_BASE64_ALPHABET,
            }

        def arm(self, _symbol: str) -> bool:
            return True

        def packets(self) -> list[str]:
            return []

    class FakeScript:
        exports_sync = FakeExports()

        def load(self) -> None:
            pass

        def unload(self) -> None:
            calls["unloaded"] = True

    class FakeSession:
        def create_script(self, _source: str) -> FakeScript:
            return FakeScript()

        def detach(self) -> None:
            calls["detached"] = True

    class FakeDevice:
        def enumerate_applications(self):
            return [
                types.SimpleNamespace(identifier="com.hexin.plat.android", pid=14186)
            ]

        def attach(self, _pid: int) -> FakeSession:
            return FakeSession()

    class FakeManager:
        def add_remote_device(self, _endpoint: str) -> FakeDevice:
            return FakeDevice()

    monkeypatch.setitem(
        sys.modules,
        "frida",
        types.SimpleNamespace(get_device_manager=lambda: FakeManager()),
    )

    def fail_template_request(*_args) -> None:
        raise DirectRequestError("DIRECT_REQUEST_TIMEOUT")

    monkeypatch.setattr(
        FridaParsedValueSource,
        "_read_core_runtime",
        staticmethod(fail_template_request),
    )

    with pytest.raises(DirectRequestError) as error:
        capture_core_session_material("127.0.0.1:27043", timeout_seconds=3.5)

    assert error.value.error_code == "DIRECT_REQUEST_TIMEOUT"
    assert calls["unloaded"] is True
    assert calls["detached"] is True


def test_core_session_capture_sanitizes_secret_bearing_template_exception(
    monkeypatch,
    caplog,
) -> None:
    synthetic_secret = "synthetic-template-secret-marker"

    class FakeExports:
        def capture(self, _timeout: int) -> dict[str, str]:
            return {
                "server_ip": "60.204.184.46",
                "server_port": "9528",
                "auth_packet_hex": core_auth_packet_hex(),
                "base64_alphabet": CORE_BASE64_ALPHABET,
            }

        def arm(self, _symbol: str) -> bool:
            return True

        def packets(self) -> list[str]:
            return []

    class FakeScript:
        exports_sync = FakeExports()

        def load(self) -> None:
            pass

        def unload(self) -> None:
            pass

    class FakeSession:
        def create_script(self, _source: str) -> FakeScript:
            return FakeScript()

        def detach(self) -> None:
            pass

    class FakeDevice:
        def enumerate_applications(self):
            return [
                types.SimpleNamespace(identifier="com.hexin.plat.android", pid=14186)
            ]

        def attach(self, _pid: int) -> FakeSession:
            return FakeSession()

    class FakeManager:
        def add_remote_device(self, _endpoint: str) -> FakeDevice:
            return FakeDevice()

    monkeypatch.setitem(
        sys.modules,
        "frida",
        types.SimpleNamespace(get_device_manager=lambda: FakeManager()),
    )

    def fail_template_request(*_args) -> None:
        raise RuntimeError(f"RPC failed with {synthetic_secret}")

    monkeypatch.setattr(
        FridaParsedValueSource,
        "_read_core_runtime",
        staticmethod(fail_template_request),
    )

    with pytest.raises(DirectRequestError) as caught:
        capture_core_session_material("127.0.0.1:27043", timeout_seconds=3.5)

    logging.getLogger("tests.core-capture").error(
        "core capture failed",
        exc_info=(type(caught.value), caught.value, caught.value.__traceback__),
    )
    assert caught.value.error_code == "DIRECT_PROTOCOL_HANDSHAKE_FAILED"
    assert synthetic_secret not in str(caught.value)
    assert synthetic_secret not in repr(caught.value)
    assert synthetic_secret not in caplog.text
