# Task 2: React public and admin UI report

## Scope delivered

Created a focused Vite + React + TypeScript application in `frontend/`, without changing the backend, Android runner, or deployment files.

- Public page submits a validated Shanghai/Shenzhen symbol to `POST /api/v1/jobs`, displays all Task 1 states, and refreshes the full job record after `status` SSE events from `GET /api/v1/jobs/{public_id}/events`.
- Public results show the three required capture kinds (`LARGE_ORDER_NET`, `LARGE_ORDER_AMOUNT`, and `RETAIL_COUNT`) with clear pending, ready, expired, partial, failed, and overall-expired states. Ready cards provide separate open and download actions that use the capture URL returned by the API.
- `#admin` opens a password-protected management view using the existing session endpoint. It reads runner health and the current session's lock state, copies `ths_csrf` from the cookie for lock mutations, and provides acquire/release controls.
- Admin queue and automatic-resume controls are deliberately marked as Runner-protocol placeholders because Task 1 exposes no queue-control endpoints.
- A canvas-based device-stream adapter accepts an optional `VITE_RUNNER_WS_URL`, draws string image frames, and sends normalized pointer and keyboard events only while the current session holds the lock. Without that future Runner stream, it explicitly displays an offline/unconfigured state.
- Admin notices state that passwords and device input are not recorded or displayed. The UI does not create accounts or add rate limits.

## Test-driven evidence

### RED

After the test harness and the behavior tests were created, the first `npm test` run failed because `src/App.tsx` and `src/AdminPage.tsx` did not exist. Vite reported both unresolved imports. This demonstrated that the submit/status/result and administrator tests were not passing against pre-existing UI code.

### GREEN

After the minimal implementation, the focused test run passed:

```text
Test Files  2 passed (2)
Tests  4 passed (4)
```

The tests cover public symbol submission and queued-state rendering, partial results with a ready capture link, expired tasks without capture actions, and administrator password login / runner offline health / CSRF lock acquisition.

## Final verification

Fresh verification from `frontend/`:

```text
npm test        2 files passed, 4 tests passed
npm run build   TypeScript check and Vite production build passed
git diff --check passed
```

The production bundle contains `dist/index.html`, one CSS asset, and one JavaScript asset. `dist/` is generated output and intentionally not committed.

## Remaining integration boundary

The UI correctly consumes every Task 1 route available today. Task 3 must publish a concrete device WebSocket URL/protocol and queue pause/resume endpoints before the visual stream and placeholder controls can drive an actual runner. Until then, the admin page remains safely explicit about the offline state and does not invent backend behavior.
