#!/bin/sh
# Keep one emulator's root Frida process and host forwarding alive across reboots.
set -u

once=0
if [ "${1:-}" = "--once" ]; then
    once=1
    shift
fi

serial=${1:?Usage: scripts/watch-macos-device-bridge.sh [--once] SERIAL HOST_PORT [ADB_BIN]}
host_port=${2:?Usage: scripts/watch-macos-device-bridge.sh [--once] SERIAL HOST_PORT [ADB_BIN]}
adb_bin=${3:-${ADB_BIN:-}}
if [ -z "$adb_bin" ]; then
    adb_bin=$(command -v adb 2>/dev/null || true)
fi
if [ -z "$adb_bin" ] && [ -x /opt/homebrew/bin/adb ]; then
    adb_bin=/opt/homebrew/bin/adb
fi
[ -x "$adb_bin" ] || exit 2
device_port=27042

adb_for() {
    "$adb_bin" -s "$serial" "$@"
}

device_ready() {
    adb_for get-state >/dev/null 2>&1 \
        && [ "$(adb_for shell getprop sys.boot_completed 2>/dev/null | tr -d '\r')" = "1" ]
}

wait_for_adb_after_root() {
    attempt=0
    until device_ready; do
        attempt=$((attempt + 1))
        [ "$attempt" -lt 30 ] || return 1
        sleep 1
    done
}

ensure_bridge() {
    device_ready || return 1

    identity=$(adb_for shell id 2>/dev/null || true)
    case "$identity" in
        uid=0*) ;;
        *)
            adb_for root >/dev/null 2>&1 || return 1
            wait_for_adb_after_root || return 1
            ;;
    esac

    if ! adb_for shell pidof ths-frida-server >/dev/null 2>&1; then
        adb_for shell 'nohup /data/local/tmp/ths-frida-server >/data/local/tmp/ths-frida-server.log 2>&1 &' >/dev/null 2>&1 || return 1
    fi

    adb_for shell pidof ths-frida-server >/dev/null 2>&1 || return 1
    adb_for forward "tcp:$host_port" "tcp:$device_port" >/dev/null 2>&1 || return 1
    return 0
}

if [ "$once" -eq 1 ]; then
    ensure_bridge
    exit $?
fi

while :; do
    ensure_bridge || :
    sleep 2
done
