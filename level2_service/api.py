"""Public task submission and status API."""

from __future__ import annotations

import json
import secrets
from asyncio import Event, TimeoutError, create_task, sleep, wait_for
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import AsyncIterator, Optional

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from .models import CaptureKind, CaptureStatus, TaskRecord, TaskStatus, utc_now
from .queue import InMemoryStreams, QueueFullError, TaskStore
from .runner import RunnerControl
from .security import AdminSessionManager


class SubmitTask(BaseModel):
    symbol: str = Field(pattern=r"^(?:0|3|6)\d{5}$")


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


class LockResponse(BaseModel):
    locked: bool


def create_app(
    *,
    store: TaskStore | None = None,
    admin_password_hash: str | None = None,
    capture_root: Path | None = None,
    cleanup_interval_seconds: float = 60.0,
) -> FastAPI:
    """Build an isolated application instance for one service process."""
    if cleanup_interval_seconds <= 0:
        raise ValueError("cleanup_interval_seconds must be positive")

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

        app.state.cleanup_stop = stop
        app.state.cleanup_task = create_task(retention_loop())
        try:
            yield
        finally:
            stop.set()
            await app.state.cleanup_task

    app = FastAPI(title="THS Level2 Capture Service", lifespan=lifespan)
    app.state.store = store or InMemoryStreams()
    app.state.admin_sessions = AdminSessionManager(admin_password_hash)
    app.state.runner_control = RunnerControl()
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

    return app
