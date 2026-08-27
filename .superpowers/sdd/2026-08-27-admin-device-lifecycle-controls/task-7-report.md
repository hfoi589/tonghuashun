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
