#!/bin/sh
# Install the loopback lifecycle broker from a project checkout into a stable user location.
set -eu
exec 3>&2
exec 2>/dev/null

usage() {
    printf '%s\n' 'Usage: install-macos-device-lifecycle.sh --project-root PATH --env-file PATH' >&3
    exit 64
}

fail() {
    printf '%s\n' 'DEVICE_LIFECYCLE_INSTALL_FAILED' >&3
    exit 1
}

project_root=
env_file=
while [ "$#" -gt 0 ]; do
    case "$1" in
        --project-root)
            [ "$#" -ge 2 ] || usage
            project_root=$2
            shift 2
            ;;
        --env-file)
            [ "$#" -ge 2 ] || usage
            env_file=$2
            shift 2
            ;;
        *) usage ;;
    esac
done
[ -n "$project_root" ] && [ -n "$env_file" ] || usage

[ "$(uname -s)" = Darwin ] || fail
python3_bin=$(command -v python3 2>/dev/null) || fail
adb_bin=$(command -v adb 2>/dev/null) || fail
emulator_bin=$(command -v emulator 2>/dev/null) || fail
command -v launchctl >/dev/null 2>&1 || fail

service_source=$project_root/scripts/macos-device-lifecycle.py
watcher_source=$project_root/scripts/watch-macos-device-bridge.sh
display_source=$project_root/scripts/configure-macos-core-display.sh
[ -f "$service_source" ] && [ -f "$watcher_source" ] && [ -f "$display_source" ] || fail
avds=$("$emulator_bin" -list-avds 2>/dev/null) || fail
printf '%s\n' "$avds" | grep -Fx 'THS_CORE_33_ARM64' >/dev/null || fail
printf '%s\n' "$avds" | grep -Fx 'THS_API_33_ARM64' >/dev/null || fail

runtime_dir=${HOME}/.local/lib/ths-device-lifecycle/
config_dir=${HOME}/.config
launch_agents_dir=${HOME}/Library/LaunchAgents
host_config=${HOME}/.config/ths-device-lifecycle.env
service_plist=${HOME}/Library/LaunchAgents/com.ths.device-lifecycle.plist
fund_bridge_plist=${launch_agents_dir}/com.ths.device-bridge.27042.plist
core_bridge_plist=${launch_agents_dir}/com.ths.device-bridge.27043.plist

install -d -m 700 "$runtime_dir" "$config_dir" "$launch_agents_dir" || fail
install -m 755 "$service_source" "$runtime_dir/macos-device-lifecycle.py" || fail
install -m 755 "$watcher_source" "$runtime_dir/watch-macos-device-bridge.sh" || fail
install -m 755 "$display_source" "$runtime_dir/configure-macos-core-display.sh" || fail

token=$(grep '^THS_DEVICE_LIFECYCLE_TOKEN=' "$env_file" 2>/dev/null | sed -n '1s/^[^=]*=//p' || true)
if [ -z "$token" ]; then
    token=$(
        "$python3_bin" -c 'import secrets; print(secrets.token_urlsafe(32))'
    ) || fail
    env_parent=$(dirname "$env_file")
    [ -d "$env_parent" ] || fail
    env_tmp=$(mktemp "$env_file.tmp.XXXXXX") || fail
    if [ -e "$env_file" ]; then
        token_replaced=0
        while IFS= read -r line || [ -n "$line" ]; do
            case "$line" in
                THS_DEVICE_LIFECYCLE_TOKEN=*)
                    printf '%s\n' "THS_DEVICE_LIFECYCLE_TOKEN=$token" >> "$env_tmp" || fail
                    token_replaced=1
                    ;;
                *) printf '%s\n' "$line" >> "$env_tmp" || fail ;;
            esac
        done < "$env_file"
    else
        token_replaced=0
    fi
    [ "$token_replaced" -eq 1 ] || printf '%s\n' "THS_DEVICE_LIFECYCLE_TOKEN=$token" >> "$env_tmp" || fail
    chmod 0600 "$env_tmp" || fail
    mv "$env_tmp" "$env_file" || fail
fi
chmod 0600 "$env_file" || fail

config_tmp=$(mktemp "$host_config.tmp.XXXXXX") || fail
if [ -e "$host_config" ]; then
    while IFS= read -r line || [ -n "$line" ]; do
        case "$line" in
            THS_DEVICE_LIFECYCLE_TOKEN=*|THS_DEVICE_LIFECYCLE_BIND_HOST=*|THS_DEVICE_LIFECYCLE_PORT=*|THS_DEVICE_LIFECYCLE_EMULATOR_BIN=*|PATH=*|CORE_AVD_NAME=*|CORE_ADB_SERIAL=*|CORE_EMULATOR_PORT=*|CORE_FRIDA_HOST_PORT=*|FUND_AVD_NAME=*|FUND_ADB_SERIAL=*|FUND_EMULATOR_PORT=*|FUND_FRIDA_HOST_PORT=*) ;;
            *) printf '%s\n' "$line" >> "$config_tmp" || fail ;;
        esac
    done < "$host_config"
fi
{
    printf '%s\n' "THS_DEVICE_LIFECYCLE_TOKEN=$token"
    printf '%s\n' 'THS_DEVICE_LIFECYCLE_BIND_HOST=127.0.0.1'
    printf '%s\n' 'THS_DEVICE_LIFECYCLE_PORT=18765'
    printf '%s\n' 'THS_DEVICE_LIFECYCLE_EMULATOR_BIN=emulator'
    printf '%s\n' "PATH=$PATH"
    printf '%s\n' 'CORE_AVD_NAME=THS_CORE_33_ARM64'
    printf '%s\n' 'CORE_ADB_SERIAL=emulator-5556'
    printf '%s\n' 'CORE_EMULATOR_PORT=5556'
    printf '%s\n' 'CORE_FRIDA_HOST_PORT=27043'
    printf '%s\n' 'FUND_AVD_NAME=THS_API_33_ARM64'
    printf '%s\n' 'FUND_ADB_SERIAL=emulator-5554'
    printf '%s\n' 'FUND_EMULATOR_PORT=5554'
    printf '%s\n' 'FUND_FRIDA_HOST_PORT=27042'
} >> "$config_tmp" || fail
mv "$config_tmp" "$host_config" || fail
chmod 0600 "$host_config" || fail

write_service_plist() {
    plist_tmp=$(mktemp "$service_plist.tmp.XXXXXX") || fail
    cat > "$plist_tmp" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>com.ths.device-lifecycle</string>
<key>ProgramArguments</key><array>
<string>$python3_bin</string><string>$runtime_dir/macos-device-lifecycle.py</string>
<string>--config</string><string>$host_config</string>
</array>
<key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
</dict></plist>
EOF
    chmod 600 "$plist_tmp" || fail
    mv "$plist_tmp" "$service_plist" || fail
}

write_bridge_plist() {
    bridge_plist=$1
    bridge_label=$2
    bridge_serial=$3
    bridge_port=$4
    plist_tmp=$(mktemp "$bridge_plist.tmp.XXXXXX") || fail
    cat > "$plist_tmp" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>$bridge_label</string>
<key>ProgramArguments</key><array>
<string>$runtime_dir/watch-macos-device-bridge.sh</string><string>$bridge_serial</string>
<string>$bridge_port</string><string>$adb_bin</string>
</array>
<key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
</dict></plist>
EOF
    chmod 600 "$plist_tmp" || fail
    mv "$plist_tmp" "$bridge_plist" || fail
}

write_service_plist
write_bridge_plist "$fund_bridge_plist" com.ths.device-bridge.27042 emulator-5554 27042
write_bridge_plist "$core_bridge_plist" com.ths.device-bridge.27043 emulator-5556 27043

launchctl bootout gui/$UID/com.ths.device-lifecycle >/dev/null 2>&1 || true
launchctl bootout gui/$UID/com.ths.device-bridge.27042 >/dev/null 2>&1 || true
launchctl bootout gui/$UID/com.ths.device-bridge.27043 >/dev/null 2>&1 || true
launchctl bootstrap gui/$UID "$service_plist" >/dev/null 2>&1 || fail
launchctl bootstrap gui/$UID "$fund_bridge_plist" >/dev/null 2>&1 || fail
launchctl bootstrap gui/$UID "$core_bridge_plist" >/dev/null 2>&1 || fail
launchctl kickstart -k gui/$UID/com.ths.device-lifecycle >/dev/null 2>&1 || fail
launchctl kickstart -k gui/$UID/com.ths.device-bridge.27042 >/dev/null 2>&1 || fail
launchctl kickstart -k gui/$UID/com.ths.device-bridge.27043 >/dev/null 2>&1 || fail

printf '%s\n' 'DEVICE_LIFECYCLE_INSTALL_READY'
