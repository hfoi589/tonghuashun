# App-Independent Market and Warm Direct Transport Design

**Date:** 2026-08-27
**Status:** Approved in chat
**Scope:** Core 9528 request latency, symbol confirmation/search, market data, lifecycle scheduling, documentation, deployment, and release

## 1. Goal

Make the production service independent from a running Tonghuashun App for:

- task-time core metric collection after a human-authenticated session bundle exists;
- exact symbol confirmation and name/code suggestions;
- the market application's basic quote, intraday, and K-line experience.

The Tonghuashun App remains only a human-operated session bootstrap and refresh mechanism for private direct transports. It must not be launched, navigated, stopped, or queried during ordinary background tasks, symbol lookup, or public market polling.

## 2. Non-goals

- Do not automate account passwords, verification codes, device verification, or session renewal.
- Do not replay a cached authentication response on a new TCP connection.
- Do not reuse a 9528 connection after one completed business request sequence. Isolated tests showed that a second request sequence on the same used connection returns non-curve frames rather than a valid result.
- Do not use OCR, screenshots, UI text, or guessed values for task metrics.
- Do not make public quote providers authoritative for the original eight task metrics.
- Do not promise ten-level order books or trade-by-trade data when the selected public providers expose only lower-detail market data.

## 3. Data ownership

| Data | Authoritative production source |
| --- | --- |
| Exact symbol identity and suggestions | Versioned local SQLite catalog refreshed from Sina public security lists |
| Basic market quote | Tencent public quote feed; Sina public quote fallback |
| Current-day intraday price | Tencent public minute feed |
| Five-day, week, and month market series | Tencent public K-line feeds |
| Front-adjusted daily K-line | Existing Tonghuashun public web K-line feed; Tencent public qfq fallback |
| Original eight task metrics | Verified 9528 direct transport only when `CORE_METRICS_TRANSPORT=direct`; otherwise the explicitly selected existing transport |
| Large-order/retail/MACDFS market enhancement | Optional 9528 direct enrichment, never required for the basic market snapshot |
| 1/3/5-day main fund flow | Existing verified fund HTTP direct transport |

Public symbol or quote data must never fill any field in an asynchronous task's eight required metric values.

## 4. Core 9528 warm connection design

### 4.1 Connection model

The service will maintain at most one ready, pre-authenticated 9528 TCP connection for the single core task consumer.

Each prepared connection has these states:

1. `CONNECTING`: TCP connection is being established.
2. `AUTHENTICATING`: the cached encrypted session's authentication packet has been sent and authentication frames are being read.
3. `READY`: authenticated but no business request has been sent.
4. `CHECKED_OUT`: atomically removed from the pool for one request.
5. `CLOSED`: business request completed or any failure occurred.

A checked-out connection is never returned to the pool. Success, timeout, invalid frame, EOF, decode failure, session change, and shutdown all close it.

### 4.2 Protocol boundaries

`Core9528TemplateProtocol` will expose three internal phases:

```python
prepare(material: object, symbol: str) -> CoreRequestMaterial
authenticate(prepared: CoreRequestMaterial) -> WarmCoreConnection
read_authenticated(
    warm: WarmCoreConnection,
    prepared: CoreRequestMaterial,
    symbol: str,
    market: str,
) -> DirectReadOutcome
```

`CoreRequestMaterial` contains validated host, port, authentication packet, patched request batches, MACDFS parameters, timeout, and an in-memory session fingerprint. Secret material remains `repr=False` or otherwise excluded from representations and logs.

### 4.3 Pool behavior

`Core9528WarmPool` will:

- target exactly one `READY` connection;
- protect its deque, session fingerprint, refill state, and closed state with a thread lock;
- never perform network I/O or decoding while holding the state lock;
- atomically remove a connection on acquire;
- discard ready connections older than 25 seconds;
- invalidate all ready connections when the encrypted session bundle fingerprint changes;
- asynchronously replenish only after the checked-out business request has
  completed and the socket has closed;
- allow a synchronous cold authentication when no ready connection exists;
- expose `prewarm()`, `invalidate()`, and `close()` lifecycle methods.

The fingerprint is a SHA-256 digest computed in memory from the session update timestamp and validated core wire material. The digest may be retained in memory; raw authentication packets and keys may not be logged or returned.

### 4.4 Prewarm triggers

Prewarming occurs without App interaction:

- after a successful exact symbol confirmation;
- immediately before a job is enqueued;
- after a checked-out connection finishes its single business request;
- after a successful administrator refresh of the core session bundle;
- during service startup when a valid core bundle exists.

Background prewarm failures update only sanitized internal health state. They do not fail an unrelated in-flight task. A task observes only errors from its own acquired or synchronously created connection.

### 4.5 Error compatibility

The pool introduces no public task error codes. Existing codes remain authoritative:

- `DIRECT_SESSION_UNAVAILABLE`
- `DIRECT_SESSION_EXPIRED`
- `DIRECT_PROTOCOL_HANDSHAKE_FAILED`
- `DIRECT_PROTOCOL_RESPONSE_TIMEOUT`
- `DIRECT_PROTOCOL_RESPONSE_INVALID`

The pool must not reinterpret a generic malformed response as session expiry and must not fall back to App, UI, OCR, or screenshots.

## 5. Immediate runner wake

The Redis queue remains the durable FIFO authority. An in-process `RunnerWake` only removes avoidable latency for API submissions handled in the same service process.

`RunnerWake` uses an `asyncio.Event` owned by the lifespan event loop. Synchronous API handlers notify it through `loop.call_soon_threadsafe(event.set)`.

The runner loop will:

1. clear the wake event;
2. execute `runner.run_once()` in a worker thread;
3. immediately continue while work was claimed, draining backlog without a polling pause;
4. wait for either a wake notification, shutdown, or the existing poll interval as a fallback for externally enqueued Redis tasks.

Successful transitions to `QUEUED` notify the runner from:

- new public job submission;
- public retry;
- administrator retry;
- administrator resume;
- queue resume.

## 6. Versioned public symbol catalog

### 6.1 Source

The production catalog provider reads these public Sina nodes:

- `hs_a`
- `etf_hq_fund`
- `lof_hq_fund`

Only codes accepted by `market_code_for_symbol()` are published. The source exchange prefix must agree with the expected market:

- `sh` for market `17` and `20`;
- `sz` for market `33` and `36`;
- `bj` for market `151`.

Duplicate rows with identical identity and name are deduplicated. Conflicting names or source exchanges for the same supported identity reject the candidate catalog version.

### 6.2 Storage

The catalog is stored separately at `/data/market/symbol-catalog.db` with versioned tables:

```text
catalog_versions(version_id, source, fetched_at, status, row_count, checksum, error_code)
catalog_securities(version_id, symbol, name, market, exchange, kind, name_norm)
catalog_active(singleton_id, version_id)
```

Indexes cover `(version_id, symbol)`, `(version_id, name_norm)`, and `(version_id, market)`.

A refresh inserts and validates a complete candidate version before a short transaction changes `catalog_active`. A failed refresh never modifies the active version. Old versions are retained long enough for rollback and then pruned.

### 6.3 Refresh and validation

- Refresh at startup when no active catalog exists or the active catalog is older than 18 hours.
- Schedule a refresh once per day after 16:20 Asia/Shanghai.
- Treat the active catalog as readable for up to seven days when refreshes fail.
- Reject a first catalog with fewer than 5,000 supported securities.
- Reject a later catalog whose total count is below 90% of the active version unless an explicit maintenance override is used.
- Require non-empty names, exact six-digit codes, valid source exchange prefixes, valid market mapping, and a deterministic checksum.

### 6.4 Lookup semantics

`GET /api/v1/symbols/{symbol}`, task submission verification, market watchlist additions, market snapshot routes, and market series routes all use the local catalog. None of these routes call Frida or the App.

- malformed or unsupported code: `422`;
- healthy catalog with no exact row: `404`;
- unavailable catalog or catalog older than seven days: `503` with a fixed catalog error code;
- exact row: existing response shape `{symbol, name, market}`.

Search supports code and Chinese-name input. Ranking is deterministic:

1. exact code;
2. code prefix;
3. exact normalized name;
4. name prefix;
5. name substring;
6. symbol ascending as the stable tie-breaker.

The existing result limit of eight remains unchanged. SQL wildcard characters are escaped and input length limits remain enforced.

Watchlist responses use the current catalog name when available, so corporate renames do not require removing and re-adding an item. Stored watchlist identity remains a historical fallback only.

## 7. Public market data plane

### 7.1 Provider chain

The market application's basic snapshot does not depend on a core session.

Primary provider:

- Tencent public quote endpoint for lightweight watchlist snapshots.
- Tencent public minute endpoint for detail snapshots and current-day intraday points.
- Tencent public qfq K-line endpoint for five-day, week, month, and daily fallback series.

Fallback provider:

- Sina public quote endpoint for basic quote fields when Tencent quote retrieval fails.

Daily K-line provider chain:

1. existing Tonghuashun public qfq daily feed;
2. Tencent public qfq daily feed;
3. stale validated local cache;
4. empty `KLINE_SOURCES_UNAVAILABLE` page when no cache exists.

Eastmoney is not a production dependency because it succeeded from the host but was repeatedly disconnected from the deployed API container.

### 7.2 Provider symbol mapping

Tencent identifiers:

- market `17` and `20`: `sh<symbol>`;
- market `33` and `36`: `sz<symbol>`;
- market `151`: `bj<symbol>`.

The provider response code and name must match the requested catalog identity. Mismatches are invalid responses rather than alternative matches.

### 7.3 Normalization

All public providers normalize before publishing:

- prices preserve the provider precision, normally two decimals for stocks and three for exchange-traded funds;
- `MarketSnapshot.price_precision` carries the required display precision;
- volume is normalized to shares;
- amount is normalized to yuan;
- percentage values include `%` and use two decimals;
- intraday time uses `HH:mm`, is monotonically increasing, and is filtered to supported exchange sessions;
- cumulative Tencent minute volume is converted to per-minute volume;
- standard intraday MACD DIF/DEA may be calculated locally from public price points;
- OHLC values are non-negative and satisfy high/low bounds;
- no upstream response body, URL query, or exception detail is copied into public errors.

### 7.4 Snapshot behavior

For `detail=false`, fetch only a lightweight public quote. For `detail=true`, fetch quote plus minute data.

The basic quote contains:

- price;
- change percentage;
- open, high, low, previous close;
- turnover rate;
- volume and amount;
- source time.

Tencent failure plus successful Sina quote returns a valid basic snapshot with `source=SINA_PUBLIC` and no fresh intraday points. If all providers fail and the broker has a previous snapshot, the broker keeps and publishes the stale snapshot. With no cache, the HTTP route returns `503 MARKET_QUOTE_UNAVAILABLE`.

### 7.5 Series behavior

- `day`: Tonghuashun public qfq first, Tencent qfq fallback;
- `five_day`: the latest five Tencent qfq daily bars;
- `week`: Tencent qfq weekly bars;
- `month`: Tencent qfq monthly bars;
- unsupported lower-level periods retain an explicit fixed capability error rather than calling App.

Derived MA, BOLL, and MACD indicators continue to use the existing local calculations.

### 7.6 Optional direct L2 enrichment

When `MARKET_DIRECT_ENRICHMENT=1` and the core/fund direct clients are configured, detail snapshots may merge a cached direct enrichment no more frequently than once per symbol every 15 seconds.

The enrichment owns only:

- large-order net value and curve;
- large-order amount and curve;
- retail count and curve;
- the proprietary MACDFS curve;
- 1/3/5-day main fund flow.

It may not overwrite public name, price, change percentage, OHLC, turnover, volume, amount, or public intraday price points. Direct errors populate sanitized `source_errors` and leave enrichment fields empty; they never make the basic public snapshot unavailable.

Core enrichment calls are serialized with task core calls. A market enrichment can delay a task by at most one short direct request but cannot create concurrent request sequences for the same core session.

## 8. Market delivery reliability

- Preserve current detail, watchlist, and closed-market polling classes, but allow the public source to choose lightweight versus detail fetches.
- Replace the single-item per-client event queue with a bounded latest-event-per-symbol structure so one active symbol cannot overwrite another symbol's update.
- Add frontend WebSocket reconnection with bounded exponential backoff, re-subscription, and HTTP snapshot fallback while disconnected.
- Remove all user-facing text that says the market is waiting for or reading an App interface.
- Hide optional L2 sections when no valid enrichment exists, or label them as direct enhancement unavailable. Basic quote and charts remain visible.
- Label public source and stale-cache state accurately.

## 9. Configuration

Add these deployment settings with production defaults:

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

The existing `/data/market` volume persists the catalog. No emulator, Redis, or session volume is recreated.

## 10. Security and privacy

- Raw cookies, user agents, authentication packets, protocol templates, keys, and session fingerprints never appear in logs, task records, public APIs, management APIs, or exceptions.
- Public provider errors are mapped to fixed internal codes.
- Catalog input and provider payloads are treated as untrusted and validated before persistence or publication.
- Public providers are used only for security identity, quotes, and K-lines described in this design; not for private account actions.
- `emulator-5554` remains protected and is not stopped, navigated, reinstalled, cleared, or switched.

## 11. Test strategy

### Core transport

- warm connection authentication is separated from business reads;
- a checked-out connection is closed and never returned;
- two tasks consume two different authenticated sockets;
- idle expiry, session fingerprint changes, concurrent acquire, shutdown, and refill failures are covered;
- existing response timeout and invalid-frame semantics remain unchanged.

### Runner wake

- a job submitted under a long fallback poll interval starts immediately;
- new, retry, resume, and queue-resume paths notify;
- backlog drains without inter-task sleep;
- shutdown leaves no runner or wake task pending.

### Symbol catalog

- fixtures cover Shanghai, Shenzhen, Beijing, Shanghai funds, and Shenzhen funds;
- parsing, filtering, deduplication, conflicts, atomic version activation, stale reads, rejected shrink, and failed refresh rollback are covered;
- exact and suggestion ranking semantics are covered;
- App/Frida offline does not affect lookup or submission verification.

### Public market

- Tencent quote, minute, five-day, daily, weekly, and monthly fixtures cover all five market categories;
- Sina fallback quote and Tonghuashun/Tencent daily fallback are covered;
- precision, volume units, cumulative-to-minute conversion, time monotonicity, and identity mismatch validation are covered;
- public success with direct enrichment failure still returns a usable snapshot;
- stale and no-cache error paths are covered;
- WebSocket multi-symbol delivery, reconnection, re-subscription, and HTTP fallback are covered.

### Full acceptance

- complete Python and frontend test suites pass;
- three data-only tasks return eight metrics, three 241-point L2 curves, and three fund-flow periods without Frida or App interaction;
- symbol exact lookup and suggestions work with both emulators unavailable;
- market watchlist, quote, intraday, daily, five-day, weekly, and monthly views work with both emulators unavailable;
- logs and API responses pass sensitive-material scanning;
- task values never identify a public provider as their metric source.

## 12. Documentation, deployment, and release

Update:

- `AGENTS.md` to authorize the public catalog and public market data plane while keeping strict task-metric ownership;
- `handoff.md` with the deployed architecture, operational refresh behavior, verification evidence, rollback information, and remaining session-refresh dependency;
- `README.md`, deployment examples, and frontend source labels.

Deploy with the canonical OrbStack Compose command and both environment files. Preserve Redis, market, session, capture, and emulator data volumes.

After verification:

1. stage only confirmed project paths and exclude `graphify-out/`;
2. commit the completed implementation on `codex/symbol-search-tab-delete`;
3. merge that branch into the existing local `main` worktree;
4. run post-merge verification;
5. push `main` to `origin` at `https://github.com/hfoi589/tonghuashun.git`;
6. verify the remote branch and deployed API health.

The current dirty feature branch is intentionally used in place because it contains the uncommitted direct-protocol work requested for inclusion. Creating a new worktree would omit or duplicate that work. The existing separate `main` worktree remains the merge destination.
