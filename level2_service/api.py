"""Public task submission and status API."""

from __future__ import annotations

import json
import re
import secrets
from asyncio import FIRST_COMPLETED, CancelledError, Event, TimeoutError, create_task, gather, get_running_loop, sleep, to_thread, wait, wait_for
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import AsyncIterator, Optional

from fastapi import Depends, FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, StreamingResponse
from starlette.websockets import WebSocketState
from pydantic import BaseModel, Field, field_validator

from .models import CaptureKind, CaptureStatus, TaskRecord, TaskStatus, utc_now
from .queue import InMemoryStreams, QueueFullError, TaskStore
from .runner import ADBDeviceBridge, DeviceBridge, Level2Runner, RunnerControl, jpeg_base64
from .security import AdminSessionManager


class SubmitTask(BaseModel):
    symbol: str

    @field_validator("symbol")
    @classmethod
    def normalize_app_search_symbol(cls, value: str) -> str:
        normalized = value.strip().upper()
        if not re.fullmatch(r"[A-Z0-9._-]{1,16}", normalized):
            raise ValueError("symbol must be 1-16 App-searchable characters")
        return normalized


class LoginRequest(BaseModel):
    password: str = Field(min_length=1)


class CaptureResponse(BaseModel):
    kind: CaptureKind
    status: CaptureStatus
    url: Optional[str]
    expires_at: Optional[datetime]


class TaskResponse(BaseModel):
    public_id: str
    symbol: str
    status: TaskStatus
    error_code: Optional[str]
    created_at: datetime
    captures: list[CaptureResponse]


class RunnerHealthResponse(BaseModel):
    state: str
    last_heartbeat: Optional[datetime]
    queue_paused: bool


class LockResponse(BaseModel):
    locked: bool


class QueueResponse(BaseModel):
    paused: bool


def create_app(
    *,
    store: TaskStore | None = None,
    admin_password_hash: str | None = None,
    capture_root: Path | None = None,
    cleanup_interval_seconds: float = 60.0,
    device_bridge: DeviceBridge | None = None,
    runner_control: RunnerControl | None = None,
    runner: Level2Runner | None = None,
    runner_poll_interval_seconds: float = 1.0,
    admin_session_secret: str | None = None,
) -> FastAPI:
    """Build an isolated application instance for one service process."""
    if cleanup_interval_seconds <= 0:
        raise ValueError("cleanup_interval_seconds must be positive")
    if runner_poll_interval_seconds <= 0:
        raise ValueError("runner_poll_interval_seconds must be positive")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        stop = Event()

        async def retention_loop() -> None:
            while not stop.is_set():
                app.state.store.cleanup(utc_now())
                try:
                    await wait_for(stop.wait(), timeout=cleanup_interval_seconds)
                except TimeoutError:
                    continue

        async def runner_loop() -> None:
            """Run the blocking ADB worker off the API event loop until shutdown."""
            assert runner is not None
            while not stop.is_set():
                try:
                    await to_thread(runner.run_once)
                except Exception:
                    # Device failures are surfaced through the authenticated health API.
                    app.state.runner_control.heartbeat("OFFLINE")
                try:
                    await wait_for(stop.wait(), timeout=runner_poll_interval_seconds)
                except TimeoutError:
                    continue

        app.state.cleanup_stop = stop
        app.state.cleanup_task = create_task(retention_loop())
        app.state.runner_task = create_task(runner_loop()) if runner is not None else None
        try:
            yield
        finally:
            stop.set()
            await app.state.cleanup_task
            if app.state.runner_task is not None:
                await app.state.runner_task

    app = FastAPI(title="THS Level2 Capture Service", lifespan=lifespan)
    app.state.store = store or InMemoryStreams()
    app.state.admin_sessions = AdminSessionManager(admin_password_hash, session_secret=admin_session_secret)
    app.state.runner_control = runner_control or RunnerControl()
    app.state.device_bridge = device_bridge or ADBDeviceBridge()
    app.state.capture_root = (capture_root or Path("captures")).resolve()
    set_capture_root = getattr(app.state.store, "set_capture_root", None)
    if callable(set_capture_root):
        set_capture_root(app.state.capture_root)

    def require_admin(request: Request):
        session = app.state.admin_sessions.valid_session(request.cookies.get("ths_admin_session"))
        if session is None:
            raise HTTPException(status_code=401, detail="admin authentication required")
        return session

    def require_csrf(request: Request, session=Depends(require_admin)):
        if request.headers.get("X-CSRF-Token") != session.csrf_token:
            raise HTTPException(status_code=403, detail="CSRF token required")
        return session

    @app.post("/api/v1/jobs", status_code=202, response_model=TaskResponse)
    def submit_task(payload: SubmitTask) -> TaskResponse:
        task = TaskRecord(task_id=secrets.token_urlsafe(24), symbol=payload.symbol)
        try:
            app.state.store.enqueue(task)
        except QueueFullError:
            raise HTTPException(status_code=429, detail="queue is full") from None
        return TaskResponse.model_validate(task.as_public())

    @app.get("/api/v1/jobs/{public_id}", response_model=TaskResponse)
    def get_task(public_id: str) -> TaskResponse:
        task = app.state.store.get(public_id)
        if task is None:
            raise HTTPException(status_code=404, detail="task not found")
        return TaskResponse.model_validate(task.as_public())

    @app.get("/api/v1/jobs/{public_id}/captures/{kind}")
    def get_capture(public_id: str, kind: CaptureKind):
        task = app.state.store.get(public_id)
        if task is None:
            raise HTTPException(status_code=404, detail="task not found")
        capture = task.captures[kind]
        if capture.status.value == "EXPIRED":
            raise HTTPException(status_code=410, detail="capture has expired")
        if capture.status.value != "READY" or capture.path is None:
            raise HTTPException(status_code=404, detail="capture is not available")
        path = capture.path.resolve()
        try:
            path.relative_to(app.state.capture_root)
        except ValueError:
            raise HTTPException(status_code=404, detail="capture is not available") from None
        if not path.is_file():
            raise HTTPException(status_code=404, detail="capture is not available")
        return FileResponse(path, media_type="image/png")

    @app.get("/api/v1/jobs/{public_id}/events")
    async def task_events(public_id: str, request: Request, after: int = 0, once: bool = False):
        task = app.state.store.get(public_id)
        if task is None:
            raise HTTPException(status_code=404, detail="task not found")

        async def event_stream() -> AsyncIterator[str]:
            event_index = after
            while not await request.is_disconnected():
                events = app.state.store.events_after(public_id, event_index)
                for event in events:
                    payload = json.dumps({"public_id": public_id, "status": event["data"]})
                    yield f"event: {event['event']}\ndata: {payload}\n\n"
                event_index += len(events)
                if once:
                    return
                if not events:
                    yield ": keepalive\n\n"
                await sleep(1)

        return StreamingResponse(event_stream(), media_type="text/event-stream")

    @app.post("/api/admin/session", status_code=204)
    def admin_login(payload: LoginRequest, response: Response) -> None:
        sessions = app.state.admin_sessions
        if not sessions.configured:
            raise HTTPException(status_code=503, detail="admin login is not configured")
        session = sessions.authenticate(payload.password)
        if session is None:
            raise HTTPException(status_code=401, detail="invalid credentials")
        response.set_cookie("ths_admin_session", session.session_id, httponly=True, samesite="strict", secure=True)
        response.set_cookie("ths_csrf", session.csrf_token, httponly=False, samesite="strict", secure=True)

    @app.post("/api/admin/session/logout", status_code=204)
    def admin_logout(response: Response, session=Depends(require_csrf)) -> None:
        app.state.admin_sessions.revoke(session.session_id)
        app.state.runner_control.disconnect_session(session.session_id)
        response.delete_cookie("ths_admin_session", httponly=True, samesite="strict", secure=True)
        response.delete_cookie("ths_csrf", httponly=False, samesite="strict", secure=True)

    @app.get("/api/admin/runner")
    def runner_health(_session=Depends(require_admin)) -> RunnerHealthResponse:
        return RunnerHealthResponse.model_validate(app.state.runner_control.health())

    @app.get("/api/admin/lock")
    def lock_state(session=Depends(require_admin)) -> LockResponse:
        return LockResponse.model_validate(app.state.runner_control.lock_state(session.session_id))

    @app.post("/api/admin/lock/acquire")
    def acquire_lock(session=Depends(require_csrf)) -> LockResponse:
        if not app.state.runner_control.lock(session.session_id):
            raise HTTPException(status_code=409, detail="runner is locked by another admin")
        return LockResponse(locked=True)

    @app.post("/api/admin/lock/release")
    def release_lock(session=Depends(require_csrf)) -> LockResponse:
        if not app.state.runner_control.release(session.session_id):
            raise HTTPException(status_code=409, detail="runner lock is not owned by this admin")
        return LockResponse(locked=False)

    @app.post("/api/admin/jobs/{public_id}/resume", response_model=TaskResponse)
    def resume_waiting_job(public_id: str, _session=Depends(require_csrf)) -> TaskResponse:
        task = app.state.store.get(public_id)
        if task is None:
            raise HTTPException(status_code=404, detail="task not found")
        try:
            resumed = app.state.store.requeue_waiting(public_id)
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from None
        return TaskResponse.model_validate(resumed.as_public())

    @app.get("/api/admin/queue", response_model=QueueResponse)
    def queue_status(_session=Depends(require_admin)) -> QueueResponse:
        return QueueResponse(paused=app.state.runner_control.queue_paused)

    @app.post("/api/admin/queue/pause", response_model=QueueResponse)
    def pause_queue(_session=Depends(require_csrf)) -> QueueResponse:
        app.state.runner_control.pause_queue()
        return QueueResponse(paused=True)

    @app.post("/api/admin/queue/resume", response_model=QueueResponse)
    def resume_queue(_session=Depends(require_csrf)) -> QueueResponse:
        app.state.runner_control.resume_queue()
        return QueueResponse(paused=False)

    @app.websocket("/api/admin/device")
    async def device_stream(websocket: WebSocket) -> None:
        session = app.state.admin_sessions.valid_session(websocket.cookies.get("ths_admin_session"))
        if session is None:
            await websocket.close(code=1008)
            return
        await websocket.accept()
        control = app.state.runner_control
        bridge = app.state.device_bridge
        loop = get_running_loop()
        unregister_socket = control.register_socket(
            session.session_id,
            lambda: loop.call_soon_threadsafe(lambda: create_task(_close_device_socket(websocket, 1008))),
        )
        await websocket.send_json(control.status(session.session_id))

        def session_is_valid() -> bool:
            return app.state.admin_sessions.valid_session(session.session_id) is not None

        async def receive_input() -> None:
            last_input_sequence = 0
            while True:
                if not session_is_valid():
                    await _close_device_socket(websocket, 1008)
                    return
                try:
                    raw = await websocket.receive_json()
                except WebSocketDisconnect:
                    return
                if not session_is_valid():
                    await _close_device_socket(websocket, 1008)
                    return
                event = _validated_input(raw)
                input_sequence = raw.get("sequence") if isinstance(raw, dict) else None
                if event is None or not isinstance(input_sequence, int) or input_sequence <= last_input_sequence:
                    await _close_device_socket(websocket, 1003)
                    return
                last_input_sequence = input_sequence
                if control.authorizes_input(session.session_id):
                    _forward_input(bridge, event)

        async def emit_frames() -> None:
            frame_sequence = 0
            while True:
                await sleep(0.25)
                if not session_is_valid():
                    await _close_device_socket(websocket, 1008)
                    return
                try:
                    encoded = jpeg_base64(bridge.screenshot_png())
                except Exception:
                    control.heartbeat("OFFLINE")
                    await websocket.send_json(control.status(session.session_id))
                    continue
                frame_sequence += 1
                await websocket.send_json({"type": "frame", "encoding": "jpeg", "sequence": frame_sequence, "capturedAt": utc_now().isoformat(), "data": encoded})
                await websocket.send_json(control.status(session.session_id))

        receiver = create_task(receive_input())
        ticker = create_task(emit_frames())
        try:
            _done, pending = await wait({receiver, ticker}, return_when=FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            await gather(receiver, ticker, return_exceptions=True)
        except (WebSocketDisconnect, CancelledError):
            return
        finally:
            for task in (receiver, ticker):
                if not task.done():
                    task.cancel()
            await gather(receiver, ticker, return_exceptions=True)
            unregister_socket()

    return app


def _validated_input(value: object) -> dict | None:
    """Validate the documented input envelope without retaining key values."""
    if not isinstance(value, dict) or value.get("type") != "input" or not isinstance(value.get("sequence"), int):
        return None
    event = value.get("event")
    if not isinstance(event, dict) or event.get("kind") not in {"tap", "swipe", "key"}:
        return None
    kind = event["kind"]
    if kind == "tap":
        if not _normalised(event.get("x")) or not _normalised(event.get("y")):
            return None
    elif kind == "swipe":
        if not all(_normalised(event.get(field)) for field in ("startX", "startY", "endX", "endY")):
            return None
    elif not isinstance(event.get("key"), str) or event.get("action") not in {"down", "up"}:
        return None
    return event


async def _close_device_socket(websocket: WebSocket, code: int) -> None:
    """Close once: logout and the loop may observe the same revocation."""
    if websocket.application_state != WebSocketState.CONNECTED:
        return
    try:
        await websocket.close(code=code)
    except (RuntimeError, WebSocketDisconnect):
        return


def _normalised(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= 1


def _forward_input(bridge: DeviceBridge, event: dict) -> None:
    """Dispatch only normalised events; deliberately do not log keyboard payloads."""
    if event["kind"] == "tap":
        bridge.tap(float(event["x"]), float(event["y"]))
    elif event["kind"] == "swipe":
        bridge.swipe(float(event["startX"]), float(event["startY"]), float(event["endX"]), float(event["endY"]))
    else:
        bridge.key(event["key"], event["action"])
