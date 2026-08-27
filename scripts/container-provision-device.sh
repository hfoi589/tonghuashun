#!/bin/sh
# Provision image-contained APK and Frida assets onto one newly created fixed AVD.
set -eu
exec 3>&2
exec 4>&1
exec 1>/dev/null
exec 2>/dev/null

if [ "$#" -ne 2 ]; then
    exit 2
fi

case "$1" in
    core_metrics) serial=emulator-5556; host_port=27043 ;;
    main_fund_flow) serial=emulator-5554; host_port=27042 ;;
    *) exit 2 ;;
esac
step=$2
case "$step" in
    apk|frida) ;;
    *) exit 2 ;;
esac

adb() {
    command adb -s "$serial" "$@"
}

device_ready() {
    adb get-state >/dev/null 2>&1 \
        && [ "$(adb shell getprop sys.boot_completed 2>/dev/null | tr -d '\r')" = 1 ]
}

fail() {
    printf '%s\n' "$1" >&3
    exit 1
}

boot_completed=$(adb shell getprop sys.boot_completed | tr -d '\r') \
    || fail DEVICE_PROVISION_FAILED
[ "$boot_completed" = 1 ] || fail DEVICE_BOOT_INCOMPLETE

verify_apk() {
    package_output=$(adb shell pm path com.hexin.plat.android) \
        || fail DEVICE_PROVISION_FAILED
    package_path=$(printf '%s\n' "$package_output" | sed -n '1s/^package://p')
    [ -n "$package_path" ] || return 1
    [ "$(printf '%s\n' "$package_output" | wc -l | tr -d ' ')" = 1 ] \
        || fail INSTALLED_APK_PATH_INVALID
    case "$package_path" in
        /data/app/*/base.apk) ;;
        *) fail INSTALLED_APK_PATH_INVALID ;;
    esac
    digest_output=$(adb shell sha256sum "$package_path") \
        || fail DEVICE_PROVISION_FAILED
    digest=$(printf '%s\n' "$digest_output" | awk 'NR == 1 {print $1}')
    digest_path=$(printf '%s\n' "$digest_output" | awk 'NR == 1 {print $2}')
    [ "$digest" = 2554490aa3f5e2df17ac0a711311f3f85ee3130008af9bb4ab12510b3d6e971e ] \
        || fail INSTALLED_APK_MISMATCH
    [ "$digest_path" = "$package_path" ] || fail INSTALLED_APK_PATH_INVALID
    return 0
}

if [ "$step" = apk ]; then
    if ! verify_apk; then
        adb install /opt/ths/assets/ths.apk \
            || fail DEVICE_PROVISION_FAILED
        verify_apk || fail DEVICE_PROVISION_FAILED
    fi
    printf '%s\n' DEVICE_APK_VERIFIED >&4
    exit 0
fi

adb root || fail DEVICE_PROVISION_FAILED
attempt=0
until device_ready; do
    attempt=$((attempt + 1))
    [ "$attempt" -lt 30 ] || fail DEVICE_PROVISION_FAILED
    sleep 1
done
adb push /opt/ths/assets/ths-frida-server /data/local/tmp/ths-frida-server \
    || fail DEVICE_PROVISION_FAILED
adb shell chmod 0755 /data/local/tmp/ths-frida-server \
    || fail DEVICE_PROVISION_FAILED
adb shell 'nohup /data/local/tmp/ths-frida-server >/dev/null 2>&1 &' \
    || fail DEVICE_PROVISION_FAILED
adb shell pidof ths-frida-server >/dev/null 2>&1 \
    || fail DEVICE_PROVISION_FAILED
adb forward "tcp:$host_port" tcp:27042 \
    || fail DEVICE_PROVISION_FAILED

printf '%s\n' DEVICE_FRIDA_READY >&4
