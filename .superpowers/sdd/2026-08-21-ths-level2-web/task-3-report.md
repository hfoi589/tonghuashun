# Task 3 — Android runner and device WebSocket protocol

## Delivered

- `DeviceBridge` defines normalised launch, screenshot, touch, swipe, key, text,
  selector, active-tab, and text-input operations. `FakeDeviceBridge` makes all
  runner tests deterministic. `ADBDeviceBridge` shells out lazily and imports
  `uiautomator2` only when UI selector access is actually requested. The OpenCV
  template fallback is also optional and lazy.
- `Level2Navigator` launches `com.hexin.plat.android/.LogoEmptyActivity`, uses
  selector-first recipes with a visual fallback, verifies the exact requested
  symbol plus the active tab (`大单净量`, `大单金额`, or `散户数量`) before it accepts a
  PNG screen, and turns login/CAPTCHA/device/entitlement screens into an admin
  wait rather than attempting to bypass them.
- `Level2Runner` atomically claims one FIFO task, retries transient UI failures
  at most three times per capture, writes verified full PNGs at
  `<capture-root>/<task-id>/<kind>.png`, publishes successful captures as it
  goes, preserves partial results, and moves login/CAPTCHA/device/entitlement
  work to `WAITING_ADMIN`. It records neither credentials nor keystrokes.
- `RunnerControl` now has the exact executor states (`BOOTING`, `READY`,
  `ADMIN_CONTROL`, `NEEDS_ADMIN`, `OFFLINE`), heartbeats, a paused queue state,
  status sequence broadcasts, and owner-only input authorisation.
- Admin queue pause/resume/status routes have the existing authenticated-session
  and CSRF protections. `/api/admin/device` authenticates the admin cookie,
  emits the documented JPEG frame/status envelopes at four FPS, strictly checks
  input envelopes, and dispatches input only when that session owns the lock.
  ADB and Redis remain process-local and are not exposed as HTTP routes.
- The admin UI enables queue controls and treats `READY` and `ADMIN_CONTROL` as
  usable runner states; it no longer accepts the unsupported `ONLINE` status.

## RED/GREEN evidence

| Behaviour | RED evidence | GREEN evidence |
| --- | --- | --- |
| Runner bridge/navigator/worker | `pytest tests/test_android_runner.py tests/test_device_websocket.py -q` initially failed at collection because `FakeDeviceBridge`, navigator, and runner were absent | focused suite: `9 passed` after the bridge, navigator, bounded retry, capture, lock, and WebSocket implementation |
| Exact active-tab validation | `pytest tests/test_android_runner.py -q` failed because `FakeDeviceBridge` lacked active-tab control | focused suite: `7 passed` after adding selected-tab verification to the bridge recipe |
| Queue UI and exact frontend states | `npm test -- --run src/AdminPage.test.tsx src/device-stream.test.ts` failed: queue controls remained disabled and mock response ordering exposed the missing queue request | focused frontend suite: `2 files passed, 6 tests passed` after queue routes/UI and `READY`/`ADMIN_CONTROL` protocol handling |

## Final verification

```text
.venv/bin/python -m pytest -q
38 passed

.venv/bin/python -m compileall -q level2_service
completed with exit status 0

frontend: npm test
3 files passed, 11 tests passed

frontend: npm run build
TypeScript check and Vite production build passed

git diff --check
completed with exit status 0
```

## Boundary notes

- No live Android device, account, Level2 entitlement, CAPTCHA, or private THS
  protocol was accessed. The real ADB and optional image/UI adapters are lazy,
  so importing or testing the web service requires neither ADB nor OpenCV nor
  uiautomator2.
- The ADB selector identifiers are recipe-bound placeholders for deployment UI
  mappings. Deployment validation must still confirm the actual installed app's
  identifiers and visual templates after an administrator manually signs in.
