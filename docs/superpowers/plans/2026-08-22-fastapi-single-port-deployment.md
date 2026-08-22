# FastAPI Single-Port Deployment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Serve the React site and existing Level2 APIs from FastAPI on HTTP port 8000 while removing every Caddy project component.

**Architecture:** The API application mounts the built asset directory and adds a final SPA fallback that excludes `/api`. Production settings control the frontend path and cookie security, while the API Docker image embeds the React build and Compose publishes only FastAPI.

**Tech Stack:** FastAPI, Starlette `StaticFiles`, React/Vite, pytest, Docker Compose

**Spec:** `docs/superpowers/specs/2026-08-22-fastapi-single-port-deployment-design.md`

## Global Constraints

- Keep all existing API, SSE, WebSocket, Redis, ADB, capture, OCR, and long-screenshot behavior unchanged.
- `secure_admin_cookies` defaults to `True`; only an explicit deployment setting enables HTTP cookies.
- `/api` and `/api/*` must never receive the SPA HTML fallback.
- Compose publishes `${APP_PORT:-8000}:8000` and contains no Caddy service or Caddy volumes.
- Do not delete existing Docker data volumes or Android login state.
- Preserve all unrelated user changes in the dirty worktree.

---

### Task 1: FastAPI frontend serving and HTTP administrator cookies

**Files:**
- Create: `tests/test_frontend_serving.py`
- Modify: `tests/test_admin_security.py`
- Modify: `level2_service/api.py`

**Interfaces:**
- Consumes: an optional directory containing `index.html` and `assets/*`.
- Produces: `create_app(frontend_root: Path | None = None, secure_admin_cookies: bool = True) -> FastAPI`.

- [ ] **Step 1: Write failing frontend routing tests**

  Create a temporary bundle with literal HTML and JavaScript contents. Assert
  that `/`, `/assets/app.js`, and `/portfolio/601872` return the correct files,
  while `/api/not-a-route` returns JSON `404` rather than HTML.

- [ ] **Step 2: Write the failing HTTP cookie test**

  Create an app with `secure_admin_cookies=False` and an HTTP `TestClient`.
  Log in, verify neither `Set-Cookie` header contains `Secure`, and verify
  `GET /api/admin/session` returns `204` using the stored HTTP cookie.

- [ ] **Step 3: Run the focused tests and verify the expected failures**

  Run:

  ```bash
  pytest -q tests/test_frontend_serving.py tests/test_admin_security.py -k 'frontend or http_cookie'
  ```

  Expected failures: `create_app()` rejects the new keyword arguments or the
  frontend routes return `404`.

- [ ] **Step 4: Implement minimal application behavior**

  Import `StaticFiles`, add the two `create_app()` arguments, use the cookie
  setting for login/logout, mount an existing `assets` directory, and register
  the final GET/HEAD SPA handler only when `index.html` exists. Explicitly raise
  `HTTPException(404)` for `api` and `api/*` paths.

- [ ] **Step 5: Run the focused tests and verify they pass**

  Run:

  ```bash
  pytest -q tests/test_frontend_serving.py tests/test_admin_security.py
  ```

  Expected: all selected tests pass with no warnings or errors.

### Task 2: Production setting propagation

**Files:**
- Modify: `tests/test_deployment.py`
- Modify: `level2_service/main.py`

**Interfaces:**
- Consumes: `FRONTEND_ROOT` and `ADMIN_COOKIE_SECURE` environment variables.
- Produces: `DeploymentSettings.frontend_root: Path | None` and `DeploymentSettings.admin_cookie_secure: bool`.

- [ ] **Step 1: Write failing setting and factory tests**

  Assert that `FRONTEND_ROOT=/app/frontend` resolves to a `Path`,
  `ADMIN_COOKIE_SECURE=0` produces `False`, the default produces `True`, an
  invalid boolean raises `ValueError`, and the production app receives the
  settings by checking its frontend state and HTTP login behavior.

- [ ] **Step 2: Run focused tests and verify the expected failures**

  Run:

  ```bash
  pytest -q tests/test_deployment.py -k 'frontend_root or cookie_secure'
  ```

  Expected: missing `DeploymentSettings` fields or parsing behavior causes the
  new assertions to fail.

- [ ] **Step 3: Implement settings and factory wiring**

  Add strict boolean parsing for the documented values, resolve the optional
  frontend path, and pass both fields into `create_app()`.

- [ ] **Step 4: Run deployment tests and verify they pass**

  Run:

  ```bash
  pytest -q tests/test_deployment.py
  ```

  Expected: all deployment tests pass.

### Task 3: Docker and Compose without Caddy

**Files:**
- Modify: `tests/test_deploy_configuration.py`
- Modify: `Dockerfile`
- Modify: `deploy/compose.yml`
- Delete: `deploy/Caddyfile`
- Modify: `deploy/macos.env.example`

**Interfaces:**
- Consumes: the Vite output from the existing `frontend-build` stage and optional `APP_PORT`.
- Produces: one API image containing `/app/frontend` and one published HTTP port.

- [ ] **Step 1: Replace Caddy tests with failing single-port deployment tests**

  Parse the rendered Compose configuration and assert there is no `caddy`
  service, the API publishes host port `8000`, and its environment contains
  `FRONTEND_ROOT=/app/frontend` plus `ADMIN_COOKIE_SECURE=0`. Assert the
  Dockerfile copies the frontend build into the API stage and contains no Caddy
  stage.

- [ ] **Step 2: Run deployment configuration tests and verify failures**

  Run:

  ```bash
  pytest -q tests/test_deploy_configuration.py
  ```

  Expected: assertions fail because Caddy still exists and the API is only
  exposed internally.

- [ ] **Step 3: Apply the minimal deployment change**

  Copy the frontend build into the API stage, delete the Caddy stage and
  `deploy/Caddyfile`, publish `${APP_PORT:-8000}:8000`, configure the two API
  environment variables, and remove the Caddy service and named-volume
  declarations. Replace the example Caddy variables with `APP_PORT=8000`.

- [ ] **Step 4: Validate Compose and run configuration tests**

  Run:

  ```bash
  docker compose --env-file deploy/macos.env -f deploy/compose.yml config --quiet
  pytest -q tests/test_deploy_configuration.py
  ```

  Expected: Compose validation and all deployment configuration tests pass.

### Task 4: Operator documentation

**Files:**
- Modify: `README.md`

**Interfaces:**
- Consumes: the single-port Compose deployment from Task 3.
- Produces: exact Linux, macOS, LAN, login, and image build instructions without Caddy references.

- [ ] **Step 1: Update deployment and security instructions**

  State that API and frontend share port `8000`, use
  `http://host:8000/#admin`, build only the API image, keep Redis/ADB private,
  and use a trusted LAN because transport is plain HTTP.

- [ ] **Step 2: Check documentation for stale project references**

  Run:

  ```bash
  rg -n -i 'caddy|CADDY_SITE_ADDRESS|CADDY_DEFAULT_SNI|CADDY_TLS_ISSUER|https://your-domain' README.md deploy Dockerfile
  ```

  Expected: no matches.

### Task 5: Full verification and local re-deployment

**Files:**
- Verify only; do not modify capture or Android data.

**Interfaces:**
- Consumes: the updated source tree and existing `deploy/macos.env` secrets.
- Produces: a running API/Redis deployment at `http://10.0.0.119:8000/` with the existing Android AVD connection.

- [ ] **Step 1: Run all automated tests**

  Run:

  ```bash
  pytest -q
  npm test -- --run
  npm run build
  ```

  Run the two npm commands from `frontend/`. Expected: zero failures and a
  successful production bundle.

- [ ] **Step 2: Build the API image**

  Run:

  ```bash
  DOCKER_BUILDKIT=0 docker compose --env-file deploy/macos.env -f deploy/compose.yml build api
  ```

  Expected: the API image builds successfully with the React bundle copied in.

- [ ] **Step 3: Recreate only required services and remove the orphan**

  Run:

  ```bash
  docker compose --env-file deploy/macos.env -f deploy/compose.yml up -d --remove-orphans api redis
  ```

  Expected: only API and Redis services remain in the default profile; no data
  volume is deleted.

- [ ] **Step 4: Verify HTTP, session, runner, containers, and listeners**

  Confirm the LAN root returns the current HTML and asset bundle, an unknown API
  route returns JSON `404`, administrator login works over HTTP and survives a
  session probe, the authenticated runner endpoint is not `OFFLINE`, Compose has
  no Caddy container, and this Compose deployment no longer binds ports 80/443.

- [ ] **Step 5: Re-read the specification and inspect the final diff**

  Check every specification statement against the test/deployment evidence and
  confirm no unrelated user changes were removed.
