#!/bin/sh
# Provision image-contained APK and Frida assets onto one newly created fixed AVD.
set -eu
exec 3>&2
exec 4>&1
exec 1>/dev/null
exec 2>/dev/null

if [ "$#" -ne 1 ]; then
    exit 2
fi

case "$1" in
    core_metrics) serial=emulator-5556; host_port=27043 ;;
    main_fund_flow) serial=emulator-5554; host_port=27042 ;;
    *) exit 2 ;;
esac

adb() {
    command adb -s "$serial" "$@"
}

fail() {
    printf '%s\n' "$1" >&3
    exit 1
}

boot_completed=$(adb shell getprop sys.boot_completed | tr -d '\r') \
    || fail DEVICE_PROVISION_FAILED
[ "$boot_completed" = 1 ] || fail DEVICE_BOOT_INCOMPLETE

package_path=$(adb shell pm path com.hexin.plat.android) \
    || fail DEVICE_PROVISION_FAILED
if [ -n "$package_path" ]; then
    fail DEVICE_PACKAGE_ALREADY_INSTALLED
fi

adb install /opt/ths/assets/ths.apk \
    || fail DEVICE_PROVISION_FAILED
adb root \
    || fail DEVICE_PROVISION_FAILED
adb push /opt/ths/assets/ths-frida-server /data/local/tmp/ths-frida-server \
    || fail DEVICE_PROVISION_FAILED
adb shell chmod 0755 /data/local/tmp/ths-frida-server \
    || fail DEVICE_PROVISION_FAILED
adb shell 'nohup /data/local/tmp/ths-frida-server >/dev/null 2>&1 &' \
    || fail DEVICE_PROVISION_FAILED
adb forward "tcp:$host_port" tcp:27042 \
    || fail DEVICE_PROVISION_FAILED

printf '%s\n' DEVICE_PROVISION_READY >&4
