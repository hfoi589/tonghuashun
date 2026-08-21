#!/bin/sh
set -eu

if [ "${ADB_CONNECT:-0}" = "1" ]; then
    attempt=0
    until adb connect "${ADB_SERIAL:?ADB_SERIAL is required}" >/dev/null 2>&1; do
        attempt=$((attempt + 1))
        if [ "$attempt" -ge 60 ]; then
            echo "ADB device ${ADB_SERIAL} did not become available" >&2
            exit 1
        fi
        sleep 2
    done
fi

exec uvicorn level2_service.main:create_production_app --factory --host 0.0.0.0 --port 8000
