"""Authenticated same-origin proxy for the host-side Vibe Research service."""

from __future__ import annotations

import hmac
import json
import os
import re
from collections.abc import Callable
from typing import Any
from urllib.parse import quote

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from starlette.background import BackgroundTask
from starlette.responses import StreamingResponse

from .vibe_watchlist import VibeWatchlistSyncError, read_vibe_watchlist


_ADMIN_ONLY_PATHS = {
    "admin/settings",
    "admin/settings/test",
    "admin/settings/update",
    "admin/settings/clear",
    "llm-probe",
    "product",
    "local-agents",
    "local-agents/codebuddy/path",
}


def _token() -> str:
    token = os.environ.get("VRA_API_TOKEN", "")
    if len(token) < 32:
        raise HTTPException(status_code=503, detail="Vibe 服务凭据尚未配置")
    return token


def _backend_url(path: str, query: str = "") -> str:
    base = os.environ.get("VRA_BACKEND_URL", "http://host.docker.internal:8765").rstrip("/")
    encoded = "/".join(quote(part, safe="-._~") for part in path.split("/") if part)
    url = f"{base}/{encoded}"
    return f"{url}?{query}" if query else url


async def _admin_json(method: str, path: str, body: bytes | None = None, content_type: str | None = None) -> Any:
    headers = {
        "Authorization": f"Bearer {_token()}",
        "Origin": "http://127.0.0.1",
        "Accept": "application/json",
    }
    if content_type:
        headers["Content-Type"] = content_type
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=5.0)) as client:
            response = await client.request(method, _backend_url(path), headers=headers, content=body)
    except httpx.RequestError:
        raise HTTPException(status_code=503, detail="Vibe 主机服务暂时不可用") from None
    try:
        payload = response.json()
    except ValueError:
        payload = {"error": "bad_upstream_response", "message": "Vibe 服务返回了无效响应"}
    if response.is_error:
        detail = payload.get("message") or payload.get("error") or "Vibe 设置请求失败"
        raise HTTPException(status_code=response.status_code, detail=str(detail))
    return payload


def install_vibe_routes(
    app: FastAPI,
    *,
    require_ready_market_user: Callable[..., Any],
    resolve_symbol: Callable[[str], Any],
    require_admin: Callable[..., Any],
    require_admin_csrf: Callable[..., Any],
) -> None:
    """Attach Vibe routes after the market account and administrator dependencies exist."""

    @app.get("/api/internal/vibe/workspaces", include_in_schema=False)
    def internal_vibe_workspaces(request: Request) -> dict[str, list[int]]:
        token = os.environ.get("VRA_API_TOKEN", "")
        authorization = request.headers.get("authorization", "")
        if len(token) < 32 or not hmac.compare_digest(authorization, f"Bearer {token}"):
            raise HTTPException(status_code=401, detail="internal authentication required")
        accounts = getattr(request.app.state, "market_account_store", None)
        if accounts is None:
            raise HTTPException(status_code=503, detail="market application is not configured")
        return {
            "workspaces": [
                user.id for user in accounts.list_users()
                if user.enabled and not user.must_change_password
            ]
        }

    @app.api_route(
        "/api/vibe/{path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        include_in_schema=False,
    )
    async def vibe_proxy(path: str, request: Request, user=Depends(require_ready_market_user)):
        normalized = path.strip("/")
        if not normalized or any(part in {".", ".."} or "\\" in part for part in normalized.split("/")):
            raise HTTPException(status_code=404, detail="Not Found")
        if normalized in _ADMIN_ONLY_PATHS or normalized.startswith("admin/") or normalized.startswith("settings/") or normalized.startswith("local-agents/"):
            raise HTTPException(status_code=404, detail="Not Found")
        if normalized == "update" and request.method != "GET":
            raise HTTPException(status_code=404, detail="Not Found")
        if normalized == "local-agents" and request.method != "GET":
            raise HTTPException(status_code=404, detail="Not Found")

        body = await request.body()
        if len(body) > 32 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="request body too large")

        # Vibe's personal watch ledger also feeds its background research jobs.
        # Keep supported six-digit symbols in the Market account list whenever a
        # Vibe page writes or removes a ledger entry.
        if normalized == "ledger/watch" and request.method == "POST":
            try:
                payload = json.loads(body) if body else {}
            except ValueError:
                payload = {}
            record = payload.get("record") if isinstance(payload, dict) else None
            if not isinstance(record, dict) and isinstance(payload, dict):
                record = payload
            symbol = record.get("symbol") if isinstance(record, dict) else None
            if isinstance(symbol, str) and re.fullmatch(r"[0-9]{6}", symbol):
                try:
                    lookup = resolve_symbol(symbol)
                except HTTPException as error:
                    if error.status_code in {404, 409, 422}:
                        lookup = None
                    else:
                        raise
                if lookup is not None:
                    accounts = getattr(request.app.state, "market_account_store", None)
                    if accounts is None:
                        raise HTTPException(status_code=503, detail="market application is not configured")
                    if not accounts.has_watchlist_symbol(user.id, lookup.symbol):
                        groups = accounts.list_watchlists(user.id)
                        primary = next((group for group in groups if group.is_primary), groups[0] if groups else None)
                        if primary is None:
                            raise HTTPException(status_code=503, detail="行情自选分组暂不可用")
                        try:
                            accounts.add_symbol(user.id, primary.id, lookup)
                        except ValueError as error:
                            raise HTTPException(status_code=409, detail=str(error)) from None

        if normalized == "ledger/watch/delete" and request.method == "POST":
            try:
                payload = json.loads(body) if body else {}
            except ValueError:
                payload = {}
            record_id = payload.get("id") if isinstance(payload, dict) else None
            if isinstance(record_id, str) and record_id:
                try:
                    records = read_vibe_watchlist(user.id)
                except VibeWatchlistSyncError:
                    raise HTTPException(status_code=503, detail="Vibe 自选暂不可用，请重试。") from None
                target = next((record for record in records if record.get("id") == record_id), None)
                symbol = target.get("symbol") if target else None
                remains = any(
                    record.get("symbol") == symbol and record.get("id") != record_id
                    for record in records
                ) if isinstance(symbol, str) else False
                if isinstance(symbol, str) and re.fullmatch(r"[0-9]{6}", symbol) and not remains:
                    try:
                        resolve_symbol(symbol)
                    except HTTPException as error:
                        if error.status_code not in {404, 409, 422}:
                            raise
                    else:
                        accounts = getattr(request.app.state, "market_account_store", None)
                        if accounts is None:
                            raise HTTPException(status_code=503, detail="market application is not configured")
                        try:
                            accounts.remove_symbol_everywhere(user.id, symbol)
                        except LookupError:
                            pass

        headers = {
            "Authorization": f"Bearer {_token()}",
            "Origin": "http://127.0.0.1",
            "X-THS-Market-User": str(user.id),
            "Accept": request.headers.get("accept", "application/json"),
        }
        content_type = request.headers.get("content-type")
        if content_type:
            headers["Content-Type"] = content_type
        target = _backend_url(normalized, request.url.query)
        client = httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=5.0))
        try:
            upstream_request = client.build_request(
                request.method,
                target,
                headers=headers,
                content=body if body else None,
            )
            upstream = await client.send(upstream_request, stream=True)
        except httpx.RequestError:
            await client.aclose()
            raise HTTPException(status_code=503, detail="Vibe 主机服务暂时不可用") from None

        response_headers = {
            key: value for key, value in upstream.headers.items()
            if key.lower() in {"content-type", "content-disposition", "cache-control", "etag", "last-modified", "retry-after"}
        }

        async def stream_body():
            try:
                async for chunk in upstream.aiter_raw():
                    yield chunk
            finally:
                await upstream.aclose()
                await client.aclose()

        return StreamingResponse(
            stream_body(),
            status_code=upstream.status_code,
            headers=response_headers,
            background=BackgroundTask(client.aclose),
        )

    @app.get("/api/admin/vibe/settings")
    def admin_vibe_settings(_admin=Depends(require_admin)):
        return _admin_json_sync("GET", "/admin/settings")

    @app.post("/api/admin/vibe/settings/test")
    async def test_admin_vibe_settings(request: Request, _admin=Depends(require_admin_csrf)):
        body = await request.body()
        return await _admin_json("POST", "/admin/settings/test", body, request.headers.get("content-type"))

    @app.put("/api/admin/vibe/settings")
    async def save_admin_vibe_settings(request: Request, _admin=Depends(require_admin_csrf)):
        body = await request.body()
        return await _admin_json("PUT", "/admin/settings", body, request.headers.get("content-type"))

    @app.delete("/api/admin/vibe/settings")
    async def clear_admin_vibe_settings(_admin=Depends(require_admin_csrf)):
        return await _admin_json("DELETE", "/admin/settings")

    @app.post("/api/admin/vibe/settings/update")
    async def update_admin_vibe_settings(request: Request, _admin=Depends(require_admin_csrf)):
        body = await request.body()
        return await _admin_json("POST", "/admin/settings/update", body, request.headers.get("content-type"))

    @app.post("/api/admin/vibe/settings/codebuddy-path")
    async def update_admin_vibe_codebuddy(request: Request, _admin=Depends(require_admin_csrf)):
        body = await request.body()
        return await _admin_json("POST", "/admin/settings/codebuddy-path", body, request.headers.get("content-type"))

    @app.post("/api/admin/vibe/settings/sec-contact")
    async def update_admin_vibe_sec_contact(request: Request, _admin=Depends(require_admin_csrf)):
        body = await request.body()
        return await _admin_json("POST", "/admin/settings/sec-contact", body, request.headers.get("content-type"))


def _admin_json_sync(method: str, path: str) -> dict[str, Any]:
    """Synchronous adapter for the small admin status read."""
    try:
        response = httpx.get(
            _backend_url(path),
            headers={"Authorization": f"Bearer {_token()}", "Origin": "http://127.0.0.1", "Accept": "application/json"},
            timeout=httpx.Timeout(20.0, connect=5.0),
        )
    except httpx.RequestError:
        raise HTTPException(status_code=503, detail="Vibe 主机服务暂时不可用") from None
    try:
        payload = response.json()
    except ValueError:
        payload = {"error": "bad_upstream_response", "message": "Vibe 服务返回了无效响应"}
    if response.is_error:
        raise HTTPException(status_code=response.status_code, detail=str(payload.get("message") or payload.get("error") or "Vibe 设置读取失败"))
    return payload
