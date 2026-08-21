#!/bin/sh
# Bootstrap only a native Apple Silicon Android VM. It does not run Android in Docker.
set -eu

apk_path=${1:?Usage: scripts/bootstrap-macos-avd.sh /absolute/path/to/ths.apk}
avd_name=${AVD_NAME:-THS_API_33_ARM64}
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
project_root=$(CDPATH= cd -- "$script_dir/.." && pwd)

python3 "$project_root/scripts/preflight.py" --apk-only --apk "$apk_path"

for command in sdkmanager avdmanager emulator adb; do
    command -v "$command" >/dev/null 2>&1 || {
        echo "Missing $command. Install Android SDK command-line tools and add them to PATH." >&2
        exit 2
    }
done

sdkmanager "platform-tools" "emulator" "platforms;android-33" "system-images;android-33;google_apis;arm64-v8a"
if ! emulator -list-avds | grep -Fx "$avd_name" >/dev/null; then
    printf 'no\n' | avdmanager create avd --force --name "$avd_name" --package "system-images;android-33;google_apis;arm64-v8a"
fi

if ! adb devices | grep -E '^emulator-[0-9]+[[:space:]]+device$' >/dev/null; then
    emulator -avd "$avd_name" -no-snapshot -no-audio -gpu host >/tmp/ths-avd.log 2>&1 &
fi
adb wait-for-device
adb install -r "$apk_path"
adb start-server
echo "Native AVD ready. Keep its default localhost-only ADB server; Docker reaches it through host.docker.internal."
