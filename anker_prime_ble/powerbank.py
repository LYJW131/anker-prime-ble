"""Anker Prime Power Bank (20K, 220W) — model A110G — telemetry decoding.

Every field here was pinned down by making the hardware change state and
watching which bytes moved; `docs/powerbank.md` records what was tested for
each. Firmware v0.0.5.1, re-verified on v0.0.5.2.

Two encodings are in play and mixing them up is the main trap:

* **tenths** — u16 little-endian, one decimal place. Volts, amps, watts.
* **decimal pair** — `[integer, remainder out of 100]`. Battery percentage and
  time remaining. Read as a u16 these give large plausible-looking wrong numbers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from . import ff09
from .device import DeviceProfile, DeviceState, PortReading, decimal_pair, tenths

# --- 0x0300 realtime stream ------------------------------------------------

TLV_STATE = 0xA1  # constant 0x31 through charge and discharge alike; unknown
TLV_BATTERY = 0xA2  # [percent, hundredths]
# [flag, hours, minutes], time to *full*; 0h00m on discharge. The hours byte was
# only ever 0 until a capture that charged at 28.6 W while outputting 23.0 W and
# reported 9h48m, which is what ~5.6 W of net input should take.
TLV_TIME_LEFT = 0xA3
# u8 thermal state: 1 normal, 2 thermally limited. Proven by sampling a bank with
# nothing plugged into it at all as it cooled — no power flowed in any sample, so
# "power is flowing" is ruled out, and it still flipped 2 -> 1 as 0xAF crossed
# from 45 C to 44 C. While it reads 2 the bank refuses to charge even with a
# cable attached and live.
TLV_THERMAL_STATE = 0xA4
TLV_INPUT_TOTAL = 0xA5  # [flag, watts x10]
# [flag, watts x10]. A true sum, not a mirror of the busiest port: with C1 at
# 28.6 W and C2 at 89.1 W simultaneously this read exactly 117.7 W.
TLV_OUTPUT_TOTAL = 0xA6
# The charging base. Same [mode, V, A, W] shape as a port block but no attach or
# identity tail, because dock contacts negotiate nothing. Found by contradiction:
# it carried a live 99.4 W -> 70 W charge while both C-port blocks read "nothing
# attached", so the power was arriving through something that was not a USB-C
# port. The owner confirmed the bank was on its base for those captures.
TLV_DOCK = 0xA7

# C1 and C2 are bidirectional and carry both directions in one block, separated
# by the mode byte. Confirmed on C1 both ways (a 90 W laptop load, then a 69.6 W
# charge) and on C2 both ways (an idle 5 V cable, then a 100 W charge).
TLV_PORT_C1 = 0xA8
TLV_PORT_C2 = 0xA9
# The A port. All zeros in every capture until trickle-charge mode was switched
# on for it, then 5.0 V at zero amps — rail up, waiting for a low-draw device.
# Eight bytes rather than fourteen because USB-A negotiates nothing.
TLV_PORT_A = 0xAC
# Corroborated by 0xA4: the thermal state flipped exactly as these crossed 45 C.
TLV_TEMP_1 = 0xAF
TLV_TEMP_2 = 0xB0

# The realtime block also appears inside the 0x0200 snapshot, shifted by +0x0D
# (0x0300 0xA5 is 0x0200 0xB2). Shifting it back lets one decoder serve both.
SNAPSHOT_SHIFT = 0x0D
SNAPSHOT_STATE = 0xA1
SNAPSHOT_BATTERY = 0xA6
SNAPSHOT_TIME_LEFT = 0xA7
# Settings that appear only in the snapshot. The Pomodoro timer is plain
# seconds: it read 1500 on the 25-minute default and 1200 the moment 20 minutes
# was set in the app. 0xE2 repeats it behind an enable flag.
SNAPSHOT_POMODORO_SECONDS = 0xAC
SNAPSHOT_POMODORO_ENABLE = 0xE2
# Battery health, as a whole percent. Confirmed by elimination: it and 0xAA both
# read 100 in every capture, and setting the app's screen brightness to 30 moved
# 0xAA to 30 while this one held. One observation of "both are 100" could never
# have separated them — the two hypotheses predicted the same recording.
SNAPSHOT_BATTERY_HEALTH = 0xA9
# Screen brightness, 0-100, matching the app's slider.
SNAPSHOT_SCREEN_BRIGHTNESS = 0xAA

DIRECTION = {1: "out", 2: "in"}

REALTIME_COMMANDS = frozenset({0x0300})
SNAPSHOT_COMMANDS = frozenset({0x0200})


@dataclass
class PowerBankState(DeviceState):
    state_code: Optional[int] = None
    thermal_state: Optional[int] = None

    battery_percent: Optional[float] = None
    charging: Optional[bool] = None
    time_left_hours: Optional[int] = None
    time_left_minutes: Optional[int] = None

    input_power_w: Optional[float] = None
    output_power_w: Optional[float] = None
    dock: Optional[PortReading] = None

    temperature_1_c: Optional[int] = None
    temperature_2_c: Optional[int] = None

    pomodoro_seconds: Optional[int] = None
    pomodoro_enabled: Optional[bool] = None

    def to_dict(self) -> dict:
        data = super().to_dict()
        data.update(
            {
                "state_code": self.state_code,
                "thermal_state": self.thermal_state,
                "thermal_limited": self.thermal_limited,
                "battery_percent": self.battery_percent,
                "charging": self.charging,
                "time_left_hours": self.time_left_hours,
                "time_left_minutes": self.time_left_minutes,
                "input_power_w": self.input_power_w,
                "output_power_w": self.output_power_w,
                "dock": self.dock.to_dict() if self.dock else None,
                "temperature_1_c": self.temperature_1_c,
                "temperature_2_c": self.temperature_2_c,
                "pomodoro_seconds": self.pomodoro_seconds,
                "pomodoro_enabled": self.pomodoro_enabled,
            }
        )
        return data

    @property
    def thermal_limited(self) -> bool:
        """True while the bank refuses to charge because it is too hot."""
        return self.thermal_state == 2

    @property
    def time_left_text(self) -> Optional[str]:
        if self.time_left_hours is None:
            return None
        return f"{self.time_left_hours}h{self.time_left_minutes:02d}m"


def _port(name: str, body: bytes) -> PortReading:
    """Decode a port block: [mode, V, A, W] then, on the C ports, a tail."""
    reading = PortReading(name=name, raw=body.hex().upper())
    if len(body) < 7:
        return reading
    reading.mode = body[0]
    reading.direction = DIRECTION.get(body[0])
    reading.voltage_v = tenths(body, 1)
    reading.current_a = tenths(body, 3)
    # Sticky: an idle port keeps the last power it carried rather than resetting,
    # so this means nothing unless the mode byte is non-zero. (It does reset on
    # reboot — a firmware update cleared every port's value.)
    reading.power_w = tenths(body, 5)
    if len(body) >= 9:
        # [7] tracks the source role: 02 while outputting, FF while drawing in or
        # attached but idle. [8] is an attach flag: 07 with a cable in the port,
        # 00 empty — the only way to tell "cable attached, nothing negotiated"
        # from "nothing plugged in". The A port's 8-byte block has neither.
        reading.attached = body[8] == 0x07
    return reading


def parse_realtime(payload: bytes, state: PowerBankState) -> PowerBankState:
    """Decode one 0x0300 payload, leaving untouched fields alone."""
    fields: dict[int, bytes] = {}
    for tlv_type, value in ff09.parse_tlv(payload, ff09.tlv_offset(payload)):
        fields[tlv_type] = ff09.read_typed_value(value).payload

    def body(tlv_type: int) -> Optional[bytes]:
        return fields.get(tlv_type)

    if (raw := body(TLV_STATE)) is not None and raw:
        state.state_code = raw[0]
    if (raw := body(TLV_THERMAL_STATE)) is not None and raw:
        state.thermal_state = raw[0]
    if (raw := body(TLV_BATTERY)) is not None:
        state.battery_percent = decimal_pair(raw)
    if (raw := body(TLV_TIME_LEFT)) is not None and len(raw) >= 3:
        state.time_left_hours, state.time_left_minutes = raw[1], raw[2]
    if (raw := body(TLV_INPUT_TOTAL)) is not None:
        state.input_power_w = tenths(raw, 1)
    if (raw := body(TLV_OUTPUT_TOTAL)) is not None:
        state.output_power_w = tenths(raw, 1)
    if (raw := body(TLV_DOCK)) is not None:
        state.dock = _port("DOCK", raw)

    # Direction comes from the total-input field, which has matched the active
    # port's own reading in every capture. 0xA4 is thermal, not directional, and
    # the dock block goes stale — neither can be used for this.
    state.charging = (state.input_power_w or 0) > 0.05

    ports = []
    for name, tlv_type in (("C1", TLV_PORT_C1), ("C2", TLV_PORT_C2), ("A ", TLV_PORT_A)):
        if (raw := body(tlv_type)) is not None:
            ports.append(_port(name, raw))
    if ports:
        state.ports = ports

    if (raw := body(TLV_TEMP_1)) is not None and raw:
        state.temperature_1_c = raw[0]
    if (raw := body(TLV_TEMP_2)) is not None and raw:
        state.temperature_2_c = raw[0]

    for tlv_type, raw in fields.items():
        if tlv_type not in _KNOWN:
            state.unknown[f"0x{tlv_type:02X}"] = raw.hex().upper()
    return state


_KNOWN = frozenset(
    {
        TLV_STATE, TLV_BATTERY, TLV_TIME_LEFT, TLV_THERMAL_STATE, TLV_INPUT_TOTAL,
        TLV_OUTPUT_TOTAL, TLV_DOCK, TLV_PORT_C1, TLV_PORT_C2, TLV_PORT_A,
        TLV_TEMP_1, TLV_TEMP_2, 0xFE,
    }
)


def parse_snapshot(payload: bytes, state: PowerBankState) -> PowerBankState:
    """Decode a 0x0200 settings snapshot.

    The realtime block sits at a +0x0D TLV offset inside this frame, so it is
    shifted back and run through the same decoder rather than duplicated.
    """
    shifted = bytearray()
    extras: dict[str, str] = {}
    for tlv_type, value in ff09.parse_tlv(payload, ff09.tlv_offset(payload)):
        if tlv_type == SNAPSHOT_BATTERY:
            mapped = TLV_BATTERY
        elif tlv_type == SNAPSHOT_TIME_LEFT:
            mapped = TLV_TIME_LEFT
        elif tlv_type == SNAPSHOT_STATE:
            mapped = TLV_STATE
        elif tlv_type == SNAPSHOT_POMODORO_ENABLE:
            decoded = ff09.read_typed_value(value)
            if len(decoded.payload) >= 3:
                state.pomodoro_enabled = bool(decoded.payload[0])
                state.pomodoro_seconds = int.from_bytes(decoded.payload[1:3], "little")
            continue
        elif tlv_type == SNAPSHOT_POMODORO_SECONDS:
            decoded = ff09.read_typed_value(value)
            if decoded.u is not None:
                state.pomodoro_seconds = decoded.u
            continue
        elif 0xB2 <= tlv_type <= 0xBE:
            mapped = tlv_type - SNAPSHOT_SHIFT
        else:
            extras[f"0x{tlv_type:02X}"] = value.hex().upper()
            continue
        shifted += bytes([mapped, len(value)]) + value
    parse_realtime(bytes(shifted), state)
    state.unknown.update({f"snapshot {k}": v for k, v in extras.items()})
    return state


def parse_identity(payload: bytes, state: PowerBankState) -> None:
    from .device import parse_identity_0029

    parse_identity_0029(
        payload, state, lambda p: ff09.parse_tlv(p, ff09.tlv_offset(p))
    )


def format_state(state: PowerBankState) -> str:
    lines = [state.header()]

    battery = (
        f"{state.battery_percent:.2f}%" if state.battery_percent is not None else "?"
    )
    row = f"  battery {battery}"
    if state.time_left_text:
        row += f"   time left {state.time_left_text}"
    if state.charging is not None:
        row += f"   charging={state.charging}"
    if state.thermal_limited:
        row += "   THERMALLY LIMITED"
    lines.append(row)

    lines.append(
        f"  in {state.input_power_w or 0:.1f}W    out {state.output_power_w or 0:.1f}W"
    )
    # The dock only prints while live: like the port blocks it goes stale rather
    # than resetting, and would otherwise read as an active input forever.
    dock = [state.dock] if (state.dock and state.dock.active) else []
    for port in dock + state.ports:
        lines.append(f"    {port.name}: {port.summary()}")

    if state.pomodoro_seconds:
        flag = "" if state.pomodoro_enabled is None else (
            " (on)" if state.pomodoro_enabled else " (off)"
        )
        lines.append(f"  pomodoro {state.pomodoro_seconds // 60} min{flag}")
    temps = [t for t in (state.temperature_1_c, state.temperature_2_c) if t is not None]
    if temps:
        lines.append("  temps " + " / ".join(f"{t}C" for t in temps))
    return "\n".join(lines)


PROFILE = DeviceProfile(
    key="powerbank",
    name="Anker Prime Power Bank (20K, 220W) — A110G",
    # The bank advertises its bare serial with no stable prefix, so scanning
    # falls back to the ff09 service UUID.
    name_prefix=None,
    needs_account_id=True,
    needs_realtime_probe=False,
    new_state=PowerBankState,
    parse_realtime=parse_realtime,
    parse_snapshot=parse_snapshot,
    parse_identity=parse_identity,
    format_state=format_state,
    realtime_commands=REALTIME_COMMANDS,
    snapshot_commands=SNAPSHOT_COMMANDS,
)
