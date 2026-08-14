"""Decoding aids for an FF09 device whose field layout is not yet known.

Device-agnostic on purpose: this is what you reach for before a decoder exists
for a device, and what you go back to when one of its fields turns out to be
wrong.

Nothing in here claims to know what a power bank TLV *means* — that is the whole
point. It turns a decrypted payload into every plausible reading of every field
so a human can spot which reading tracks reality, and it remembers values across
frames so a field that moves when the battery moves stands out.

Pure logic, no BLE. `replay` re-runs it over a recorded capture, so decoding can
be iterated on without the hardware in the room.
"""

from __future__ import annotations

import datetime as _dt
import struct
from dataclasses import dataclass, field
from typing import Optional

from . import ff09 as p

# Anything outside this stays raw hex rather than being guessed at as text.
_PRINTABLE = set(range(0x20, 0x7F))

# Plausible Unix-epoch window for a u32 field (2017-07 .. 2049-04). Frames carry
# timestamps in TLV 0xFE and it helps to recognize them on sight.
_EPOCH_MIN = 1_500_000_000
_EPOCH_MAX = 2_500_000_000


def _is_text(data: bytes) -> bool:
    if not data or len(data) < 2:
        return False
    body = data.rstrip(b"\x00")
    if not body:
        return False
    return all(b in _PRINTABLE for b in body)


def _u16s(data: bytes) -> list[int]:
    n = len(data) // 2
    return list(struct.unpack_from(f"<{n}H", data)) if n else []


def _scaled(value: int) -> str:
    """The scalings Anker actually uses on this protocol family, side by side."""
    return (
        f"/10={value / 10:g} /100={value / 100:g} /1000={value / 1000:g}"
    )


def readings(data: bytes) -> list[str]:
    """Every plausible interpretation of one raw TLV value, most useful first."""
    out: list[str] = []
    if not data:
        return ["(empty)"]

    if _is_text(data):
        out.append(f'text "{data.rstrip(chr(0).encode()).decode("ascii")}"')

    if len(data) == 1:
        out.append(f"u8={data[0]} i8={struct.unpack('<b', data)[0]}")
        if data[0] <= 100:
            out.append(f"pct?={data[0]}%")
    elif len(data) == 2:
        u = struct.unpack("<H", data)[0]
        out.append(f"u16le={u} ({_scaled(u)}) u16be={struct.unpack('>H', data)[0]}")
    elif len(data) == 4:
        u = struct.unpack("<I", data)[0]
        line = f"u32le={u}"
        if _EPOCH_MIN <= u <= _EPOCH_MAX:
            stamp = _dt.datetime.fromtimestamp(u).isoformat(sep=" ", timespec="seconds")
            line += f"  epoch={stamp}"
        out.append(line)
        out.append(f"2xu16le={_u16s(data)}")
    elif len(data) == 6 and not _is_text(data):
        out.append("mac?=" + ":".join(f"{b:02X}" for b in data))

    if len(data) > 2 and len(data) % 2 == 0 and len(data) != 4:
        out.append(f"u16le[]={_u16s(data)}")

    # Both known devices pack per-port telemetry as a leading mode byte followed
    # by u16 fields, so an odd-length blob is very often that. Worth showing the
    # raw words alongside the scaling that would make them volts/amps/watts,
    # since it is the first layout to check on anything new.
    if len(data) >= 5 and len(data) % 2 == 1:
        words = _u16s(data[1:])
        out.append(f"flag={data[0]} then u16le[]={words}")
        if len(words) >= 3:
            out.append(
                f"  charger scale: {words[0] / 1000:g}V {words[1] / 1000:g}A "
                f"{words[2] / 100:g}W  |  bank scale: {words[0] / 10:g}V "
                f"{words[1] / 10:g}A {words[2] / 10:g}W"
            )

    if 2 < len(data) <= 16:
        out.append(f"u8[]={list(data)}")

    out.append("hex=" + data.hex().upper())
    return out


@dataclass
class FieldView:
    """One TLV of a decrypted payload, with the type tag peeled off if present."""

    tlv_type: int
    raw: bytes
    tag: Optional[int] = None
    body: bytes = b""
    lines: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        return f"0x{self.tlv_type:02X}"


# The A2687 wraps most values in a one-byte type tag (see read_typed_value).
# 0x04 is the tag its per-port telemetry structs use.
_TAG_NAMES = {
    0x00: "str",
    0x01: "u8",
    0x02: "u16",
    0x03: "u32",
    0x04: "struct",
}


def view_payload(payload: bytes) -> list[FieldView]:
    """Split a decrypted payload into TLVs and annotate each one."""
    views: list[FieldView] = []
    for tlv_type, value in p.parse_tlv(payload, p.tlv_offset(payload)):
        view = FieldView(tlv_type=tlv_type, raw=value)
        # Only treat the first byte as a type tag when it looks like one and the
        # remaining length is consistent; otherwise decode the value whole.
        if value and value[0] in _TAG_NAMES:
            view.tag = value[0]
            view.body = value[1:]
        else:
            view.body = value
        view.lines = readings(view.body)
        views.append(view)
    return views


def format_payload(payload: bytes, indent: str = "    ") -> str:
    """Human-readable TLV tree for one decrypted payload."""
    views = view_payload(payload)
    if not views:
        return f"{indent}(no TLVs) hex={payload.hex().upper()}"
    lines = []
    for view in views:
        tag = (
            f" tag={_TAG_NAMES.get(view.tag, '?')}({view.tag:02X})"
            if view.tag is not None
            else ""
        )
        lines.append(f"{indent}{view.key}{tag} len={len(view.body)}")
        for reading in view.lines:
            lines.append(f"{indent}    {reading}")
    return "\n".join(lines)


@dataclass
class Change:
    command: int
    key: str
    before: bytes
    after: bytes

    def __str__(self) -> str:
        before = self.before.hex().upper() or "-"
        after = self.after.hex().upper()
        note = "  ".join(readings(self.after)[:1])
        return f"0x{self.command:04X} {self.key}: {before} -> {after}   {note}"


class Tracker:
    """Remembers the last value of every (command, TLV) so movement is visible.

    Differential capture is the actual decoding technique here: plug a phone in,
    watch which field jumps. A field that never moves is a constant; a field that
    moves with the battery is the battery.
    """

    def __init__(self) -> None:
        self._seen: dict[tuple[int, str], bytes] = {}
        self.frames = 0

    def feed(self, command: int, payload: bytes) -> list[Change]:
        self.frames += 1
        changes: list[Change] = []
        for view in view_payload(payload):
            slot = (command, view.key)
            previous = self._seen.get(slot)
            if previous != view.body:
                if previous is not None:
                    changes.append(Change(command, view.key, previous, view.body))
                self._seen[slot] = view.body
        return changes

    def table(self) -> str:
        """Everything seen so far, as a stable command/TLV table."""
        lines = []
        for (command, key), value in sorted(self._seen.items()):
            first = readings(value)[0]
            lines.append(f"  0x{command:04X} {key:>5}  {value.hex().upper():<32} {first}")
        return "\n".join(lines) or "  (nothing decoded yet)"
