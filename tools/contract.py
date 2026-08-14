#!/usr/bin/env python3
"""Emit and check the cross-language contract.

Other implementations of this protocol — the Swift reporter, most immediately —
cannot import Python. Two things travel across that boundary instead:

* **`spec/protocol.json`** — the constants, projected out of the Python modules.
  Those modules stay the source of truth because they carry the evidence for
  every field in their comments; this file is a build artifact, never edited.
* **`spec/fixtures/*.json`** — for each capture, the decoded state after every
  frame. This is the part that matters. Constants that disagree fail loudly at
  connect time; an *interpretation* that disagrees produces plausible wrong
  numbers forever, which is exactly the failure mode this repository already hit
  five times in one afternoon. A second implementation replays the capture bytes
  and must reproduce these outputs field for field.

    python3 tools/contract.py export     # regenerate everything
    python3 tools/contract.py verify     # fail if anything is stale
    python3 tools/contract.py swift      # print the generated Swift constants
"""

from __future__ import annotations

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from anker_prime_ble import charger, ff09, powerbank  # noqa: E402

SPEC_DIR = ROOT / "spec"
FIXTURE_DIR = SPEC_DIR / "fixtures"
CAPTURE_DIR = ROOT / "captures"
SWIFT_OUT = SPEC_DIR / "AnkerPrimeSpec.swift"

PROFILES = {"charger": charger.PROFILE, "powerbank": powerbank.PROFILE}


# --- spec ------------------------------------------------------------------


def build_spec() -> dict:
    return {
        "_generated_by": "tools/contract.py — do not edit; edit the Python modules",
        "transport": {
            "service_uuid": ff09.SERVICE_UUID,
            "write_characteristic_uuid": ff09.WRITE_CHAR_UUID,
            "notify_characteristic_uuid": ff09.NOTIFY_CHAR_UUID,
            "advertised_service_uuid": ff09.ADVERTISED_SERVICE_UUID,
            "initial_key": ff09.INITIAL_KEY.hex(),
            "initial_iv": ff09.INITIAL_IV.hex(),
            "gcm_aad": ff09.GCM_AAD.hex(),
            "flag_encrypted": ff09.FLAG_ENCRYPTED,
            "flag_ack": ff09.FLAG_ACK,
            "group_session": ff09.GROUP_SESSION,
            "group_telemetry": ff09.GROUP_TELEMETRY,
            "command_status": ff09.CMD_STATUS,
            "command_realtime": ff09.CMD_REALTIME,
        },
        "devices": {
            "charger": {
                "name": charger.PROFILE.name,
                "name_prefix": charger.PROFILE.name_prefix,
                "needs_account_id": charger.PROFILE.needs_account_id,
                "needs_realtime_probe": charger.PROFILE.needs_realtime_probe,
                "realtime_commands": sorted(charger.REALTIME_COMMANDS),
                "snapshot_commands": sorted(charger.SNAPSHOT_COMMANDS),
                "ports": list(charger._PORT_ORDER),
                "tlv": {
                    "port_struct": {f"0x{k:02X}": v for k, v in charger._PORT_STRUCT_TYPES.items()},
                    "port_cable": {f"0x{k:02X}": v for k, v in charger._PORT_CABLE_TYPES.items()},
                    "identity": f"0x{charger.PORT_IDENTITY_TLV:02X}",
                    "brand_model": f"0x{charger.PORT_BRAND_MODEL_TLV:02X}",
                },
                "scale": {"voltage": "millivolts", "current": "milliamps", "power": "centiwatts"},
                "tables": {
                    "cable_capability": charger.CABLE_CAPABILITY_LABELS,
                    "charging_info": charger.CHARGING_INFO_LABELS,
                    "vendor_names": {f"0x{k:04X}": v for k, v in charger.USB_VENDOR_NAMES.items()},
                    "device_models": {
                        f"0x{vid:04X}:0x{pid:04X}": name
                        for (vid, pid), name in charger.DEVICE_MODEL_NAMES.items()
                    },
                    "brand_codes": {f"0x{k:02X}": v for k, v in charger.BRAND_CODES.items()},
                    "identity_sentinels": sorted(f"0x{v:04X}" for v in charger._IDENTITY_SENTINELS),
                },
            },
            "powerbank": {
                "name": powerbank.PROFILE.name,
                "name_prefix": powerbank.PROFILE.name_prefix,
                "needs_account_id": powerbank.PROFILE.needs_account_id,
                "needs_realtime_probe": powerbank.PROFILE.needs_realtime_probe,
                "realtime_commands": sorted(powerbank.REALTIME_COMMANDS),
                "snapshot_commands": sorted(powerbank.SNAPSHOT_COMMANDS),
                "ports": ["C1", "C2", "A"],
                "tlv": {
                    "state": f"0x{powerbank.TLV_STATE:02X}",
                    "battery": f"0x{powerbank.TLV_BATTERY:02X}",
                    "time_left": f"0x{powerbank.TLV_TIME_LEFT:02X}",
                    "thermal_state": f"0x{powerbank.TLV_THERMAL_STATE:02X}",
                    "input_total": f"0x{powerbank.TLV_INPUT_TOTAL:02X}",
                    "output_total": f"0x{powerbank.TLV_OUTPUT_TOTAL:02X}",
                    "dock": f"0x{powerbank.TLV_DOCK:02X}",
                    "port_c1": f"0x{powerbank.TLV_PORT_C1:02X}",
                    "port_c2": f"0x{powerbank.TLV_PORT_C2:02X}",
                    "port_a": f"0x{powerbank.TLV_PORT_A:02X}",
                    "temperature_1": f"0x{powerbank.TLV_TEMP_1:02X}",
                    "temperature_2": f"0x{powerbank.TLV_TEMP_2:02X}",
                },
                "snapshot": {
                    "shift": powerbank.SNAPSHOT_SHIFT,
                    "state": f"0x{powerbank.SNAPSHOT_STATE:02X}",
                    "battery": f"0x{powerbank.SNAPSHOT_BATTERY:02X}",
                    "time_left": f"0x{powerbank.SNAPSHOT_TIME_LEFT:02X}",
                    "pomodoro_seconds": f"0x{powerbank.SNAPSHOT_POMODORO_SECONDS:02X}",
                    "pomodoro_enable": f"0x{powerbank.SNAPSHOT_POMODORO_ENABLE:02X}",
                },
                "scale": {"voltage": "tenths", "current": "tenths", "power": "tenths"},
                "encodings": {
                    "battery_percent": "decimal_pair",
                    "time_left": "flag_hours_minutes",
                },
                "direction": {str(k): v for k, v in powerbank.DIRECTION.items()},
                "port_block": {
                    "power_slot_is_sticky": True,
                    "attach_flag_offset": 8,
                    "attach_flag_present": "0x07",
                    "source_role_offset": 7,
                    "source_role_sourcing": "0x02",
                },
            },
        },
    }


# --- fixtures --------------------------------------------------------------


def build_fixture(path: pathlib.Path, device: str) -> dict:
    profile = PROFILES[device]
    state = profile.new_state()
    frames = []
    first = None
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        event = json.loads(line)
        if event.get("dir") != "rx" or "plain" not in event:
            continue
        first = first if first is not None else event["t"]
        payload = bytes.fromhex(event["plain"])
        command = event["cmd"]
        if command == 0x0029:
            profile.parse_identity(payload, state)
        elif command in profile.snapshot_commands:
            profile.parse_snapshot(payload, state)
        elif command in profile.realtime_commands:
            profile.parse_realtime(payload, state)
        else:
            continue
        frames.append(
            {
                "offset_s": round(event["t"] - first, 3),
                "command": command,
                "state": state.to_dict(),
            }
        )
    return {"capture": path.name, "device": device, "frames": frames}


def device_for(path: pathlib.Path) -> str:
    return "charger" if "charger" in path.name else "powerbank"


# --- Swift generation ------------------------------------------------------


def _swift_literal(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return f'"{value}"'
    if isinstance(value, int):
        return str(value)
    raise TypeError(value)


def _swift_dict(mapping: dict, key_type: str, value_type: str, indent: str) -> str:
    if not mapping:
        return "[:]"
    lines = [f"{indent}    {_swift_literal(k)}: {_swift_literal(v)}," for k, v in mapping.items()]
    return "[\n" + "\n".join(lines) + f"\n{indent}]"


def build_swift(spec: dict) -> str:
    t = spec["transport"]
    charger_spec = spec["devices"]["charger"]
    bank = spec["devices"]["powerbank"]
    tables = charger_spec["tables"]

    def hex_keyed(mapping: dict, prefix_len: int) -> dict:
        return {int(k, 16): v for k, v in mapping.items()}

    vendor_lines = "\n".join(
        f"        0x{int(k, 16):04X}: \"{v}\","
        for k, v in tables["vendor_names"].items()
    )
    model_lines = "\n".join(
        f"        modelKey(0x{k.split(':')[0][2:]}, 0x{k.split(':')[1][2:]}): \"{v}\","
        for k, v in tables["device_models"].items()
    )
    brand_lines = "\n".join(
        f"        0x{int(k, 16):02X}: \"{v}\"," for k, v in tables["brand_codes"].items()
    )

    return f'''// Generated by tools/contract.py in anker-prime-ble. Do not edit.
//
// The Python modules in that repository are the source of truth — they carry the
// evidence for every field in their comments. This file is a projection of their
// constants so a Swift implementation cannot drift from them silently.
//
// Constants are the easy half. For the decoding *behaviour*, replay
// spec/fixtures/*.json and compare field by field; that is what catches the
// mistakes that matter.

import Foundation

public enum AnkerPrimeSpec {{
    public enum Transport {{
        public static let serviceUUID = "{t["service_uuid"].upper()}"
        public static let writeCharacteristicUUID = "{t["write_characteristic_uuid"].upper()}"
        public static let notifyCharacteristicUUID = "{t["notify_characteristic_uuid"].upper()}"
        public static let advertisedServiceUUID = "{t["advertised_service_uuid"].upper()}"

        public static let initialKey = Data([{", ".join(f"0x{b:02X}" for b in bytes.fromhex(t["initial_key"]))}])
        public static let initialIV = Data([{", ".join(f"0x{b:02X}" for b in bytes.fromhex(t["initial_iv"]))}])
        public static let gcmAAD = Data([{", ".join(f"0x{b:02X}" for b in bytes.fromhex(t["gcm_aad"]))}])

        public static let flagEncrypted: UInt8 = 0x{t["flag_encrypted"]:02X}
        public static let flagAcknowledged: UInt8 = 0x{t["flag_ack"]:02X}
        public static let groupSession: UInt8 = 0x{t["group_session"]:02X}
        public static let groupTelemetry: UInt8 = 0x{t["group_telemetry"]:02X}
        public static let commandStatus: UInt16 = 0x{t["command_status"]:04X}
        public static let commandRealtime: UInt16 = 0x{t["command_realtime"]:04X}
    }}

    public enum Charger {{
        public static let namePrefix = "{charger_spec["name_prefix"]}"
        public static let needsAccountID = {str(charger_spec["needs_account_id"]).lower()}
        public static let needsRealtimeProbe = {str(charger_spec["needs_realtime_probe"]).lower()}
        public static let ports = [{", ".join(f'"{p}"' for p in charger_spec["ports"])}]
        public static let realtimeCommands: Set<UInt16> = [{", ".join(f"0x{c:04X}" for c in charger_spec["realtime_commands"])}]
        public static let snapshotCommands: Set<UInt16> = [{", ".join(f"0x{c:04X}" for c in charger_spec["snapshot_commands"])}]

        // Voltage is millivolts, current milliamps, power centiwatts — unlike the
        // power bank, which is tenths throughout.
        public static let portStructTLV: [UInt8: String] = {_swift_dict({int(k,16): v for k,v in charger_spec["tlv"]["port_struct"].items()}, "UInt8", "String", "        ")}
        public static let portCableTLV: [UInt8: String] = {_swift_dict({int(k,16): v for k,v in charger_spec["tlv"]["port_cable"].items()}, "UInt8", "String", "        ")}
        public static let identityTLV: UInt8 = {charger_spec["tlv"]["identity"]}
        public static let brandModelTLV: UInt8 = {charger_spec["tlv"]["brand_model"]}
        public static let identitySentinels: Set<UInt16> = [{", ".join(tables["identity_sentinels"])}]

        public static let cableCapabilityLabels: [String: String] = {_swift_dict(tables["cable_capability"], "String", "String", "        ")}
        public static let chargingInfoLabels: [String: String] = {_swift_dict(tables["charging_info"], "String", "String", "        ")}

        public static let vendorNames: [UInt16: String] = [
{vendor_lines}
        ]

        public static let deviceModels: [UInt32: String] = [
{model_lines}
        ]

        public static let brandCodes: [UInt8: String] = [
{brand_lines}
        ]

        public static func modelKey(_ vendor: UInt16, _ product: UInt16) -> UInt32 {{
            (UInt32(vendor) << 16) | UInt32(product)
        }}
    }}

    public enum PowerBank {{
        public static let needsAccountID = {str(bank["needs_account_id"]).lower()}
        public static let needsRealtimeProbe = {str(bank["needs_realtime_probe"]).lower()}
        public static let ports = [{", ".join(f'"{p}"' for p in bank["ports"])}]
        public static let realtimeCommands: Set<UInt16> = [{", ".join(f"0x{c:04X}" for c in bank["realtime_commands"])}]
        public static let snapshotCommands: Set<UInt16> = [{", ".join(f"0x{c:04X}" for c in bank["snapshot_commands"])}]

        // Volts, amps and watts are all u16 little-endian in tenths.
        public static let stateTLV: UInt8 = {bank["tlv"]["state"]}
        public static let batteryTLV: UInt8 = {bank["tlv"]["battery"]}
        public static let timeLeftTLV: UInt8 = {bank["tlv"]["time_left"]}
        public static let thermalStateTLV: UInt8 = {bank["tlv"]["thermal_state"]}
        public static let inputTotalTLV: UInt8 = {bank["tlv"]["input_total"]}
        public static let outputTotalTLV: UInt8 = {bank["tlv"]["output_total"]}
        public static let dockTLV: UInt8 = {bank["tlv"]["dock"]}
        public static let portC1TLV: UInt8 = {bank["tlv"]["port_c1"]}
        public static let portC2TLV: UInt8 = {bank["tlv"]["port_c2"]}
        public static let portATLV: UInt8 = {bank["tlv"]["port_a"]}
        public static let temperature1TLV: UInt8 = {bank["tlv"]["temperature_1"]}
        public static let temperature2TLV: UInt8 = {bank["tlv"]["temperature_2"]}

        // The realtime block appears inside the 0x0200 snapshot at +0x0D.
        public static let snapshotShift: UInt8 = {bank["snapshot"]["shift"]}
        public static let snapshotStateTLV: UInt8 = {bank["snapshot"]["state"]}
        public static let snapshotBatteryTLV: UInt8 = {bank["snapshot"]["battery"]}
        public static let snapshotTimeLeftTLV: UInt8 = {bank["snapshot"]["time_left"]}
        public static let snapshotPomodoroSecondsTLV: UInt8 = {bank["snapshot"]["pomodoro_seconds"]}
        public static let snapshotPomodoroEnableTLV: UInt8 = {bank["snapshot"]["pomodoro_enable"]}

        /// Byte 8 of a C-port block: a cable is in the port, negotiated or not.
        public static let attachFlagOffset = {bank["port_block"]["attach_flag_offset"]}
        public static let attachFlagPresent: UInt8 = {bank["port_block"]["attach_flag_present"]}
        /// Byte 7: 0x02 while the port is sourcing, 0xFF while drawing in or idle.
        public static let sourceRoleOffset = {bank["port_block"]["source_role_offset"]}
        public static let sourceRoleSourcing: UInt8 = {bank["port_block"]["source_role_sourcing"]}

        /// The per-port power slot keeps its last value when a port goes idle.
        /// Read it only when the mode byte is non-zero.
        public static let powerSlotIsSticky = true
    }}
}}
'''


# --- commands --------------------------------------------------------------


def export() -> list[str]:
    written = []
    SPEC_DIR.mkdir(exist_ok=True)
    FIXTURE_DIR.mkdir(exist_ok=True)

    spec = build_spec()
    spec_path = SPEC_DIR / "protocol.json"
    spec_path.write_text(json.dumps(spec, indent=2, sort_keys=False) + "\n")
    written.append(str(spec_path.relative_to(ROOT)))

    for capture in sorted(CAPTURE_DIR.glob("*.jsonl")):
        fixture = build_fixture(capture, device_for(capture))
        out = FIXTURE_DIR / f"{capture.stem}.json"
        out.write_text(json.dumps(fixture, indent=1) + "\n")
        written.append(str(out.relative_to(ROOT)))

    SWIFT_OUT.write_text(build_swift(spec))
    written.append(str(SWIFT_OUT.relative_to(ROOT)))
    return written


def verify() -> int:
    """Fail if any generated file is out of date with the Python modules."""
    stale = []

    spec = build_spec()
    spec_path = SPEC_DIR / "protocol.json"
    if not spec_path.exists() or spec_path.read_text() != json.dumps(spec, indent=2) + "\n":
        stale.append(str(spec_path.relative_to(ROOT)))

    if not SWIFT_OUT.exists() or SWIFT_OUT.read_text() != build_swift(spec):
        stale.append(str(SWIFT_OUT.relative_to(ROOT)))

    for capture in sorted(CAPTURE_DIR.glob("*.jsonl")):
        out = FIXTURE_DIR / f"{capture.stem}.json"
        expected = json.dumps(build_fixture(capture, device_for(capture)), indent=1) + "\n"
        if not out.exists():
            stale.append(f"{out.relative_to(ROOT)} (missing)")
        elif out.read_text() != expected:
            stale.append(str(out.relative_to(ROOT)))

    if stale:
        print("stale — run `python3 tools/contract.py export`:")
        for name in stale:
            print(f"  {name}")
        return 1
    print(f"contract up to date ({len(list(CAPTURE_DIR.glob('*.jsonl')))} fixtures)")
    return 0


def main() -> int:
    command = sys.argv[1] if len(sys.argv) > 1 else "export"
    if command == "export":
        for name in export():
            print(f"wrote {name}")
        return 0
    if command == "verify":
        return verify()
    if command == "swift":
        print(build_swift(build_spec()))
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
