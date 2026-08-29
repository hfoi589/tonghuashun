# Project Optimization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the identified correctness, event-loop, queue, market-concurrency, cache-lifecycle, frontend, and deployment-reproducibility issues without changing the documented data-source ownership contract.

**Architecture:** Keep FastAPI as the single HTTP process, Redis as the durable queue authority, SQLite as the catalog/account store, and the verified App-internal transports as the only task-metric sources. Move blocking work off the event loop, replace full-history scans with bounded/incremental state, and make market polling bounded and cache-aware.

**Tech Stack:** Python 3.12+, FastAPI, Redis Streams/Lua, SQLite, asyncio/thread pools, React/Vite, Vitest, Docker Compose.

**Spec:** `AGENTS.md`, `README.md`, `handoff.md`, and the prior architecture analysis in this thread.

## Global Constraints

- Task metrics must remain sourced only from `FridaParsedValueSource.read_direct()` or the verified `Core9528Client` direct transport.
- Public quote/K-line providers remain separate from task metrics and optional L2 enrichment.
- `main_fund_flow` remains protected; no account switching, logout, data clearing, reinstall, or unauthorized App navigation.
- Redis, SQLite, capture, session, and Android volumes remain persistent across deployment.
- Existing public API response shapes and fixed error-code semantics remain compatible unless an additive cursor/event field is required.
- All production changes require regression tests and a fresh full-suite verification.

---

### Task 1: Redis sessions, events, and queue accounting

**Files:**
- Modify: `level2_service/market_accounts.py`, `level2_service/queue.py`, `level2_service/api.py`, `level2_service/security.py`
- Test: `tests/test_market_accounts.py`, `tests/test_redis_store.py`, `tests/test_public_api.py`, `tests/test_admin_security.py`

**Interfaces:**
- Add non-recursive Redis session expiry/revocation and atomic session index cleanup.
- Add bounded/incremental task event retrieval and retention.
- Keep `TaskStore` public methods and `TaskResponse` compatible.

- [ ] Write regression tests for expired Redis sessions, event cursor reads, bounded event retention, atomic state/event persistence, and active-queue accounting.
- [ ] Run the focused tests and confirm they fail for the intended missing behavior.
- [ ] Implement direct session deletion, Redis pipelines/Lua for state plus event writes, active-count maintenance, and cursor-based event reads.
- [ ] Move synchronous event/cleanup calls out of async request paths and add safe keepalive headers.
- [ ] Run focused backend tests and then the complete Python suite.

### Task 2: Async direct transport, market polling, cache lifecycle, and detail correctness

**Files:**
- Modify: `level2_service/parsed_values.py`, `level2_service/direct_market.py`, `level2_service/market_data.py`, `level2_service/daily_kline.py`, `level2_service/public_market.py`
- Test: `tests/test_parsed_values.py`, `tests/test_direct_market.py`, `tests/test_market_data.py`, `tests/test_daily_kline.py`, `tests/test_public_market.py`

**Interfaces:**
- Preserve `DualAccountParsedValueSource.read_direct()` and `MarketDataBroker` method signatures.
- Add bounded concurrency, cache eviction, detail-capability tracking, and provider backoff without changing source ownership.

- [ ] Add tests proving core failures do not wait for fund timeouts, market polling has bounded concurrency, low-detail cache entries cannot satisfy detail reads, and inactive symbol state is evicted.
- [ ] Run the focused tests and confirm the new tests fail.
- [ ] Implement service-level executors/single-flight, bounded polling, provider backoff, cache limits, and detail-aware cache entries.
- [ ] Unify direct fund-flow interval configuration and preserve fixed source errors.
- [ ] Run focused backend tests and the complete Python suite.

### Task 3: Runtime, deployment, persistence, and CI hardening

**Files:**
- Modify: `level2_service/main.py`, `deploy/compose.yml`, `Dockerfile`, `pyproject.toml`, `.gitignore`
- Create: `.github/workflows/ci.yml`
- Test: `tests/test_deploy_configuration.py`, `tests/test_deployment.py`, `tests/test_predeploy_deployment_hardening.py`

**Interfaces:**
- Preserve current OrbStack/macOS deployment commands and environment variable names.
- Keep internal container port 8000 and the macOS host port 8001 contract.

- [ ] Add tests for Redis timeout configuration, `service_healthy` dependency, unified enrichment defaults, lockfile-based builds, and ignored generated graph artifacts.
- [ ] Run the focused tests and confirm the new tests fail.
- [ ] Add Redis connect/read timeouts, startup retry/readiness behavior, lockfile/constraint installation, immutable image references where practical, and a CI matrix for Python/frontend/Compose checks.
- [ ] Reconcile `README.md`, `handoff.md`, and `deploy/macos.env.example` defaults and status text.
- [ ] Run deployment/configuration tests and validate Compose syntax without exposing secrets.

### Task 4: Frontend request, rendering, and bundle efficiency

**Files:**
- Modify: `frontend/src/App.tsx`, `frontend/src/MarketApp.tsx`, `frontend/src/IntradayMetricChart.tsx`, `frontend/src/DailyKChart.tsx`, `frontend/src/api.ts`, `frontend/src/main.tsx`
- Test: `frontend/src/App.test.tsx`, `frontend/src/MarketApp.test.tsx`, `frontend/src/IntradayMetricChart.test.tsx`, `frontend/src/DailyKChart.test.tsx`

**Interfaces:**
- Preserve public API calls and existing visual/data ordering.
- Keep WebSocket reconnect behavior and local tab semantics compatible.

- [ ] Add tests for lazy daily-K loading, aborting superseded requests, bounded/multiplexed task events, and route-level dynamic loading.
- [ ] Run frontend tests and confirm the new tests fail.
- [ ] Implement lazy day-K fetches, request cancellation, event subscription consolidation or bounded history, chart memoization/rAF batching, and route-based dynamic imports.
- [ ] Run all frontend tests and `npm run build`; record bundle sizes.

### Task 5: Structural cleanup and final verification

**Files:**
- Modify: `level2_service/api.py`, `level2_service/queue.py`, `level2_service/direct_market.py`, `scripts/macos_deploy.py`, `README.md`, `handoff.md`
- Test: existing full test suites plus new regression coverage from Tasks 1–4

- [ ] Identify dead compatibility paths and split only the modules touched by the fixes; do not alter behavior solely for cosmetic reasons.
- [ ] Add structured safe logging for swallowed failures and lifecycle-loop supervision.
- [ ] Update architecture documentation with queue/event/cache invariants and operational limits.
- [ ] Run `git diff --check`, the full Python suite, the full frontend suite, `npm run build`, and read-only local health probes.
- [ ] Review the final diff for accidental secrets, generated artifacts, and violations of `AGENTS.md`.

