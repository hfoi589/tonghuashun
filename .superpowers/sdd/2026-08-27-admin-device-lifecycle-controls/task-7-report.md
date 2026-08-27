# Task 7 — Complete APK/Frida Image and Existing-Mac One-Command Deployment

Status: complete

## Delivered

- Added a `mobile-assets` Docker stage with the exact tracked
  `ths_android_V11_59_03.apk` and official Frida Server `16.7.19` ARM64
  archive. The build verifies the approved APK, compressed Frida, and
  decompressed Frida sizes and SHA-256 digests before producing a fixed
  manifest.
- The final API image carries read-only APK/Frida assets, the manifest, the
  fixed-role new-device provisioner, and read-only lifecycle/bridge/display
  helper copies. OCI labels expose only the approved asset version/digests.
  No secret, local device data, AVD state, session material, capture data, or
  logs are copied.
- Enhanced `scripts/setup-admin.py` to create exactly four independent
  deployment secrets in a new exclusive mode-0600 env file, including a
  32-byte URL-safe Fernet key and lifecycle token.
- Added `scripts/container-provision-device.sh ROLE` for Task 8 new AVDs only.
  It accepts exactly one of the two fixed roles, refuses an existing package,
  fails closed when package detection fails, installs without `-r`, pushes and
  starts only the fixed Frida binary, and creates only the fixed port forward.
  It never opens or navigates the App.
- Added injectable `CommandRunner`, `LifecycleBroker`, and `FileSystem`
  boundaries plus `MacDeploymentOrchestrator.deploy_existing()` and the
  `scripts/deploy-macos-one-click.sh --mode auto|existing|provision` entry
  point.
  - Existing mode validates Apple Silicon, OrbStack, Java 17, Android tools,
    the fixed AVD names, root env mode/keys, and the dual-role macOS config.
  - It builds and reads the local image manifest, installs the Task 2 host
    lifecycle service, starts only stopped roles through the broker and waits
    for `RUNNING`, validates each installed base-APK path and on-device digest,
    then runs the canonical OrbStack Compose `up -d --build` without `down`.
  - APK mismatch fails with `INSTALLED_APK_MISMATCH` before Compose rebuild and
    never installs, reinstalls, clears, resets, deletes, pushes, saves, or
    exports anything.
  - `auto` selects the preservation path when both fixed AVDs exist. The
    accepted `provision` mode remains the explicit Task 8 extension point and
    currently returns `PROVISIONING_NOT_IMPLEMENTED`.

## TDD evidence

1. Image contract RED:

   ```text
   tests/test_macos_one_click_deploy.py -k image
   2 failed, 1 passed
   ```

   GREEN after the pinned asset stage: `3 passed`.

2. Secret/provisioner RED:

   ```text
   tests/test_macos_one_click_deploy.py -k 'setup_admin or container_provisioner'
   8 failed, 3 deselected
   ```

   GREEN after implementation: `8 passed, 3 deselected`.

3. Existing-mode orchestrator RED: ten tests failed because
   `scripts/macos_deploy.py` was absent. After implementation and the module
   identity harness correction, the scoped preservation/auto/CLI suite passed:
   `21 passed`.

4. Review regressions were added before their fixes:
   - package-manager query failure initially completed provisioning instead of
     failing closed;
   - malformed `deploy/macos.env` initially surfaced the root-env error code;
   - unreadable root env initially leaked a raw `OSError`;
   - the malicious APK path test was mutation-checked by temporarily removing
     the `..` rejection, observing `DID NOT RAISE DeploymentError`, then
     restoring the guard and observing a pass.

5. Final focused result:

   ```text
   /Users/wilson/tonghuashun/.venv/bin/python -m pytest -q \
     tests/test_macos_one_click_deploy.py tests/test_deploy_configuration.py
   52 passed in 2.17s
   ```

## Verification

Repository-wide Python suite:

```text
/Users/wilson/tonghuashun/.venv/bin/python -m pytest -q
650 passed, 24 existing deprecation warnings in 26.59s
```

Static checks:

```text
/Users/wilson/tonghuashun/.venv/bin/python -m py_compile \
  scripts/macos_deploy.py scripts/setup-admin.py
/bin/sh -n scripts/container-provision-device.sh \
  scripts/deploy-macos-one-click.sh
git diff --check
```

All exited successfully. A forbidden-command scan found no `install -r`,
`pm clear`, `wipe-data`, AVD deletion, `force-stop`, `docker push`,
`docker save`, or `down -v` in the new deployment paths.

Safe image integration:

```text
docker --context orbstack build --target api \
  -t ths-level2-api:asset-test .
```

The final rebuild completed successfully as image
`sha256:d79040ee65e5eefbb87145704f7e46299f7f9a14edb882027461f4e89e607ed3`.
The prescribed disposable-container check printed only:

```text
assets=verified
```

An additional disposable-container audit verified both asset sizes/digests,
0444 APK/manifest modes, 0555 Frida/directory modes, all four read-only helper
copies, and absence of root/app `.env` files; it printed
`assets=full-verified`. Image-label inspection returned only the approved APK
and Frida version/digests.

## Safety boundary

- No automated command ran `scripts/deploy-macos-one-click.sh` against the
  real host.
- No real ADB, Emulator, sdkmanager, avdmanager, lifecycle broker, or
  LaunchAgent action was invoked.
- No repository `.env` or `deploy/macos.env` was created or modified.
- No App was installed/reinstalled, no AVD or account state was altered, and
  no image was pushed, saved, or exported.

## Fix round 1 — Compose precedence, fixed AVD identity, and env-file boundary

The Critical/Important review findings were addressed without entering the
deferred broker-readiness or setup-interruption Minor findings.

### Exact RED evidence

Root-only secret assignments were still accepted in the later macOS env file:

```text
/Users/wilson/tonghuashun/.venv/bin/python -m pytest -q \
  tests/test_macos_one_click_deploy.py -k 'root_only_secrets_in_macos_environment'
FF                                                                       [100%]
2 failed, 28 deselected in 0.28s
```

The checked-in macOS env example still assigned empty root secrets, while the
root env example omitted the lifecycle Token:

```text
/Users/wilson/tonghuashun/.venv/bin/python -m pytest -q \
  tests/test_deploy_configuration.py::test_macos_example_keeps_root_secrets_out_of_the_later_env_file \
  tests/test_deploy_configuration.py::test_root_environment_example_documents_the_direct_session_key
FF                                                                       [100%]
2 failed in 0.21s
```

Effective Compose configuration was never validated and ambient empty values
survived into the real subprocess environment:

```text
/Users/wilson/tonghuashun/.venv/bin/python -m pytest -q \
  tests/test_macos_one_click_deploy.py \
  -k 'preserves_both_avds or empty_effective_compose or removes_ambient_compose'
FFFFF                                                                    [100%]
5 failed, 27 deselected in 0.54s
```

Fixed serials were trusted without an Emulator-console AVD identity check:

```text
/Users/wilson/tonghuashun/.venv/bin/python -m pytest -q \
  tests/test_macos_one_click_deploy.py \
  -k 'fixed_serial_identity or installs_lifecycle_before_starting'
FFFF                                                                     [100%]
4 failed, 31 deselected in 0.08s
```

Custom in-context env files were accepted and root `.env` did not require
Docker-ignore coverage:

```text
/Users/wilson/tonghuashun/.venv/bin/python -m pytest -q \
  tests/test_macos_one_click_deploy.py \
  -k 'custom_secret_file_inside or root_env_to_be_excluded or secret_env_file_outside'
FF.                                                                      [100%]
2 failed, 1 passed, 35 deselected in 0.10s
```

After changing the public regression to exercise default `auto`, the invalid
env path still reached the AVD probe before rejection:

```text
F                                                                        [100%]
FAILED tests/test_macos_one_click_deploy.py::test_existing_mode_rejects_a_custom_secret_file_inside_build_context
AssertionError: [('emulator', '-list-avds')] == []
1 failed in 0.06s
```

### Fixes and exact GREEN evidence

- Removed `THS_DEVICE_LIFECYCLE_TOKEN` and
  `THS_SESSION_ENCRYPTION_KEY` assignments from
  `deploy/macos.env.example`; root `.env` now documents both keys. Real
  `deploy/macos.env` parsing rejects every root-only secret key, including an
  empty assignment.
- `SubprocessCommandRunner` removes security-sensitive ambient overrides.
  The orchestrator executes the canonical `docker --context orbstack compose
  --env-file ... --env-file deploy/macos.env ... config --format json` before
  image build and again immediately before `up`; the effective lifecycle URL,
  Token, and session key must exactly match the validated sources and be
  non-empty.
- Both fixed serials execute read-only `adb -s <serial> emu avd name` checks
  before lifecycle installation and again after broker startup. Wrong,
  malformed, or missing identity returns only
  `FIXED_AVD_IDENTITY_MISMATCH`.
- `--env-file` paths resolving inside the project are restricted to root
  `.env`, and the effective `.dockerignore` rules must exclude it. Other
  in-context paths fail with `ENV_FILE_IN_BUILD_CONTEXT`; external resolved
  paths remain supported.

Focused GREEN results:

```text
# Root-only secrets in macOS env
2 passed, 28 deselected in 0.23s

# Checked-in env examples
2 passed in 0.12s

# Compose precedence and ambient sanitization
5 passed, 27 deselected in 0.15s

# Fixed serial identity and lifecycle ordering
4 passed, 31 deselected in 0.03s

# In-context/external env-file boundary
3 passed, 35 deselected in 0.03s

# Default auto rejects before AVD probing
1 passed in 0.02s
```

The real, non-starting Compose audit used a temporary mode-0600 fake root env
and the checked-in later env file. It preserved the root Token/session key and
fixed lifecycle URL:

```text
/Users/wilson/tonghuashun/.venv/bin/python -m pytest -q \
  tests/test_deploy_configuration.py::test_real_compose_config_preserves_root_secrets_with_canonical_env_order
.                                                                        [100%]
1 passed in 0.57s
```

### Covering verification

```text
/Users/wilson/tonghuashun/.venv/bin/python -m pytest -q \
  tests/test_macos_one_click_deploy.py tests/test_deploy_configuration.py
................................................................         [100%]
65 passed in 3.23s
```

```text
/Users/wilson/tonghuashun/.venv/bin/python -m pytest -q
663 passed, 24 existing deprecation warnings in 27.17s
```

Static verification completed successfully:

```text
/Users/wilson/tonghuashun/.venv/bin/python -m py_compile \
  scripts/macos_deploy.py scripts/setup-admin.py
/bin/sh -n scripts/deploy-macos-one-click.sh \
  scripts/container-provision-device.sh
git diff --check
```

An active-assignment scan found neither root-only key in
`deploy/macos.env.example`, and the dangerous-command scan remained empty.
The Dockerfile, mobile asset stage, manifest, APK, Frida binary, and final
image contents were unchanged in this fix round, so the image audit was not
rerun.

No real `.env`, AVD, ADB device, lifecycle broker, LaunchAgent, Emulator, or
Android SDK command was touched. The only Docker execution was read-only
`compose config`; it did not build, start, stop, or mutate containers.

## Fix round 2 — Preserve truly stopped roles with fail-closed port identity

The remaining Important finding was reproduced: the pre-install identity
check unconditionally required `adb emu avd name`, so the fake “STOPPED” role
was internally inconsistent and a real stopped role could never reach broker
startup.

### Exact RED evidence

After separating fake ADB attachment state from broker lifecycle state, the
required regressions produced three failures while the already-valid attached
identity cases remained green:

```text
/Users/wilson/tonghuashun/.venv/bin/python -m pytest -q \
  tests/test_macos_one_click_deploy.py \
  -k 'truly_stopped or wrong_or_missing_fixed_serial or wrong_starting_process or rechecks_fixed_serial'
..FFF                                                                    [100%]
3 failed, 2 passed, 34 deselected in 0.08s
```

- The truly stopped role failed at the unconditional pre-install ADB identity
  call and never reached broker startup.
- A wrong starting-process AVD returned the generic identity error without
  ever executing the required `get-state` and host `ps` checks.
- The post-start mismatch failed before installer/broker activity, proving the
  fake had incorrectly exposed ADB identity for a supposedly stopped role.

An ambiguity mutation then showed that malformed fixed-port process evidence
could be ignored until after lifecycle mutation:

```text
/Users/wilson/tonghuashun/.venv/bin/python -m pytest -q \
  tests/test_macos_one_click_deploy.py -k ambiguous_starting_process
.F                                                                       [100%]
1 failed, 1 passed, 39 deselected in 0.07s
```

The failing case contained a non-numeric `ps` PID with exact `-port 5556` and
reached the installer before rejection.

### Fix and exact GREEN evidence

- Pre-install checks now call fixed `adb -s <serial> get-state`.
  - `device` requires the exact fixed `adb emu avd name` response.
  - A non-device/absent serial triggers only read-only
    `ps -axo pid=,command=` inspection.
  - No process with the fixed port is accepted as truly stopped only when ADB
    reported the serial absent.
  - A process claiming the fixed port must contain exactly one matching
    `-port` and exactly one matching fixed `-avd`; wrong, repeated, malformed,
    multiple, or ambiguous evidence fails before lifecycle installation.
- After broker startup/wait reaches `RUNNING`, both roles still unconditionally
  require the exact ADB `emu avd name` identity.

Required and ambiguity regressions after the fix:

```text
/Users/wilson/tonghuashun/.venv/bin/python -m pytest -q \
  tests/test_macos_one_click_deploy.py \
  -k 'truly_stopped or wrong_or_missing_fixed_serial or wrong_starting_process or ambiguous_starting_process or rechecks_fixed_serial'
.......                                                                  [100%]
7 passed, 34 deselected in 0.03s
```

### Covering verification

```text
/Users/wilson/tonghuashun/.venv/bin/python -m pytest -q \
  tests/test_macos_one_click_deploy.py tests/test_deploy_configuration.py
....................................................................     [100%]
68 passed in 2.65s
```

```text
/Users/wilson/tonghuashun/.venv/bin/python -m pytest -q
666 passed, 24 existing deprecation warnings in 26.76s
```

Static verification completed successfully:

```text
/Users/wilson/tonghuashun/.venv/bin/python -m py_compile \
  scripts/macos_deploy.py scripts/setup-admin.py
/bin/sh -n scripts/deploy-macos-one-click.sh \
  scripts/container-provision-device.sh
git diff --check
```

The Dockerfile and image asset layers were unchanged, so no image rebuild or
asset audit was required. No real device, ADB server, Emulator, lifecycle
broker, LaunchAgent, Android SDK tool, or deployment env file was touched.

## Fix round 3 — Prove ADB absence and bind process identity to Emulator

Two Important fail-open cases remained after fix round 2:

1. Any nonzero fixed-serial `adb get-state` result was treated as proof that
   the serial was absent.
2. A host process was authenticated only by matching `-port` and `-avd`, so an
   unrelated executable could supply those arguments and reach lifecycle
   mutation.

### Exact RED evidence

The new ADB server/listing, offline/unauthorized, malformed listing, fake
executable, and truly stopped regressions all exposed missing boundary checks:

```text
/Users/wilson/tonghuashun/.venv/bin/python -m pytest -q \
  tests/test_macos_one_click_deploy.py \
  -k 'adb_listing_transport or non_device_adb_states or malformed_or_ambiguous_adb_listing or fake_executable or truly_stopped'
FFFFFFF                                                                  [100%]
7 failed, 40 deselected in 0.11s
```

The failures proved that:

- no `adb devices` absence proof was requested after `get-state` failure;
- offline/unauthorized serials reached host process inspection;
- malformed or duplicate server listings were ignored;
- `/usr/bin/python3 helper.py -avd THS_CORE_33_ARM64 -port 5556` reached the
  lifecycle installer;
- the truly stopped regression could not observe the required successful
  listing that omitted the fixed serial.

### Fix and exact GREEN evidence

- A successful fixed `adb devices` request is now mandatory after nonzero
  `get-state`. Its output must begin with the exact header and contain only
  unique, well-formed serial/state rows.
- A listed `device` still requires exact `adb emu avd name`; listed
  `offline`, `unauthorized`, unknown, duplicate, or malformed states fail with
  `FIXED_AVD_IDENTITY_MISMATCH` before process inspection or lifecycle
  mutation.
- Only a successful listing that omits the fixed serial may proceed to
  read-only host process inspection.
- Emulator preflight now records the resolved absolute executable path. A
  process claiming the fixed port must resolve to that trusted Emulator binary
  in addition to containing exactly one fixed `-port` and `-avd`; Python,
  wrappers, unknown paths, malformed options, or ambiguous processes fail
  before installer mutation.

Focused GREEN result for the review regressions:

```text
/Users/wilson/tonghuashun/.venv/bin/python -m pytest -q \
  tests/test_macos_one_click_deploy.py \
  -k 'adb_listing_transport or non_device_adb_states or malformed_or_ambiguous_adb_listing or fake_executable or truly_stopped'
.......                                                                  [100%]
7 passed, 40 deselected in 0.03s
```

### Covering verification

```text
/Users/wilson/tonghuashun/.venv/bin/python -m pytest -q \
  tests/test_macos_one_click_deploy.py tests/test_deploy_configuration.py
........................................................................ [ 97%]
..                                                                       [100%]
74 passed in 2.38s
```

```text
/Users/wilson/tonghuashun/.venv/bin/python -m pytest -q
672 passed, 24 existing deprecation warnings in 26.58s
```

Static verification completed successfully:

```text
/Users/wilson/tonghuashun/.venv/bin/python -m py_compile \
  scripts/macos_deploy.py scripts/setup-admin.py
/bin/sh -n scripts/deploy-macos-one-click.sh \
  scripts/container-provision-device.sh
git diff --check
```

The Dockerfile and image layers were unchanged, so no image rebuild or asset
audit was required. No real device, ADB server, Emulator, lifecycle broker,
LaunchAgent, Android SDK tool, or deployment env file was touched.
