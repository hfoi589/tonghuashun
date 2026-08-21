# Task 4 — deployment profiles, preflight, and verification

## Delivered

- `level2_service.main.create_production_app()` is an explicit production
  factory. It requires an Argon2id `ADMIN_PASSWORD_HASH`, a 32+ character
  `ADMIN_SESSION_SECRET`, Redis URL, capture root, ADB serial/server socket,
  and runner poll interval. It constructs `RedisStreamsStore`,
  `ADBDeviceBridge`, `Level2Navigator`, and `Level2Runner` around one shared
  `RunnerControl`; API health/lock/device WebSocket and the worker therefore
  observe the same state.
- `create_app()` retains its isolated `InMemoryStreams` default and no runner
  task. Only the production factory injects a runner. Its FastAPI lifespan runs
  blocking `run_once()` calls in a worker thread, schedules retention cleanup,
  marks real worker exceptions `OFFLINE`, and stops both background loops on
  shutdown.
- The session secret now signs server-side session IDs with HMAC-SHA256. The
  existing random session/CSRF tokens and Argon2id password verification remain
  intact; unit-test/default mode continues to work without a configured secret.
- The multi-stage `Dockerfile` builds the Vite frontend, a Python 3.12 API/ADB
  runtime (Pillow, Redis, Uvicorn and uiautomator2 dependencies), and a Caddy
  serving target. Caddy serves SPA fallbacks and proxies `/api/*`, including
  `/api/admin/device`, to the private API service.
- `deploy/compose.yml` provides API, Redis, Caddy, persistent capture/Redis/
  Caddy volumes, and the optional `linux-redroid` Android 13 amd64 profile.
  The Android container is privileged, uses Binder device/binderfs mounts,
  `androidboot.use_memfd=1`, guest/software rendering, 1080×1920, 480 DPI,
  private ADB expose-only networking, and persistent Android data. API and
  Caddy targets can be published for amd64 and arm64 with the documented
  `buildx` commands.
- `deploy/macos.env.example` and `scripts/bootstrap-macos-avd.sh` support only
  a native API 33 `arm64-v8a` AVD on the same Apple Silicon Mac. The web/API/
  Redis services remain Dockerized; Android does not. The Docker bridge uses
  `host.docker.internal:5037`, with no published ADB or Redis port.
- `scripts/preflight.py` verifies the exact APK SHA-256
  `2554490aa3f5e2df17ac0a711311f3f85ee3130008af9bb4ab12510b3d6e971e`, a native
  ARM ABI, baseline CPU/RAM/free-space, and the limited Linux Redroid or macOS
  AVD host dependencies. It clearly rejects rootless Linux Docker, absent
  Binder devices/binderfs, non-amd64 Linux, non-Apple-Silicon macOS, missing
  SDK, or missing AVD. `--apk-only` lets the Mac bootstrap reject an artifact
  before SDK downloads or installation.
- `scripts/setup-admin.py`/`.sh` prompt without echo, refuse overwrites, and
  create a mode-0600 env file containing only `ADMIN_PASSWORD_HASH` and a
  random `ADMIN_SESSION_SECRET`. No THS credentials are accepted, printed, or
  persisted. `.dockerignore` explicitly excludes APKs and env files.
- `README.md` includes exact Linux/macOS commands, volumes, domain/HTTPS setup,
  non-public ADB/Redis boundary, and the manual smoke gate. It explicitly does
  not claim profile support until the real APK installs, survives five minutes,
  reaches all three pages after manual login, and captures `601872` within 120
  seconds. Selector/template verification remains required.

## Tests and RED/GREEN evidence

| Behaviour | RED evidence | GREEN evidence |
| --- | --- | --- |
| Production wiring/lifecycle | `pytest tests/test_deployment.py -q` initially failed at collection: `level2_service.main` did not exist | Fake Redis/ADB/runner production factory test passes and observes one shared control plus a stopped runner task after lifespan shutdown. |
| APK hash and ABI guard | Initial fixture test failed with the deliberately wrong digest, proving the hash branch; an x86-only fixture then failed at the ABI branch before the guard was finalized | ARM fixture with an independent fixed digest passes; mismatched hash and x86-only APK both fail with the specific preflight errors. |
| Host profile guard | No pure deployment validation existed | Deterministic fakes prove an ARM Linux target and an Intel macOS target are rejected before a live Docker/Android operation. |
| Compose sanity | First Compose test omitted the profile and did not include Redroid in normalized output; its next assertion used the pre-normalized `extra_hosts` spelling | `docker compose ... --profile linux-redroid config` runs without starting or pulling containers and asserts Redroid plus Docker host bridge normalization. |

## Final verification

```text
.venv/bin/python -m pytest -q
52 passed

.venv/bin/python -m compileall -q level2_service scripts
completed with exit status 0

frontend: npm test -- --run
3 files passed, 12 tests passed

frontend: npm run build
TypeScript check and Vite production build passed

docker compose -f deploy/compose.yml --profile linux-redroid config
completed successfully; this was configuration-only (no pull or start)

git diff --check
completed with exit status 0
```

## Scope and remaining acceptance work

- No Docker image was built/pulled and no container, Android VM, APK, account,
  login, CAPTCHA, entitlement, or private THS endpoint was used.
- The real 204 MB APK was not copied into the workspace or Git. Its actual
  SHA-256/ABI validation and installation are intentionally an operator-side
  preflight action.
- No host is declared supported yet. An authorized operator still must run the
  documented smoke checklist, manually log in, verify the exact real selectors
  and visual templates, observe five-minute stability, and complete the three
  `601872` pages within 120 seconds.
