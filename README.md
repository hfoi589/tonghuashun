# 同花顺 Level2 截图服务

This is a single-host deployment: a public web/API service controls one
administrator-owned Android instance over its private ADB connection. It does
not store THS login details, bypass login/CAPTCHA/device checks, or use private
THS protocols. The deployment is **not supported yet** until the real APK
passes the smoke checklist below.

## Supported profiles only

- `linux-redroid`: Linux **amd64** host, Docker with privileged containers and
  Binder/binderfs. Redroid Android 13 runs as an internal Compose service with
  guest/software rendering, 1080×1920, 480 DPI, `androidboot.use_memfd=1`, and
  persistent `/data`. A host must expose `/dev/binder`, `/dev/hwbinder`,
  `/dev/vndbinder`, and `/dev/binderfs`; check kernel Binder/memfd support
  before deploying. Rootless Docker and generic/unverified VPS configurations
  are intentionally rejected.
- `macos-avd`: Apple Silicon Mac only. Redis, API, and Caddy run in Docker;
  the API 33 `arm64-v8a` Android VM runs natively on that same Mac. It is not
  Dockerized. Docker reaches the host's default localhost ADB server through
  `host.docker.internal`, with no public ADB port mapping.

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
   entries; only Caddy publishes 80/443.

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

1. Create the same secret file and bootstrap the native Android VM (Android SDK
   command-line tools must already be on `PATH`):

   ```sh
   ./scripts/setup-admin.sh .env
   python3 scripts/preflight.py --apk-only --apk /absolute/path/to/ths.apk
   ./scripts/bootstrap-macos-avd.sh /absolute/path/to/ths.apk
   python3 scripts/preflight.py --profile macos-avd --apk /absolute/path/to/ths.apk
   ```

2. Make the non-secret Mac bridge settings, then start only the web/API/Redis
   services. The host ADB server remains on its default local endpoint; do not
   start it with `adb -a` and do not publish 5037.

   ```sh
   cp deploy/macos.env.example deploy/macos.env
   docker compose --env-file deploy/macos.env -f deploy/compose.yml up -d --build
   ```

`host.docker.internal:5037` is an internal Docker Desktop bridge, not a public
port. The native emulator's selected serial is `emulator-5554`; set
`ADB_SERIAL` in `deploy/macos.env` if the Android SDK assigns a different one.

## Images, volumes, and HTTPS

The API and Caddy Dockerfile targets are multi-architecture base-image builds;
publish them where needed with, for example:

```sh
docker buildx build --platform linux/amd64,linux/arm64 --target api -t example/ths-level2-api:latest --push .
docker buildx build --platform linux/amd64,linux/arm64 --target caddy -t example/ths-level2-caddy:latest --push .
```

Compose persists `capture-data`, `redis-data`, `redroid-data` (Linux only),
`caddy-data`, and `caddy-config`. Capture retention remains the API's 24-hour
cleanup policy; queue metadata retention remains seven days. Do not remove any
volume as part of an upgrade unless its data has been deliberately backed up.

For a real domain, point its DNS A/AAAA records at the host, open only TCP
80/443, set `CADDY_SITE_ADDRESS=level2.example.com` in the shell or deployment
environment, and restart Caddy. Caddy then obtains/renews HTTPS certificates.
Keep API port 8000, Redis 6379, Redroid ADB 5555, and macOS ADB 5037 private.

## Smoke acceptance checklist

Do this with the real APK and a real administrator after preflight. Passing
unit tests or Compose parsing does not establish platform support.

- [ ] APK installs and launches in the selected Android instance.
- [ ] An administrator completes normal manual THS login; no automation
      bypasses login, CAPTCHA, device verification, or entitlement gates.
- [ ] The app stays alive for five minutes after login.
- [ ] Each requested page is visibly verified: 大单净量, 大单金额, 散户数量.
- [ ] Public job `601872` reaches all three verified pages within 120 seconds.
- [ ] The result files appear under the capture volume and the public status
      shows the corresponding three captures.

Real APK selector identifiers and visual templates still require this manual
verification. Until it succeeds, these are deployment candidates—not a claim
that either host profile is supported.
