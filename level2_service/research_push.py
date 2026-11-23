"""Server-side push channel adapters for monitoring rule notifications."""

from __future__ import annotations

import json
import re
from typing import Any, Callable
from urllib.parse import quote, urlencode
from urllib.request import Request

from .safe_http import SafeHttpTransport


def _resolve_sc3_chat_id(raw_chat_id: object, token: str) -> str | int:
    text = str(raw_chat_id or "").strip()
    if text:
        try:
            numeric = int(float(text))
            return numeric if numeric > 0 else text
        except (TypeError, ValueError):
            return text
    match = re.match(r"^sctp(\d+)t", token.strip())
    return int(match.group(1)) if match else ""


class PushDispatcher:
    def __init__(
        self,
        *,
        transport_factory: Callable[..., object] = SafeHttpTransport,
        timeout_seconds: float = 10,
    ) -> None:
        self.transport_factory = transport_factory
        self.timeout_seconds = timeout_seconds

    def _transport(self, base_url: str):
        return self.transport_factory(base_url, max_body_bytes=64 * 1024)

    def _response_json(self, response: object) -> dict[str, Any]:
        status = getattr(response, "status", 500)
        if not 200 <= int(status) < 300:
            raise RuntimeError("PUSH_HTTP_FAILED")
        try:
            payload = json.loads(getattr(response, "body").decode("utf-8"))
        except (AttributeError, UnicodeDecodeError, json.JSONDecodeError):
            raise RuntimeError("PUSH_RESPONSE_INVALID") from None
        return payload if isinstance(payload, dict) else {}

    def _bark(self, group: dict[str, Any], title: str, body: str) -> None:
        device_key = str(group.get("device_key") or "").strip().strip("/")
        if not device_key:
            raise RuntimeError("BARK_NOT_CONFIGURED")
        path = f"/{quote(device_key, safe='')}/{quote(title, safe='')}/{quote(body, safe='')}"
        response = self._transport(str(group.get("base_url") or "https://api.day.app")).request(
            Request(str(group.get("base_url") or "https://api.day.app").rstrip("/") + path),
            self.timeout_seconds,
        )
        if not 200 <= int(response.status) < 300:
            raise RuntimeError("BARK_SEND_FAILED")

    def _sc3(self, config: dict[str, Any], title: str, body: str) -> None:
        token = str(config.get("token") or "").strip()
        chat_id = _resolve_sc3_chat_id(config.get("chat_id"), token)
        if not token or not chat_id:
            raise RuntimeError("SC3_NOT_CONFIGURED")
        base = str(config.get("base_url") or "https://bot-go.apijia.cn").rstrip("/")
        payload = {
            "chat_id": chat_id,
            "text": f"{title}\n{body}" if body else title,
            "parse_mode": str(config.get("parse_mode") or "markdown"),
            "silent": bool(config.get("silent", False)),
        }
        response = self._transport(base).request(
            Request(
                f"{base}/bot{quote(token, safe='')}/sendMessage",
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                headers={"Content-Type": "application/json; charset=utf-8"},
                method="POST",
            ),
            self.timeout_seconds,
        )
        decoded = self._response_json(response)
        if decoded.get("ok") is False:
            raise RuntimeError("SC3_SEND_FAILED")

    def _wecom(self, config: dict[str, Any], title: str, body: str) -> None:
        corp_id = str(config.get("corp_id") or "").strip()
        corp_secret = str(config.get("corp_secret") or "").strip()
        if not corp_id or not corp_secret:
            raise RuntimeError("WECOM_NOT_CONFIGURED")
        base = str(config.get("api_base_url") or "https://qyapi.weixin.qq.com").rstrip("/")
        transport = self._transport(base)
        token_response = transport.request(
            Request(
                f"{base}/cgi-bin/gettoken?{urlencode({'corpid': corp_id, 'corpsecret': corp_secret})}"
            ),
            self.timeout_seconds,
        )
        token_payload = self._response_json(token_response)
        if int(token_payload.get("errcode", -1)) != 0 or not token_payload.get("access_token"):
            raise RuntimeError("WECOM_TOKEN_FAILED")
        content = f"{title}\n{body}" if body else title
        message: dict[str, Any] = {
            "touser": str(config.get("to_user") or "@all"),
            "msgtype": "text",
            "agentid": int(config.get("agent_id") or 0),
            "text": {"content": content},
            "safe": 0,
            "enable_duplicate_check": 1,
            "duplicate_check_interval": 1800,
        }
        to_party = str(config.get("to_party") or "").strip()
        to_tag = str(config.get("to_tag") or "").strip()
        if to_party:
            message["toparty"] = to_party
        if to_tag:
            message["totag"] = to_tag
        response = transport.request(
            Request(
                f"{base}/cgi-bin/message/send?{urlencode({'access_token': token_payload['access_token']})}",
                data=json.dumps(message, ensure_ascii=False).encode("utf-8"),
                headers={"Content-Type": "application/json; charset=utf-8"},
                method="POST",
            ),
            self.timeout_seconds,
        )
        payload = self._response_json(response)
        if int(payload.get("errcode", -1)) != 0:
            raise RuntimeError("WECOM_SEND_FAILED")

    def send(
        self,
        config: dict[str, Any],
        title: str,
        body: str,
        *,
        only_channel: str | None = None,
    ) -> dict[str, Any]:
        channels: dict[str, dict[str, Any]] = {}
        errors: dict[str, str] = {}
        target = (only_channel or "").strip().lower() or None

        if target in {None, "bark"} and (target == "bark" or config.get("enabled")):
            groups = [
                g for g in (config.get("bark_groups") or [])
                if isinstance(g, dict) and (target == "bark" or str(g.get("device_key") or "").strip())
            ]
            if not groups and target == "bark":
                groups = list(config.get("bark_groups") or [])
            if groups:
                sent = 0
                for group in groups:
                    group_id = str(group.get("id") or "bark")
                    try:
                        self._bark(group, title, body)
                        sent += 1
                    except Exception:
                        errors[f"bark:{group_id}"] = "BARK_SEND_FAILED"
                channels["bark"] = {"sent": sent, "total": len(groups)}

        sc3 = config.get("sc3_bot") or {}
        if target in {None, "sc3_bot"} and (target == "sc3_bot" or sc3.get("enabled")):
            try:
                self._sc3(sc3, title, body)
                channels["sc3_bot"] = {"sent": 1}
            except Exception:
                errors["sc3_bot"] = "SC3_SEND_FAILED"

        wecom = config.get("wecom") or {}
        if target in {None, "wecom"} and (target == "wecom" or wecom.get("enabled")):
            try:
                self._wecom(wecom, title, body)
                channels["wecom"] = {"sent": 1}
            except Exception:
                errors["wecom"] = "WECOM_SEND_FAILED"

        if not channels and not errors:
            raise ValueError("no push channel is enabled")
        return {"ok": not errors, "channels": channels, "errors": errors}
