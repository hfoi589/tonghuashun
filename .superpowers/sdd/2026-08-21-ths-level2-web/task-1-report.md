# Task 1 — Backend contracts, queue, admin session, and retention

## Delivered

- FastAPI public API: `POST /api/v1/jobs`, `GET /api/v1/jobs/{public_id}`, SSE at
  `GET /api/v1/jobs/{public_id}/events` (including reconnect cursor), and capture delivery at
  `GET /api/v1/jobs/{public_id}/captures/{kind}`.
- Opaque `secrets.token_urlsafe(24)` public IDs; normalized App-searchable
  symbols (trimmed, uppercased, 1–16 characters from `A-Z`, `0-9`, `.`, `_`,
  and `-`); typed Pydantic request/response models.
- Domain enums: `LARGE_ORDER_NET`, `LARGE_ORDER_AMOUNT`, `RETAIL_COUNT`, and
  `QUEUED`, `RUNNING`, `WAITING_ADMIN`, `COMPLETED`, `PARTIAL`, `FAILED`, `EXPIRED`.
- A `TaskStore` interface, deterministic `InMemoryStreams` fake, and a complete
  `RedisStreamsStore` adapter that serializes task state, emits Redis Stream
  events, and atomically claims FIFO work with Lua. The fake has FIFO selection, a global pending cap (default
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
28 passed

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

## Review-fix evidence (2026-08-21)

The review identified contract and concurrency defects in the first Task 1
commit. Their common causes were an internal-name-first API, an in-memory-only
queue implementation, and retention invoked only by callers. The following
test-first corrections were made.

| Fix | RED evidence | GREEN evidence |
|---|---|---|
| Public contract | `pytest tests/test_public_api.py -q` → 4 failures: `/api/v1/jobs` returned 404 | same command → 4 passed after `public_id` and `/api/v1/jobs` routes |
| Capture/SSE contract | `pytest tests/test_results_delivery.py -q` → 4 failures: new capture/event routes returned 404 | capture/results suite → 9 passed after public-ID routes |
| Exact terminal state, per-capture expiry, atomic fake claim | `pytest tests/test_queue.py -q` → 3 failures: emitted `SUCCEEDED`, expiry used task creation, and a claim stayed `QUEUED` | queue suite → 7 passed after `COMPLETED`/`EXPIRED`, capture timestamp retention, and lock-protected claim |
| Redis adapter contract | `pytest tests/test_redis_store.py -q` → adapter lacked `enqueue` and the remaining TaskStore methods | Redis fake integration suite → 3 passed with serialization, Stream event replay, Lua `LPOP`/state-update claim, and public-route injection |
| App-owned retention | `pytest tests/test_retention_lifecycle.py -q` → `create_app` rejected the cleanup interval and no lifecycle worker existed | lifecycle suite → 1 passed; task expires the capture and stops cleanly with `TestClient` shutdown |

The public API is now exclusively:

```text
POST /api/v1/jobs
GET  /api/v1/jobs/{public_id}
GET  /api/v1/jobs/{public_id}/events
GET  /api/v1/jobs/{public_id}/captures/{kind}
```

The emitted task statuses are exactly `QUEUED`, `RUNNING`, `WAITING_ADMIN`,
`COMPLETED`, `PARTIAL`, `FAILED`, and `EXPIRED`. Capture expiry is calculated
as `captured_at + 24 hours`; after a capture expires the task is marked
`EXPIRED` until its metadata is removed at the seven-day limit.

## Approved symbol-contract update (2026-08-21)

The public request validator was changed from six-digit A-share-only input to
the approved App-searchable form. It trims and uppercases input, then requires
an exact 1–16-character match against `[A-Z0-9._-]`. The normalized value is
the only value stored in `TaskRecord` and handed to the future runner; no fuzzy
matching, tokenization, or runner behavior was added.

| Cycle | RED evidence | GREEN evidence |
|---|---|---|
| Normalized public symbol | `pytest tests/test_public_api.py -q` → expected 202 for `  aapl.us  `, got 422 | same focused suite → 5 passed; API and queued record both contain exact `AAPL.US` |
| Trim-before-length rule | focused suite → padded `brk.b` was rejected with 422 because the raw-length Field bound ran first | focused suite → 6 passed after moving the 1–16 bound to the normalized validator |
