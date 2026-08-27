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

## Fix round 1 — Token boundary and documentation-test coverage

The root `.env` is now documented as the Compose/API source. The lifecycle
installer copies that same Token to the mode-0600 host configuration required
by the loopback broker; no plist, log, or browser receives it. The deployment
example test now parses only active, non-comment assignments, so a later active
Token assignment cannot be masked by a commented empty line. The APK contract
test now positively requires the corrected `204 MB` Git-history statement, old
Docker context/image exclusion, and local/private approved-image boundary.

### RED (exact output)

```text
..................FF.....................................                [100%]
=================================== FAILURES ===================================
____ test_readme_defines_the_unimplemented_private_complete_image_contract _____

E       AssertionError: assert '204 MB APK is tracked in Git history' in '# 同花顺 Level2 截图服务 ...'

____ test_lifecycle_token_docs_define_the_compose_and_host_broker_boundary _____

E       AssertionError: assert 'root `.env` is the source for Compose/API' in '# 同花顺 Level2 截图服务 ...'

=========================== short test summary info ============================
FAILED tests/test_deploy_configuration.py::test_readme_defines_the_unimplemented_private_complete_image_contract
FAILED tests/test_deploy_configuration.py::test_lifecycle_token_docs_define_the_compose_and_host_broker_boundary
2 failed, 55 passed, 1 warning in 3.26s
```

### GREEN (exact output)

```text
.........................................................                [100%]
57 passed, 1 warning in 4.18s
```

`git diff --check` exited successfully with no output.

### Direct covering output (exact output)

```text
...                                                                      [100%]
3 passed in 0.19s
```

The focused command covered:

```text
tests/test_deploy_configuration.py::test_macos_example_and_compose_document_a_secretless_lifecycle_configuration
tests/test_deploy_configuration.py::test_readme_defines_the_unimplemented_private_complete_image_contract
tests/test_deploy_configuration.py::test_lifecycle_token_docs_define_the_compose_and_host_broker_boundary
```
