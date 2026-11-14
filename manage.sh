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
    if [[ -f "${BASE_DIR}/config.json" ]] && [[ ! -L "${BASE_DIR}/config.json" ]]; then
        chmod 600 "${BASE_DIR}/config.json"
    fi
}

ensure_compiled() {
    if [[ ! -x "${ALERT_APP}/Contents/MacOS/BatteryFullscreenAlert" ]] || [[ "$ALERT_SWIFT" -nt "${ALERT_APP}/Contents/MacOS/BatteryFullscreenAlert" ]]; then
        echo "Compiling native silent fullscreen alert app (BatteryFullscreenAlert.app)..."
        /usr/bin/swiftc -O "$ALERT_SWIFT" -o "$ALERT_BIN"
        mkdir -p "${ALERT_APP}/Contents/MacOS"
        cp -f "$ALERT_BIN" "${ALERT_APP}/Contents/MacOS/BatteryFullscreenAlert"
        chmod +x "${ALERT_APP}/Contents/MacOS/BatteryFullscreenAlert"
        /usr/bin/python3 -c '
import plistlib, sys
from pathlib import Path

plist_data = {
    "CFBundleExecutable": "BatteryFullscreenAlert",
    "CFBundleIdentifier": "io.github.BatteryFullscreenAlert",
    "CFBundleName": "BatteryFullscreenAlert",
    "CFBundlePackageType": "APPL",
    "CFBundleShortVersionString": "1.0",
    "CFBundleVersion": "1",
    "LSUIElement": True,
    "NSHighResolutionCapable": True,
}
Path(sys.argv[1]).write_bytes(plistlib.dumps(plist_data, fmt=plistlib.FMT_XML, sort_keys=False))
' "${ALERT_APP}/Contents/Info.plist"
    fi
    chmod +x "$MONITOR_PY"
}

sync_to_runtime() {
    ensure_config
    ensure_compiled
    mkdir -p -m 700 "$RUNTIME_DIR"
    chmod 700 "$RUNTIME_DIR"
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
        chmod 600 "${RUNTIME_DIR}/config.json"
    fi
    # Symlink runtime state and logs back to the workspace while keeping launchd outside TCC-protected ~/Documents.
    for f in state.json battery_guardian.log launchd_stdout.log launchd_stderr.log; do
        touch "${RUNTIME_DIR}/${f}"
        chmod 600 "${RUNTIME_DIR}/${f}"
        rm -f "${BASE_DIR}/${f}"
        ln -s "${RUNTIME_DIR}/${f}" "${BASE_DIR}/${f}"
    done
}

install_service() {
    sync_to_runtime
    : > "${RUNTIME_DIR}/launchd_stderr.log"
    chmod 600 "${RUNTIME_DIR}/launchd_stderr.log"
    mkdir -p "$HOME/Library/LaunchAgents"

    /usr/bin/python3 -c '
import os, plistlib, sys
from pathlib import Path

plist_path, label, runtime_dir = sys.argv[1], sys.argv[2], sys.argv[3]
plist_data = {
    "Label": label,
    "ProgramArguments": [
        "/usr/bin/python3",
        f"{runtime_dir}/battery_monitor.py",
        "--check",
    ],
    "WorkingDirectory": runtime_dir,
    "StartInterval": 60,
    "RunAtLoad": True,
    "StandardOutPath": f"{runtime_dir}/launchd_stdout.log",
    "StandardErrorPath": f"{runtime_dir}/launchd_stderr.log",
}
Path(plist_path).write_bytes(plistlib.dumps(plist_data, fmt=plistlib.FMT_XML, sort_keys=False))
os.chmod(plist_path, 0o644)
' "$PLIST_PATH" "$PLIST_LABEL" "$RUNTIME_DIR"

    launchctl unload "$PLIST_PATH" 2>/dev/null || true
    launchctl load "$PLIST_PATH"
    echo "Background LaunchAgent installed in ${RUNTIME_DIR} and started (${PLIST_LABEL})."
    echo "Config file : ${BASE_DIR}/config.json (0600)"
    echo "Log file    : ${BASE_DIR}/battery_guardian.log (0600)"
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
    mkdir -p -m 700 "$RUNTIME_DIR"
    /usr/bin/python3 -c '
import json, os, re, sys
from pathlib import Path

email_addr = sys.argv[-1].strip()
if not email_addr or email_addr.startswith("-") or re.search(r"[\r\n\x00\s]", email_addr) or not re.fullmatch(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", email_addr):
    print("Error: invalid email address.", file=sys.stderr)
    sys.exit(1)

for p_str in sys.argv[1:-1]:
    p = Path(p_str)
    if p.exists():
        target = p.resolve()
        data = json.loads(target.read_text(encoding="utf-8"))
        data.setdefault("email", {})["to_email"] = email_addr
        target.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.chmod(str(target), 0o600)
print(f"Updated alert recipient email to: {email_addr}")
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
        if launchctl print "gui/$(id -u)/${PLIST_LABEL}" >/dev/null 2>&1; then
            echo "  Running (${PLIST_LABEL}, checks every 60s, runtime: ~/Library/Application Support/BatteryGuardian)"
        else
            echo "  Not running (run ./manage.sh install to enable background monitoring)"
        fi
        ;;
    health)
        sync_to_runtime
        /usr/bin/python3 "${RUNTIME_DIR}/battery_monitor.py" --health
        if [[ "${2:-}" == "--screen" || "${2:-}" == "--hud" ]]; then
            /usr/bin/python3 "${RUNTIME_DIR}/battery_monitor.py" --hud
        fi
        ;;
    hud|screen)
        sync_to_runtime
        /usr/bin/python3 "${RUNTIME_DIR}/battery_monitor.py" --hud
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
        echo "Usage: ./manage.sh {install|uninstall|status|health [--screen]|hud|pause [minutes]|unmute|set-email <email>|test-high|test-low|test-critical|test-email}"
        exit 1
        ;;
esac
