# syntax=docker/dockerfile:1

FROM node:22-bookworm-slim AS frontend-build
WORKDIR /src/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM debian:bookworm-slim AS mobile-assets
RUN apt-get update \
    && apt-get install --no-install-recommends -y ca-certificates curl xz-utils \
    && rm -rf /var/lib/apt/lists/* \
    && install -d -m 0555 /opt/ths/assets
COPY --chmod=0444 ths_android_V11_59_03.apk /opt/ths/assets/ths.apk
RUN set -eu; \
    test "$(stat -c '%s' /opt/ths/assets/ths.apk)" = '214088292'; \
    test "$(sha256sum /opt/ths/assets/ths.apk | cut -d ' ' -f 1)" = '2554490aa3f5e2df17ac0a711311f3f85ee3130008af9bb4ab12510b3d6e971e'; \
    curl --fail --location --show-error --silent \
      --proto '=https' --tlsv1.2 \
      'https://github.com/frida/frida/releases/download/16.7.19/frida-server-16.7.19-android-arm64.xz' \
      --output /tmp/frida-server-16.7.19-android-arm64.xz; \
    test "$(stat -c '%s' /tmp/frida-server-16.7.19-android-arm64.xz)" = '15972776'; \
    test "$(sha256sum /tmp/frida-server-16.7.19-android-arm64.xz | cut -d ' ' -f 1)" = '36ec3d7474b1ac69c4e7ec985612fae771d37ffb71cb94858bc6978f69f5e581'; \
    xz --decompress --stdout /tmp/frida-server-16.7.19-android-arm64.xz > /opt/ths/assets/ths-frida-server; \
    test "$(stat -c '%s' /opt/ths/assets/ths-frida-server)" = '53702368'; \
    test "$(sha256sum /opt/ths/assets/ths-frida-server | cut -d ' ' -f 1)" = '4eebf1fbc66ff54aba9a9124c2ef8b32b566616388c60e2caa65148a529d826a'; \
    printf '%s\n' \
      '{' \
      '  "apk": {' \
      '    "filename": "ths.apk",' \
      '    "size": 214088292,' \
      '    "sha256": "2554490aa3f5e2df17ac0a711311f3f85ee3130008af9bb4ab12510b3d6e971e",' \
      '    "abis": ["arm64-v8a", "armeabi-v7a"]' \
      '  },' \
      '  "frida_server": {' \
      '    "version": "16.7.19",' \
      '    "size": 53702368,' \
      '    "sha256_xz": "36ec3d7474b1ac69c4e7ec985612fae771d37ffb71cb94858bc6978f69f5e581",' \
      '    "sha256": "4eebf1fbc66ff54aba9a9124c2ef8b32b566616388c60e2caa65148a529d826a"' \
      '  }' \
      '}' > /opt/ths/assets/manifest.json; \
    chmod 0444 /opt/ths/assets/ths.apk /opt/ths/assets/manifest.json; \
    chmod 0555 /opt/ths/assets/ths-frida-server; \
    rm -f /tmp/frida-server-16.7.19-android-arm64.xz

FROM python:3.12-slim-bookworm AS api
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
COPY --from=ghcr.io/astral-sh/uv:0.8.14 /uv /uvx /bin/
WORKDIR /app
LABEL org.opencontainers.image.ths.apk.sha256="2554490aa3f5e2df17ac0a711311f3f85ee3130008af9bb4ab12510b3d6e971e" \
      org.opencontainers.image.ths.frida-server.version="16.7.19" \
      org.opencontainers.image.ths.frida-server.sha256="4eebf1fbc66ff54aba9a9124c2ef8b32b566616388c60e2caa65148a529d826a" \
      org.opencontainers.image.ths.frida-server.xz.sha256="36ec3d7474b1ac69c4e7ec985612fae771d37ffb71cb94858bc6978f69f5e581"
RUN apt-get update \
    && apt-get install --no-install-recommends -y android-sdk-platform-tools adb ca-certificates tesseract-ocr tesseract-ocr-chi-sim \
    && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml ./
COPY uv.lock ./
COPY level2_service/ ./level2_service/
RUN uv sync --frozen --no-dev
ENV PATH="/app/.venv/bin:${PATH}"
COPY --from=frontend-build /src/frontend/dist /app/frontend
COPY --from=mobile-assets --chmod=0444 /opt/ths/assets/manifest.json /opt/ths/assets/manifest.json
COPY --from=mobile-assets --chmod=0444 /opt/ths/assets/ths.apk /opt/ths/assets/ths.apk
COPY --from=mobile-assets --chmod=0555 /opt/ths/assets/ths-frida-server /opt/ths/assets/ths-frida-server
COPY --chmod=0555 scripts/container-provision-device.sh /usr/local/bin/container-provision-device
COPY --chmod=0555 scripts/install-macos-device-lifecycle.sh /opt/ths/deployment/install-macos-device-lifecycle.sh
COPY --chmod=0555 scripts/macos-device-lifecycle.py /opt/ths/deployment/macos-device-lifecycle.py
COPY --chmod=0444 scripts/macos_device_identity.py /opt/ths/deployment/macos_device_identity.py
COPY --chmod=0555 scripts/watch-macos-device-bridge.sh /opt/ths/deployment/watch-macos-device-bridge.sh
COPY --chmod=0555 scripts/configure-macos-core-display.sh /opt/ths/deployment/configure-macos-core-display.sh
COPY scripts/container-entrypoint.sh /usr/local/bin/ths-entrypoint
RUN chmod 0555 /opt/ths/assets /opt/ths/deployment \
    && chmod 0755 /usr/local/bin/ths-entrypoint \
    && mkdir -p /data/captures /data/templates
EXPOSE 8000
ENTRYPOINT ["/usr/local/bin/ths-entrypoint"]
