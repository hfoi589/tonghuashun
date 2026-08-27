# Task 8 — Fresh-Mac and Partial-AVD Interactive Provisioning

Status: complete

## Delivered

- Added `MacDeploymentOrchestrator.provision_missing()` and connected both
  `--mode auto` and explicit `--mode provision` to the approved fixed-role
  flow. `auto` still sends a complete two-AVD Mac to the reviewed existing
  deployment path; any fixed-role absence records one immutable initial
  missing-role set and provisions only that set. Unknown AVD names are ignored.
- Provisioning checks the existing Apple Silicon, OrbStack, Java, Android tool,
  env, Compose, and image-asset boundaries without installing host tooling.
  When the fixed Android 33 ARM64 image is absent, only that exact package is
  requested. SDK stdin is closed so licenses are never accepted or prompted by
  the script; failures map to `ANDROID_LICENSE_REQUIRED` or
  `ANDROID_SYSTEM_IMAGE_UNAVAILABLE` without diagnostics escaping.
- Missing fixed AVDs are created without `--force`, launched sequentially with
  the fixed names, ports, emulator path, and arguments, and verified through
  `boot_completed` plus the existing serial-to-AVD identity check. No failure
  path deletes or resets a partially created AVD.
- The image-contained `container-provision-device ROLE` runs in a short-lived
  OrbStack container with only the fixed host ADB socket and only for roles
  created by that invocation. Existing roles receive no install/push/chmod
  commands and retain the existing on-device APK path/digest validation.
- Core display calibration runs only for a newly created core role. After
  assets are installed, the stable lifecycle service is installed and the
  broker performs the fixed `start_and_launch_app` action for both roles; no App
  page navigation was added.
- Standard Compose deployment and health verification now lead to a recoverable
  human gate. Missing `core_metrics.session` or `main_fund_flow.session` returns
  `FIRST_TIME_LOGIN_REQUIRED` with only the admin URL, manual login/verification,
  matching session-refresh, and rerun instructions. A later invocation never
  recreates or reinstalls the now-existing AVDs.
- `READY` requires both encrypted session files and a completed fixed-symbol
  data-only public-API acceptance task. The acceptance client confirms symbol
  `601872`, submits `include_long_capture=false`, polls only the opaque task ID,
  and requires all eight scalar values.
- Added executable `scripts/provision-macos-from-image.sh`, which resolves the
  stable project root and runs the fixed provision mode while forwarding only
  the ordinary deployment arguments.
- Task 5 already rendered both role session states, refresh controls, timestamps,
  pending state, and fixed errors. No UI redesign or production UI change was
  needed; Task 8 adds the missing timestamp and pending-state assertions only.

## TDD evidence

Initial provisioning RED:

```text
14 failed, 53 deselected
```

The failures were the absent provisioning constructor/interface, acceptance
verifier, immutable missing-role flow, human gate, and wrapper. After the
minimal implementation, the focused slice passed:

```text
14 passed, 53 deselected
```

Self-review found that `sdkmanager` still inherited terminal stdin. A new
regression required closed stdin and failed twice with `None == b''`; after the
fix:

```text
2 passed, 65 deselected
```

Final focused backend result:

```text
67 passed in 1.30s
```

## Verification

Required Task 8 checks:

```text
/Users/wilson/tonghuashun/.venv/bin/python -m pytest -q \
  tests/test_macos_one_click_deploy.py
67 passed

cd frontend
npm test -- --run src/AdminPage.test.tsx
1 file passed, 26 tests passed

npm run build
TypeScript and Vite build completed successfully
```

Full project regression checks:

```text
/Users/wilson/tonghuashun/.venv/bin/python -m pytest -q
692 passed, 24 existing warnings

cd frontend && npm test -- --run
6 files passed, 91 tests passed
```

Static checks also passed:

```text
/Users/wilson/tonghuashun/.venv/bin/python -m py_compile scripts/macos_deploy.py
/bin/sh -n scripts/provision-macos-from-image.sh scripts/deploy-macos-one-click.sh
git diff --check
```

The scoped production paths contain no `--force`, automatic license command,
Homebrew/OrbStack/JDK installer, reinstall, clear/reset, AVD deletion,
`force-stop`, image push/save, or Compose volume deletion command.

## Safety boundary

- All provisioning tests used injected fake command runners, lifecycle brokers,
  acceptance clients, HTTP responses, and temporary paths.
- No real SDK, ADB, Emulator, AVD, Docker deployment, lifecycle/admin session,
  device action, App navigation, login, session refresh, or market task ran.
- Neither real AVD, emulator, account, session bundle, Docker volume, `.env`, nor
  `deploy/macos.env` state was changed.
