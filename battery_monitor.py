#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
macOS Battery Guardian: Silent Full-Screen Battery Alert, Hardware Telemetry HUD & Critical Email Daemon.

Features:
1. High battery (> 70% on AC power by default): shows a silent full-screen alert reminding you to unplug.
   Auto-dismisses immediately when AC power is unplugged.
2. Low battery (< 30% on battery power by default): shows a silent full-screen alert reminding you to plug in.
   Auto-dismisses immediately when AC power is connected.
3. Critical low battery (< 15% unplugged for >= 10 minutes by default): automatically sends an emergency email
   alert (via Apple Mail, SMTP, or SSH sendmail) and displays a critical full-screen alert.
4. Zero-Risk Read-Only Hardware Telemetry & Anti-Spoofing HUD (--health / --hud): reads AppleSmartBattery
   IOKit properties locally (zero network sockets, zero root, masked serial never written to disk).
"""

import argparse
import datetime
import json
import os
import plistlib
import re
import smtplib
import socket
import subprocess
import tempfile
import time
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formatdate, make_msgid
from pathlib import Path
from typing import Any, Dict, List, Optional

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
EXAMPLE_CONFIG_PATH = BASE_DIR / "config.example.json"
STATE_PATH = BASE_DIR / "state.json"
LOG_PATH = BASE_DIR / "battery_guardian.log"
ALERT_APP = BASE_DIR / "BatteryFullscreenAlert.app"
ALERT_BIN = BASE_DIR / "BatteryFullscreenAlert"


def _chmod_private_file(path: Path) -> None:
    """Enforce 0600 permissions on private config/state/log targets (resolving symlinks)."""
    try:
        target = path.resolve()
        if target.exists():
            os.chmod(str(target), 0o600)
    except OSError:
        pass


def escape_applescript_str(value: str) -> str:
    """Escape untrusted strings before interpolation into AppleScript string literals."""
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\r", "\\r")
        .replace("\n", "\\n")
    )


def sanitize_header_token(value: str) -> str:
    """Strip CR/LF/NUL characters to prevent SMTP/MIME header injection."""
    return re.sub(r"[\r\n\x00]+", " ", str(value)).strip()


def is_safe_email_addr(addr: str, allow_localhost: bool = False) -> bool:
    """Validate email address syntax without whitespace, control chars, or leading '-'."""
    if not addr or addr.startswith("-") or re.search(r"[\r\n\x00\s]", addr):
        return False
    pattern = (
        r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+$"
        if allow_localhost
        else r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$"
    )
    return re.fullmatch(pattern, addr) is not None


def is_safe_ssh_target(host: str) -> bool:
    """Validate SSH destination host/user@host and reject option-like leading hyphens."""
    if not host or host.startswith("-"):
        return False
    return re.fullmatch(r"[A-Za-z0-9._-]+(@[A-Za-z0-9._-]+)?", host) is not None


def log(msg: str) -> None:
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{now_str}] {msg}"
    print(line)
    try:
        target = LOG_PATH.resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.chmod(fd, 0o600)
            os.write(fd, (line + "\n").encode("utf-8"))
        finally:
            os.close(fd)
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
    if CONFIG_PATH.exists():
        _chmod_private_file(CONFIG_PATH)
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
        "history_samples": [],
    }
    if not STATE_PATH.exists():
        return default_state
    _chmod_private_file(STATE_PATH)
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        default_state.update(data)
        if not isinstance(default_state.get("history_samples"), list):
            default_state["history_samples"] = []
        return default_state
    except Exception:
        return default_state


def save_state(state: dict) -> None:
    tmp_path: Optional[str] = None
    try:
        target = STATE_PATH.resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(prefix=".state.", suffix=".tmp", dir=str(target.parent))
        os.chmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(state, indent=2, ensure_ascii=False) + "\n")
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, str(target))
        os.chmod(str(target), 0o600)
    except Exception as e:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
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


def _to_signed_i64(val: Any) -> Optional[int]:
    """Decode unsigned 64-bit two's-complement IORegistry integers to signed int."""
    if val is None or isinstance(val, bool):
        return None
    try:
        ival = int(val)
    except (TypeError, ValueError):
        return None
    if ival >= (1 << 63):
        return ival - (1 << 64)
    return ival


def _mask_serial(raw: Any) -> str:
    """Mask hardware battery serial immediately in memory; never persist raw serial to disk."""
    if not isinstance(raw, str):
        return "N/A"
    cleaned = re.sub(r"[\s\r\n\x00]+", "", raw)
    if len(cleaned) >= 6:
        return f"{cleaned[:2]}****{cleaned[-2:]}"
    if len(cleaned) > 0:
        return "****"
    return "N/A"


def _walk_ioreg_tree(node: Dict[str, Any], acc: Dict[str, Any]) -> None:
    cls = str(node.get("IOObjectClass") or node.get("IORegistryEntryName") or "")
    name = str(node.get("IORegistryEntryName") or "")
    if (cls == "AppleSmartBatteryPack" or name == "AppleSmartBatteryPack") and acc["pack"] is None:
        acc["pack"] = node
    elif cls == "AppleSmartBatteryBank" or name == "AppleSmartBatteryBank":
        acc["banks"].append(node)
    for child in node.get("IORegistryEntryChildren") or []:
        if isinstance(child, dict):
            _walk_ioreg_tree(child, acc)


def _pick_val(sources: List[Dict[str, Any]], keys: List[str]) -> Any:
    for src in sources:
        if not isinstance(src, dict):
            continue
        for key in keys:
            if key in src and src[key] is not None:
                return src[key]
    return None


def read_smart_battery_telemetry() -> Optional[Dict[str, Any]]:
    """
    Zero-risk read-only hardware battery telemetry & anti-spoofing audit via ioreg XML plist.
    Never writes hardware registers, opens network sockets, or persists raw serial strings.
    """
    try:
        raw_xml = subprocess.check_output(
            ["/usr/sbin/ioreg", "-a", "-l", "-r", "-c", "AppleSmartBattery"],
            timeout=5,
        )
        entries = plistlib.loads(raw_xml)
    except Exception:
        return None

    if not isinstance(entries, list) or not entries:
        return None

    root = entries[0] if isinstance(entries[0], dict) else None
    if root is None:
        return None

    acc: Dict[str, Any] = {"pack": None, "banks": []}
    _walk_ioreg_tree(root, acc)
    pack: Dict[str, Any] = acc["pack"] if isinstance(acc["pack"], dict) else {}
    banks: List[Dict[str, Any]] = acc["banks"]

    def _bank_sort_key(node: Dict[str, Any]) -> tuple:
        loc = str(node.get("BankID") if node.get("BankID") is not None else (node.get("IORegistryEntryLocation") or ""))
        return (0, int(loc)) if loc.isdigit() else (1, loc)

    banks.sort(key=_bank_sort_key)

    root_bdata = root.get("BatteryData") if isinstance(root.get("BatteryData"), dict) else {}
    pack_bdata = pack.get("BatteryData") if isinstance(pack.get("BatteryData"), dict) else {}
    sources_cap = [root, root_bdata, pack_bdata]

    os_percent = _to_signed_i64(root.get("CurrentCapacity"))
    raw_current_mah = _to_signed_i64(_pick_val(sources_cap, ["AppleRawCurrentCapacity"]))
    raw_max_mah = _to_signed_i64(_pick_val(sources_cap, ["AppleRawMaxCapacity", "FullChargeCapacity"]))
    nominal_mah = _to_signed_i64(_pick_val(sources_cap, ["NominalChargeCapacity"]))
    design_mah = _to_signed_i64(_pick_val(sources_cap, ["DesignCapacity"]))

    health_ref_mah = raw_max_mah if (raw_max_mah and raw_max_mah > 0) else nominal_mah
    true_health_pct: Optional[float] = None
    if health_ref_mah and design_mah and design_mah > 0:
        true_health_pct = round((float(health_ref_mah) / float(design_mah)) * 100.0, 2)

    nominal_health_pct: Optional[float] = None
    if nominal_mah and design_mah and design_mah > 0:
        nominal_health_pct = round((float(nominal_mah) / float(design_mah)) * 100.0, 2)

    raw_soc_pct: Optional[float] = None
    if raw_current_mah is not None and raw_max_mah and raw_max_mah > 0:
        raw_soc_pct = round((float(raw_current_mah) / float(raw_max_mah)) * 100.0, 2)
    elif os_percent is not None:
        raw_soc_pct = float(os_percent)

    cycle_count = _to_signed_i64(_pick_val([root, root_bdata, pack_bdata], ["CycleCount"]))
    design_cycles = _to_signed_i64(_pick_val([root, pack_bdata], ["DesignCycleCount9C"])) or 1000
    gauge_model = str(root.get("DeviceName") or pack_bdata.get("DeviceName") or "AppleSmartBattery")
    masked_serial = _mask_serial(pack_bdata.get("Serial") or root_bdata.get("Serial") or root.get("Serial"))

    voltage_mv = _to_signed_i64(_pick_val([root, pack_bdata], ["AppleRawBatteryVoltage", "Voltage"]))
    amperage_ma = _to_signed_i64(root.get("InstantAmperage"))
    if amperage_ma is None:
        amperage_ma = _to_signed_i64(root.get("Amperage"))

    is_charging = bool(root.get("IsCharging", False))
    external_connected = bool(root.get("ExternalConnected", False))
    tr_raw = _to_signed_i64(root.get("TimeRemaining"))
    time_remaining_min: Optional[int] = tr_raw if (tr_raw is not None and 0 <= tr_raw < 65535) else None

    ptd = root.get("PowerTelemetryData") if isinstance(root.get("PowerTelemetryData"), dict) else {}
    batt_power_mw = _to_signed_i64(ptd.get("BatteryPower"))
    battery_power_w: Optional[float] = None
    if batt_power_mw is not None:
        battery_power_w = float(batt_power_mw) / 1000.0
        if amperage_ma is not None:
            if amperage_ma < 0 and battery_power_w > 0:
                battery_power_w = -battery_power_w
            elif amperage_ma > 0 and battery_power_w < 0:
                battery_power_w = -battery_power_w
    elif voltage_mv is not None and amperage_ma is not None:
        battery_power_w = (float(voltage_mv) * float(amperage_ma)) / 1_000_000.0

    sys_load_mw = _to_signed_i64(ptd.get("SystemLoad"))
    sys_in_mw = _to_signed_i64(ptd.get("SystemPowerIn"))
    system_load_w = round(float(sys_load_mw) / 1000.0, 2) if sys_load_mw is not None else None
    wall_in_w = round(float(sys_in_mw) / 1000.0, 2) if sys_in_mw is not None else None

    adapter = root.get("AdapterDetails") if isinstance(root.get("AdapterDetails"), dict) else {}
    adapter_watts = _to_signed_i64(adapter.get("Watts"))
    adapter_mv = _to_signed_i64(adapter.get("AdapterVoltage"))
    adapter_ma = _to_signed_i64(adapter.get("Current"))
    adapter_desc = str(adapter.get("Description") or "").strip()

    inst_rate_pct_h: Optional[float] = None
    denom_mah = raw_max_mah if (raw_max_mah and raw_max_mah > 0) else nominal_mah
    if amperage_ma is not None and denom_mah and denom_mah > 0:
        inst_rate_pct_h = round((float(amperage_ma) / float(denom_mah)) * 100.0, 2)

    lifetime = pack_bdata.get("LifetimeData") if isinstance(pack_bdata.get("LifetimeData"), dict) else {}
    if not lifetime and isinstance(root_bdata.get("LifetimeData"), dict):
        lifetime = root_bdata["LifetimeData"]

    temp_c: Optional[float] = None
    if pack_bdata.get("Temperature") is not None:
        temp_c = round(float(pack_bdata["Temperature"]) / 100.0, 2)
    elif root.get("Temperature") is not None:
        raw_t = float(root["Temperature"])
        c_centi = raw_t / 100.0
        c_kelvin = raw_t / 10.0 - 273.15
        if lifetime.get("AverageTemperature") is not None:
            avg_ref = float(lifetime["AverageTemperature"]) / 10.0
            chosen = c_kelvin if abs(c_kelvin - avg_ref) < abs(c_centi - avg_ref) else c_centi
            temp_c = round(chosen, 2)
        else:
            temp_c = round(c_centi, 2)

    virtual_temp_c: Optional[float] = None
    if pack_bdata.get("VirtualTemperature") is not None:
        virtual_temp_c = round(float(pack_bdata["VirtualTemperature"]) / 100.0, 2)

    flash_writes = _to_signed_i64(_pick_val([pack_bdata, root_bdata], ["DataFlashWriteCount"]))
    chem_id = _to_signed_i64(_pick_val([pack_bdata, root_bdata], ["ChemID"]))
    daily_min_soc = _to_signed_i64(_pick_val([pack_bdata, root_bdata], ["DailyMinSoc"]))
    daily_max_soc = _to_signed_i64(_pick_val([pack_bdata, root_bdata], ["DailyMaxSoc"]))
    pf_status = _to_signed_i64(_pick_val([pack_bdata, root_bdata], ["PermanentFailureStatus"]))
    disconnect_count = _to_signed_i64(_pick_val([pack_bdata, root_bdata], ["BatteryCellDisconnectCount"]))

    cycle_last_qmax = _to_signed_i64(lifetime.get("CycleCountLastQmax"))
    temp_samples = _to_signed_i64(lifetime.get("TemperatureSamples"))
    operating_hours = _to_signed_i64(lifetime.get("TotalOperatingTime"))
    min_pack_mv = _to_signed_i64(lifetime.get("MinimumPackVoltage"))
    max_pack_mv = _to_signed_i64(lifetime.get("MaximumPackVoltage"))
    max_charge_ma = _to_signed_i64(lifetime.get("MaximumChargeCurrent"))
    max_discharge_ma = _to_signed_i64(lifetime.get("MaximumDischargeCurrent"))
    min_temp_c = _to_signed_i64(lifetime.get("MinimumTemperature"))
    max_temp_c = _to_signed_i64(lifetime.get("MaximumTemperature"))
    avg_temp_raw = _to_signed_i64(lifetime.get("AverageTemperature"))
    avg_temp_c = round(float(avg_temp_raw) / 10.0, 1) if avg_temp_raw is not None else None

    cell_voltages_mv: List[int] = []
    qmax_mah: List[int] = []
    dod0: List[int] = []
    weighted_ra_mohm: List[int] = []

    if banks:
        for bank in banks:
            bdata = bank.get("BatteryData") if isinstance(bank.get("BatteryData"), dict) else {}
            cv = _to_signed_i64(bdata.get("CellVoltage"))
            qm = _to_signed_i64(bdata.get("Qmax"))
            d0 = _to_signed_i64(bdata.get("DOD0"))
            ra = _to_signed_i64(bdata.get("WeightedRa"))
            if cv is not None and cv > 0:
                cell_voltages_mv.append(cv)
            if qm is not None and qm > 0:
                qmax_mah.append(qm)
            if d0 is not None:
                dod0.append(d0)
            if ra is not None:
                weighted_ra_mohm.append(ra)
    else:
        for key, target_list in (
            ("CellVoltage", cell_voltages_mv),
            ("Qmax", qmax_mah),
            ("DOD0", dod0),
            ("WeightedRa", weighted_ra_mohm),
        ):
            raw_list = pack_bdata.get(key) or root_bdata.get(key)
            if isinstance(raw_list, list):
                for item in raw_list:
                    iv = _to_signed_i64(item)
                    if iv is not None and (key not in ("CellVoltage", "Qmax") or iv > 0):
                        target_list.append(iv)

    cell_delta_mv: Optional[int] = (max(cell_voltages_mv) - min(cell_voltages_mv)) if len(cell_voltages_mv) >= 2 else None
    qmax_delta_mah: Optional[int] = (max(qmax_mah) - min(qmax_mah)) if len(qmax_mah) >= 2 else None

    checks: List[Dict[str, str]] = []

    if cycle_last_qmax is not None and cycle_count is not None:
        if cycle_last_qmax <= cycle_count:
            checks.append({
                "name": "Calibration Timeline",
                "status": "PASS",
                "detail": f"CycleCountLastQmax ({cycle_last_qmax}) <= CycleCount ({cycle_count})",
            })
        else:
            checks.append({
                "name": "Calibration Timeline",
                "status": "FAIL",
                "detail": f"Anomaly: CycleCountLastQmax ({cycle_last_qmax}) > CycleCount ({cycle_count})",
            })

    implied_hours: Optional[float] = None
    clock_diff_pct: Optional[float] = None
    if temp_samples is not None and temp_samples >= 0 and operating_hours is not None:
        implied_hours = round((float(temp_samples) * 225.0) / 3600.0, 1)
        if operating_hours > 0:
            rel_diff = abs(implied_hours - float(operating_hours)) / max(1.0, float(operating_hours))
            clock_diff_pct = round(rel_diff * 100.0, 2)
            if rel_diff <= 0.15:
                c_status = "PASS"
            elif rel_diff <= 0.40:
                c_status = "WARN"
            else:
                c_status = "FAIL"
            checks.append({
                "name": "Dual-Clock Integrity",
                "status": c_status,
                "detail": f"TempSamples {implied_hours:.1f} h vs OperatingTime {operating_hours} h (diff {clock_diff_pct:.2f}%)",
            })
        elif temp_samples > 100:
            checks.append({
                "name": "Dual-Clock Integrity",
                "status": "FAIL",
                "detail": f"OperatingTime=0 h while TempSamples implies {implied_hours:.1f} h (possible reset)",
            })

    if cell_delta_mv is not None:
        bal_status = "PASS"
        bal_notes = [f"Cell spread {cell_delta_mv} mV ({'/'.join(str(v) for v in cell_voltages_mv)} mV)"]
        if cell_delta_mv > 120:
            bal_status = "FAIL"
        elif cell_delta_mv > 50:
            bal_status = "WARN"
        if cycle_count is not None and cycle_count > 50 and qmax_delta_mah == 0 and len(qmax_mah) >= 2:
            if bal_status == "PASS":
                bal_status = "WARN"
            bal_notes.append(f"Qmax identical across {len(qmax_mah)} banks after {cycle_count} cycles")
        elif qmax_delta_mah is not None:
            bal_notes.append(f"Qmax spread {qmax_delta_mah} mAh")
        checks.append({
            "name": "Cell Balance & Qmax",
            "status": bal_status,
            "detail": "; ".join(bal_notes),
        })

    if pf_status is not None or disconnect_count is not None or flash_writes is not None:
        pf_val = pf_status or 0
        disc_val = disconnect_count or 0
        virgin_dod = bool(dod0) and all(x == 16384 for x in dod0)
        if pf_val != 0 or disc_val > 0:
            fl_status = "FAIL"
            fl_detail = f"PF=0x{pf_val:x}, Disconnects={disc_val}, FlashWrites={flash_writes}"
        elif (flash_writes == 0 and ((cycle_count is not None and cycle_count <= 2) or virgin_dod)):
            fl_status = "WARN"
            fl_detail = f"Virgin/zero flash signature (FlashWrites=0, Cycles={cycle_count})"
        else:
            fl_status = "PASS"
            fl_detail = f"PF=0, Disconnects=0, FlashWrites={flash_writes if flash_writes is not None else 'N/A'}"
        checks.append({
            "name": "Flash & Hardware Flags",
            "status": fl_status,
            "detail": fl_detail,
        })

    statuses = {c["status"] for c in checks}
    if not checks:
        forensics_verdict = "PARTIAL"
    elif "FAIL" in statuses:
        forensics_verdict = "ALERT"
    elif "WARN" in statuses:
        forensics_verdict = "REVIEW"
    else:
        forensics_verdict = "PASS"

    return {
        "os_percent": os_percent,
        "raw_soc_pct": raw_soc_pct,
        "raw_current_mah": raw_current_mah,
        "raw_max_mah": raw_max_mah,
        "nominal_mah": nominal_mah,
        "design_mah": design_mah,
        "true_health_pct": true_health_pct,
        "nominal_health_pct": nominal_health_pct,
        "cycle_count": cycle_count,
        "design_cycles": design_cycles,
        "gauge_model": gauge_model,
        "masked_serial": masked_serial,
        "voltage_mv": voltage_mv,
        "amperage_ma": amperage_ma,
        "is_charging": is_charging,
        "external_connected": external_connected,
        "time_remaining_min": time_remaining_min,
        "battery_power_w": round(battery_power_w, 2) if battery_power_w is not None else None,
        "system_load_w": system_load_w,
        "wall_in_w": wall_in_w,
        "adapter_watts": adapter_watts,
        "adapter_voltage_v": round(float(adapter_mv) / 1000.0, 2) if adapter_mv is not None else None,
        "adapter_current_a": round(float(adapter_ma) / 1000.0, 2) if adapter_ma is not None else None,
        "adapter_desc": adapter_desc,
        "inst_rate_pct_h": inst_rate_pct_h,
        "temp_c": temp_c,
        "virtual_temp_c": virtual_temp_c,
        "flash_writes": flash_writes,
        "chem_id": chem_id,
        "daily_min_soc": daily_min_soc,
        "daily_max_soc": daily_max_soc,
        "pf_status": pf_status,
        "disconnect_count": disconnect_count,
        "cycle_last_qmax": cycle_last_qmax,
        "temp_samples": temp_samples,
        "operating_hours": operating_hours,
        "implied_hours": implied_hours,
        "clock_diff_pct": clock_diff_pct,
        "min_pack_mv": min_pack_mv,
        "max_pack_mv": max_pack_mv,
        "max_charge_ma": max_charge_ma,
        "max_discharge_ma": max_discharge_ma,
        "min_temp_c": min_temp_c,
        "max_temp_c": max_temp_c,
        "avg_temp_c": avg_temp_c,
        "cell_voltages_mv": cell_voltages_mv,
        "cell_delta_mv": cell_delta_mv,
        "qmax_mah": qmax_mah,
        "qmax_delta_mah": qmax_delta_mah,
        "dod0": dod0,
        "weighted_ra_mohm": weighted_ra_mohm,
        "checks": checks,
        "forensics_verdict": forensics_verdict,
    }


def record_and_compute_rates(
    state: dict,
    telemetry: Optional[Dict[str, Any]],
    on_ac: bool,
    os_percent: int,
    append: bool = False,
) -> Dict[str, Any]:
    """Maintain a compact 180-sample ring buffer and compute 15m/60m sliding-window %/h rates."""
    now = time.time()
    soc_val = (
        float(telemetry["raw_soc_pct"])
        if telemetry and telemetry.get("raw_soc_pct") is not None
        else float(os_percent)
    )
    ac_flag = 1 if on_ac else 0

    raw_hist = state.get("history_samples")
    samples: List[List[Any]] = []
    if isinstance(raw_hist, list):
        cutoff = now - 10800.0
        for row in raw_hist:
            if (
                isinstance(row, list)
                and len(row) == 3
                and isinstance(row[0], (int, float))
                and isinstance(row[1], (int, float))
                and row[0] >= cutoff
            ):
                samples.append([float(row[0]), float(row[1]), 1 if row[2] else 0])

    current_row = [round(now, 1), round(soc_val, 3), ac_flag]
    should_append = (
        not samples
        or samples[-1][2] != ac_flag
        or (now - samples[-1][0]) >= 30.0
    )
    if append:
        if should_append:
            samples.append(current_row)
        else:
            samples[-1] = current_row
        if len(samples) > 180:
            samples = samples[-180:]
        state["history_samples"] = samples
        eval_samples = samples
    else:
        eval_samples = list(samples)
        if should_append or (now - samples[-1][0]) >= 5.0:
            eval_samples.append(current_row)

    segment: List[List[Any]] = []
    for row in reversed(eval_samples):
        if row[2] == ac_flag:
            segment.append(row)
        else:
            break
    segment.reverse()

    def _window_rate(window_sec: float) -> Dict[str, Optional[float]]:
        if len(segment) < 2:
            return {"rate": None, "span_min": None}
        latest_ts = segment[-1][0]
        window_pts = [p for p in segment if p[0] >= latest_ts - window_sec - 60.0]
        if len(window_pts) < 2:
            window_pts = segment
        dt = window_pts[-1][0] - window_pts[0][0]
        if dt < 120.0:
            return {"rate": None, "span_min": round(dt / 60.0, 1)}
        rate = (window_pts[-1][1] - window_pts[0][1]) / (dt / 3600.0)
        return {"rate": round(rate, 2), "span_min": round(dt / 60.0, 1)}

    r15 = _window_rate(900.0)
    r60 = _window_rate(3600.0)
    return {
        "rate_15m_pct_h": r15["rate"],
        "span_15m_min": r15["span_min"],
        "rate_60m_pct_h": r60["rate"],
        "span_60m_min": r60["span_min"],
        "sample_count": len(segment),
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
    """Close any running BatteryFullscreenAlert overlay window owned by the current UID."""
    subprocess.run(
        ["/usr/bin/pkill", "-x", "-u", str(os.getuid()), "BatteryFullscreenAlert"],
        capture_output=True,
    )


def trigger_fullscreen_alert(
    mode: str,
    percent: int,
    message: str = "",
    device_name: str = "MacBook",
    dismiss_policy: str = "auto",
) -> None:
    """Launch the native Swift silent full-screen alert or HUD via macOS LaunchServices (open -a)."""
    safe_device = sanitize_header_token(device_name) or "MacBook"
    log(
        f"Triggering silent fullscreen alert: device={safe_device}, mode={mode}, "
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
            safe_device,
            str(STATE_PATH),
            dismiss_policy,
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode == 0:
            return
        log(f"open -a BatteryFullscreenAlert.app failed (rc={res.returncode}): {res.stderr.strip()}")

    if ALERT_BIN.exists():
        subprocess.Popen(
            [str(ALERT_BIN), mode, str(percent), message, safe_device, str(STATE_PATH), dismiss_policy]
        )
        return

    fallback_msg = message or f"[{safe_device}] Battery is at {percent}%."
    escaped_title = escape_applescript_str(f"[{safe_device}] Battery Alert ({percent}%)")
    escaped_body = escape_applescript_str(fallback_msg)
    apple_script = (
        f'display alert "{escaped_title}" '
        f'message "{escaped_body}" as critical buttons {{"Dismiss"}} default button 1'
    )
    subprocess.Popen(["/usr/bin/osascript", "-e", apple_script])


def send_emergency_email(cfg: dict, batt: dict, duration_min: float, is_test: bool = False) -> bool:
    """Send a critical low-battery (<15%) email notification via Mail.app, SMTP, or SSH sendmail."""
    email_cfg = cfg.get("email", {})
    to_email = sanitize_header_token(email_cfg.get("to_email", ""))
    device_name = sanitize_header_token(cfg.get("device_name", "MacBook")) or "MacBook"
    if not to_email or to_email in ("you@example.com", "your_email@example.com"):
        log("No valid recipient address configured in config.json (email.to_email); skipping email.")
        return False
    if not is_safe_email_addr(to_email):
        log("Invalid or unsafe email.to_email address in config.json; skipping email.")
        return False

    hostname = sanitize_header_token(socket.gethostname())
    try:
        comp_name = sanitize_header_token(
            subprocess.check_output(["/usr/sbin/scutil", "--get", "ComputerName"], text=True)
        )
    except Exception:
        comp_name = hostname
    try:
        cpu_brand = sanitize_header_token(
            subprocess.check_output(["/usr/sbin/sysctl", "-n", "machdep.cpu.brand_string"], text=True)
        )
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
    subject = sanitize_header_token(subject)

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

    method = str(email_cfg.get("method", "mail_app")).lower()

    if method == "ssh_sendmail":
        ssh_socket_raw = str(email_cfg.get("ssh_socket") or "").strip()
        ssh_host = sanitize_header_token(email_cfg.get("ssh_host") or "")
        if not ssh_host or ssh_host == "user@remote-host.example.com":
            log("SSH sendmail selected, but email.ssh_host is not configured.")
            return False
        if not is_safe_ssh_target(ssh_host):
            log("Unsafe email.ssh_host value rejected (must match [user@]host without leading '-').")
            return False

        from_email = sanitize_header_token(email_cfg.get("from_email", ""))
        if not from_email or from_email == "battery-guardian@example.com":
            from_email = ssh_host if "@" in ssh_host else "battery-guardian@localhost"
        if not is_safe_email_addr(from_email, allow_localhost=True):
            log("Unsafe email.from_email address rejected; aborting SSH sendmail.")
            return False

        msg_domain = from_email.split("@", 1)[1] if "@" in from_email else sanitize_header_token(socket.getfqdn())
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
            if re.search(r"[\r\n\x00]", ssh_socket_raw):
                log("Invalid control character in email.ssh_socket; aborting SSH sendmail.")
                return False
            sock_path = str(Path(ssh_socket_raw).expanduser())
            if sock_path.startswith("-"):
                log("Unsafe email.ssh_socket path rejected; aborting SSH sendmail.")
                return False
            try:
                if Path(sock_path).exists():
                    ssh_cmd.extend(["-S", sock_path])
            except PermissionError:
                ssh_cmd.extend(["-S", sock_path])
        ssh_cmd.extend(["--", ssh_host, "/usr/sbin/sendmail -t -oi"])

        res = subprocess.run(ssh_cmd, input=msg.as_string(), text=True, capture_output=True)
        if res.returncode == 0:
            log(f"Email sent via SSH sendmail to {to_email}")
            return True
        log(f"SSH sendmail failed (rc={res.returncode}): {res.stderr.strip()}")
        return False

    if method == "mail_app":
        log(f"Sending [{device_name}] alert email via Apple Mail.app to {to_email}...")
        escaped_subject = escape_applescript_str(subject)
        escaped_body = escape_applescript_str(body)
        escaped_to = escape_applescript_str(to_email)
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
        smtp_host = sanitize_header_token(email_cfg.get("smtp_host", ""))
        smtp_port = int(email_cfg.get("smtp_port", 465))
        smtp_user = sanitize_header_token(email_cfg.get("smtp_user", ""))
        smtp_pass = str(email_cfg.get("smtp_password", ""))
        from_email = sanitize_header_token(email_cfg.get("from_email", "")) or smtp_user
        if not smtp_host or re.search(r"\s", smtp_host) or smtp_host.startswith("-"):
            log("Invalid email.smtp_host configuration; aborting SMTP send.")
            return False
        if not is_safe_email_addr(from_email, allow_localhost=True):
            log("Invalid email.from_email / smtp_user address; aborting SMTP send.")
            return False
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
                    server.sendmail(from_email, [to_email], msg.as_string())
            else:
                with smtplib.SMTP(smtp_host, smtp_port, timeout=15) as server:
                    server.starttls()
                    server.login(smtp_user, smtp_pass)
                    server.sendmail(from_email, [to_email], msg.as_string())
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

    device_name = sanitize_header_token(cfg.get("device_name", "MacBook")) or "MacBook"
    pct = batt["percent"]
    on_ac = batt["on_ac"]
    now = time.time()

    telemetry = read_smart_battery_telemetry()
    record_and_compute_rates(state, telemetry, on_ac, pct, append=True)

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
                to_addr = sanitize_header_token(cfg.get("email", {}).get("to_email", ""))
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


def _fmt_num(val: Any, suffix: str = "", digits: int = 1, signed: bool = False) -> str:
    if val is None:
        return "N/A"
    if isinstance(val, int) and digits == 0:
        return (f"{val:+d}" if signed else f"{val}") + suffix
    fval = float(val)
    fmt = f"{{:{'+' if signed else ''}.{digits}f}}{suffix}"
    return fmt.format(fval)


def show_status() -> None:
    cfg = load_config()
    state = load_state()
    batt = get_battery_info()
    telemetry = read_smart_battery_telemetry()
    rates = record_and_compute_rates(state, telemetry, batt["on_ac"], batt["percent"], append=False)
    quiet = evaluate_quiet_state(cfg, state)
    device_name = sanitize_header_token(cfg.get("device_name", "MacBook")) or "MacBook"
    power_label = "AC Power (Charging/Connected)" if batt["on_ac"] else "Battery Power (Unplugged)"

    print(f"=== Device: {device_name} ===")
    print(f"  Battery Level   : {batt['percent']}%")
    print(f"  Power Source    : {power_label}")
    print(f"  Charge Status   : {batt['status']} ({batt['remaining']})")

    if telemetry:
        health_str = _fmt_num(telemetry.get("true_health_pct"), "%", 1)
        raw_max = telemetry.get("raw_max_mah")
        design = telemetry.get("design_mah")
        cap_str = f"{raw_max} / {design} mAh" if (raw_max and design) else "N/A"
        cyc = telemetry.get("cycle_count")
        dcyc = telemetry.get("design_cycles")
        cyc_str = f"{cyc} / {dcyc}" if cyc is not None else "N/A"
        temp_str = _fmt_num(telemetry.get("temp_c"), " C", 1)
        pwr_str = _fmt_num(telemetry.get("battery_power_w"), " W", 1, signed=True)
        rate_str = _fmt_num(telemetry.get("inst_rate_pct_h"), "%/h", 1, signed=True)
        r15_str = _fmt_num(rates.get("rate_15m_pct_h"), "%/h", 1, signed=True)
        print("\n=== Hardware Health & Forensics (0-Risk IOKit) ===")
        print(f"  True Health     : {health_str} ({cap_str})")
        print(f"  Cycles & Gauge  : {cyc_str} cycles ({telemetry.get('gauge_model')}, Serial {telemetry.get('masked_serial')})")
        print(f"  Live Power/Temp : {pwr_str} (Inst {rate_str}, 15m {r15_str}) | Temp {temp_str}")
        print(f"  Forensics Audit : {telemetry.get('forensics_verdict')} (run './manage.sh health' or './manage.sh hud')")

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


def show_health() -> None:
    cfg = load_config()
    state = load_state()
    batt = get_battery_info()
    t = read_smart_battery_telemetry()
    device_name = sanitize_header_token(cfg.get("device_name", "MacBook")) or "MacBook"

    if not t:
        print("Unable to read AppleSmartBattery hardware telemetry from IOKit.")
        return

    rates = record_and_compute_rates(state, t, batt["on_ac"], batt["percent"], append=False)
    volt_v = (float(t["voltage_mv"]) / 1000.0) if t.get("voltage_mv") is not None else None
    amp_ma = t.get("amperage_ma")

    print(f"=== [{device_name}] Battery Health & Forensics Report (0-Risk Read-Only IOKit) ===")
    print(f"  OS Charge / AC  : {batt['percent']}% (Raw SoC {_fmt_num(t.get('raw_soc_pct'), '%', 2)}) | {batt['status']} ({batt['remaining']})")
    print(
        f"  True Health     : {_fmt_num(t.get('true_health_pct'), '%', 2)} "
        f"(RawMax {t.get('raw_max_mah')} / Design {t.get('design_mah')} mAh; "
        f"Nominal {t.get('nominal_mah')} mAh = {_fmt_num(t.get('nominal_health_pct'), '%', 2)})"
    )
    print(
        f"  Cycle Count     : {t.get('cycle_count')} / {t.get('design_cycles')} "
        f"| Gauge: {t.get('gauge_model')} | Masked Serial: {t.get('masked_serial')}"
    )

    print("\n=== Electrical, Thermal & Rate Telemetry ===")
    print(
        f"  Pack Voltage/I  : {_fmt_num(volt_v, ' V', 3)} | {_fmt_num(amp_ma, ' mA', 0, signed=True)} "
        f"| Battery Power: {_fmt_num(t.get('battery_power_w'), ' W', 2, signed=True)}"
    )
    print(
        f"  System / Wall   : Sys Load {_fmt_num(t.get('system_load_w'), ' W', 2)} "
        f"| Wall In {_fmt_num(t.get('wall_in_w'), ' W', 2)} "
        f"| Adapter: {t.get('adapter_watts') or 0} W ({t.get('adapter_desc') or 'disconnected'})"
    )
    print(
        f"  Charge Rate     : Instant {_fmt_num(t.get('inst_rate_pct_h'), '%/h', 1, signed=True)} "
        f"| 15m Avg {_fmt_num(rates.get('rate_15m_pct_h'), '%/h', 1, signed=True)} "
        f"| 60m Avg {_fmt_num(rates.get('rate_60m_pct_h'), '%/h', 1, signed=True)} "
        f"(samples={rates.get('sample_count', 0)})"
    )
    print(
        f"  Temperature     : Pack {_fmt_num(t.get('temp_c'), ' C', 2)} "
        f"| Virtual {_fmt_num(t.get('virtual_temp_c'), ' C', 2)} "
        f"| Daily SoC: {t.get('daily_min_soc')}%..{t.get('daily_max_soc')}%"
    )

    print("\n=== Multi-Cell Balance & Lifetime Extremes ===")
    cv = t.get("cell_voltages_mv") or []
    qm = t.get("qmax_mah") or []
    ra = t.get("weighted_ra_mohm") or []
    print(
        f"  Cell Voltages   : {cv if cv else 'N/A'} mV "
        f"(Spread dV = {_fmt_num(t.get('cell_delta_mv'), ' mV', 0)})"
    )
    print(
        f"  Cell Qmax / Ra  : Qmax={qm if qm else 'N/A'} mAh (dQmax={_fmt_num(t.get('qmax_delta_mah'), ' mAh', 0)}) "
        f"| Ra={ra if ra else 'N/A'} mOhm"
    )
    print(
        f"  Lifetime V / I  : {t.get('min_pack_mv')}..{t.get('max_pack_mv')} mV "
        f"| MaxChg {_fmt_num(t.get('max_charge_ma'), ' mA', 0)} / MaxDis {_fmt_num(t.get('max_discharge_ma'), ' mA', 0)}"
    )
    print(
        f"  Lifetime Temp/T : {t.get('min_temp_c')}..{t.get('max_temp_c')} C "
        f"(Avg {_fmt_num(t.get('avg_temp_c'), ' C', 1)}) "
        f"| OperatingTime: {t.get('operating_hours')} h"
    )

    print(f"\n=== Anti-Spoofing Gas-Gauge Forensics: [{t.get('forensics_verdict')}] ===")
    for chk in t.get("checks") or []:
        print(f"  [{chk['status']:4s}] {chk['name']:22s}: {chk['detail']}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="macOS Battery Guardian: Silent Full-Screen Alert, Telemetry HUD & Critical Email Daemon"
    )
    parser.add_argument("--check", action="store_true", help="Run a single battery check")
    parser.add_argument("--status", action="store_true", help="Show current battery and monitor status")
    parser.add_argument("--health", action="store_true", help="Print detailed 0-risk IOKit battery health & forensics report")
    parser.add_argument("--hud", "--screen", dest="hud", action="store_true", help="Display live battery health & forensics HUD on screen")
    parser.add_argument("--pause", type=int, nargs="?", const=120, metavar="MINUTES", help="Pause fullscreen alerts for N minutes (default 120)")
    parser.add_argument("--unmute", action="store_true", help="Resume normal silent fullscreen alerts")
    parser.add_argument("--test-high", action="store_true", help="Preview >70%% silent fullscreen alert")
    parser.add_argument("--test-low", action="store_true", help="Preview <30%% silent fullscreen alert")
    parser.add_argument("--test-critical", action="store_true", help="Preview <15%% critical silent fullscreen alert")
    parser.add_argument("--test-email", action="store_true", help="Send a test emergency email")
    args = parser.parse_args()

    cfg = load_config()
    state = load_state()
    device_name = sanitize_header_token(cfg.get("device_name", "MacBook")) or "MacBook"
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

    if args.health:
        show_health()
    elif args.hud:
        batt = get_battery_info()
        trigger_fullscreen_alert(
            "inspect",
            batt["percent"],
            "",
            device_name,
            dismiss_policy="manual",
        )
    elif args.status:
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
        to_addr = sanitize_header_token(cfg.get("email", {}).get("to_email", "you@example.com"))
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
