from __future__ import annotations

import json

from level2_service.research_push import PushDispatcher
from level2_service.safe_http import SafeHttpResponse


class Transport:
    def __init__(self, base_url, requests):
        self.base_url = base_url
        self.requests = requests

    def request(self, request, timeout_seconds):
        self.requests.append((self.base_url, request))
        if request.full_url.endswith("/cgi-bin/gettoken?corpid=corp&corpsecret=secret"):
            return SafeHttpResponse(200, b'{"errcode":0,"access_token":"access"}')
        return SafeHttpResponse(200, b'{"ok":true,"errcode":0}')


def test_dispatcher_sends_bark_sc3_and_wecom_without_returning_secrets():
    requests = []
    dispatcher = PushDispatcher(
        transport_factory=lambda base, **_kwargs: Transport(base, requests)
    )
    config = {
        "enabled": True,
        "bark_groups": [{"id": "phone", "name": "手机", "base_url": "https://api.day.app", "device_key": "device"}],
        "sc3_bot": {"enabled": True, "base_url": "https://bot.example", "token": "token", "chat_id": "7", "parse_mode": "markdown", "silent": False},
        "wecom": {"enabled": True, "api_base_url": "https://qyapi.weixin.qq.com", "corp_id": "corp", "corp_secret": "secret", "agent_id": 1, "to_user": "@all"},
    }

    result = dispatcher.send(config, "标题", "正文")

    assert result["ok"] is True
    assert set(result["channels"]) == {"bark", "sc3_bot", "wecom"}
    assert "device" not in json.dumps(result, ensure_ascii=False)
    assert "token" not in json.dumps(result, ensure_ascii=False)
    assert "secret" not in json.dumps(result, ensure_ascii=False)
    assert any("/device/" in request.full_url for _base, request in requests)
    assert any(request.full_url.endswith("/bottoken/sendMessage") for _base, request in requests)
    assert any("/cgi-bin/message/send?access_token=access" in request.full_url for _base, request in requests)
