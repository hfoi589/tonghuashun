# App-Independent Market Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove ordinary runtime dependence on a running Tonghuashun App for symbol lookup and the market UI, while reducing direct-task latency with one-use pre-authenticated 9528 connections and immediate runner wakeups.

**Architecture:** A versioned SQLite catalog sourced from Sina becomes the identity authority. Tencent and public Tonghuashun web feeds provide the basic market data plane, while verified 9528/fund direct clients are optional L2 enrichment only. The core transport gains a single-use warm connection pool, and the API lifespan gains an in-process wake event while Redis remains the durable queue authority.

**Tech Stack:** Python 3.12, FastAPI, SQLite, Redis, stdlib `urllib`, TCP sockets, React/TypeScript/Vitest, pytest, Docker Compose on OrbStack.

**Spec:** `docs/superpowers/specs/2026-08-27-app-independent-market-design.md`

## Global Constraints

- Original eight task metrics remain owned by the verified core transport; public symbol/quote providers never fill task metric values.
- A 9528 connection is authenticated once, checked out once, and always closed after one business request sequence.
- App, Frida, OCR, screenshots, and UI navigation are forbidden fallbacks for symbol lookup and public market data.
- Raw cookies, user agents, authentication packets, templates, keys, fingerprints, upstream response bodies, and URL queries never enter logs or public errors.
- `emulator-5554` is never stopped, navigated, switched, reinstalled, or cleared.
- Production deployment uses OrbStack with `.env`, `deploy/macos.env`, `deploy/compose.yml`, port 8001, and persistent existing volumes.
- `graphify-out/` is excluded from commits.

## File responsibility map

- `level2_service/direct_market.py`: verified direct transports, prepared core material, warm connection pool, optional L2 enrichment primitives.
- `level2_service/symbol_catalog.py`: Sina catalog parsing, versioned SQLite storage, lookup/search, refresh scheduling state.
- `level2_service/public_market.py`: Tencent/Sina quote and series parsing, public market source, normalization and fixed error codes.
- `level2_service/daily_kline.py`: public-only daily K-line provider chain and local indicators.
- `level2_service/market_data.py`: broker cache and per-symbol event delivery.
- `level2_service/api.py`: runner wake, catalog lifecycle, enqueue notifications, public error mapping.
- `level2_service/main.py`: configuration and production dependency wiring without Frida symbol/market sources.
- `level2_service/market_api.py`: catalog-backed watchlists and public market routes.
- `frontend/src/MarketApp.tsx`, `frontend/src/market-api.ts`, `frontend/src/DailyKChart.tsx`: source labels, precision, optional L2 UI, WebSocket recovery.
- `AGENTS.md`, `README.md`, `handoff.md`, `deploy/*`: operational contract and deployment settings.

---

### Task 1: Preserve and commit the verified direct-transport baseline

**Files:**
- Modify/commit existing changes: `README.md`
- Modify/commit existing changes: `level2_service/app_sessions.py`
- Modify/commit existing changes: `level2_service/direct_market.py`
- Modify/commit existing changes: `level2_service/main.py`
- Modify/commit existing changes: `tests/test_app_sessions.py`
- Modify/commit existing changes: `tests/test_deployment.py`
- Modify/commit existing changes: `tests/test_direct_market.py`
- Create/commit existing file: `docs/direct-core-isolated-analysis-2026-08-27.md`

**Interfaces:**
- Produces: the already verified direct core decoder/session capture baseline that later tasks extend.
- Excludes: `graphify-out/` and the new implementation plan from this baseline commit.

- [ ] **Step 1: Run the complete existing regression suite**

Run: `.venv/bin/python -m pytest -q`

Expected: all tests pass; the last known baseline was 461 passing tests.

- [ ] **Step 2: Inspect and stage only the verified baseline paths**

Run: `git diff --check`

Stage exactly the eight paths listed above with `git add -- <paths>`.

- [ ] **Step 3: Commit the baseline**

Run: `git commit -m "feat: complete direct core market transport"`

Expected: working tree retains only plan/feature work not included in this baseline and `graphify-out/` remains untracked.

---

### Task 2: Add one-use warm 9528 connections

**Files:**
- Modify: `level2_service/direct_market.py`
- Modify: `level2_service/main.py`
- Test: `tests/test_direct_market.py`
- Test: `tests/test_deployment.py`

**Interfaces:**
- Produces: `CoreRequestMaterial`, `WarmCoreConnection`, `Core9528WarmPool`.
- Produces on `Core9528Client`: `prewarm() -> None`, `invalidate() -> None`, `close() -> None`.
- Preserves: `Core9528Client.read_direct(symbol: str) -> DirectReadOutcome`.

- [ ] **Step 1: Write failing protocol-phase tests**

Add tests that construct deterministic fake sockets and assert:

```python
prepared = protocol.prepare(material, "601872")
warm = protocol.authenticate(prepared)
outcome = protocol.read_authenticated(warm, prepared, "601872", "17")
assert warm.connection.closed is True
assert outcome.values[MetricKind.STOCK_NAME] == "测试股票"
```

Also assert `authenticate()` sends only the auth packet and `read_authenticated()` never sends it again.

Run: `.venv/bin/python -m pytest -q tests/test_direct_market.py -k 'prepare or authenticate or read_authenticated'`

Expected: fail because the phase APIs do not exist.

- [ ] **Step 2: Implement prepared material and phase separation**

Add frozen dataclasses with secret fields excluded from repr:

```python
@dataclass(frozen=True)
class CoreRequestMaterial:
    host: str
    port: int
    auth_packet: bytes = field(repr=False)
    request_packets: tuple[bytes, ...] = field(repr=False)
    macdfs_params: tuple[int, int, int]
    timeout_seconds: float
    session_fingerprint: bytes = field(repr=False)

@dataclass
class WarmCoreConnection:
    connection: object = field(repr=False)
    session_fingerprint: bytes = field(repr=False)
    authenticated_at: float
```

Move existing validation into `prepare()`, existing auth send/read into `authenticate()`, and business batches/decoder into `read_authenticated()`. Keep `read_direct()` as a cold compatibility wrapper.

- [ ] **Step 3: Write failing pool lifecycle tests**

Cover:

```python
pool.prewarm(session)
first = pool.acquire(session)
assert pool.ready_count == 0
outcome = protocol.read_authenticated(first, prepared, "601872", "17")
assert outcome.values[MetricKind.STOCK_NAME] == "测试股票"
assert first.connection.closed is True
```

Add tests for max-idle eviction at 25 seconds, session fingerprint invalidation, concurrent acquire returning one unique ready socket, refill failure isolation, and `close()` draining unused sockets.

Run: `.venv/bin/python -m pytest -q tests/test_direct_market.py -k 'warm_pool'`

Expected: fail because the pool does not exist.

- [ ] **Step 4: Implement `Core9528WarmPool` and integrate the client**

Use a lock only for deque/fingerprint/refill/closed state. Perform connect/auth outside the lock. `acquire()` consumes a ready connection once or authenticates synchronously. Schedule the refill worker only after the business request completes and the consumed socket closes; live verification showed that parallel authentication during a business read can invalidate the response. Never return a used connection.

Serialize `Core9528Client.read_direct()` with a request lock so task and market enrichment do not issue concurrent core sequences.

- [ ] **Step 5: Verify direct transport tests**

Run: `.venv/bin/python -m pytest -q tests/test_direct_market.py tests/test_deployment.py tests/test_android_runner.py`

Expected: all pass.

- [ ] **Step 6: Commit**

Stage the four Task 2 files only.

Run: `git commit -m "perf: preauthenticate core direct connections"`

---

### Task 3: Wake the runner immediately on queued work

**Files:**
- Modify: `level2_service/api.py`
- Test: `tests/test_retention_lifecycle.py`
- Test: `tests/test_public_api.py`
- Test: `tests/test_admin_security.py`

**Interfaces:**
- Produces: internal `RunnerWake.bind(loop)`, `notify()`, `wait(timeout)`, and `close()` behavior.
- Preserves: Redis queue storage, Lua scripts, FIFO claim, and fallback polling configuration.

- [ ] **Step 1: Write failing lifecycle and notification tests**

Create a fake runner with a call event. Configure `runner_poll_interval_seconds=30`, submit a job, and assert the runner is called within one second. Add equivalent assertions for public retry, admin retry/resume, and queue resume.

Run: `.venv/bin/python -m pytest -q tests/test_retention_lifecycle.py tests/test_public_api.py tests/test_admin_security.py -k 'runner_wake or retry or resume'`

Expected: the long-poll submission test fails under the current fixed sleep.

- [ ] **Step 2: Implement `RunnerWake` and the event-driven loop**

Use the lifespan loop and `loop.call_soon_threadsafe(event.set)` from sync routes. The loop must clear before `run_once()`, immediately continue after a claimed task, and otherwise wait for wake, stop, or the configured timeout fallback.

Notify only after a durable operation returns a task with status `QUEUED`.

- [ ] **Step 3: Verify queue and lifecycle behavior**

Run: `.venv/bin/python -m pytest -q tests/test_queue.py tests/test_retention_lifecycle.py tests/test_public_api.py tests/test_admin_security.py`

Expected: all pass and no shutdown task remains pending.

- [ ] **Step 4: Commit**

Run: `git commit -m "perf: wake task runner on enqueue"`

---

### Task 4: Build the versioned Sina symbol catalog

**Files:**
- Create: `level2_service/symbol_catalog.py`
- Create: `tests/test_symbol_catalog.py`
- Modify: `level2_service/api.py`
- Modify: `level2_service/main.py`
- Modify: `level2_service/market_api.py`
- Modify: `tests/test_public_api.py`
- Modify: `tests/test_deployment.py`
- Modify: `tests/test_market_user_api.py`

**Interfaces:**
- Produces: `SinaSymbolCatalogProvider.fetch() -> tuple[SymbolLookup, ...]`.
- Produces: `SQLiteSymbolCatalog.lookup(symbol)`, `search(query, limit)`, `refresh()`, `status()`.
- Produces: fixed errors `SYMBOL_CATALOG_UNAVAILABLE`, `SYMBOL_CATALOG_STALE`, `SYMBOL_CATALOG_RESPONSE_INVALID`.

- [ ] **Step 1: Add fixed Sina fixtures and failing parser tests**

Fixtures must include one supported row from each market category plus duplicates and unsupported prefixes. Assert source prefix/market agreement, conflict rejection, and deterministic deduplication.

Run: `.venv/bin/python -m pytest -q tests/test_symbol_catalog.py -k 'parse or conflict or market'`

Expected: fail because the module does not exist.

- [ ] **Step 2: Implement provider parsing and HTTP pagination**

Fetch `hs_a`, `etf_hq_fund`, and `lof_hq_fund` with page size 500, fixed User-Agent, timeout, and fixed sanitized errors. Decode JSON and normalize only supported six-digit identities.

- [ ] **Step 3: Write failing versioned-store tests**

Cover first activation, failed candidate rollback, checksum stability, first-load minimum 5,000, later 90% shrink rejection, seven-day stale cutoff, and concurrent readers during activation.

Use test-only lower validation thresholds through constructor parameters; production defaults remain exact.

- [ ] **Step 4: Implement SQLite version activation and lookup/search**

Create the three schema tables and indexes from the spec. Insert a complete candidate version, validate it, then change `catalog_active` in one transaction. Escape `%`, `_`, and `\\` for search. Return deterministic ranking: exact code, code prefix, exact name, name prefix, contains, symbol.

- [ ] **Step 5: Replace App symbol wiring**

Production `create_app()` receives catalog `lookup` and `search`. Remove `symbol_source=frida_core_source` from task and market identity paths. Keep `market_code_for_symbol()` validation before catalog reads. Successful exact lookup invokes the core prewarm callback without awaiting it.

Task submission performs the same catalog exact check and invokes prewarm before durable enqueue.

- [ ] **Step 6: Add catalog lifecycle refresh**

At lifespan startup, refresh in a worker thread when no active version exists or age exceeds 18 hours. Run the daily refresh after 16:20 Asia/Shanghai and expose sanitized status in the admin market health response.

- [ ] **Step 7: Verify App-independent identity**

Run: `.venv/bin/python -m pytest -q tests/test_symbol_catalog.py tests/test_public_api.py tests/test_deployment.py tests/test_market_user_api.py`

Expected: all pass, including tests where symbol Frida callables raise if invoked.

- [ ] **Step 8: Commit**

Run: `git commit -m "feat: add public symbol catalog"`

---

### Task 5: Add Tencent/Sina public market providers

**Files:**
- Create: `level2_service/public_market.py`
- Create: `tests/test_public_market.py`
- Modify: `level2_service/market_data.py`

**Interfaces:**
- Produces: `TencentPublicMarketProvider.read_snapshot(identity, detail)` and `read_series(identity, period, limit)`.
- Produces: `SinaPublicQuoteProvider.read_quote(identity)`.
- Produces: `PublicMarketDataSource.read_market_snapshot()` and non-day `read_market_series()`.
- Produces: `MarketSnapshot.source: str | None` and `MarketSnapshot.price_precision: int`.

- [ ] **Step 1: Add sanitized Tencent and Sina fixtures**

Include quote/minute/K-line fixtures for market IDs `17`, `33`, `151`, `20`, and `36`. Include cumulative minute volume, two- and three-decimal prices, identity mismatch, malformed rows, and out-of-session times.

- [ ] **Step 2: Write failing normalization tests**

Assert:

```python
snapshot.price_precision == 3  # ETF fixture
snapshot.quote["volume"] == "10000"  # normalized shares
snapshot.timeshare[1].volume == "500"  # cumulative delta
snapshot.source_errors["tencent_public"] is None
```

Also assert code/name mismatch raises `PUBLIC_MARKET_RESPONSE_INVALID` without embedding response content.

- [ ] **Step 3: Implement provider mappings and parsers**

Use `sh`, `sz`, and `bj` identifiers from the spec. For detail snapshots, parse Tencent minute response and embedded quote. For lightweight snapshots, parse Tencent quote response decoded as GB18030. Normalize source time, precision, OHLC, percentage, turnover, shares, yuan, and 241-point session times.

- [ ] **Step 4: Implement Sina basic-quote fallback**

When Tencent quote retrieval fails, return a Sina quote snapshot with no fresh timeshare and set the Tencent error code in `source_errors`. Both failures raise `MARKET_QUOTE_UNAVAILABLE`.

- [ ] **Step 5: Implement Tencent five-day/week/month series**

Map `five_day` to the latest five qfq daily bars, `week` to qfq weekly bars, and `month` to qfq monthly bars. Amount may be `None` when Tencent does not publish it. Reject unsupported minute periods with a fixed capability error.

- [ ] **Step 6: Verify public market providers**

Run: `.venv/bin/python -m pytest -q tests/test_public_market.py tests/test_market_data.py`

Expected: all pass.

- [ ] **Step 7: Commit**

Run: `git commit -m "feat: add public realtime market source"`

---

### Task 6: Remove App market fallback and add optional direct enrichment

**Files:**
- Modify: `level2_service/daily_kline.py`
- Modify: `level2_service/direct_market.py`
- Modify: `level2_service/main.py`
- Modify: `level2_service/market_data.py`
- Modify: `tests/test_daily_kline.py`
- Modify: `tests/test_deployment.py`
- Modify: `tests/test_market_data.py`

**Interfaces:**
- Produces: public-only daily chain `THS_PUBLIC -> TENCENT_PUBLIC -> stale cache`.
- Produces: `DirectMarketEnricher.read(symbol) -> DirectReadOutcome | None` with 15-second per-symbol cache.
- Produces: a composite public snapshot whose basic quote never depends on enrichment.

- [ ] **Step 1: Write failing no-App fallback tests**

Inject an App source that raises `AssertionError` if called. Make Tonghuashun daily fail and Tencent daily succeed; assert source `TENCENT_PUBLIC`. Make both fail with cache and assert stale page. Make both fail without cache and assert an empty page with `KLINE_SOURCES_UNAVAILABLE`.

- [ ] **Step 2: Refactor daily K-line wrapper to public-only providers**

Rename internal App-oriented fields and source error keys. Delete all calls to `app_source.read_market_series()` as a fallback. Retain indicator calculations and cache semantics.

- [ ] **Step 3: Write failing enrichment ownership tests**

Build a public snapshot and a direct outcome with conflicting price/name plus valid L2 fields. Assert public price/name remain unchanged while only large-order, retail, MACD/MACDFS curves, and fund flow merge.

Assert direct failure returns the public snapshot with empty enhancement and sanitized `source_errors`.

- [ ] **Step 4: Implement cached optional enrichment**

For `detail=true` and `MARKET_DIRECT_ENRICHMENT=1`, read the serialized direct source at most every 15 seconds per symbol. Merge only owned fields. For `detail=false`, never call direct enrichment.

- [ ] **Step 5: Rewire production market source**

Construct the market broker from the public source and public daily wrapper regardless of Frida availability. Pass direct enrichment only when both selected task transports are direct/shadow-compatible; never construct a Frida market source.

- [ ] **Step 6: Verify public market survives App offline**

Run: `.venv/bin/python -m pytest -q tests/test_daily_kline.py tests/test_market_data.py tests/test_deployment.py tests/test_market_stream_api.py`

Expected: all pass and deployment tests prove no market/symbol source references `FridaParsedValueSource`.

- [ ] **Step 7: Commit**

Run: `git commit -m "feat: detach market data from app"`

---

### Task 7: Fix market event delivery and frontend recovery

**Files:**
- Modify: `level2_service/market_data.py`
- Modify: `tests/test_market_data.py`
- Modify: `frontend/src/market-api.ts`
- Modify: `frontend/src/MarketApp.tsx`
- Modify: `frontend/src/DailyKChart.tsx`
- Modify: `frontend/src/MarketApp.test.tsx`
- Modify: `frontend/src/DailyKChart.test.tsx`

**Interfaces:**
- Produces: `MarketSnapshot.price_precision` in backend/public TypeScript shape.
- Produces: latest-per-symbol bounded subscriber delivery.
- Produces: reconnecting WebSocket client with HTTP fallback.

- [ ] **Step 1: Write failing multi-symbol broker test**

Subscribe one client to two symbols, publish updates for both before consumption, and assert two symbol-distinct events remain available. Assert repeated updates for the same symbol coalesce to the latest event.

- [ ] **Step 2: Implement latest-per-symbol subscriber buffering**

Replace queue size one with a bounded structure keyed by symbol plus a wake event. Preserve source-status events and prevent unbounded growth beyond the subscription's symbol count plus one status slot.

- [ ] **Step 3: Write failing frontend precision/L2/reconnect tests**

Assert an ETF with `price_precision=3` renders three decimals, a public-only snapshot hides L2 metric/fund sections, and a closed WebSocket schedules reconnect, re-subscribes, and fetches the selected snapshot through HTTP while disconnected.

- [ ] **Step 4: Implement frontend behavior**

Update types and display helpers to use `price_precision`. Replace App-specific text with public/direct source labels. Hide empty L2 sections. Add bounded exponential reconnect delays of 1, 2, 4, 8, and 15 seconds, reset after a successful open, and cancel timers on unmount/logout.

- [ ] **Step 5: Verify backend and frontend**

Run: `.venv/bin/python -m pytest -q tests/test_market_data.py tests/test_market_stream_api.py`

Run: `npm test` in `frontend/`.

Run: `npm run build` in `frontend/`.

Expected: all pass.

- [ ] **Step 6: Commit**

Run: `git commit -m "fix: make public market delivery resilient"`

---

### Task 8: Configuration, contracts, deployment, merge, and GitHub release

**Files:**
- Modify: `AGENTS.md`
- Modify: `README.md`
- Modify: `handoff.md`
- Modify: `.env.example`
- Modify: `deploy/macos.env.example`
- Modify: `deploy/compose.yml`
- Modify: `level2_service/main.py`
- Modify: `tests/test_deploy_configuration.py`
- Modify: `tests/test_deployment.py`

**Interfaces:**
- Produces exact environment variables from the spec.
- Produces the final operating contract and evidence record.

- [ ] **Step 1: Add failing configuration tests**

Assert defaults and Compose wiring for:

```text
SYMBOL_CATALOG_PATH=/data/market/symbol-catalog.db
SYMBOL_CATALOG_MAX_AGE_SECONDS=604800
SYMBOL_CATALOG_REFRESH_HOUR=16
SYMBOL_CATALOG_REFRESH_MINUTE=20
PUBLIC_MARKET_TIMEOUT_SECONDS=8
MARKET_DIRECT_ENRICHMENT=1
MARKET_DIRECT_ENRICHMENT_TTL_SECONDS=15
CORE_WARM_CONNECTION_MAX_IDLE_SECONDS=25
```

- [ ] **Step 2: Implement configuration parsing and deployment wiring**

Validate positive numeric ranges and 24-hour clock bounds. Reuse the existing `/data/market` volume. Do not alter emulator or Redis volumes.

- [ ] **Step 3: Update documentation and rules**

`AGENTS.md` must state:

- task values retain strict direct/App-internal ownership;
- exact symbol lookup/search use the local public catalog;
- market basic quote/intraday/K-line use public providers;
- optional direct L2 enrichment cannot make the public market unavailable;
- no App/Frida fallback is allowed for catalog or market.

`handoff.md` must record architecture, refresh schedules, provider endpoints/categories, verification timings, deployment command, rollback image/commit, remaining human session-refresh dependency, and protected emulator status.

- [ ] **Step 4: Run complete local verification**

Run: `.venv/bin/python -m pytest -q`

Run: `npm test` in `frontend/`.

Run: `npm run build` in `frontend/`.

Run: `git diff --check`.

Expected: zero failures and no whitespace errors.

- [ ] **Step 5: Commit final configuration and docs**

Stage only confirmed paths, excluding `graphify-out/`.

Run: `git commit -m "docs: publish app-independent market operations"`

- [ ] **Step 6: Deploy on OrbStack**

Run:

```bash
docker --context orbstack compose \
  --env-file .env \
  --env-file deploy/macos.env \
  -f deploy/compose.yml up -d --build
```

If Docker Hub is temporarily unavailable, use only a documented local-image fallback and retain a rollback tag; retry the canonical build before GitHub publication when network permits.

- [ ] **Step 7: Refresh the symbol catalog and run live acceptance**

Verify:

- catalog status is ready and contains supported entries in all five market categories;
- exact lookup and suggestions work without Frida;
- public market quote/intraday/day/five-day/week/month work without Frida;
- three data-only tasks complete with eight metrics, three 241-point L2 curves, and three fund periods;
- warm core timing and end-to-end task timing are recorded;
- API and Redis containers are healthy;
- no operation was issued to `emulator-5554`.

- [ ] **Step 8: Review the complete branch**

Use an independent reviewer over `git merge-base main HEAD..HEAD`. Fix all Critical/Important findings and re-run covering tests.

- [ ] **Step 9: Merge into local main**

In `/private/tmp/tonghuashun-main-merge`, verify the worktree is clean, then merge `codex/symbol-search-tab-delete` with a merge commit. Run the complete Python and frontend verification again from the main worktree.

- [ ] **Step 10: Publish GitHub**

Push `main` to `origin`, then verify:

```bash
git ls-remote --heads origin main
git rev-parse main
```

Expected: both hashes match. Report the pushed commit, deployment health, acceptance timings, and any provider/service-term maintenance risk.
