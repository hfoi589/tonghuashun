# 同花顺 Level2 网页截图系统 Implementation Plan

> **For agentic workers:** Implement task-by-task with tests first. Keep the APK as a deployment artifact; never commit credentials.

**Goal:** Build a single-host web service that queues public symbol requests, drives the user's logged-in THS Android app, and returns three verified Level2 screenshots while allowing password-protected admin control.

**Architecture:** React/Vite frontend, FastAPI backend, Redis Streams queue/state, one Python Android runner using ADB/uiautomator2 with OpenCV fallback, and Caddy for HTTPS. Linux amd64 uses Redroid Android 13 with ARM translation; ARM macOS uses a native API 33 ARM64 AVD while the web services remain in Docker.

**Tech Stack:** Python 3.12, FastAPI, Redis, React + TypeScript, Vite, Playwright/Vitest, pytest, ADB, uiautomator2, OpenCV, Caddy, Docker Compose.

**Spec:** Conversation-approved Level2 web screenshot design.

## Global Constraints

- APK SHA-256: `2554490aa3f5e2df17ac0a711311f3f85ee3130008af9bb4ab12510b3d6e971e`.
- Capture kinds: `LARGE_ORDER_NET`, `LARGE_ORDER_AMOUNT`, `RETAIL_COUNT`.
- Queue is FIFO and single-runner; no per-IP rate limit; global pending cap defaults to 200.
- Screenshot retention is 24 hours; task metadata retention is 7 days.
- Do not bypass CAPTCHA, device verification, certificate checks, emulator detection, or private THS protocols.
- Android credentials and admin keystrokes must not be persisted or logged.

### Task 1: Backend contracts, queue, admin session, and retention

Implement FastAPI API, Redis Streams job state, public task token, SSE updates, capture serving, Argon2id admin session, CSRF protection, lock/runner health endpoints, and retention cleanup. Write pytest tests first for symbol validation, state transitions, FIFO ordering, partial completion, expiry, queue cap, and session/CSRF behavior.

### Task 2: React public and admin UI

Implement the public submit/status/result pages and password-protected admin page. Use the Task 1 API types. Add SSE status rendering, three capture cards, admin queue controls, runner health, 3–5 FPS WebSocket screen display, pointer/keyboard event forwarding, and explicit admin takeover/resume controls. Write frontend tests for submit flow, partial results, expired links, admin auth, and lock state.

### Task 3: Android runner, deployment profiles, and verification

Implement the runner state machine, ADB/uiautomator2 selectors, OpenCV fallback, screenshot validation, retry and WAITING_ADMIN handling, fake-device fixtures, Linux Redroid profile, ARM Mac AVD bootstrap, APK hash/preflight checks, Caddy/Compose configuration, health checks, and end-to-end verification documentation. Do not claim a platform supported until the real APK installs, stays alive for five minutes, reaches Level2 after manual login, and captures the three pages.

