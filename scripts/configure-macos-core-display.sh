#!/bin/sh
# Keep the core capture device aligned with the runner's calibrated viewport.
set -eu

serial=${1:?Usage: scripts/configure-macos-core-display.sh SERIAL [ADB_BIN]}
adb_bin=${2:-${ADB_BIN:-adb}}

"$adb_bin" -s "$serial" shell wm size 1080x1920
"$adb_bin" -s "$serial" shell wm density 480
