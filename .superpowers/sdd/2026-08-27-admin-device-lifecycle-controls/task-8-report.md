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

## Fix round 1 — acceptance, session validation, journal recovery, and role scope

### Corrected findings

- `LoopbackDataOnlyAcceptance` now calls its opener with
  `timeout=<seconds>` as a keyword. The injected opener is keyword-only, and a
  loopback `ThreadingHTTPServer` regression exercises the real `urllib`
  request/response boundary.
- Acceptance requires the full data-only contract: `COMPLETED`, no task/source
  errors, `include_long_capture=false`, all capture records and long capture
  `SKIPPED` with no URL, eight nonempty scalar values with `INTERFACE` sources,
  three valid ordered App-internal intraday curves with `INTERFACE` sources, and
  complete 1/3/5-day fund periods with `INTERFACE` sources. Capture, OCR/public
  fallback, missing values, and partial results are rejected.
- Session readiness now runs a fixed Python probe inside the deployed API
  container. It uses `EncryptedFileSessionProvider` and each fixed role's public
  status, decrypts and role-validates both bundles, requires regular owned
  mode-0600 nonempty files and a READY timestamp no older than 24 hours, and
  emits only `READY` or `NOT_READY`. Empty, corrupt, symlinked, role-swapped,
  wrong-key, stale, and missing states are rejected.
- Added the fixed host journal
  `~/.config/ths-device-provisioning.json`. It is written atomically as mode
  `0600`, owned by the current user, and accepts only version 1, the two fixed
  roles/AVD names, and `PENDING_CREATE`, `AVD_CREATED`, or
  `ASSETS_PROVISIONED`. Corruption, extra fields, wrong modes, symlinks, wrong
  roles/names, and invalid transitions fail with
  `PROVISIONING_JOURNAL_INVALID`.
- Initial missing roles are journaled before AVD creation. A boot-timeout rerun
  resumes the journaled AVD without `--force`, recreation, deletion, or reset;
  asset installation and lifecycle work continue from the recorded step. A role
  is removed only after its fixed lifecycle action reaches `RUNNING`.
- Provisioning lifecycle actions now target only current or journaled
  provisioning roles. Pre-existing core/fund roles receive no lifecycle action.
  Direct core calibration was removed; one fixed lifecycle action performs the
  new core calibration. A rerun with no missing or incomplete roles performs no
  lifecycle action.

### Exact RED evidence

Acceptance transport and strict result validation:

```text
/Users/wilson/tonghuashun/.venv/bin/python -m pytest -q \
  tests/test_macos_one_click_deploy.py -k 'data_only_acceptance'
12 failed, 2 passed, 66 deselected
```

The keyword-only fake rejected the positional timeout, the real loopback server
constructor was unavailable, and the incomplete/capture/fallback mutations were
accepted by the old scalar-only validator.

Encrypted session readiness:

```text
/Users/wilson/tonghuashun/.venv/bin/python -m pytest -q \
  tests/test_macos_one_click_deploy.py \
  -k 'session_readiness_probe or fixed_in_container_session'
9 failed, 80 deselected
```

The production probe constant and provider-based command did not exist; the old
implementation performed only `Path.is_file()` checks.

Journal recovery and lifecycle scope:

```text
/Users/wilson/tonghuashun/.venv/bin/python -m pytest -q \
  tests/test_macos_one_click_deploy.py \
  -k 'provisioning_journal or boot_timeout_rerun or lifecycle_touches or \
      rerun_without_incomplete or partial_provisioning_preserves or \
      provisioning_uses_only_fixed'
11 failed, 87 deselected
```

The journal interface/class was absent, direct core calibration still ran, and
provisioning still sent lifecycle actions to both roles.

### Exact GREEN and covering evidence

```text
# Acceptance transport and strictness
14 passed, 84 deselected in 0.55s

# Encrypted session readiness
9 passed, 89 deselected in 0.47s

# Journal recovery and lifecycle scope
11 passed, 87 deselected in 0.05s

# Complete Task 8 backend file
98 passed in 2.66s
```

Full regression and artifact checks:

```text
/Users/wilson/tonghuashun/.venv/bin/python -m pytest -q
723 passed, 24 existing warnings in 27.69s

cd frontend
npm test -- --run src/AdminPage.test.tsx
1 file passed, 26 tests passed

npm run build
TypeScript and Vite build completed successfully

/Users/wilson/tonghuashun/.venv/bin/python -m py_compile scripts/macos_deploy.py
/bin/sh -n scripts/provision-macos-from-image.sh scripts/deploy-macos-one-click.sh
git diff --check
```

All fixes remained fake/local-only: no real SDK, ADB, Emulator, AVD, Docker
deployment, lifecycle broker, admin session, device action, App navigation,
session refresh, or market task ran.

## Fix round 2 — acceptance identity, exact formats, and session age ruling

### Corrected findings

- Acceptance now binds the exact catalog confirmation through the entire task:
  lookup must return symbol `601872` and a nonblank name; submission must return
  a safe nonempty public ID; every queued/running/terminal response must carry
  that same public ID and symbol; and terminal `values.stock_name` must exactly
  equal the confirmed lookup name.
- Added named strict validators for current price, percentage fields, signed
  two-decimal values, large-order amount, MACDFS, fund units, intraday units,
  intraday values, and polled identity. The completed-task validator now
  requires:
  - two-decimal stock price;
  - two-decimal change/turnover percentages with the established sign rules;
  - two-decimal large-order net and retail values;
  - one-decimal large-order amount with the API's `万` suffix;
  - signed three-decimal MACDFS (`+` for positive, `-` for negative, unsigned
    zero);
  - only `万元` or `亿元` fund units and four signed two-decimal fields for all
    three periods;
  - exact intraday units (`null`, `万`, `null`), field-specific precision,
    valid `HH:mm`, and strictly increasing times.
- The encrypted-session probe no longer applies a 24-hour age policy. It still
  requires safe regular mode-0600 files, successful provider decryption, fixed
  role/material validation, public `READY`, no provider error, and a valid
  timezone-aware timestamp. Live strict acceptance is the liveness proof.
- Journal behavior, lifecycle role scoping, image commands, license handling,
  human instructions, and frontend behavior were not changed.

### Exact RED evidence

The desired older-session behavior, valid exact-format response, named
validators, and polled identity validator all failed before implementation:

```text
/Users/wilson/tonghuashun/.venv/bin/python -m pytest -q \
  tests/test_macos_one_click_deploy.py \
  -k 'confirmed_fixed_symbol or named_strict_format or \
      strict_polled_identity or older_valid_bundle'
10 failed, 117 deselected in 0.38s
```

A covering mutation removed lookup-name and queued/running identity binding;
the dedicated false-positive scenarios then reproduced the acceptance bug:

```text
/Users/wilson/tonghuashun/.venv/bin/python -m pytest -q \
  tests/test_macos_one_click_deploy.py -k 'binds_lookup_submission'
3 failed, 2 passed, 122 deselected in 0.09s
```

The two passing mutation cases were still protected independently by the safe
submitted-ID check and terminal public-ID check.

### Exact GREEN and covering evidence

```text
# Combined identity, strict-format, valid-response, and older-session slice
31 passed, 96 deselected in 0.21s

# Identity false-positive scenarios after restoring the guards
5 passed, 122 deselected in 0.03s

# Named validators, exact formats, strict polled identity, and older session
26 passed, 101 deselected in 0.15s

# Complete Task 8 backend file
127 passed in 2.46s
```

Full regression and artifact verification:

```text
/Users/wilson/tonghuashun/.venv/bin/python -m pytest -q
752 passed, 24 existing warnings in 28.04s

cd frontend
npm test -- --run src/AdminPage.test.tsx
1 file passed, 26 tests passed

npm run build
TypeScript and Vite build completed successfully

/Users/wilson/tonghuashun/.venv/bin/python -m py_compile scripts/macos_deploy.py
/bin/sh -n scripts/provision-macos-from-image.sh scripts/deploy-macos-one-click.sh
git diff --check
```

No real host, Docker deployment, SDK, AVD, ADB, lifecycle, admin-session,
device, App, session-refresh, or market action ran.
