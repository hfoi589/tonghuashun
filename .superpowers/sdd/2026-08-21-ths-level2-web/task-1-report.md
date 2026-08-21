# Task 1 — Backend contracts, queue, admin session, and retention

## Delivered

- FastAPI public API: `POST /api/tasks`, `GET /api/tasks/{task_id}`, SSE at
  `GET /api/tasks/{task_id}/events` (including reconnect cursor), and capture delivery at
  `GET /api/tasks/{task_id}/captures/{kind}`.
- Opaque `secrets.token_urlsafe(24)` task IDs; six-digit A-share validation
  (`0`, `3`, or `6` prefix); typed Pydantic request/response models.
- Domain enums: `LARGE_ORDER_NET`, `LARGE_ORDER_AMOUNT`, `RETAIL_COUNT`, and
  `QUEUED`, `RUNNING`, `WAITING_ADMIN`, `PARTIAL`, `SUCCEEDED`, `FAILED`.
- A `TaskStore` interface, deterministic `InMemoryStreams` fake, and a small
  `RedisStreamsStore` adapter boundary that appends production job events to a
  Redis Stream. The fake has FIFO selection, a global pending cap (default
  200), event records, transition enforcement, and partial-to-complete capture
  state handling.
- Admin login only accepts a supplied Argon2id password hash; it creates random
  server-side sessions plus strict Secure/HttpOnly session cookies and a
  separate CSRF cookie. Admin runner health and lock acquire/release require an
  authenticated session; state-changing routes additionally require
  `X-CSRF-Token`. Logout revokes the server-side session.
- Capture records expire after 24 hours and their individual files are removed
  only when they are beneath the configured capture root; metadata is removed
  after seven days. Expired capture URLs return HTTP 410.

## TDD evidence

All production behavior was driven by a focused pytest RED/GREEN cycle.

| Cycle | RED command/result | GREEN command/result |
|---|---|---|
| API module | `pytest tests/test_backend_contract.py -q` → `ModuleNotFoundError: level2_service` | same command → 1 passed |
| App factory | same focused test → callable assertion failed | same command → 2 passed |
| Public submit | `pytest tests/test_public_api.py -q` → expected 202, got 404 | public focused suite → 4 passed |
| Queue FIFO/transitions | `pytest tests/test_queue.py -q` → missing queue behavior (2 failures) | queue focused suite → 2 passed, then expanded to 6 passed |
| Admin configuration | `pytest tests/test_admin_security.py -q` → expected 503, got 404 | focused test → 1 passed |
| Admin CSRF/lock | admin focused suite → expected 401, got 404 | admin focused suite → 2 passed, then 3 passed after logout cycle |
| Captures/SSE | `pytest tests/test_results_delivery.py -q` → missing route/configuration (2 failures) | focused suite → 2 passed |
| Disk retention | results focused suite → expected expired file missing, it remained | results focused suite → 3 passed |
| Capture claim guard | queue focused suite → completion before `RUNNING` was accepted | queue focused suite → 6 passed |
| Retention path guard | results focused suite → outside-root file was deleted | results and queue focused suites → 10 passed |
| SSE reconnect cursor | results focused suite → already-seen event replayed | results focused suite → 5 passed |

## Final verification

Run from the project virtual environment:

```text
.venv/bin/python -m pytest -q
20 passed

.venv/bin/python -m compileall -q level2_service
git diff --check
```

The latter two commands completed with exit status 0.

## Boundary notes

- This task deliberately does not implement Android execution, APK handling,
  frontend controls, WebSocket screen streaming, Docker, or deployment.
- Redis itself is not required by tests. The production adapter accepts an
  injected redis-py-compatible client, while tests use the in-memory fake.
- The local host exposes Python 3.9; the project metadata retains the planned
  Python 3.12 minimum. Tests ran in `.venv` successfully under the local
  interpreter, using compatible syntax in FastAPI route annotations.
