# FastAPI Single-Port Deployment Design

## Goal

Remove Caddy from the project and local deployment. FastAPI will serve both the
React production bundle and all existing API/WebSocket routes over plain HTTP on
one published port, defaulting to `8000`.

The local LAN entry point is:

```text
http://10.0.0.119:8000/
```

The administrator entry point is:

```text
http://10.0.0.119:8000/#admin
```

## Application Routing

`create_app()` accepts an optional `frontend_root`. When it contains a built
React bundle:

- `/assets/*` is served from `frontend_root/assets` with Starlette static-file
  path validation.
- `/` returns `frontend_root/index.html`.
- unmatched browser `GET` and `HEAD` routes return `index.html`, allowing the
  React single-page application to resolve client-side routes.
- any unmatched path under `/api` or `/api/*` returns the normal FastAPI JSON
  `404` response and is never rewritten to HTML.
- existing API routes, capture downloads, SSE, OpenAPI, and the administrator
  WebSocket keep their current paths and behavior.

When `frontend_root` is not configured or does not contain `index.html`, the app
continues to operate as an API-only test/development instance and does not
register a SPA fallback.

## Administrator Cookies

`create_app()` accepts `secure_admin_cookies`, defaulting to `True`. Both the
administrator session cookie and readable CSRF cookie use that value when they
are created or deleted.

Production settings expose this as `ADMIN_COOKIE_SECURE`. Truthy values are
`1`, `true`, `yes`, and `on`; false values are `0`, `false`, `no`, and `off`.
Invalid values stop startup with a configuration error. The local HTTP Compose
deployment sets it to `0`, so login and page-refresh session restoration work
over `http://10.0.0.119:8000`. Deployments placed behind HTTPS can set it to
`1`.

Plain HTTP is intentionally limited to the trusted local network in this
deployment. It must not be treated as internet-safe transport.

## Production Assembly

`DeploymentSettings` reads:

- `FRONTEND_ROOT`, optional, with `/app/frontend` used by Compose.
- `ADMIN_COOKIE_SECURE`, default `1`.

`create_production_app()` passes both values to `create_app()`.

The Dockerfile retains the Node-based `frontend-build` stage. The API image
copies `/src/frontend/dist` from that stage into `/app/frontend`; there is no
Caddy stage or Caddy configuration in the image.

## Compose Deployment

The `api` service:

- publishes `${APP_PORT:-8000}:8000` on all host interfaces;
- sets `FRONTEND_ROOT=/app/frontend`;
- sets `ADMIN_COOKIE_SECURE=0` for the approved local HTTP deployment;
- retains the existing Redis, capture, templates, administrator password, and
  host ADB wiring.

The Compose project contains no Caddy service and declares no Caddy data or
configuration volumes. `deploy/Caddyfile` is deleted. Environment examples and
README instructions use `APP_PORT=8000` and HTTP URLs instead of Caddy or HTTPS
settings.

Re-deployment removes the obsolete Caddy container with Compose orphan cleanup.
Previously created Docker named volumes are not destructively deleted from the
host; they are no longer referenced by the project and can be removed later if
the operator explicitly requests data cleanup.

## Verification

Automated checks cover:

- root HTML, static assets, and SPA route fallback;
- JSON `404` behavior for unknown `/api` routes;
- secure cookies by default and HTTP-compatible cookies when explicitly
  configured;
- production setting parsing and propagation;
- a valid Compose model with the API published on port `8000` and no Caddy
  service;
- an API Docker image that contains the built frontend and has no Caddy stage.

Local acceptance additionally verifies that only Redis and API are running,
the LAN URL serves the current frontend, administrator login survives refresh,
the Android runner remains reachable, and host ports `80` and `443` are no
longer owned by this Compose deployment.

## Boundaries

- This change does not modify stock navigation, OCR, long-screenshot assembly,
  queue semantics, Redis persistence, or Android login state.
- This change does not add TLS to FastAPI.
- This change does not delete capture, Redis, template, administrator, or
  Android data volumes.
