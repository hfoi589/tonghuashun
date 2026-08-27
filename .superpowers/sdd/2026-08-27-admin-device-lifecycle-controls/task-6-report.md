# Task 6 — Documentation, Rules, and Deployment Contract

## Delivered contract

- Chinese and English `AGENTS.md` now allow only authenticated,
  lock-protected `shutdown` and `start_and_launch_app` actions for the two
  fixed device roles. All fund-account restrictions remain in force.
- `README.md`, `handoff.md`, and `deploy/macos.env.example` document the
  local/private APK-and-Frida image contract, the future
  `scripts/deploy-macos-one-click.sh --mode auto` entry point, existing-AVD
  preservation, fresh/partial-host human-login gate, fixed errors, LaunchAgent
  installation, manual queue recovery, and non-destructive rollback.
- The prior false statement that the APK is absent from Git history is removed.
  Documentation states that the old image excluded the APK and that the
  approved complete image will include digest-verified assets locally/privately.

## Deliberate boundary

This is documentation and deployment-contract work only. It performs no host,
device, image, AVD, account, or provisioning action, and it does not claim real
deployment or clean-Mac acceptance before Tasks 7–9 implement and verify those
flows.

## Verification

RED: the new documentation contract tests failed on the missing lifecycle,
image, and operator-procedure documentation.

GREEN: `/Users/wilson/tonghuashun/.venv/bin/python -m pytest -q tests/test_deployment.py tests/test_deploy_configuration.py` completed with `56 passed` (one existing FastAPI/TestClient deprecation warning), followed by `git diff --check` with no whitespace errors.
