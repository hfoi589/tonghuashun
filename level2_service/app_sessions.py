"""Encrypted storage for App-authenticated market-data session material."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from threading import Event, RLock
from typing import Callable, Mapping, Protocol

from cryptography.fernet import Fernet, InvalidToken

from .parsed_values import (
    APP_PACKAGE,
    DirectRequestError,
    FridaParsedValueSource,
    _add_remote_frida_device,
    market_code_for_symbol,
    sanitized_direct_error_code,
)


ACCOUNT_ROLES = ("core_metrics", "main_fund_flow")

_CORE_SESSION_MATERIAL_KEYS = (
    "server_ip",
    "server_port",
    "auth_packet_hex",
    "base64_alphabet",
)
_CORE_TEMPLATE_MATERIAL_KEYS = (
    "template_symbol",
    "request_packets_hex",
    "macdfs_params",
)


_FRIDA_FUND_SESSION_CAPTURE_SCRIPT = r"""
Java.perform(function () {
  var CallServerInterceptor = Java.use('okhttp3.internal.http.CallServerInterceptor');
  var intercept = CallServerInterceptor.intercept.overload('okhttp3.Interceptor$Chain');
  intercept.implementation = function (chain) {
    try {
      var request = chain.request();
      var url = request.url();
      if (
        String(url.host()) === 'dataq.10jqka.com.cn'
        && String(url.encodedPath()) === '/fetch-data-server/fetch/v1/specific_data'
      ) {
        var cookie = request.header('Cookie');
        var userAgent = request.header('User-Agent');
        var platform = request.header('Platform');
        if (cookie !== null && userAgent !== null) {
          send({
            cookie: String(cookie),
            user_agent: String(userAgent),
            platform: platform === null ? 'android' : String(platform)
          });
        }
      }
    } catch (_) {}
    return intercept.call(this, chain);
  };
});
"""


_FRIDA_CORE_SESSION_CAPTURE_SCRIPT = r"""
var capture_auth = false;
var auth_packet_hex = null;
var target_symbol = null;
var target_frames = [];
var capture_target = false;
var target_packets = [];
var macdfs_params_json = null;
var macdfs_params_conflict = false;
var capture_macdfs_params = false;

function contains_text(buffer, text) {
  var length = buffer.length;
  var textLength = text.length;
  for (var offset = 0; offset <= length - textLength; offset += 1) {
    var ascii = true;
    for (var index = 0; index < textLength; index += 1) {
      var value = buffer[offset + index];
      if (value < 0) value += 256;
      if (value !== text.charCodeAt(index)) ascii = false;
    }
    if (ascii) return true;
  }
  for (var wideOffset = 0; wideOffset <= length - textLength * 2; wideOffset += 1) {
    var bigEndian = true;
    var littleEndian = true;
    for (var wideIndex = 0; wideIndex < textLength; wideIndex += 1) {
      var first = buffer[wideOffset + wideIndex * 2];
      var second = buffer[wideOffset + wideIndex * 2 + 1];
      if (first < 0) first += 256;
      if (second < 0) second += 256;
      if (first !== 0 || second !== text.charCodeAt(wideIndex)) bigEndian = false;
      if (first !== text.charCodeAt(wideIndex) || second !== 0) littleEndian = false;
    }
    if (bigEndian || littleEndian) return true;
  }
  return false;
}

function to_hex(buffer, offset, length) {
  var result = '';
  for (var index = 0; index < length; index += 1) {
    var value = buffer[offset + index];
    if (value < 0) value += 256;
    result += ('0' + value.toString(16)).slice(-2);
  }
  return result;
}

Java.perform(function () {
  try {
    var MacdProcessor = Java.use('wmg');
    var calculateMacd = MacdProcessor.E.overload(
      'qxg',
      'com.hexin.android.finance_chart.domain.CurveLineParser$EQCurveLineDesc'
    );
    calculateMacd.implementation = function (indicator, descriptor) {
      try {
        if (!capture_macdfs_params || target_symbol === null) {
          return calculateMacd.call(this, indicator, descriptor);
        }
        var source = indicator === null ? null : indicator.g();
        var sourceSymbol = source === null ? null : String(source.getExtData(4));
        if (sourceSymbol !== target_symbol) {
          return calculateMacd.call(this, indicator, descriptor);
        }
        var params = descriptor === null ? null : descriptor.getTechParam();
        if (params !== null && params.length >= 3) {
          var candidate = JSON.stringify([
            Number(params[0]),
            Number(params[1]),
            Number(params[2])
          ]);
          if (macdfs_params_json === null) macdfs_params_json = candidate;
          else if (macdfs_params_json !== candidate) macdfs_params_conflict = true;
        }
      } catch (_) {}
      return calculateMacd.call(this, indicator, descriptor);
    };
  } catch (_) {}
  var CommunicationManager = Java.use('nsv');
  var transform = CommunicationManager.I.overload('[B', 'int', 'int', 'int');
  transform.implementation = function (buffer, frame, sequence, sessionType) {
    if (target_symbol !== null && contains_text(buffer, target_symbol)) {
      target_frames.push(Number(frame));
    }
    return transform.call(this, buffer, frame, sequence, sessionType);
  };
  var Socket = Java.use('com.hexin.plat.android.net.Socket');
  var socketSend = Socket.send.overload('[B', 'int', 'int');
  socketSend.implementation = function (buffer, frame, sequence) {
    if (Number(frame) === 100 && Number(sequence) === 0) {
      capture_auth = true;
    }
    if (target_frames.length && Number(frame) === target_frames[0]) {
      target_frames.shift();
      capture_target = true;
    }
    return socketSend.call(this, buffer, frame, sequence);
  };

  var OutputStream = Java.use('java.net.SocketOutputStream');
  var write = OutputStream.write.overload('[B', 'int', 'int');
  write.implementation = function (buffer, offset, length) {
    if (length > 13) {
      var valid_header = true;
      for (var index = 0; index < 4; index += 1) {
        var value = buffer[offset + index];
        if (value < 0) value += 256;
        if (value !== 253) valid_header = false;
      }
      if (valid_header && capture_auth) {
        auth_packet_hex = to_hex(buffer, offset, length);
        capture_auth = false;
      }
      if (valid_header && capture_target && length > 20) {
        target_packets.push(to_hex(buffer, offset, length));
        capture_target = false;
      }
    }
    return write.call(this, buffer, offset, length);
  };
});

rpc.exports = {
  capture: function (timeoutMilliseconds) {
    return new Promise(function (resolve) {
      Java.perform(function () {
        var result = {};
        try {
          var Service = Java.use('com.hexin.plat.android.CommunicationService');
          var service = Service.getCommunicationService();
          result.server_ip = String(service.getServerIp());
          result.server_port = String(service.getConnectPort());
          var Manager = Java.use('lkv');
          var StringClass = Java.use('java.lang.String');
          var bytes = [];
          for (var index = 0; index < 64; index += 1) {
            bytes.push((index << 2) & 255);
            bytes.push(0);
            bytes.push(0);
          }
          var encoded = String(StringClass.$new(
            Manager.g().d(Java.array('byte', bytes))
          ));
          var alphabet = '';
          for (var position = 0; position < 64; position += 1) {
            alphabet += encoded.charAt(position * 4);
          }
          result.base64_alphabet = alphabet;
          try {
            var Registry = Java.use('rwg');
            var IntegerClass = Java.use('java.lang.Integer');
            var CurveXmlConfig = Java.use(
              'com.hexin.android.finance_chart.domain.CurveXmlConfig'
            );
            var configMap = Registry.i()._k.value.c();
            var rawConfig = configMap.get(IntegerClass.valueOf(2));
            if (rawConfig !== null) {
              var config = Java.cast(rawConfig, CurveXmlConfig);
              var tech = config.getUnit(2).get(IntegerClass.valueOf(7051));
              if (tech !== null) {
                var lines = tech.getLines();
                for (var lineIndex = 0; lineIndex < lines.size(); lineIndex += 1) {
                  var descriptor = lines.get(lineIndex).getLineDesc();
                  var params = descriptor === null ? null : descriptor.getTechParam();
                  if (params !== null && params.length >= 3) {
                    result.macdfs_params = JSON.stringify([
                      Number(params[0]),
                      Number(params[1]),
                      Number(params[2])
                    ]);
                    break;
                  }
                }
              }
            }
          } catch (_) {}
          try {
            Java.use('msv').INSTANCE.value.sendAuthRequest(0);
          } catch (_) {}
        } catch (error) {
          result.error = String(error);
        }
        setTimeout(function () {
          result.auth_packet_hex = auth_packet_hex;
          resolve(result);
        }, Math.max(1000, Number(timeoutMilliseconds)));
      });
    });
  },
  arm: function (symbol) {
    target_symbol = String(symbol);
    target_frames = [];
    target_packets = [];
    capture_target = false;
    macdfs_params_json = null;
    macdfs_params_conflict = false;
    capture_macdfs_params = true;
    return true;
  },
  packets: function () {
    return target_packets.slice();
  },
  macdfsparams: function () {
    return new Promise(function (resolve) {
      Java.perform(function () {
        var result = null;
        try {
          var Registry = Java.use('rwg');
          var IntegerClass = Java.use('java.lang.Integer');
          var CurveXmlConfig = Java.use(
            'com.hexin.android.finance_chart.domain.CurveXmlConfig'
          );
          var configMap = Registry.i()._k.value.c();
          var rawConfig = configMap.get(IntegerClass.valueOf(2));
          if (rawConfig !== null) {
            var config = Java.cast(rawConfig, CurveXmlConfig);
            var tech = config.getUnit(2).get(IntegerClass.valueOf(7051));
            if (tech !== null) {
              var lines = tech.getLines();
              for (var lineIndex = 0; lineIndex < lines.size(); lineIndex += 1) {
                var descriptor = lines.get(lineIndex).getLineDesc();
                var params = descriptor === null ? null : descriptor.getTechParam();
                if (params !== null && params.length >= 3) {
                  result = JSON.stringify([
                    Number(params[0]),
                    Number(params[1]),
                    Number(params[2])
                  ]);
                  break;
                }
              }
            }
          }
        } catch (_) {}
        capture_macdfs_params = false;
        if (
          macdfs_params_conflict
          || (macdfs_params_json !== null && result !== null && macdfs_params_json !== result)
        ) {
          resolve('__CONFLICT__');
          return;
        }
        resolve(macdfs_params_json !== null ? macdfs_params_json : result);
      });
    });
  }
};
"""


def _validated_role(role: str) -> str:
    normalized = str(role).strip()
    if normalized not in ACCOUNT_ROLES:
        raise ValueError(f"unknown account role: {normalized}")
    return normalized


def _validated_core_material(captured: Mapping[str, object]) -> dict[str, str]:
    normalized = {
        key: "" if captured.get(key) is None else str(captured.get(key, "")).strip()
        for key in (
            *_CORE_SESSION_MATERIAL_KEYS,
            *_CORE_TEMPLATE_MATERIAL_KEYS,
        )
    }
    if any(
        not normalized[key]
        for key in _CORE_SESSION_MATERIAL_KEYS
        if key != "base64_alphabet"
    ):
        raise DirectRequestError("DIRECT_SESSION_UNAVAILABLE")
    if not normalized["base64_alphabet"]:
        raise DirectRequestError("DIRECT_PROTOCOL_HANDSHAKE_FAILED")
    if any(not normalized[key] for key in _CORE_TEMPLATE_MATERIAL_KEYS):
        raise DirectRequestError("DIRECT_PROTOCOL_HANDSHAKE_FAILED")
    try:
        port = int(normalized["server_port"])
        auth_packet = bytes.fromhex(normalized["auth_packet_hex"])
    except ValueError:
        raise DirectRequestError("DIRECT_SESSION_UNAVAILABLE") from None
    if not 1 <= port <= 65535 or not auth_packet:
        raise DirectRequestError("DIRECT_SESSION_UNAVAILABLE")
    alphabet = normalized["base64_alphabet"]
    if len(alphabet) != 64 or len(set(alphabet)) != 64:
        raise DirectRequestError("DIRECT_PROTOCOL_HANDSHAKE_FAILED")

    macdfs_params = normalized["macdfs_params"]
    if macdfs_params:
        try:
            parsed_params = json.loads(macdfs_params)
        except json.JSONDecodeError:
            raise DirectRequestError(
                "DIRECT_PROTOCOL_HANDSHAKE_FAILED"
            ) from None
        if (
            not isinstance(parsed_params, list)
            or len(parsed_params) != 3
            or any(
                isinstance(value, bool)
                or not isinstance(value, int)
                or value <= 0
                or value > 1000
                for value in parsed_params
            )
        ):
            raise DirectRequestError("DIRECT_PROTOCOL_HANDSHAKE_FAILED")
        normalized["macdfs_params"] = json.dumps(
            parsed_params,
            separators=(",", ":"),
        )

    template_symbol = normalized["template_symbol"]
    try:
        from .direct_market import patch_core_packet_symbol, validate_core_auth_packet

        validate_core_auth_packet(auth_packet)

        if len(template_symbol) != 6 or not template_symbol.isdigit():
            raise ValueError("invalid template symbol")
        market_code_for_symbol(template_symbol)
        raw_packets = json.loads(normalized["request_packets_hex"])
        if not isinstance(raw_packets, list) or not raw_packets:
            raise ValueError("missing request packets")
        packets: list[str] = []
        for raw_packet in raw_packets:
            if not isinstance(raw_packet, str):
                raise ValueError("invalid request packet")
            packet = raw_packet.strip()
            packet_bytes = bytes.fromhex(packet)
            if not packet or not packet_bytes:
                raise ValueError("invalid request packet")
            patch_core_packet_symbol(
                packet_bytes,
                template_symbol,
                template_symbol,
                alphabet=alphabet,
            )
            packets.append(packet)
    except (DirectRequestError, json.JSONDecodeError, TypeError, ValueError):
        raise DirectRequestError("DIRECT_PROTOCOL_HANDSHAKE_FAILED") from None
    normalized["request_packets_hex"] = json.dumps(
        packets,
        separators=(",", ":"),
    )
    return normalized


@dataclass(frozen=True)
class AccountSessionBundle:
    """Opaque credentials captured after a human completes the App login flow."""

    role: str
    cookie: str = field(repr=False)
    user_agent: str = field(repr=False)
    platform: str
    updated_at: datetime
    device_profile: dict[str, str] = field(default_factory=dict, repr=False)
    core_material: dict[str, str] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        _validated_role(self.role)
        if self.role == "main_fund_flow" and not self.cookie.strip():
            raise ValueError("session cookie must not be empty")
        if self.role == "main_fund_flow" and not self.user_agent.strip():
            raise ValueError("session user agent must not be empty")
        if self.updated_at.tzinfo is None:
            raise ValueError("session updated_at must be timezone-aware")

    def _as_storage(self) -> dict[str, object]:
        return {
            "role": self.role,
            "cookie": self.cookie,
            "user_agent": self.user_agent,
            "platform": self.platform,
            "updated_at": self.updated_at.isoformat(),
            "device_profile": dict(self.device_profile),
            "core_material": dict(self.core_material),
        }

    @classmethod
    def _from_storage(cls, payload: dict[str, object]) -> "AccountSessionBundle":
        return cls(
            role=str(payload["role"]),
            cookie=str(payload["cookie"]),
            user_agent=str(payload["user_agent"]),
            platform=str(payload["platform"]),
            updated_at=datetime.fromisoformat(str(payload["updated_at"])),
            device_profile={
                str(key): str(value)
                for key, value in dict(payload.get("device_profile", {})).items()
            },
            core_material={
                str(key): str(value)
                for key, value in dict(payload.get("core_material", {})).items()
            },
        )


@dataclass(frozen=True)
class AccountSessionStatus:
    role: str
    state: str
    updated_at: datetime | None
    error_code: str | None

    def as_public(self) -> dict[str, object]:
        return {
            "role": self.role,
            "state": self.state,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "error_code": self.error_code,
        }


class SessionProvider(Protocol):
    def get(self, role: str) -> AccountSessionBundle | None: ...

    def put(self, bundle: AccountSessionBundle) -> None: ...

    def status(self, role: str) -> AccountSessionStatus: ...

    def mark_error(self, role: str, error_code: str) -> None: ...


class FundAccountSessionRefresher:
    """Turn a one-shot App network capture into an encrypted-session bundle."""

    def __init__(
        self,
        capture: Callable[[], Mapping[str, str]],
        *,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.capture = capture
        self.now = now

    def __call__(self, role: str) -> AccountSessionBundle:
        if role != "main_fund_flow":
            raise ValueError("fund session refresher only supports main_fund_flow")
        try:
            captured = dict(self.capture())
        except DirectRequestError as error:
            raise DirectRequestError(
                sanitized_direct_error_code(
                    error.error_code, "DIRECT_SESSION_UNAVAILABLE"
                )
            ) from None
        except Exception:
            raise DirectRequestError("DIRECT_SESSION_UNAVAILABLE") from None
        cookie = str(captured.get("cookie", "")).strip()
        user_agent = str(captured.get("user_agent", "")).strip()
        platform = str(captured.get("platform", "android")).strip() or "android"
        if not cookie or not user_agent:
            raise DirectRequestError("DIRECT_SESSION_UNAVAILABLE")
        return AccountSessionBundle(
            role=role,
            cookie=cookie,
            user_agent=user_agent,
            platform=platform,
            updated_at=self.now(),
        )


class CoreAccountSessionRefresher:
    """Capture the opaque 9528 authentication material after human login."""

    REQUIRED_MATERIAL = (*_CORE_SESSION_MATERIAL_KEYS, *_CORE_TEMPLATE_MATERIAL_KEYS)

    def __init__(
        self,
        capture: Callable[[], Mapping[str, str]],
        *,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.capture = capture
        self.now = now

    def __call__(self, role: str) -> AccountSessionBundle:
        if role != "core_metrics":
            raise ValueError("core session refresher only supports core_metrics")
        try:
            captured = dict(self.capture())
        except DirectRequestError as error:
            raise DirectRequestError(
                sanitized_direct_error_code(
                    error.error_code, "DIRECT_SESSION_UNAVAILABLE"
                )
            ) from None
        except Exception:
            raise DirectRequestError("DIRECT_SESSION_UNAVAILABLE") from None
        core_material = _validated_core_material(captured)
        return AccountSessionBundle(
            role=role,
            cookie=str(captured.get("cookie", "")).strip(),
            user_agent=str(captured.get("user_agent", "")).strip(),
            platform=str(captured.get("platform", "android")).strip() or "android",
            updated_at=self.now(),
            core_material=core_material,
        )


def capture_fund_http_session(
    endpoint: str,
    *,
    package: str = APP_PACKAGE,
    timeout_seconds: float = 10.0,
    trigger_symbol: str = "601872",
) -> Mapping[str, str]:
    """Capture final OkHttp headers during one App read-only query."""

    import frida  # type: ignore[import-not-found]

    device = _add_remote_frida_device(frida, endpoint)
    application = next(
        (
            item
            for item in device.enumerate_applications()
            if item.identifier == package and item.pid
        ),
        None,
    )
    if application is None:
        raise DirectRequestError(
            "DIRECT_SESSION_UNAVAILABLE", "THS process is not running"
        )
    session = device.attach(application.pid)
    script = session.create_script(_FRIDA_FUND_SESSION_CAPTURE_SCRIPT)
    captured: dict[str, str] = {}
    received = Event()

    def on_message(message: Mapping[str, object], _data: object) -> None:
        if message.get("type") != "send" or captured:
            return
        payload = message.get("payload")
        if not isinstance(payload, Mapping):
            return
        cookie = str(payload.get("cookie", "")).strip()
        user_agent = str(payload.get("user_agent", "")).strip()
        if not cookie or not user_agent:
            return
        captured.update(
            {
                "cookie": cookie,
                "user_agent": user_agent,
                "platform": str(payload.get("platform", "android")).strip()
                or "android",
            }
        )
        received.set()

    script.on("message", on_message)
    trigger_error: Exception | None = None
    try:
        script.load()
        try:
            FridaParsedValueSource._read_fund_runtime(
                endpoint,
                package,
                timeout_seconds,
                trigger_symbol,
                market_code_for_symbol(trigger_symbol),
            )
        except Exception as error:
            trigger_error = error
        received.wait(timeout=max(0.1, timeout_seconds))
        if not captured:
            if isinstance(trigger_error, DirectRequestError):
                raise DirectRequestError(
                    sanitized_direct_error_code(
                        trigger_error.error_code, "DIRECT_SESSION_UNAVAILABLE"
                    )
                ) from None
            raise DirectRequestError("DIRECT_SESSION_UNAVAILABLE") from None
        return captured
    finally:
        try:
            script.unload()
        finally:
            session.detach()


def capture_core_session_material(
    endpoint: str,
    *,
    package: str = APP_PACKAGE,
    timeout_seconds: float = 10.0,
    template_symbol: str = "600519",
) -> Mapping[str, str]:
    """Capture the reusable 9528 authentication packet after a human App login."""

    import frida  # type: ignore[import-not-found]

    device = _add_remote_frida_device(frida, endpoint)
    application = next(
        (
            item
            for item in device.enumerate_applications()
            if item.identifier == package and item.pid
        ),
        None,
    )
    if application is None:
        raise DirectRequestError(
            "DIRECT_SESSION_UNAVAILABLE", "THS process is not running"
        )
    session = device.attach(application.pid)
    script = session.create_script(_FRIDA_CORE_SESSION_CAPTURE_SCRIPT)
    try:
        script.load()
        try:
            captured = script.exports_sync.capture(
                max(1, int(timeout_seconds * 1000))
            )
        except DirectRequestError as error:
            raise DirectRequestError(
                sanitized_direct_error_code(
                    error.error_code, "DIRECT_SESSION_UNAVAILABLE"
                )
            ) from None
        except Exception:
            raise DirectRequestError("DIRECT_SESSION_UNAVAILABLE") from None
        if not isinstance(captured, Mapping):
            raise DirectRequestError("DIRECT_SESSION_UNAVAILABLE")
        result: dict[str, object] = dict(captured)
        try:
            if script.exports_sync.arm(template_symbol) is not True:
                raise DirectRequestError("DIRECT_PROTOCOL_HANDSHAKE_FAILED")
            FridaParsedValueSource._read_core_runtime(
                endpoint,
                package,
                timeout_seconds,
                template_symbol,
                market_code_for_symbol(template_symbol),
            )
            result["template_symbol"] = template_symbol
            result["request_packets_hex"] = json.dumps(
                script.exports_sync.packets(),
                separators=(",", ":"),
            )
            try:
                macdfs_params = script.exports_sync.macdfsparams()
            except Exception:
                macdfs_params = None
            if not macdfs_params:
                raise DirectRequestError("DIRECT_PROTOCOL_HANDSHAKE_FAILED")
            result["macdfs_params"] = str(macdfs_params)
        except DirectRequestError as error:
            raise DirectRequestError(
                sanitized_direct_error_code(
                    error.error_code, "DIRECT_PROTOCOL_HANDSHAKE_FAILED"
                )
            ) from None
        except Exception:
            raise DirectRequestError("DIRECT_PROTOCOL_HANDSHAKE_FAILED") from None
        return _validated_core_material(result)
    finally:
        try:
            script.unload()
        finally:
            session.detach()


class EncryptedFileSessionProvider:
    """Persist one Fernet-encrypted credential bundle per account role."""

    def __init__(self, root: Path, encryption_key: str) -> None:
        try:
            self._fernet = Fernet(encryption_key.encode("ascii"))
        except (UnicodeEncodeError, ValueError) as error:
            raise ValueError(
                "THS_SESSION_ENCRYPTION_KEY must be a URL-safe base64 Fernet key"
            ) from error
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        self._errors: dict[str, str] = {}
        self._lock = RLock()

    def _path(self, role: str) -> Path:
        return self.root / f"{_validated_role(role)}.session"

    def get(self, role: str) -> AccountSessionBundle | None:
        path = self._path(role)
        with self._lock:
            if not path.is_file():
                return None
            try:
                plaintext = self._fernet.decrypt(path.read_bytes())
                payload = json.loads(plaintext.decode("utf-8"))
                bundle = AccountSessionBundle._from_storage(payload)
            except (
                InvalidToken,
                OSError,
                UnicodeDecodeError,
                ValueError,
                KeyError,
                TypeError,
            ):
                self._errors[role] = "DIRECT_SESSION_UNAVAILABLE"
                return None
            if bundle.role != role:
                self._errors[role] = "DIRECT_SESSION_UNAVAILABLE"
                return None
            if role == "core_metrics":
                try:
                    validated = _validated_core_material(bundle.core_material)
                except DirectRequestError as error:
                    self._errors[role] = sanitized_direct_error_code(
                        error.error_code,
                        "DIRECT_SESSION_UNAVAILABLE",
                    )
                    return None
                bundle = AccountSessionBundle(
                    role=bundle.role,
                    cookie=bundle.cookie,
                    user_agent=bundle.user_agent,
                    platform=bundle.platform,
                    updated_at=bundle.updated_at,
                    device_profile=dict(bundle.device_profile),
                    core_material=validated,
                )
            return bundle

    def put(self, bundle: AccountSessionBundle) -> None:
        if bundle.role == "core_metrics":
            validated = _validated_core_material(bundle.core_material)
            bundle = AccountSessionBundle(
                role=bundle.role,
                cookie=bundle.cookie,
                user_agent=bundle.user_agent,
                platform=bundle.platform,
                updated_at=bundle.updated_at,
                device_profile=dict(bundle.device_profile),
                core_material=validated,
            )
        path = self._path(bundle.role)
        encoded = json.dumps(
            bundle._as_storage(),
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        ciphertext = self._fernet.encrypt(encoded)
        with self._lock:
            with NamedTemporaryFile(
                dir=self.root, prefix=f".{bundle.role}.", delete=False
            ) as handle:
                temporary = Path(handle.name)
                os.chmod(temporary, 0o600)
                handle.write(ciphertext)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            os.chmod(path, 0o600)
            self._errors.pop(bundle.role, None)

    def status(self, role: str) -> AccountSessionStatus:
        normalized = _validated_role(role)
        bundle = self.get(normalized)
        error_code = self._errors.get(normalized)
        if error_code is not None:
            state = "ERROR"
        elif bundle is None:
            state = "MISSING"
        else:
            state = "READY"
        return AccountSessionStatus(
            role=normalized,
            state=state,
            updated_at=bundle.updated_at if bundle is not None else None,
            error_code=error_code,
        )

    def mark_error(self, role: str, error_code: str) -> None:
        normalized = _validated_role(role)
        with self._lock:
            self._errors[normalized] = str(error_code)
