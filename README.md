# 同花顺 Level2 截图服务

This is a single-host deployment: a public web/API service controls two
administrator-owned Android instances over private ADB connections. It does
not store THS login details or bypass login/CAPTCHA/device checks. Data-only
tasks ask the already logged-in App's own curve manager to perform its normal
authentication, signing, and market-data requests, then read the App-parsed
callback through Frida; the service does not reimplement or expose the private
wire protocol. OCR is used only for non-data structural validation of an
optional long screenshot and never fills task metrics. Data-only tasks skip search, text input, stock
page switching, scrolling, screenshots, stitching, OCR, and PNG storage. The
deployment is **not supported yet** until the real APK
passes the smoke checklist below.

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
GiB free disk. Preflight also requires the exact external APK SHA-256:
`2554490aa3f5e2df17ac0a711311f3f85ee3130008af9bb4ab12510b3d6e971e`, and at
least one ARM ABI (`arm64-v8a` or `armeabi-v7a`). The 204 MB APK is deliberately
not in this repository, Docker build context, or Git history.

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

1. Create the same secret file and bootstrap the two isolated native Android
   VMs (Android SDK command-line tools must already be on `PATH`):

   ```sh
   ./scripts/setup-admin.sh .env
   python3 scripts/preflight.py --apk-only --apk /absolute/path/to/ths.apk
   ./scripts/bootstrap-macos-dual-avd.sh \
     /absolute/path/to/ths.apk \
     /absolute/path/to/frida-server-16.7.19-android-arm64
   python3 scripts/preflight.py --profile macos-avd --apk /absolute/path/to/ths.apk
   ```

2. Make the non-secret Mac bridge settings, then start only the web/API/Redis
   services. The host ADB server remains on its default local endpoint; do not
   start it with `adb -a` and do not publish 5037.

   ```sh
   cp deploy/macos.env.example deploy/macos.env
   docker compose --env-file deploy/macos.env -f deploy/compose.yml up -d --build
   ```

3. The bootstrap keeps `THS_API_33_ARM64 / emulator-5554` and its login
   untouched for `main_fund_flow`. It creates the clean
   `THS_CORE_33_ARM64 / emulator-5556` only when absent, then pauses for manual
   login with the big-order-enabled account. It never accepts credentials.
   Frida server `16.7.19` is forwarded independently:

   ```sh
   adb -s emulator-5554 forward tcp:27042 tcp:27042
   adb -s emulator-5556 forward tcp:27043 tcp:27042
   ```

`host.docker.internal:5037` is an internal Docker Desktop bridge, not a public
port. Dual mode uses `CORE_ADB_SERIAL`, `CORE_FRIDA_SERVER_ENDPOINT`,
`FUND_ADB_SERIAL`, and `FUND_FRIDA_SERVER_ENDPOINT`; if any one is set, all
four are required. Legacy `ADB_SERIAL` and `FRIDA_SERVER_ENDPOINT` remain
available for a single device.

## Image, volumes, and HTTP access

The API image includes the built React frontend and uses multi-architecture
base images. Publish it where needed with, for example:

```sh
docker buildx build --platform linux/amd64,linux/arm64 --target api -t example/ths-level2-api:latest --push .
```

Compose persists `capture-data`, `redis-data`, `redroid-data` (Linux only),
`template-data`, `admin-data`, and `market-data`. The last volume contains the
SQLite user and grouped-watchlist database; ordinary browser sessions remain
revocable Redis records. Put only manually calibrated,
non-secret PNG anchors under `template-data` (`search.png` and optional tab
anchors); the API loads them as an OpenCV fallback after selector checks.
Capture retention remains the API's 24-hour cleanup policy; queue metadata
retention remains seven days. Do not remove any volume as part of an upgrade
unless its data has been deliberately backed up.

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

Public submissions accept `{"symbol":"601872","include_long_capture":true}`.
The screenshot option defaults to `true` for existing clients. Set it to
`false` to request the eight required values plus optional three-period fund
flow without any App UI navigation or image creation. Core metrics and symbol
lookup use only the core account; fund flow uses only the preserved fund
account. The response also includes current-day intraday curves for large-order
net volume, large-order amount, and retail count in `values.intraday_series`.
Those curves come from the same core App-internal direct request; the existing
scalar fields remain the latest points. Confirmed market mappings are
`600/601/603/605/688/689 → 17`,
`000/001/002/003/300/301 → 33`, `920 → 151`,
`501/502/506/508/510/511/512/513/515/516/517/518/519/520/526/530/551/560/561/562/563/588/589 → 20`,
and `158/159/160/161/162/163/164/165/166/167/168/169/180 → 36`; an unknown
prefix is rejected instead of guessed. If an exact code is shared by a bond
and a fund, the App result is filtered to the expected fund market and the bond
market is ignored. Such a result reports `long_capture.status` as `SKIPPED`;
missing App callback fields make the task `PARTIAL` and never trigger UI,
screenshot, or OCR fallback.

The market PWA shares one server-side subscription broker across users. During
weekday A-share sessions it refreshes the selected detail symbol every 2
seconds and watchlist-only symbols every 15 seconds; outside the configured
09:30–11:30 and 13:00–15:00 Asia/Shanghai windows it uses a 60-second cadence.
The independently owned fund-flow account is capped at one request per symbol
per 15 seconds even while the core quote refreshes faster. These are polling
cadences, not an exchange tick guarantee: measured App callback latency and
device availability still determine when a fresh frame arrives. The current
App response provides 241 one-minute points for the trading-day curves. The
Market detail page separately loads a front-adjusted daily K-line once per
selected symbol from the 10jqka public yearly line endpoint. It returns 240
visible bars after 249 hidden warm-up bars and computes MA(5/13/21/60/120/250),
BOLL(20,2), MACD(12,26,9), and volume server-side. This public chart source is
isolated from job values: it never fills or replaces any of the eight task
metrics, which remain App-interface-only. Daily K-line responses expose their
source, adjustment, cache, stale, and per-source error state. Fresh cache TTL is
60 seconds during trading sessions and 15 minutes outside them; stale data is
served only after both the public source and the App fallback path fail.

A core-device probe confirmed that the App constructs daily K requests with
`period=5`, front-adjustment `quan=10`, and data IDs `1/7/8/9/11/13/19` for
date/open/high/low/close/volume/amount. The independent Frida request path is
still deliberately gated as `DIRECT_KLINE_UNAVAILABLE` because it has not yet
completed reliably outside the visible chart controller. It is not enabled by
guessing or by UI/OCR extraction. Ten-level order book and time-and-sales panels
also stay explicitly unavailable until their exact App-internal contracts are
confirmed.

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
