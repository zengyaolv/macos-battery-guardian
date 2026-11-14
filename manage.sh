#!/bin/zsh
set -e

BASE_DIR="$(cd "$(dirname "$0")" && pwd)"
RUNTIME_DIR="$HOME/Library/Application Support/BatteryGuardian"
PLIST_LABEL="io.github.battery-guardian"
PLIST_PATH="$HOME/Library/LaunchAgents/${PLIST_LABEL}.plist"
MONITOR_PY="${BASE_DIR}/battery_monitor.py"
ALERT_SWIFT="${BASE_DIR}/BatteryFullscreenAlert.swift"
ALERT_BIN="${BASE_DIR}/BatteryFullscreenAlert"
ALERT_APP="${BASE_DIR}/BatteryFullscreenAlert.app"

ensure_config() {
    if [[ ! -f "${BASE_DIR}/config.json" ]] && [[ -f "${BASE_DIR}/config.example.json" ]]; then
        cp "${BASE_DIR}/config.example.json" "${BASE_DIR}/config.json"
        echo "Initialized config.json from config.example.json"
    fi
}

ensure_compiled() {
    if [[ ! -x "${ALERT_APP}/Contents/MacOS/BatteryFullscreenAlert" ]] || [[ "$ALERT_SWIFT" -nt "${ALERT_APP}/Contents/MacOS/BatteryFullscreenAlert" ]]; then
        echo "Compiling native silent fullscreen alert app (BatteryFullscreenAlert.app)..."
        /usr/bin/swiftc -O "$ALERT_SWIFT" -o "$ALERT_BIN"
        mkdir -p "${ALERT_APP}/Contents/MacOS"
        cp -f "$ALERT_BIN" "${ALERT_APP}/Contents/MacOS/BatteryFullscreenAlert"
        chmod +x "${ALERT_APP}/Contents/MacOS/BatteryFullscreenAlert"
        cat > "${ALERT_APP}/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleExecutable</key>
    <string>BatteryFullscreenAlert</string>
    <key>CFBundleIdentifier</key>
    <string>io.github.BatteryFullscreenAlert</string>
    <key>CFBundleName</key>
    <string>BatteryFullscreenAlert</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleShortVersionString</key>
    <string>1.0</string>
    <key>CFBundleVersion</key>
    <string>1</string>
    <key>LSUIElement</key>
    <true/>
    <key>NSHighResolutionCapable</key>
    <true/>
</dict>
</plist>
EOF
    fi
    chmod +x "$MONITOR_PY"
}

sync_to_runtime() {
    ensure_config
    ensure_compiled
    mkdir -p "$RUNTIME_DIR"
    cp -f "$MONITOR_PY" "${RUNTIME_DIR}/battery_monitor.py"
    chmod +x "${RUNTIME_DIR}/battery_monitor.py"
    cp -f "$ALERT_BIN" "${RUNTIME_DIR}/BatteryFullscreenAlert"
    rm -rf "${RUNTIME_DIR}/BatteryFullscreenAlert.app"
    cp -R "$ALERT_APP" "${RUNTIME_DIR}/BatteryFullscreenAlert.app"
    if [[ -f "${BASE_DIR}/config.example.json" ]]; then
        cp -f "${BASE_DIR}/config.example.json" "${RUNTIME_DIR}/config.example.json"
    fi
    if [[ -f "${BASE_DIR}/config.json" ]] && [[ ! -L "${BASE_DIR}/config.json" ]]; then
        cp -f "${BASE_DIR}/config.json" "${RUNTIME_DIR}/config.json"
    fi
    # Symlink runtime state and logs back to the workspace while keeping launchd outside TCC-protected ~/Documents.
    for f in state.json battery_guardian.log launchd_stdout.log launchd_stderr.log; do
        touch "${RUNTIME_DIR}/${f}"
        rm -f "${BASE_DIR}/${f}"
        ln -s "${RUNTIME_DIR}/${f}" "${BASE_DIR}/${f}"
    done
}

install_service() {
    sync_to_runtime
    : > "${RUNTIME_DIR}/launchd_stderr.log"
    mkdir -p "$HOME/Library/LaunchAgents"

    cat > "$PLIST_PATH" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${PLIST_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/python3</string>
        <string>${RUNTIME_DIR}/battery_monitor.py</string>
        <string>--check</string>
    </array>
    <key>WorkingDirectory</key>
    <string>${RUNTIME_DIR}</string>
    <key>StartInterval</key>
    <integer>60</integer>
    <key>RunAtLoad</key>
    <true/>
    <key>StandardOutPath</key>
    <string>${RUNTIME_DIR}/launchd_stdout.log</string>
    <key>StandardErrorPath</key>
    <string>${RUNTIME_DIR}/launchd_stderr.log</string>
</dict>
</plist>
EOF

    launchctl unload "$PLIST_PATH" 2>/dev/null || true
    launchctl load "$PLIST_PATH"
    echo "Background LaunchAgent installed in ${RUNTIME_DIR} and started (${PLIST_LABEL})."
    echo "Config file : ${BASE_DIR}/config.json"
    echo "Log file    : ${BASE_DIR}/battery_guardian.log"
}

uninstall_service() {
    if [[ -f "$PLIST_PATH" ]]; then
        launchctl unload "$PLIST_PATH" 2>/dev/null || true
        rm -f "$PLIST_PATH"
        echo "Stopped and removed LaunchAgent (${PLIST_LABEL})."
    else
        echo "LaunchAgent is not currently installed."
    fi
}

set_email() {
    local target_email="$1"
    if [[ -z "$target_email" ]]; then
        echo "Usage: ./manage.sh set-email <recipient@example.com>"
        exit 1
    fi
    ensure_config
    /usr/bin/python3 -c '
import json, sys
from pathlib import Path
for p_str in sys.argv[1:-1]:
    p = Path(p_str)
    if p.exists():
        data = json.loads(p.read_text(encoding="utf-8"))
        data.setdefault("email", {})["to_email"] = sys.argv[-1]
        p.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
print(f"Updated alert recipient email to: {sys.argv[-1]}")
' "${BASE_DIR}/config.json" "${RUNTIME_DIR}/config.json" "$target_email"
}

case "${1:-status}" in
    install)
        install_service
        ;;
    uninstall)
        uninstall_service
        ;;
    status)
        sync_to_runtime
        /usr/bin/python3 "${RUNTIME_DIR}/battery_monitor.py" --status
        echo "\n=== Background LaunchAgent Status ==="
        if launchctl list | grep -q "$PLIST_LABEL"; then
            echo "  Running (${PLIST_LABEL}, checks every 60s, runtime: ~/Library/Application Support/BatteryGuardian)"
        else
            echo "  Not running (run ./manage.sh install to enable background monitoring)"
        fi
        ;;
    pause|mute)
        sync_to_runtime
        /usr/bin/python3 "${RUNTIME_DIR}/battery_monitor.py" --pause "${2:-120}"
        ;;
    unmute)
        sync_to_runtime
        /usr/bin/python3 "${RUNTIME_DIR}/battery_monitor.py" --unmute
        ;;
    test-high)
        sync_to_runtime
        /usr/bin/python3 "${RUNTIME_DIR}/battery_monitor.py" --test-high
        ;;
    test-low)
        sync_to_runtime
        /usr/bin/python3 "${RUNTIME_DIR}/battery_monitor.py" --test-low
        ;;
    test-critical)
        sync_to_runtime
        /usr/bin/python3 "${RUNTIME_DIR}/battery_monitor.py" --test-critical
        ;;
    test-email)
        sync_to_runtime
        /usr/bin/python3 "${RUNTIME_DIR}/battery_monitor.py" --test-email
        ;;
    set-email)
        set_email "$2"
        ;;
    *)
        echo "Usage: ./manage.sh {install|uninstall|status|pause [minutes]|unmute|set-email <email>|test-high|test-low|test-critical|test-email}"
        exit 1
        ;;
esac
