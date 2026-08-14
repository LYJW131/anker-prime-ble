"""What the two devices have in common.

The charger and the power bank speak the same transport and differ only in what
they pack into their TLVs. Giving both the same shape — one `PortReading`, one
`DeviceProfile` — is what lets a single CLI drive either without caring which is
on the other end.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

# --- unit helpers ----------------------------------------------------------
#
# Both devices encode electrical values as u16 little-endian, but at different
# scales: the charger uses millivolts/milliamps/centiwatts, the power bank uses
# tenths throughout. Keeping both here makes the difference explicit at the call
# site instead of hiding it behind a magic divisor.


def u16(data: bytes, offset: int = 0) -> Optional[int]:
    if len(data) < offset + 2:
        return None
    return struct.unpack_from("<H", data, offset)[0]


def tenths(data: bytes, offset: int = 0) -> Optional[float]:
    raw = u16(data, offset)
    return None if raw is None else raw / 10.0


def thousandths(data: bytes, offset: int = 0) -> Optional[float]:
    raw = u16(data, offset)
    return None if raw is None else raw / 1000.0


def hundredths(data: bytes, offset: int = 0) -> Optional[float]:
    raw = u16(data, offset)
    return None if raw is None else raw / 100.0


def decimal_pair(data: bytes) -> Optional[float]:
    """[integer, remainder-of-100] -> float.

    The power bank uses this for battery percentage and time remaining. It is
    not a u16 — reading it as one yields a large number that looks plausible and
    is wrong, which is exactly why it gets a named function.
    """
    if len(data) < 2 or data[1] > 99:
        return None
    return data[0] + data[1] / 100.0


# --- shared shapes ---------------------------------------------------------


@dataclass
class PortReading:
    """One physical port on either device.

    Not every field applies to every device: the charger reports cable ratings
    and what is plugged in, the power bank reports direction and attach state.
    Whatever a device does not send stays None rather than being invented.
    """

    name: str
    mode: int = 0
    voltage_v: Optional[float] = None
    current_a: Optional[float] = None
    power_w: Optional[float] = None

    # Power bank: 1 = sourcing, 2 = drawing in, 0 = idle.
    direction: Optional[str] = None
    # Power bank: a cable is present, negotiated or not.
    attached: bool = False
    # Charger: cable capability and fast-charge protocol, decoded from its TLVs.
    cable: Optional[str] = None
    charging_info: Optional[str] = None
    # Charger: what is plugged in, when the firmware reports it.
    device_name: Optional[str] = None
    vendor: Optional[str] = None

    raw: str = ""

    @property
    def active(self) -> bool:
        """Power is flowing.

        The mode byte is the only trustworthy signal. Do not add a
        `power_w > 0` fallback here, however reasonable it looks: on the power
        bank that field is sticky and keeps its last value after a port goes
        idle, so the fallback renders an idle port as `0.0V 0.0A 7.9W`. That
        exact regression was introduced here once and caught on hardware.
        """
        return self.mode != 0

    @property
    def energized(self) -> bool:
        """Rail up, nothing drawing — how the power bank's trickle mode looks."""
        return not self.active and bool(self.voltage_v and self.voltage_v > 0.1)

    def summary(self) -> str:
        if self.active:
            arrow = ""
            if self.direction:
                arrow = f" {self.direction}"
            line = (
                f"{self.voltage_v or 0:.1f}V {self.current_a or 0:.1f}A "
                f"{self.power_w or 0:.1f}W{arrow}"
            )
            for extra in (self.device_name or self.vendor, self.cable, self.charging_info):
                if extra and extra != "N/A":
                    line += f"  {extra}"
            return line
        if self.energized:
            return f"energized, nothing drawing ({self.voltage_v:.1f}V)"
        if self.attached:
            return f"attached, no power ({self.voltage_v or 0:.1f}V, no PD contract)"
        return "idle"


@dataclass
class DeviceState:
    """Fields both devices report. Each device subclasses this for its own."""

    serial: Optional[str] = None
    firmware: Optional[str] = None
    mac_address: Optional[str] = None
    # The 0x0029 string. On the power bank it reads "Charging" even mid-discharge,
    # so treat it as a fixed product label, never as live state.
    label: Optional[str] = None

    ports: list[PortReading] = field(default_factory=list)
    # Every TLV with no confirmed meaning, as raw hex, so a later capture can be
    # diffed against this one without going back to the radio.
    unknown: dict[str, str] = field(default_factory=dict)

    def header(self) -> str:
        text = self.serial or "(unknown device)"
        if self.firmware:
            text += f"  fw {self.firmware}"
        if self.label:
            text += f"  [{self.label}]"
        return text


@dataclass
class DeviceProfile:
    """Everything the CLI needs to drive one device type.

    Adding a third Anker Prime device means writing one of these plus a decoder;
    nothing in the transport, the session, or the CLI has to change.
    """

    key: str
    name: str
    # Advertised-name prefix, when the device has a stable one. Scanning falls
    # back to the ff09 service UUID, which the whole product line advertises.
    name_prefix: Optional[str]
    # The charger will not start its stream without an Anker account ID; the
    # power bank streams after the plain handshake.
    needs_user_id: bool
    new_state: Callable[[], Any]
    parse_realtime: Callable[[bytes, Any], Any]
    parse_snapshot: Callable[[bytes, Any], Any]
    parse_identity: Callable[[bytes, Any], None]
    format_state: Callable[[Any], str]
    realtime_commands: frozenset[int]
    snapshot_commands: frozenset[int]


def parse_identity_0029(payload: bytes, state: DeviceState, tlv_iter) -> None:
    """Pull serial / firmware / MAC out of the 0x0029 handshake reply.

    Both devices answer 0x0029 the same way, so this is shared. Fields are
    matched on content as well as TLV number because firmware revisions move
    them around.
    """
    for tlv_type, value in tlv_iter(payload):
        text = "".join(
            c for c in value.decode("ascii", errors="ignore") if c.isprintable()
        ).strip()
        if tlv_type == 0xA2 and text:
            state.label = text
        elif tlv_type == 0xA3 and _looks_like_firmware(text):
            state.firmware = text
        elif tlv_type == 0xA4 and _looks_like_serial(text):
            state.serial = text
        elif tlv_type == 0xA5 and len(value) >= 6:
            mac = _mac_from_bytes(value)
            if mac:
                state.mac_address = mac

        if not state.firmware and _looks_like_firmware(text):
            state.firmware = text
        if not state.serial and _looks_like_serial(text):
            state.serial = text


def _looks_like_firmware(text: str) -> bool:
    parts = text.lstrip("vV").split(".")
    return len(parts) >= 2 and all(p.isdigit() for p in parts if p != "")


def _looks_like_serial(text: str) -> bool:
    return (
        10 <= len(text) <= 30
        and all(c.isalnum() or c in "_-" for c in text)
        and not _looks_like_firmware(text)
    )


def _mac_from_bytes(value: bytes) -> Optional[str]:
    if len(value) < 6 or all(b == 0 for b in value[:6]):
        return None
    if all(0x20 <= b <= 0x7E for b in value[:2]):
        return None  # printable start — this is text, not an address
    return ":".join(f"{b:02X}" for b in value[:6])
