# Observability and Market Stability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans or implement task-by-task with the same test-first checks.

**Goal:** Stabilize the non-account-related issues observed from 2026-09-15 through 2026-09-30 and redeploy with actionable request and data-quality telemetry.

**Architecture:** Keep App-internal data ownership unchanged. Make catalog/calendar readiness explicit, add safe error provenance and timing fields to request logs, distinguish valid/partial/empty fund-flow batches without filling missing values, and enforce an end-to-end deadline around market snapshot refreshes.

**Tech Stack:** Python, FastAPI, SQLite, asyncio, pytest, OrbStack Docker Compose.

**Spec:** User-requested remediation based on the 2026-09-15–2026-09-30 log audit; 2026-09-21 account anomaly is excluded.

## Global Constraints

- Task metrics remain App-internal; public quote sources never fill task metrics.
- Missing fund-flow fields remain null; no OCR or UI fallback is introduced.
- Deployment uses OrbStack with `.env`, `deploy/macos.env`, and `deploy/compose.yml`; preserve Redis and emulator volumes.
- Never log cookies, User-Agent values, auth packets, or protocol keys.

## Review Focus

- A stale or missing symbol catalog must fail with the original readiness code and recover after a validated refresh.
- A missing trading calendar must not silently create weekend research rows.
- A partial fund-flow response must remain partial and be visible in status telemetry.
- A slow snapshot source must not hold an HTTP request for minutes.
- Repeated identical failures must retain source detail while avoiding opaque `HTTP_503`-only diagnosis.

### Task 1: Catalog and Trading-Day Readiness

**Files:**
- Modify: `level2_service/symbol_catalog.py`, `level2_service/main.py`, `deploy/compose.yml`
- Test: `tests/test_symbol_catalog.py`, `tests/test_deployment.py`, `tests/test_workday_calendar.py`

- [ ] Add failing tests for stale-catalog refresh readiness, calendar path provisioning, and weekend gating.
- [ ] Implement the smallest readiness/configuration fix without changing market-data ownership.
- [ ] Run targeted tests, then the full Python suite.

### Task 2: Request Error Provenance and Timing

**Files:**
- Modify: `level2_service/request_logs.py`, `level2_service/api.py`
- Test: `tests/test_request_logs.py`, relevant API tests.

- [ ] Add failing tests proving sanitized error detail/source and timing fields are persisted.
- [ ] Implement safe structured fields while preserving existing filters/API compatibility.
- [ ] Run targeted and full tests.

### Task 3: Fund-Flow Batch Quality

**Files:**
- Modify: `level2_service/fund_flow_history.py`, `level2_service/research_store.py`
- Test: `tests/test_fund_flow_history.py`, `tests/test_research_store.py`.

- [ ] Add failing tests for unit-only/metric-empty batches and valid partial batches.
- [ ] Record explicit partial/empty quality without filling null fields.
- [ ] Run targeted and full tests.

### Task 4: Market Snapshot Deadline

**Files:**
- Modify: `level2_service/market_data.py`, `level2_service/api.py`
- Test: `tests/test_market_data.py`, relevant API tests.

- [ ] Add a failing test showing a blocked source is cut off at the configured request deadline.
- [ ] Implement bounded refresh behavior and preserve source error semantics.
- [ ] Run targeted and full tests.

### Task 5: Redeploy and Verify

- [ ] Build/restart with the canonical OrbStack Compose command.
- [ ] Verify health, symbol lookup readiness, admin/market status, and recent logs.
- [ ] Confirm Redis and emulator volumes remain attached and no protected fund-account lifecycle action occurred.
