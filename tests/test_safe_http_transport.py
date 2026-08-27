from __future__ import annotations

from contextlib import contextmanager
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from urllib.request import Request

import pytest

from level2_service.device_lifecycle import DeviceLifecycleClient, DeviceLifecycleError
from tests.test_macos_one_click_deploy import _load_macos_deploy


@contextmanager
def serve(handler: type[BaseHTTPRequestHandler]):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


class QuietHandler(BaseHTTPRequestHandler):
    def log_message(self, _format: str, *_args: object) -> None:
        return None


def test_lifecycle_client_ignores_host_proxy_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ambient proxy must never receive the lifecycle bearer capability."""
    proxy_requests: list[tuple[str, str | None]] = []

    class Target(QuietHandler):
        def do_GET(self) -> None:
            body = b'{"devices":[]}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    class Proxy(QuietHandler):
        def do_GET(self) -> None:
            proxy_requests.append((self.path, self.headers.get("Authorization")))
            self.send_response(502)
            self.end_headers()

    with serve(Target) as target, serve(Proxy) as proxy:
        monkeypatch.setenv("HTTP_PROXY", f"http://127.0.0.1:{proxy.server_port}")
        monkeypatch.setenv("http_proxy", f"http://127.0.0.1:{proxy.server_port}")
        monkeypatch.delenv("NO_PROXY", raising=False)
        monkeypatch.delenv("no_proxy", raising=False)

        statuses = DeviceLifecycleClient(
            f"http://127.0.0.1:{target.server_port}", "host-secret"
        ).devices()

    assert statuses == ()
    assert proxy_requests == []


def test_lifecycle_client_rejects_redirect_without_forwarding_authorization() -> None:
    """A redirect must not move the broker token to another origin."""
    redirected_authorization: list[str | None] = []

    class RedirectTarget(QuietHandler):
        def do_GET(self) -> None:
            redirected_authorization.append(self.headers.get("Authorization"))
            body = b'{"devices":[]}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    with serve(RedirectTarget) as target:
        location = f"http://127.0.0.1:{target.server_port}/stolen"

        class RedirectSource(QuietHandler):
            def do_GET(self) -> None:
                self.send_response(302)
                self.send_header("Location", location)
                self.end_headers()

        with serve(RedirectSource) as source:
            client = DeviceLifecycleClient(
                f"http://127.0.0.1:{source.server_port}", "host-secret"
            )
            with pytest.raises(DeviceLifecycleError) as caught:
                client.devices()

    assert caught.value.error_code == "DEVICE_LIFECYCLE_FAILED"
    assert caught.value.__cause__ is None
    assert redirected_authorization == []


class FinalUrlResponse:
    status = 200

    def __init__(self, body: bytes, final_url: str) -> None:
        self._body = body
        self._final_url = final_url

    def read(self, size: int = -1) -> bytes:
        return self._body if size < 0 else self._body[:size]

    def geturl(self) -> str:
        return self._final_url

    def close(self) -> None:
        return None


@pytest.mark.parametrize(
    "final_url",
    [
        "http://localhost:18765/v1/devices",
        "http://127.0.0.1:18766/v1/devices",
        "https://127.0.0.1:18765/v1/devices",
    ],
)
def test_lifecycle_client_rejects_wrong_final_origin(final_url: str) -> None:
    """Response provenance must match the configured scheme, host, and effective port."""
    client = DeviceLifecycleClient(
        "http://127.0.0.1:18765",
        "host-secret",
        opener=lambda _request, _timeout: FinalUrlResponse(
            b'{"devices":[]}', final_url
        ),
    )

    with pytest.raises(DeviceLifecycleError) as caught:
        client.devices()

    assert caught.value.error_code == "DEVICE_LIFECYCLE_UNAVAILABLE"
    assert caught.value.__cause__ is None


def test_lifecycle_client_caps_response_before_json_parsing() -> None:
    """Valid JSON with unbounded trailing whitespace must still fail at the transport cap."""
    body = b'{"devices":[]}' + b" " * 100_000
    client = DeviceLifecycleClient(
        "http://127.0.0.1:18765",
        "host-secret",
        opener=lambda _request, _timeout: FinalUrlResponse(
            body, "http://127.0.0.1:18765/v1/devices"
        ),
    )

    with pytest.raises(DeviceLifecycleError) as caught:
        client.devices()

    assert caught.value.error_code == "DEVICE_LIFECYCLE_UNAVAILABLE"
    assert caught.value.__cause__ is None


def test_acceptance_client_ignores_host_proxy_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Acceptance must use the selected loopback API even under hostile proxy settings."""
    module = _load_macos_deploy()
    proxy_requests: list[str] = []

    class Target(QuietHandler):
        def do_GET(self) -> None:
            body = b'{"symbol":"601872","name":"\u62db\u5546\u8f6e\u8239","market":"17"}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    class Proxy(QuietHandler):
        def do_GET(self) -> None:
            proxy_requests.append(self.path)
            self.send_response(502)
            self.end_headers()

    with serve(Target) as target, serve(Proxy) as proxy:
        monkeypatch.setenv("HTTP_PROXY", f"http://127.0.0.1:{proxy.server_port}")
        monkeypatch.setenv("http_proxy", f"http://127.0.0.1:{proxy.server_port}")
        monkeypatch.delenv("NO_PROXY", raising=False)
        monkeypatch.delenv("no_proxy", raising=False)
        acceptance = module.LoopbackDataOnlyAcceptance(
            base_url=f"http://127.0.0.1:{target.server_port}"
        )

        document = acceptance._request_json("GET", "/api/v1/symbols/601872")

    assert document["symbol"] == "601872"
    assert proxy_requests == []


def test_acceptance_client_rejects_redirect_without_following() -> None:
    """Even unauthenticated acceptance traffic must remain on the selected API origin."""
    module = _load_macos_deploy()
    redirected_requests: list[str] = []

    class RedirectTarget(QuietHandler):
        def do_GET(self) -> None:
            redirected_requests.append(self.path)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"symbol":"601872"}')

    with serve(RedirectTarget) as target:
        location = f"http://127.0.0.1:{target.server_port}/redirected"

        class RedirectSource(QuietHandler):
            def do_GET(self) -> None:
                self.send_response(307)
                self.send_header("Location", location)
                self.end_headers()

        with serve(RedirectSource) as source:
            acceptance = module.LoopbackDataOnlyAcceptance(
                base_url=f"http://127.0.0.1:{source.server_port}"
            )
            with pytest.raises(module.DeploymentError) as caught:
                acceptance._request_json("GET", "/api/v1/symbols/601872")

    assert caught.value.error_code == "DATA_ONLY_ACCEPTANCE_FAILED"
    assert caught.value.__cause__ is None
    assert redirected_requests == []


@pytest.mark.parametrize(
    "final_url",
    [
        "http://localhost:8001/api/v1/symbols/601872",
        "http://127.0.0.1:8002/api/v1/symbols/601872",
        "https://127.0.0.1:8001/api/v1/symbols/601872",
    ],
)
def test_acceptance_client_rejects_wrong_final_origin(final_url: str) -> None:
    """A custom opener cannot smuggle acceptance data from a different origin."""
    module = _load_macos_deploy()
    response = FinalUrlResponse(b'{"symbol":"601872"}', final_url)
    acceptance = module.LoopbackDataOnlyAcceptance(
        opener=lambda _request, **_kwargs: response,
        base_url="http://127.0.0.1:8001",
    )

    with pytest.raises(module.DeploymentError) as caught:
        acceptance._request_json("GET", "/api/v1/symbols/601872")

    assert caught.value.error_code == "DATA_ONLY_ACCEPTANCE_FAILED"
    assert caught.value.__cause__ is None


def test_acceptance_client_caps_response_before_json_parsing() -> None:
    """Acceptance responses are bounded even when oversized JSON would otherwise parse."""
    module = _load_macos_deploy()
    body = b'{"symbol":"601872"}' + b" " * 1_100_000
    response = FinalUrlResponse(
        body, "http://127.0.0.1:8001/api/v1/symbols/601872"
    )
    acceptance = module.LoopbackDataOnlyAcceptance(
        opener=lambda _request, **_kwargs: response,
        base_url="http://127.0.0.1:8001",
    )

    with pytest.raises(module.DeploymentError) as caught:
        acceptance._request_json("GET", "/api/v1/symbols/601872")

    assert caught.value.error_code == "DATA_ONLY_ACCEPTANCE_FAILED"
    assert caught.value.__cause__ is None


def test_acceptance_client_allows_an_ordinary_same_origin_response() -> None:
    """The hardened transport must preserve the normal loopback JSON path."""
    module = _load_macos_deploy()
    response = FinalUrlResponse(
        b'{"symbol":"601872"}',
        "http://127.0.0.1:8001/api/v1/symbols/601872",
    )
    requests: list[Request] = []

    def opener(request: Request, **_kwargs):
        requests.append(request)
        return response

    acceptance = module.LoopbackDataOnlyAcceptance(
        opener=opener,
        base_url="http://127.0.0.1:8001",
    )

    assert acceptance._request_json("GET", "/api/v1/symbols/601872") == {
        "symbol": "601872"
    }
    assert requests[0].full_url == (
        "http://127.0.0.1:8001/api/v1/symbols/601872"
    )
