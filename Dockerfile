# syntax=docker/dockerfile:1

FROM node:22-bookworm-slim AS frontend-build
WORKDIR /src/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim-bookworm AS api
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
RUN apt-get update \
    && apt-get install --no-install-recommends -y android-sdk-platform-tools ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml ./
COPY level2_service/ ./level2_service/
RUN pip install --no-cache-dir .
COPY scripts/container-entrypoint.sh /usr/local/bin/ths-entrypoint
RUN chmod 0755 /usr/local/bin/ths-entrypoint \
    && mkdir -p /data/captures
EXPOSE 8000
ENTRYPOINT ["/usr/local/bin/ths-entrypoint"]

FROM caddy:2.8-alpine AS caddy
COPY deploy/Caddyfile /etc/caddy/Caddyfile
COPY --from=frontend-build /src/frontend/dist /srv
