#!/bin/sh
# Idempotent native-macOS bootstrap for two isolated Tonghuashun accounts.
# The existing fund AVD is never cloned, reinstalled, cleared, stopped, or navigated.
set -eu

apk_path=${1:?Usage: scripts/bootstrap-macos-dual-avd.sh /absolute/path/to/ths.apk /absolute/path/to/frida-server}
frida_path=${2:?Usage: scripts/bootstrap-macos-dual-avd.sh /absolute/path/to/ths.apk /absolute/path/to/frida-server}
fund_avd=${FUND_AVD_NAME:-THS_API_33_ARM64}
core_avd=${CORE_AVD_NAME:-THS_CORE_33_ARM64}
fund_serial=${FUND_ADB_SERIAL:-emulator-5554}
core_serial=${CORE_ADB_SERIAL:-emulator-5556}
package_name=com.hexin.plat.android
system_image='system-images;android-33;google_apis;arm64-v8a'
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
project_root=$(CDPATH= cd -- "$script_dir/.." && pwd)
emulator_bin=${EMULATOR_BIN:-}
if [ -z "$emulator_bin" ]; then
    emulator_bin=$(command -v emulator 2>/dev/null || true)
fi
if [ -z "$emulator_bin" ] && [ -x /opt/homebrew/share/android-commandlinetools/emulator/emulator ]; then
    emulator_bin=/opt/homebrew/share/android-commandlinetools/emulator/emulator
fi
[ -x "$emulator_bin" ] || {
    echo "Missing emulator. Set EMULATOR_BIN to the Android emulator binary." >&2
    exit 2
}
ths_java_home=${THS_JAVA_HOME:-}
if [ -z "$ths_java_home" ] && [ -d '/Applications/Android Studio.app/Contents/jbr/Contents/Home' ]; then
    ths_java_home='/Applications/Android Studio.app/Contents/jbr/Contents/Home'
fi
if [ -z "$ths_java_home" ] && [ -d /opt/homebrew/opt/openjdk@17/libexec/openjdk.jdk/Contents/Home ]; then
    ths_java_home=/opt/homebrew/opt/openjdk@17/libexec/openjdk.jdk/Contents/Home
fi

for command_name in sdkmanager avdmanager adb python3; do
    command -v "$command_name" >/dev/null 2>&1 || {
        echo "Missing $command_name. Install Android SDK command-line tools first." >&2
        exit 2
    }
done
adb_bin=$(command -v adb)

[ -f "$apk_path" ] || { echo "APK not found: $apk_path" >&2; exit 2; }
[ -x "$frida_path" ] || { echo "Frida server is missing or not executable: $frida_path" >&2; exit 2; }
python3 "$project_root/scripts/preflight.py" --apk-only --apk "$apk_path"

free_kb=$(df -Pk "$project_root" | awk 'NR == 2 {print $4}')
minimum_kb=$((10 * 1024 * 1024))
[ "$free_kb" -ge "$minimum_kb" ] || {
    echo "At least 10 GiB of free disk is required before creating the core AVD." >&2
    exit 2
}

adb_for() {
    serial=$1
    shift
    adb -s "$serial" "$@"
}

device_ready() {
    adb_for "$1" get-state >/dev/null 2>&1
}

wait_for_device() {
    serial=$1
    attempt=0
    until device_ready "$serial"; do
        attempt=$((attempt + 1))
        [ "$attempt" -lt 120 ] || {
            echo "Timed out waiting for $serial" >&2
            exit 1
        }
        sleep 2
    done
    attempt=0
    until [ "$(adb_for "$serial" shell getprop sys.boot_completed 2>/dev/null | tr -d '\r')" = "1" ]; do
        attempt=$((attempt + 1))
        [ "$attempt" -lt 120 ] || {
            echo "Timed out waiting for Android boot on $serial" >&2
            exit 1
        }
        sleep 2
    done
}

launch_avd_if_needed() {
    serial=$1
    avd_name=$2
    port=$3
    log_path=$4
    if ! device_ready "$serial"; then
        if command -v launchctl >/dev/null 2>&1; then
            service_label="com.ths.avd.$port"
            launchctl remove "$service_label" >/dev/null 2>&1 || true
            launchctl submit -l "$service_label" -- "$emulator_bin" -avd "$avd_name" -port "$port" -no-snapshot -no-audio -gpu host -memory 2048 -cores 4
        else
            nohup "$emulator_bin" -avd "$avd_name" -port "$port" -no-snapshot -no-audio -gpu host -memory 2048 -cores 4 </dev/null >"$log_path" 2>&1 &
        fi
    fi
    wait_for_device "$serial"
}

start_frida_if_needed() {
    serial=$1
    if adb_for "$serial" shell pidof ths-frida-server >/dev/null 2>&1; then
        return
    fi
    adb_for "$serial" root >/dev/null
    wait_for_device "$serial"
    if ! adb_for "$serial" shell 'test -x /data/local/tmp/ths-frida-server && test -s /data/local/tmp/ths-frida-server'; then
        adb_for "$serial" push "$frida_path" /data/local/tmp/ths-frida-server >/dev/null
        adb_for "$serial" shell chmod 755 /data/local/tmp/ths-frida-server
    fi
    adb_for "$serial" shell 'nohup /data/local/tmp/ths-frida-server >/dev/null 2>&1 &'
}

start_bridge_supervisor() {
    serial=$1
    host_port=$2
    watcher="$script_dir/watch-macos-device-bridge.sh"
    if command -v launchctl >/dev/null 2>&1; then
        service_label="com.ths.bridge.$host_port"
        launchctl remove "$service_label" >/dev/null 2>&1 || true
        launchctl submit -l "$service_label" -- "$watcher" "$serial" "$host_port" "$adb_bin"
    else
        nohup "$watcher" "$serial" "$host_port" "$adb_bin" </dev/null >/dev/null 2>&1 &
    fi
}

# Preserve the current fund account: if its AVD is stopped, only launch its existing data.
if ! "$emulator_bin" -list-avds | grep -Fx "$fund_avd" >/dev/null; then
    echo "Existing fund AVD $fund_avd was not found; refusing to create or replace it." >&2
    exit 2
fi
launch_avd_if_needed "$fund_serial" "$fund_avd" 5554 /tmp/ths-fund-avd.log

JAVA_HOME="$ths_java_home" sdkmanager "platform-tools" "emulator" "platforms;android-33" "$system_image"
core_created=0
if ! "$emulator_bin" -list-avds | grep -Fx "$core_avd" >/dev/null; then
    printf 'no\n' | JAVA_HOME="$ths_java_home" avdmanager create avd --force --name "$core_avd" --package "$system_image"
    core_created=1
fi
launch_avd_if_needed "$core_serial" "$core_avd" 5556 /tmp/ths-core-avd.log
"$script_dir/configure-macos-core-display.sh" "$core_serial" "$adb_bin"

core_installed=0
if ! adb_for "$core_serial" shell pm path "$package_name" >/dev/null 2>&1; then
    adb_for "$core_serial" install "$apk_path"
    core_installed=1
fi
if ! adb_for "$core_serial" shell pidof "$package_name" >/dev/null 2>&1; then
    adb_for "$core_serial" shell am start -n "$package_name/com.hexin.plat.android.LogoEmptyActivity" >/dev/null
fi

if [ "$core_created" -eq 1 ] || [ "$core_installed" -eq 1 ]; then
    echo "Core AVD is ready for manual login and big-order permission verification."
    echo "No account credentials are accepted or stored by this script."
    if [ -t 0 ]; then
        printf 'Press Enter only after the second account is fully verified: '
        read -r _manual_confirmation
    else
        echo "Run this script again interactively after manual login." >&2
        exit 3
    fi
fi

start_frida_if_needed "$fund_serial"
start_frida_if_needed "$core_serial"
adb_for "$fund_serial" forward tcp:27042 tcp:27042
adb_for "$core_serial" forward tcp:27043 tcp:27042
start_bridge_supervisor "$fund_serial" 27042
start_bridge_supervisor "$core_serial" 27043

echo "Dual devices ready: core=$core_serial/27043, fund=$fund_serial/27042."
