#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
macOS Battery Guardian: Silent Full-Screen Battery Alert & Critical Low-Battery Email Daemon.

Features:
1. High battery (> 70% on AC power by default): shows a silent full-screen alert reminding you to unplug.
   Auto-dismisses immediately when AC power is unplugged.
2. Low battery (< 30% on battery power by default): shows a silent full-screen alert reminding you to plug in.
   Auto-dismisses immediately when AC power is connected.
3. Critical low battery (< 15% unplugged for >= 10 minutes by default): automatically sends an emergency email
   alert (via Apple Mail, SMTP, or SSH sendmail) and displays a critical full-screen alert.
"""

import argparse
import datetime
import json
import re
import smtplib
import socket
import subprocess
import time
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formatdate, make_msgid
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
EXAMPLE_CONFIG_PATH = BASE_DIR / "config.example.json"
STATE_PATH = BASE_DIR / "state.json"
LOG_PATH = BASE_DIR / "battery_guardian.log"
ALERT_APP = BASE_DIR / "BatteryFullscreenAlert.app"
ALERT_BIN = BASE_DIR / "BatteryFullscreenAlert"


def log(msg: str) -> None:
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{now_str}] {msg}"
    print(line)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def load_config() -> dict:
    default_cfg = {
        "device_name": "MacBook",
        "sound_enabled": False,
        "high_threshold_percent": 70,
        "high_only_on_ac": True,
        "high_snooze_minutes": 15,
        "low_threshold_percent": 30,
        "low_only_on_battery": True,
        "low_snooze_minutes": 5,
        "critical_threshold_percent": 15,
        "critical_unchanged_minutes": 10,
        "critical_email_repeat_minutes": 30,
        "quiet_mode": {
            "schedules": [],
        },
        "email": {
            "enabled": False,
            "method": "mail_app",
            "to_email": "you@example.com",
            "from_email": "battery-guardian@example.com",
            "ssh_socket": "~/.ssh/master.sock",
            "ssh_host": "user@remote-host.example.com",
            "smtp_host": "smtp.gmail.com",
            "smtp_port": 465,
            "smtp_user": "you@gmail.com",
            "smtp_password": "your_app_password",
        },
    }
    cfg_file = CONFIG_PATH if CONFIG_PATH.exists() else EXAMPLE_CONFIG_PATH
    if not cfg_file.exists():
        return default_cfg
    try:
        with open(cfg_file, "r", encoding="utf-8") as f:
            user_cfg = json.load(f)
        for key, val in user_cfg.items():
            if isinstance(val, dict) and isinstance(default_cfg.get(key), dict):
                default_cfg[key].update(val)
            else:
                default_cfg[key] = val
        return default_cfg
    except Exception as e:
        log(f"Failed to read {cfg_file.name}; using defaults: {e}")
        return default_cfg


def load_state() -> dict:
    default_state = {
        "last_high_alert_ts": 0.0,
        "last_low_alert_ts": 0.0,
        "below_15_since_ts": None,
        "below_15_start_percent": None,
        "last_critical_email_ts": 0.0,
        "last_percent": None,
        "last_on_ac": None,
        "pause_until_ts": 0.0,
        "active_alert_mode": None,
    }
    if not STATE_PATH.exists():
        return default_state
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        default_state.update(data)
        return default_state
    except Exception:
        return default_state


def save_state(state: dict) -> None:
    try:
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
    except Exception as e:
        log(f"Failed to save state.json: {e}")


def get_battery_info() -> dict:
    """Query pmset -g batt for current battery percentage, AC state, and time remaining."""
    out = subprocess.check_output(["/usr/bin/pmset", "-g", "batt"], text=True)
    on_ac = "AC Power" in out
    m_pct = re.search(r"(\d+)%", out)
    if not m_pct:
        raise RuntimeError(f"Unable to parse battery percentage from pmset output: {out}")
    percent = int(m_pct.group(1))

    parts = [p.strip() for p in out.splitlines()[-1].split(";")]
    status = parts[1] if len(parts) > 1 else "unknown"
    remaining = parts[2] if len(parts) > 2 else "unknown"

    return {
        "percent": percent,
        "on_ac": on_ac,
        "status": status,
        "remaining": remaining,
        "raw": out.strip(),
    }


def is_time_in_window(now_dt: datetime.datetime, start_str: str, end_str: str) -> bool:
    sh, sm = [int(x) for x in start_str.split(":")]
    eh, em = [int(x) for x in end_str.split(":")]
    cur_mins = now_dt.hour * 60 + now_dt.minute
    start_mins = sh * 60 + sm
    end_mins = eh * 60 + em
    if start_mins <= end_mins:
        return start_mins <= cur_mins <= end_mins
    return cur_mins >= start_mins or cur_mins <= end_mins


def evaluate_quiet_state(cfg: dict, state: dict) -> dict:
    """Return whether fullscreen popups are currently paused (Meeting / Quiet Mode)."""
    now = time.time()
    now_dt = datetime.datetime.now()

    pause_until = float(state.get("pause_until_ts") or 0.0)
    if pause_until > now:
        rem = int((pause_until - now) / 60) + 1
        return {"paused": True, "reason": f"Manual pause active (~{rem} min remaining)"}

    qcfg = cfg.get("quiet_mode", {})
    iso_weekday = now_dt.isoweekday()
    for sched in qcfg.get("schedules", []):
        if not sched.get("enabled", False):
            continue
        days = sched.get("days_of_week", [1, 2, 3, 4, 5, 6, 7])
        if iso_weekday in days and is_time_in_window(now_dt, sched.get("start", "00:00"), sched.get("end", "00:00")):
            name = sched.get("name", "Scheduled quiet window")
            return {"paused": True, "reason": f"Matched quiet schedule: {name}"}

    return {"paused": False, "reason": "Silent fullscreen alerts active"}


def dismiss_active_alert() -> None:
    """Close any running BatteryFullscreenAlert overlay window."""
    subprocess.run(["/usr/bin/killall", "BatteryFullscreenAlert"], capture_output=True)


def trigger_fullscreen_alert(
    mode: str,
    percent: int,
    message: str = "",
    device_name: str = "MacBook",
    dismiss_policy: str = "auto",
) -> None:
    """Launch the native Swift silent full-screen alert via macOS LaunchServices (open -a)."""
    log(
        f"Triggering silent fullscreen alert: device={device_name}, mode={mode}, "
        f"percent={percent}%, policy={dismiss_policy}, msg={message}"
    )

    dismiss_active_alert()

    if ALERT_APP.exists():
        cmd = [
            "/usr/bin/open",
            "-n",
            "-a",
            str(ALERT_APP),
            "--args",
            mode,
            str(percent),
            message,
            device_name,
            str(STATE_PATH),
            dismiss_policy,
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode == 0:
            return
        log(f"open -a BatteryFullscreenAlert.app failed (rc={res.returncode}): {res.stderr.strip()}")

    if ALERT_BIN.exists():
        subprocess.Popen(
            [str(ALERT_BIN), mode, str(percent), message, device_name, str(STATE_PATH), dismiss_policy]
        )
        return

    fallback_msg = message or f"[{device_name}] Battery is at {percent}%."
    apple_script = (
        f'display alert "[{device_name}] Battery Alert ({percent}%)" '
        f'message "{fallback_msg}" as critical buttons {{"Dismiss"}} default button 1'
    )
    subprocess.Popen(["/usr/bin/osascript", "-e", apple_script])


def send_emergency_email(cfg: dict, batt: dict, duration_min: float, is_test: bool = False) -> bool:
    """Send a critical low-battery (<15%) email notification via Mail.app, SMTP, or SSH sendmail."""
    email_cfg = cfg.get("email", {})
    to_email = email_cfg.get("to_email", "").strip()
    device_name = cfg.get("device_name", "MacBook")
    if not to_email or to_email in ("you@example.com", "your_email@example.com"):
        log("No valid recipient address configured in config.json (email.to_email); skipping email.")
        return False

    hostname = socket.gethostname()
    try:
        comp_name = subprocess.check_output(["/usr/sbin/scutil", "--get", "ComputerName"], text=True).strip()
    except Exception:
        comp_name = hostname
    try:
        cpu_brand = subprocess.check_output(["/usr/sbin/sysctl", "-n", "machdep.cpu.brand_string"], text=True).strip()
    except Exception:
        cpu_brand = "Apple Silicon / macOS"

    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    crit_th = int(cfg.get("critical_threshold_percent", 15))

    if is_test:
        subject = f"[{device_name} Battery Guardian Test] {device_name} ({comp_name}) at {batt['percent']}% - Email OK"
    else:
        subject = (
            f"[{device_name} CRITICAL BATTERY] {device_name} at {batt['percent']}% "
            f"unplugged for {duration_min:.0f} min"
        )

    power_desc = "AC Power (Charging)" if batt["on_ac"] else "Battery Power (Unplugged)"
    footer_note = (
        f"[Note] This is a test message confirming emergency email alerts (<{crit_th}%) for {device_name} are working."
        if is_test
        else f"WARNING: Please plug in AC power for {device_name} immediately to prevent unexpected shutdown and data loss."
    )

    body = (
        f"Battery Guardian notification for [{device_name}]:\n\n"
        f"- Device: {device_name} ({comp_name} / {cpu_brand})\n"
        f"- Hostname: {hostname}\n"
        f"- Current Battery: {batt['percent']}%\n"
        f"- Power Source: {power_desc}\n"
        f"- Charging Status: {batt['status']} ({batt['remaining']})\n"
        f"- Unplugged Duration Below {crit_th}%: {duration_min:.1f} minutes\n"
        f"- Timestamp: {now_str}\n\n"
        f"{footer_note}\n"
    )

    method = email_cfg.get("method", "mail_app").lower()

    if method in ("ssh_sendmail", "quest_sendmail"):
        ssh_socket_raw = (email_cfg.get("ssh_socket") or email_cfg.get("quest_socket") or "").strip()
        ssh_host = (email_cfg.get("ssh_host") or email_cfg.get("quest_host") or "").strip()
        if not ssh_host or ssh_host == "user@remote-host.example.com":
            log("SSH sendmail selected, but email.ssh_host is not configured.")
            return False

        from_email = email_cfg.get("from_email", "").strip()
        if not from_email or from_email == "battery-guardian@example.com":
            from_email = ssh_host if "@" in ssh_host else "battery-guardian@localhost"

        msg_domain = from_email.split("@", 1)[1] if "@" in from_email else socket.getfqdn()
        log(f"Sending [{device_name}] alert email via SSH sendmail ({ssh_host}) to {to_email}...")

        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = Header(subject, "utf-8")
        msg["From"] = f"{device_name} Battery Guardian <{from_email}>"
        msg["To"] = to_email
        msg["Date"] = formatdate(localtime=True)
        msg["Message-ID"] = make_msgid(domain=msg_domain)

        ssh_cmd = [
            "/usr/bin/ssh",
            "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=8",
        ]
        if ssh_socket_raw:
            sock_path = str(Path(ssh_socket_raw).expanduser())
            try:
                if Path(sock_path).exists():
                    ssh_cmd.extend(["-S", sock_path])
            except PermissionError:
                ssh_cmd.extend(["-S", sock_path])
        ssh_cmd.extend([ssh_host, "/usr/sbin/sendmail -t -oi"])

        res = subprocess.run(ssh_cmd, input=msg.as_string(), text=True, capture_output=True)
        if res.returncode == 0:
            log(f"Email sent via SSH sendmail to {to_email}")
            return True
        log(f"SSH sendmail failed (rc={res.returncode}): {res.stderr.strip()}")
        return False

    if method == "mail_app":
        log(f"Sending [{device_name}] alert email via Apple Mail.app to {to_email}...")
        escaped_subject = subject.replace("\\", "\\\\").replace('"', '\\"')
        escaped_body = body.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
        escaped_to = to_email.replace("\\", "\\\\").replace('"', '\\"')
        script = f'''
        tell application "Mail"
            set newMessage to make new outgoing message with properties {{subject:"{escaped_subject}", content:"{escaped_body}", visible:false}}
            tell newMessage
                make new to recipient at end of to recipients with properties {{address:"{escaped_to}"}}
                send
            end tell
        end tell
        '''
        res = subprocess.run(["/usr/bin/osascript", "-e", script], capture_output=True, text=True)
        if res.returncode == 0:
            log(f"Email sent via Mail.app to {to_email}")
            return True
        log(f"Mail.app send failed: {res.stderr.strip()}")
        return False

    if method == "smtp":
        smtp_host = email_cfg.get("smtp_host", "")
        smtp_port = int(email_cfg.get("smtp_port", 465))
        smtp_user = email_cfg.get("smtp_user", "")
        smtp_pass = email_cfg.get("smtp_password", "")
        from_email = email_cfg.get("from_email", "").strip() or smtp_user
        log(f"Sending [{device_name}] alert email via SMTP ({smtp_host}:{smtp_port}) to {to_email}...")
        try:
            msg = MIMEText(body, "plain", "utf-8")
            msg["Subject"] = Header(subject, "utf-8")
            msg["From"] = f"{device_name} Battery Guardian <{from_email}>"
            msg["To"] = to_email
            msg["Date"] = formatdate(localtime=True)

            if smtp_port == 465:
                with smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=15) as server:
                    server.login(smtp_user, smtp_pass)
                    server.sendmail(smtp_user, [to_email], msg.as_string())
            else:
                with smtplib.SMTP(smtp_host, smtp_port, timeout=15) as server:
                    server.starttls()
                    server.login(smtp_user, smtp_pass)
                    server.sendmail(smtp_user, [to_email], msg.as_string())
            log(f"Email sent via SMTP to {to_email}")
            return True
        except Exception as e:
            log(f"SMTP send failed: {e}")
            return False

    log(f"Unknown email method: {method}")
    return False


def run_check() -> None:
    cfg = load_config()
    state = load_state()
    batt = get_battery_info()
    quiet = evaluate_quiet_state(cfg, state)

    device_name = cfg.get("device_name", "MacBook")
    pct = batt["percent"]
    on_ac = batt["on_ac"]
    now = time.time()

    high_th = int(cfg.get("high_threshold_percent", 70))
    low_th = int(cfg.get("low_threshold_percent", 30))
    crit_th = int(cfg.get("critical_threshold_percent", 15))

    high_only_ac = bool(cfg.get("high_only_on_ac", True))
    low_only_batt = bool(cfg.get("low_only_on_battery", True))

    high_snooze_sec = float(cfg.get("high_snooze_minutes", 15)) * 60.0
    low_snooze_sec = float(cfg.get("low_snooze_minutes", 5)) * 60.0
    crit_unchanged_sec = float(cfg.get("critical_unchanged_minutes", 10)) * 60.0
    crit_email_repeat_sec = float(cfg.get("critical_email_repeat_minutes", 30)) * 60.0

    # --- 1. Check High Battery (> high_threshold_percent) ---
    high_cond = (pct > high_th) and (on_ac if high_only_ac else True)
    if high_cond:
        if not quiet["paused"] and (now - state.get("last_high_alert_ts", 0.0) >= high_snooze_sec):
            policy = "auto" if high_only_ac else "change"
            trigger_fullscreen_alert(
                "high",
                pct,
                f"[{device_name}] Battery is at {pct}% (> {high_th}%). Unplug your charger to auto-dismiss and protect battery health.",
                device_name,
                dismiss_policy=policy,
            )
            state["last_high_alert_ts"] = now
            state["active_alert_mode"] = "high"
    else:
        state["last_high_alert_ts"] = 0.0
        if state.get("active_alert_mode") == "high":
            log(f"[{device_name}] High-battery condition cleared (pct={pct}%, on_ac={on_ac}); closing active alert.")
            dismiss_active_alert()
            state["active_alert_mode"] = None

    # --- 2. Check Critical Low Battery (< critical_threshold_percent unplugged) ---
    crit_cond = (pct < crit_th) and (not on_ac)
    if crit_cond:
        if state.get("below_15_since_ts") is None:
            state["below_15_since_ts"] = now
            state["below_15_start_percent"] = pct
            log(f"[{device_name}] Battery entered < {crit_th}% unplugged zone ({pct}%); starting critical timer...")

        elapsed_sec = now - float(state["below_15_since_ts"])
        elapsed_min = elapsed_sec / 60.0

        if elapsed_sec >= crit_unchanged_sec:
            if now - state.get("last_critical_email_ts", 0.0) >= crit_email_repeat_sec:
                if cfg.get("email", {}).get("enabled", False):
                    send_emergency_email(cfg, batt, elapsed_min, is_test=False)
                to_addr = cfg.get("email", {}).get("to_email", "")
                if not quiet["paused"]:
                    policy = "auto" if low_only_batt else "change"
                    trigger_fullscreen_alert(
                        "critical",
                        pct,
                        f"[{device_name}] Battery is critically low at {pct}% (< {crit_th}%) for {elapsed_min:.0f} min unplugged! Emergency email sent to {to_addr}. Plug in AC power immediately.",
                        device_name,
                        dismiss_policy=policy,
                    )
                    state["active_alert_mode"] = "critical"
                state["last_critical_email_ts"] = now
                state["last_low_alert_ts"] = now
    else:
        if state.get("below_15_since_ts") is not None:
            log(f"[{device_name}] Exited < {crit_th}% unplugged state (pct={pct}%, on_ac={on_ac}); resetting critical timer.")
        state["below_15_since_ts"] = None
        state["below_15_start_percent"] = None
        state["last_critical_email_ts"] = 0.0

    # --- 3. Check Low Battery (< low_threshold_percent) ---
    low_cond = (pct < low_th) and ((not on_ac) if low_only_batt else True)
    if low_cond:
        if not quiet["paused"] and (now - state.get("last_low_alert_ts", 0.0) >= low_snooze_sec):
            mode = "critical" if pct < crit_th else "low"
            policy = "auto" if low_only_batt else "change"
            trigger_fullscreen_alert(
                mode,
                pct,
                f"[{device_name}] Battery is at {pct}% (< {low_th}%). Plug in your charger to auto-dismiss.",
                device_name,
                dismiss_policy=policy,
            )
            state["last_low_alert_ts"] = now
            state["active_alert_mode"] = mode
    else:
        state["last_low_alert_ts"] = 0.0
        if not crit_cond and state.get("active_alert_mode") in ("low", "critical"):
            log(f"[{device_name}] Low-battery condition cleared (pct={pct}%, on_ac={on_ac}); closing active alert.")
            dismiss_active_alert()
            state["active_alert_mode"] = None

    state["last_percent"] = pct
    state["last_on_ac"] = on_ac
    save_state(state)


def show_status() -> None:
    cfg = load_config()
    state = load_state()
    batt = get_battery_info()
    quiet = evaluate_quiet_state(cfg, state)
    device_name = cfg.get("device_name", "MacBook")
    power_label = "AC Power (Charging/Connected)" if batt["on_ac"] else "Battery Power (Unplugged)"

    print(f"=== Device: {device_name} ===")
    print(f"  Battery Level   : {batt['percent']}%")
    print(f"  Power Source    : {power_label}")
    print(f"  Charge Status   : {batt['status']} ({batt['remaining']})")
    print("\n=== Fullscreen Alert Mode (Silent) ===")
    print(f"  Status          : {quiet['reason']}")
    print("\n=== Active Rules (config.json) ===")
    print(
        f"  High Battery    : > {cfg['high_threshold_percent']}% "
        f"(only_on_ac={cfg['high_only_on_ac']}, cooldown={cfg['high_snooze_minutes']} min, silent fullscreen)"
    )
    print(
        f"  Low Battery     : < {cfg['low_threshold_percent']}% "
        f"(only_on_battery={cfg['low_only_on_battery']}, cooldown={cfg['low_snooze_minutes']} min, silent fullscreen)"
    )
    print(
        f"  Critical Email  : < {cfg['critical_threshold_percent']}% unplugged for "
        f"{cfg['critical_unchanged_minutes']} min -> send email"
    )
    email_cfg = cfg.get("email", {})
    print(
        f"  Email Target    : {email_cfg.get('to_email')} "
        f"(enabled={email_cfg.get('enabled')}, method={email_cfg.get('method')})"
    )
    if state.get("below_15_since_ts"):
        mins = (time.time() - state["below_15_since_ts"]) / 60.0
        print(f"  Critical Timer  : Below {cfg['critical_threshold_percent']}% for {mins:.1f} min")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="macOS Battery Guardian: Silent Full-Screen Alert & Critical Low-Battery Email Daemon"
    )
    parser.add_argument("--check", action="store_true", help="Run a single battery check")
    parser.add_argument("--status", action="store_true", help="Show current battery and monitor status")
    parser.add_argument("--pause", type=int, nargs="?", const=120, metavar="MINUTES", help="Pause fullscreen alerts for N minutes (default 120)")
    parser.add_argument("--unmute", action="store_true", help="Resume normal silent fullscreen alerts")
    parser.add_argument("--test-high", action="store_true", help="Preview >70%% silent fullscreen alert")
    parser.add_argument("--test-low", action="store_true", help="Preview <30%% silent fullscreen alert")
    parser.add_argument("--test-critical", action="store_true", help="Preview <15%% critical silent fullscreen alert")
    parser.add_argument("--test-email", action="store_true", help="Send a test emergency email")
    args = parser.parse_args()

    cfg = load_config()
    state = load_state()
    device_name = cfg.get("device_name", "MacBook")
    high_th = int(cfg.get("high_threshold_percent", 70))
    low_th = int(cfg.get("low_threshold_percent", 30))
    crit_th = int(cfg.get("critical_threshold_percent", 15))

    if args.pause is not None:
        mins = max(1, args.pause)
        state["pause_until_ts"] = time.time() + mins * 60.0
        save_state(state)
        until_str = datetime.datetime.fromtimestamp(state["pause_until_ts"]).strftime("%H:%M")
        log(f"Meeting Mode enabled: fullscreen alerts paused for {mins} min (until {until_str}); critical <{crit_th}% email remains active.")
        return

    if args.unmute:
        state["pause_until_ts"] = 0.0
        save_state(state)
        log("Meeting Mode disabled: silent fullscreen alerts resumed.")
        return

    if args.status:
        show_status()
    elif args.test_high:
        trigger_fullscreen_alert(
            "high",
            max(high_th + 5, 75),
            f"[Test Preview - {device_name}] Battery is above {high_th}%. Unplug your charger (or press Esc) to auto-close.",
            device_name,
            dismiss_policy="change",
        )
    elif args.test_low:
        trigger_fullscreen_alert(
            "low",
            max(low_th - 5, 25),
            f"[Test Preview - {device_name}] Battery is below {low_th}%. Plug in your charger (or press Esc) to auto-close.",
            device_name,
            dismiss_policy="change",
        )
    elif args.test_critical:
        to_addr = cfg.get("email", {}).get("to_email", "you@example.com")
        trigger_fullscreen_alert(
            "critical",
            max(crit_th - 3, 12),
            f"[Test Preview - {device_name}] Battery is critically low (< {crit_th}%)! Alert email target: {to_addr}. Plug/unplug AC (or press Esc) to auto-close.",
            device_name,
            dismiss_policy="change",
        )
    elif args.test_email:
        batt = get_battery_info()
        send_emergency_email(cfg, batt, float(cfg.get("critical_unchanged_minutes", 10)), is_test=True)
    else:
        run_check()


if __name__ == "__main__":
    main()
