# 同花顺 Level2 截图服务

This single-host deployment serves an asynchronous Level2 task API and a
multi-user market application. Exact security identity comes from a versioned
local SQLite catalog refreshed from Sina public lists. Basic market quote,
intraday, and period data come from Tencent/Sina public endpoints, while daily
qfq K-line prefers the public Tonghuashun web feed. The original eight task
metrics remain isolated to the selected verified App-internal transport; in
the current Mac deployment both core metrics and fund flow use direct
server-side clients with encrypted bundles captured after normal human login.

The service never accepts THS account passwords or bypasses login, CAPTCHA, or
device checks. The App is needed only for human session bootstrap/renewal and
explicit long captures. OCR is limited to structural validation of an optional
long screenshot and never fills task metrics. Data-only tasks do not navigate
the App, scroll, capture, stitch, or run OCR.

## Supported profiles only

- `linux-redroid`: Linux **amd64** host, Docker with privileged containers and
  Binder/binderfs. Redroid Android 13 runs as an internal Compose service with
  guest/software rendering, 1080×1920, 480 DPI, `androidboot.use_memfd=1`, and
  persistent `/data`. A host must expose `/dev/binder`, `/dev/hwbinder`,
  `/dev/vndbinder`, and `/dev/binderfs`; check kernel Binder/memfd support
  before deploying. Rootless Docker and generic/unverified VPS configurations
  are intentionally rejected.
- `macos-avd`: Apple Silicon Mac only. Redis and the combined web/API service
  run in Docker; the API 33 `arm64-v8a` Android VM runs natively on that same
  Mac. It is not Dockerized. Docker reaches the host's default localhost ADB
  server through `host.docker.internal`, with no public ADB port mapping.

Both profiles require a practical minimum of 4 CPU cores, 8 GiB RAM, and 30
GiB free disk. Preflight also requires the verified APK SHA-256:
`2554490aa3f5e2df17ac0a711311f3f85ee3130008af9bb4ab12510b3d6e971e`, and at
least one ARM ABI (`arm64-v8a` or `armeabi-v7a`). The 204 MB APK is tracked in
Git history, but the old Docker build context and image excluded it. The
approved image is local/private-only and deliberately adds the APK only after
fixed digest verification; it never includes credentials, sessions, AVD data,
or login snapshots.

## Linux amd64 / Redroid

1. Create the deployment secret file. It prompts without echoing and creates a
   mode-0600 `.env` containing only an Argon2id `ADMIN_PASSWORD_HASH` and a
   random `ADMIN_SESSION_SECRET`:

   ```sh
   ./scripts/setup-admin.sh .env
   ```

2. Check the host and the separately supplied APK. This must pass before any
   Compose start:

   ```sh
   python3 scripts/preflight.py --profile linux-redroid --apk /absolute/path/to/ths.apk
   ```

3. Build and start the isolated profile. Redis and ADB have no host `ports:`
   entries; the combined FastAPI web/API service publishes HTTP port 8000.

   ```sh
   docker compose -f deploy/compose.yml --profile linux-redroid up -d --build
   ```

4. Install the verified external APK without adding it to the project. Copying
   this temporary file into the API container does not persist credentials or
   place the APK under Git control:

   ```sh
   api_id=$(docker compose -f deploy/compose.yml ps -q api)
   docker cp /absolute/path/to/ths.apk "$api_id":/tmp/ths.apk
   docker compose -f deploy/compose.yml exec api adb -s redroid:5555 install -r /tmp/ths.apk
   ```

The Redroid service requires the documented privileged Binder mount. If its
host preflight fails, do not remove `privileged`, substitute an x86 APK, or
claim the VPS is supported.

## Apple Silicon macOS / native AVD

### Approved complete-image and one-command contract — pending implementation

Tasks 7–9 implement and verify this contract. This documentation does not claim
real deployment or clean-Mac acceptance before those tasks run; no host, device,
image, login, or provisioning action is performed by documenting it.

The approved `ths-level2-api:local` image is local/private-only. It will carry
the fixed THS APK, Frida Server 16.7.19, a non-secret manifest, and read-only
deployment helpers. Build-time digest checks cover the APK and Frida assets.
It must not carry `.env`, tokens, account credentials, encrypted sessions, AVD
data, captures, Redis data, or logs. It must not run `docker push`,
`docker save`, or publish to a public registry.

The standard future entry point is:

```sh
scripts/deploy-macos-one-click.sh --mode auto
```

It will use Apple Silicon macOS and the explicit OrbStack context only. It
checks the Android/Java/OrbStack prerequisites, accepted Android image license,
asset digests, mode-0600 `.env`, dual-role `deploy/macos.env`, and that no
device task is running. It never installs Homebrew or OrbStack and never
accepts a third-party license for the operator. The Compose rebuild remains:

```sh
docker --context orbstack compose --env-file .env --env-file deploy/macos.env -f deploy/compose.yml up -d --build
```

When both fixed existing AVDs are present, `auto` preserves them: it does not
create, delete, copy, wipe, reset, reinstall, or alter either App's data. It
checks the installed APK digest and returns `INSTALLED_APK_MISMATCH` instead of
overwriting a different installation. It then installs the stable local
lifecycle/bridge LaunchAgents, starts only stopped AVDs through the lifecycle
broker, launches only the THS entry Activity, and rebuilds API/Redis without
deleting data volumes.

When an AVD is fresh or partially missing, `auto` records which roles already
exist and creates and installs assets only for the missing fixed role(s).
Existing AVDs remain untouched. New AVDs require human THS login, CAPTCHA or
device verification, and permission confirmation; the recoverable stopping
state is `FIRST_TIME_LOGIN_REQUIRED`. It never enters credentials or turns this
human-login gate into unattended provisioning.

The lifecycle broker is installed with
`scripts/install-macos-device-lifecycle.sh` as a macOS LaunchAgent. The root
`.env` is the source for Compose/API; the installer copies the same lifecycle
Token into the mode-0600 host config required by the broker. That host config
is private, and the Token is never exposed through a plist, log, or browser;
it is also never written to `deploy/macos.env` or API responses. Operators
acquire the device lock, wait for running tasks to finish, perform one device
action at a time, release the lock, and explicitly resume the queue. Relevant
fixed errors include
`DEVICE_LIFECYCLE_UNAVAILABLE`, `DEVICE_LIFECYCLE_LOCK_REQUIRED`,
`DEVICE_LIFECYCLE_BUSY`, `DEVICE_ACTION_IN_PROGRESS`,
`DEVICE_AVD_NOT_FOUND`, `DEVICE_BOOT_TIMEOUT`, `DEVICE_APP_LAUNCH_FAILED`,
`DEVICE_SHUTDOWN_FAILED`, `DEVICE_LIFECYCLE_FAILED`,
`INSTALLED_APK_MISMATCH`, and `FIRST_TIME_LOGIN_REQUIRED`.

To roll back, remove the lifecycle URL and token from the Compose environment,
unload the `com.ths.device-lifecycle` LaunchAgent, and return to the existing
manual AVD workflow. Do not delete AVDs, login data, session bundles, or Docker
data volumes; public Market and direct collection remain available.

`host.docker.internal:5037` is an internal Docker Desktop bridge, not a public
port. Dual mode uses `CORE_ADB_SERIAL`, `CORE_FRIDA_SERVER_ENDPOINT`,
`FUND_ADB_SERIAL`, and `FUND_FRIDA_SERVER_ENDPOINT`; if any one is set, all
four are required. Legacy `ADB_SERIAL` and `FRIDA_SERVER_ENDPOINT` remain
available for a single device.

The Mac profile also carries non-secret public-data and warm-connection
settings:

```dotenv
SYMBOL_CATALOG_PATH=/data/market/symbol-catalog.db
SYMBOL_CATALOG_MAX_AGE_SECONDS=604800
SYMBOL_CATALOG_REFRESH_HOUR=16
SYMBOL_CATALOG_REFRESH_MINUTE=20
PUBLIC_MARKET_TIMEOUT_SECONDS=8
MARKET_DIRECT_ENRICHMENT=1
MARKET_DIRECT_ENRICHMENT_TTL_SECONDS=15
CORE_WARM_CONNECTION_MAX_IDLE_SECONDS=25
```

## Image, volumes, and HTTP access

The API image includes the built React frontend. The approved macOS image is
local/private-only and must not be pushed, saved, or exported until a separate
APK-distribution authorization and private-registry policy exists.

Compose persists `capture-data`, `redis-data`, `redroid-data` (Linux only),
`template-data`, `admin-data`, and `market-data`. The last volume contains the
SQLite user/grouped-watchlist database and the versioned public security
catalog (`symbol-catalog.db`); ordinary browser sessions remain revocable
Redis records. Put only manually calibrated,
non-secret PNG anchors under `template-data` (`search.png` and optional tab
anchors); the API loads them as an OpenCV fallback after selector checks.
Capture retention remains the API's 24-hour cleanup policy. Task metadata and
interface values persist without an automatic expiry, while each browser keeps
its own permanent stock-tab list in localStorage. Startup migration keeps one
canonical task per symbol and physically removes older duplicates. Do not remove
any volume as part of an upgrade unless its data has been deliberately backed up.

The approved local deployment serves both the React site and API from
`http://HOST:8001/`; set `APP_PORT` to change the host-side port. Redis 6379,
Redroid ADB 5555, and macOS ADB 5037 remain private. The administrator console
is `http://HOST:8001/#admin`. It shows both devices through independent
same-origin WebSockets; the fund panel is marked “当前账号，禁止退出”.
The multi-user market PWA is `http://HOST:8001/market`. Administrators create
ordinary users and temporary passwords in the “行情用户” section; each user
must change that password on first login and receives an isolated grouped
watchlist (50 unique symbols maximum).
For this HTTP deployment, `ADMIN_COOKIE_SECURE=0` allows the administrator
session to survive page refreshes.

Plain HTTP does not encrypt the administrator password, session cookie, device
screen, or input events. Use this mode only on a trusted local network. If the
service is later published through a trusted external HTTPS reverse proxy, set
`ADMIN_COOKIE_SECURE=1` and restrict direct access to port 8001.

The current Mac profile sets both `CORE_METRICS_TRANSPORT=direct` and
`FUND_FLOW_TRANSPORT=direct`. Direct/shadow modes require a Fernet
`THS_SESSION_ENCRYPTION_KEY`; cookies, auth packets, templates, and indicator
parameters are stored only in encrypted bundles under
`/data/admin/ths-sessions` and never appear in task or management responses.

The core client implements the verified authenticated 9528 frame state machine
plus pure-Python Snappy, `gov`, `cv3`, HXLONG, retail-count (`216`),
large-order-net (`33007`), large-order-amount (`33015`), and MACDFS decoding.
It keeps one single-use pre-authenticated connection: checkout permanently
removes that socket, every business request closes it, and a background refill
prepares the next connection. Session refresh invalidates all warm state.
Unsupported encryption/compression and malformed frames fail closed.

The task Runner is woken immediately after a durable enqueue/retry/resume while
Redis remains the FIFO authority and the configured poll interval remains an
external-enqueue fallback.

After manual login, an authenticated administrator can inspect
`GET /api/admin/account-sessions` and refresh a role with
`POST /api/admin/account-sessions/{role}/refresh` plus the existing CSRF header.
These endpoints never accept account passwords and never return session values.

Public submissions accept `{"symbol":"601872","include_long_capture":true}`.
The collection form accepts a stock-name or code prefix. `GET /api/v1/symbols`
and `GET /api/v1/symbols/{symbol}` read the active local catalog; task
submission rechecks the same active version before enqueueing. The catalog is
refreshed daily from Sina public A-share/ETF/LOF categories, activates only a
complete validated version, retains the previous version on failure, and fails
closed after seven days. Symbol lookup and suggestions do not call App or
Frida. Removing a browser tab never deletes or cancels the server-side task.

The screenshot option defaults to `true` for existing clients. Set it to
`false` to request the eight required values plus optional three-period fund
flow without App navigation or image creation. The response also includes
current-day direct curves for large-order net volume, large-order amount, and
retail count in `values.intraday_series`; scalar fields are their latest points.
Confirmed market mappings are
`600/601/603/605/688/689 → 17`,
`000/001/002/003/300/301 → 33`, `920 → 151`,
`501/502/506/508/510/511/512/513/515/516/517/518/519/520/526/530/551/560/561/562/563/588/589 → 20`,
and `158/159/160/161/162/163/164/165/166/167/168/169/180 → 36`; an unknown
prefix is rejected. Missing direct fields make the task `PARTIAL` and never
trigger UI, screenshot, public-quote, or OCR fallback.

The market PWA shares one server-side subscription broker across users. During
weekday A-share sessions it refreshes the selected detail symbol every 2
seconds and watchlist-only symbols every 15 seconds; outside the configured
09:30–11:30 and 13:00–15:00 Asia/Shanghai windows it uses a 60-second cadence.
Selected/watchlist snapshots use Tencent public quote and minute endpoints;
Sina public quote is the basic fallback. Five-day, week, and month series use
Tencent qfq data. Daily K-line prefers the 10jqka public yearly line endpoint,
falls back to Tencent qfq, and then to validated stale cache—never App. It
returns 240 visible bars after indicator warm-up and computes
MA(5/13/21/60/120/250), BOLL(20,2), MACD(12,26,9), and volume server-side.
Stocks display two price decimals and exchange-traded funds three.

When both task transports are explicitly `direct`, the market detail snapshot
may merge a 15-second cached L2 enhancement containing large-order, retail,
MACDFS, and fund-flow fields. This enhancement cannot overwrite public name,
price, OHLC, turnover, volume, amount, or public intraday points, and its
failure cannot make the public snapshot unavailable. Per-client WebSocket
delivery keeps the latest event for each symbol; the frontend reconnects with
bounded exponential backoff and refreshes the selected snapshot over HTTP while
disconnected. Ten-level order book and time-and-sales remain explicitly
unavailable until an exact supported source exists.

Taking the administrator device lock pauses new Runner work automatically. The
lock release leaves the queue paused; use the explicit queue-resume control
after manual login or recovery. Failed jobs can be retried from the admin page
with their task ID, while a generated long screenshot and any recognized
values remain available.

## Smoke acceptance checklist

Do this with the real APK and a real administrator after preflight. Passing
unit tests or Compose parsing does not establish platform support.

- [ ] APK installs and launches in the selected Android instance.
- [ ] An administrator completes normal manual THS login; no automation
      bypasses login, CAPTCHA, device verification, or entitlement gates.
- [ ] The app stays alive for five minutes after login.
- [ ] A stock page can be opened once and scrolled through all four stacked
      charts, including 大单净量, 大单金额, and 散户数量.
- [ ] Public job `601872` returns 股票名称, 当前股价, 当前涨跌幅, 换手率,
      散户数量, 大单净量, 大单金额, and the latest MACDFS point, plus one
      complete stitched long screenshot, within 120 seconds excluding queue time.
- [ ] The long screenshot appears under the capture volume, covers the stock
      header through the bottom chart, and is available from the public result
      page for 24 hours.

Real APK selector identifiers and visual templates still require this manual
verification. Until it succeeds, these are deployment candidates—not a claim
that either host profile is supported.
