"""Anker Prime 160W charger — model A2687 — telemetry decoding.

Ported from a Python service that in turn came from `AnkerPrimeWebBle_A2687.js`
(https://github.com/Hyper-Beast/Anker_Prime_160W_WebBLE), reshaped here to the
same interface the power bank decoder uses so one CLI can drive either. The
cable-capability and charging-protocol tables below are that project's.

Mostly telemetry. The handshake and `0x0200` status read are the usual writes.
Screensaver writes: `0x021F` selects, `0x0220` starts a pixel transfer, `0x0221`
sends 156-byte JPEG chunks.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Optional

from . import ff09
from .device import DeviceProfile, DeviceState, PortReading, thousandths

DEVICE_NAME_PREFIX = "ASHDJW"

# Commands whose payload is a status snapshot rather than live per-port data.
SNAPSHOT_COMMANDS = frozenset({0x0200, 0x0A00, 0x0040, 0x0405})
REALTIME_COMMANDS = frozenset({0x020A, 0x0207, 0x0206, 0x4300, 0x0300, 0x0303, 0x0410})
CMD_SET_SCREENSAVER = 0x021F
CMD_TRANSFER_START = 0x0220
CMD_TRANSFER_DATA = 0x0221
SCREENSAVER_TYPE_CUSTOM = 3
SCREENSAVER_CHUNK = 156
SCREENSAVER_ACK_EVERY = 10
_SCREENSAVER_URL_KEY = b"\x00SmallChargingUrl"

_PORT_STRUCT_TYPES = {0xA5: "C1", 0xA6: "C2", 0xA7: "C3"}
_PORT_CABLE_TYPES = {0xAC: "C1", 0xAD: "C2", 0xAE: "C3"}
_PORT_ORDER = ("C1", "C2", "C3")

# --- Cable mappings --------------------------------------------------------

CABLE_PROFILES: dict[str, tuple[str, Optional[str]]] = {
    "0100": ("5A-100W MAX", None),
    "0200": ("EPR-240W MAX", None),
    "0201": ("EPR-240W MAX", "Apple PD Fast Charging"),
}
CABLE_CAPABILITY_LABELS = {
    "00": "3A-60W MAX",
    "01": "5A-100W MAX",
    "02": "EPR-240W MAX",
}
CHARGING_INFO_LABELS = {
    "01": "Apple PD Fast Charging",
    "02": "Samsung Fast Charging",
    "03": "Samsung Super Fast Charging",
}

# --- Connected-device identity ---------------------------------------------
#
# INFERRED from captured traffic. TLV 0xB4 is 12 bytes = 3 ports x (VID u16 LE,
# PID u16 LE). On the capture that established this, C2 held an Apple device and
# decoded to VID 0x05AC while C1 had no cable and read as the 0xFFFA/0xFFFB
# sentinel. Provisional until more devices are sampled.
#
# Worth contrasting with the power bank, whose equivalent slot stays all-0xFF
# even with a 90 W laptop attached — that firmware reports no device identity.
PORT_IDENTITY_TLV = 0xB4
PORT_BRAND_MODEL_TLV = 0xB5

_IDENTITY_SENTINELS = {0x0000, 0xFFFA, 0xFFFB, 0xFFFC, 0xFFFD, 0xFFFE, 0xFFFF}

USB_VENDOR_NAMES = {
    0x05AC: "Apple",
    0x04E8: "Samsung",
    0x2717: "Xiaomi",
    0x12D1: "Huawei",
    0x18D1: "Google",
    0x1004: "LG",
    0x0451: "Texas Instruments",
    0x291A: "Anker",
    0x03F0: "HP",
    0x413C: "Dell",
    0x17EF: "Lenovo",
    0x045E: "Microsoft",
    0x0B05: "ASUS",
    0x0DB0: "MSI",
    0x1532: "Razer",
}

# (VID, PID) -> the label the official Anker app shows. Built by observation:
# attach a device, read the PID here, note what the app prints. An unknown PID
# falls back to the vendor name plus the raw value — never a guess.
DEVICE_MODEL_NAMES = {
    (0x05AC, 0x7519): "iPhone 17 series",
    (0x05AC, 0x7319): "MacBook Pro series",
    (0x05AC, 0x7117): "iPad Pro series",
    (0x291A, 0x110B): "20K Prime Power Bank",
}

# Brand codes from the app 3.18.0 table. The matching 85-entry model-code table
# was not recovered, so a model code can be reported numerically but not named.
BRAND_CODES = {
    0x01: "Apple", 0x02: "Samsung", 0x03: "Xiaomi", 0x04: "Huawei", 0x05: "Google",
    0x06: "LG", 0x07: "IDT", 0x08: "TI", 0x09: "YBZ", 0x0A: "Anker", 0x0B: "Honor",
    0x0C: "HP", 0x0D: "Dell", 0x0E: "Lenovo", 0x0F: "Microsoft", 0x10: "ASUS",
    0x11: "ASUS", 0x12: "MSI", 0x13: "Razer",
}

LEGACY_FIELD_NAMES = {
    0xA1: "state_code",
    0xA2: "serial_or_identifier",
    0xA4: "product_code",
    0xA9: "screen_brightness",
    0xD0: "port_config_0",
    0xD1: "port_config_1",
    0xE1: "screensaver",
    0xFD: "firmware_tag",
}


@dataclass
class ChargerState(DeviceState):
    product_code: Optional[str] = None
    firmware_tag: Optional[str] = None
    screen_brightness: Optional[int] = None
    screensaver_id: Optional[int] = None
    screensaver_flags: Optional[int] = None
    total_output_power_w: Optional[float] = None
    raw_status: dict[str, str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.raw_status is None:
            self.raw_status = {}
        if not self.ports:
            self.ports = [PortReading(name=key) for key in _PORT_ORDER]

    def to_dict(self) -> dict:
        data = super().to_dict()
        data.update(
            {
                "product_code": self.product_code,
                "firmware_tag": self.firmware_tag,
                "screen_brightness": self.screen_brightness,
                "screensaver_id": self.screensaver_id,
                "screensaver_flags": self.screensaver_flags,
                "total_output_power_w": self.total_output_power_w,
            }
        )
        return data

    def port(self, key: str) -> PortReading:
        for reading in self.ports:
            if reading.name == key:
                return reading
        reading = PortReading(name=key)
        self.ports.append(reading)
        return reading


def screensaver_id_from_payload(payload: bytes) -> Optional[int]:
    """Cloud picture id from a 0x0200 / 0x0300 body (TLV 0xE1, bytes 2–3 u16le)."""
    for tlv_type, value in ff09.parse_tlv(payload, ff09.tlv_offset(payload)):
        if tlv_type != 0xE1:
            continue
        body = ff09.read_typed_value(value).payload
        if len(body) >= 4:
            return struct.unpack_from("<H", body, 2)[0]
    return None


def screensaver_select_tlv(picture_id: int, hash_code: int, saver_type: int = SCREENSAVER_TYPE_CUSTOM) -> list[tuple[int, bytes]]:
    """Official 47-byte 0x021F body that selects an already-uploaded custom picture.

    Recovered by XORing same-session official writes against the known 0x0300
    header: A1=0x21, A3=typed-u8 type (3=custom), A4=typed-bytes id u32le,
    A5=typed-bytes hash_code u32le, FD=text "SmallChargingUrl", FE=typed-u32
    epoch. Slot index is not sent.
    """
    return [
        (0xA1, b"\x21"),
        (0xA3, bytes([0x01, saver_type & 0xFF])),
        (0xA4, b"\x04" + struct.pack("<I", picture_id & 0xFFFFFFFF)),
        (0xA5, b"\x04" + struct.pack("<I", hash_code & 0xFFFFFFFF)),
        (0xFD, _SCREENSAVER_URL_KEY),
        (0xFE, b"\x03" + ff09.epoch_bytes()),
    ]


def screensaver_transfer_start_tlv(
    picture_id: int,
    hash_code: int,
    file_size: int,
    *,
    chunk_size: int = SCREENSAVER_CHUNK,
    chunk_count: int,
    ack_every: int = SCREENSAVER_ACK_EVERY,
) -> list[tuple[int, bytes]]:
    """Official 49-byte 0x0220 body that starts a custom-cover pixel transfer.

    Recovered from add-cover-0819.pklg by XORing the same-session 0x021F
    (known 47-byte layout) onto the 75-byte 0x0220. A2=1, A3=id, A4=hash,
    A5=JPEG size, A6=ACK every N chunks, A7=chunk payload, A8=chunk count.
    """
    return [
        (0xA1, b"\x21"),
        (0xA2, bytes([0x01, 0x01])),
        (0xA3, b"\x04" + struct.pack("<I", picture_id & 0xFFFFFFFF)),
        (0xA4, b"\x04" + struct.pack("<I", hash_code & 0xFFFFFFFF)),
        (0xA5, b"\x03" + struct.pack("<I", file_size & 0xFFFFFFFF)),
        (0xA6, bytes([0x01, ack_every & 0xFF])),
        (0xA7, bytes([0x02]) + struct.pack("<H", chunk_size & 0xFFFF)),
        (0xA8, bytes([0x02]) + struct.pack("<H", chunk_count & 0xFFFF)),
        (0xFE, b"\x03" + ff09.epoch_bytes()),
    ]


def screensaver_chunk_tlv(seq: int, data: bytes) -> list[tuple[int, bytes]]:
    """Official 167-byte 0x0221 body: A2=seq u16le, A3=0x04-tagged JPEG slice.

    First official chunk starts `FF D8 FF E0 … JFIF`. Last chunk is zero-padded
    to 156 bytes so every frame stays 193 bytes on the wire.
    """
    if len(data) > SCREENSAVER_CHUNK:
        raise ValueError(f"screensaver chunk {len(data)} > {SCREENSAVER_CHUNK}")
    payload = data + bytes(SCREENSAVER_CHUNK - len(data))
    return [
        (0xA1, b"\x21"),
        (0xA2, bytes([0x02]) + struct.pack("<H", seq & 0xFFFF)),
        (0xA3, b"\x04" + payload),
    ]


def screensaver_chunks(jpeg: bytes, chunk_size: int = SCREENSAVER_CHUNK) -> list[bytes]:
    if not jpeg:
        raise ValueError("empty JPEG")
    return [jpeg[i : i + chunk_size] for i in range(0, len(jpeg), chunk_size)]


def cable_profile(code: Optional[str]) -> Optional[tuple[str, Optional[str]]]:
    if not code:
        return None
    if code in CABLE_PROFILES:
        return CABLE_PROFILES[code]
    normalized = code.upper()
    if len(normalized) != 4 or any(c not in "0123456789ABCDEF" for c in normalized):
        return None
    label = CABLE_CAPABILITY_LABELS.get(normalized[0:2])
    if not label:
        return None
    return label, CHARGING_INFO_LABELS.get(normalized[2:4])


def _apply_cable(port: PortReading, decoded: Optional[ff09.TypedValue]) -> None:
    code = None
    if decoded and decoded.tag == 0x04 and len(decoded.payload) >= 2:
        code = f"{decoded.payload[-2]:02X}{decoded.payload[-1]:02X}"
    connected = port.active
    if not code:
        port.cable = "Connected" if connected else "N/A"
    elif code.upper().startswith("03"):
        port.cable = "N/A"
    else:
        profile = cable_profile(code)
        port.cable = profile[0] if profile else f"UNKNOWN ({code})"
    if connected:
        profile = cable_profile(code)
        port.charging_info = (profile[1] if profile else None) or "N/A"
    else:
        port.charging_info = "N/A"


def _apply_identity(fields: dict[int, ff09.TypedValue], state: ChargerState) -> bool:
    """Decode the per-port connected-device identity blocks (0xB4 / 0xB5)."""
    changed = False

    decoded = fields.get(PORT_IDENTITY_TLV)
    if decoded and decoded.tag == 0x04 and len(decoded.payload) >= 12:
        for index, key in enumerate(_PORT_ORDER):
            slot = decoded.payload[index * 4 : index * 4 + 4]
            vid, pid = struct.unpack("<HH", slot)
            port = state.port(key)
            if vid in _IDENTITY_SENTINELS:
                port.vendor = port.device_name = None
            else:
                product = None if pid in _IDENTITY_SENTINELS else pid
                port.vendor = USB_VENDOR_NAMES.get(vid) or f"VID {vid:04X}"
                port.device_name = DEVICE_MODEL_NAMES.get((vid, product))
            changed = True

    decoded = fields.get(PORT_BRAND_MODEL_TLV)
    if decoded and decoded.tag == 0x04 and len(decoded.payload) >= 12:
        for index, key in enumerate(_PORT_ORDER):
            slot = decoded.payload[index * 4 : index * 4 + 4]
            port = state.port(key)
            if not (all(b == 0xFF for b in slot) or not any(slot)):
                brand = BRAND_CODES.get(slot[0])
                if brand and not port.vendor:
                    port.vendor = brand
            changed = True

    return changed


def parse_realtime(payload: bytes, state: ChargerState) -> ChargerState:
    """Decode a 0x020A-family payload.

    The A2687 packs per-port telemetry into type-0x04 structs of
    [mode, voltage_mV, current_mA, power_cW] — note the scales differ from the
    power bank, which uses tenths for all three.
    """
    fields: dict[int, ff09.TypedValue] = {}
    for tlv_type, value in ff09.parse_tlv(payload, ff09.tlv_offset(payload)):
        fields[tlv_type] = ff09.read_typed_value(value)

    total = 0.0
    saw_port = False
    for tlv_type, key in _PORT_STRUCT_TYPES.items():
        decoded = fields.get(tlv_type)
        if not decoded or decoded.tag != 0x04 or len(decoded.payload) < 7:
            continue
        body = decoded.payload
        port = state.port(key)
        port.raw = body.hex().upper()
        port.mode = body[0]
        port.direction = "out" if body[0] else None
        port.voltage_v = thousandths(body, 1)
        port.current_a = thousandths(body, 3)
        port.power_w = (body[5] | (body[6] << 8)) / 100.0
        if body[0]:
            total += port.power_w
        saw_port = True
    if saw_port:
        state.total_output_power_w = round(total, 2)

    for tlv_type, key in _PORT_CABLE_TYPES.items():
        if tlv_type in fields:
            _apply_cable(state.port(key), fields[tlv_type])

    _apply_identity(fields, state)

    e1 = fields.get(0xE1)
    if e1 and len(e1.payload) >= 4:
        state.screensaver_flags = struct.unpack_from("<H", e1.payload, 0)[0]
        state.screensaver_id = struct.unpack_from("<H", e1.payload, 2)[0]
    return state


def parse_snapshot(payload: bytes, state: ChargerState) -> ChargerState:
    """Decode the 0x0200 snapshot — identifiers plus raw settings."""
    snapshot: dict[str, str] = {}
    for tlv_type, value in ff09.parse_tlv(payload, ff09.tlv_offset(payload)):
        decoded = ff09.read_typed_value(value)
        name = LEGACY_FIELD_NAMES.get(tlv_type, f"field_0x{tlv_type:02X}")
        if decoded.text is not None:
            snapshot[name] = decoded.text
        elif decoded.u is not None:
            snapshot[name] = str(decoded.u)
        else:
            snapshot[name] = value.hex().upper()

        if tlv_type == 0xA2 and decoded.text:
            state.serial = state.serial or decoded.text
        elif tlv_type == 0xA4 and decoded.text:
            state.product_code = decoded.text
        elif tlv_type == 0xFD and decoded.text:
            state.firmware_tag = decoded.text
        elif tlv_type == 0xA9 and decoded.u is not None:
            state.screen_brightness = decoded.u
    state.raw_status = snapshot

    # The snapshot also carries live per-port structs on this firmware, which
    # matters because without an account ID it is the only frame the charger
    # sends. The service this decoder came from deliberately skipped ports here;
    # a capture disagreed. 0xA5 decoded to 20.08 V x 4.444 A = 89.2 W against a
    # reported 89.15 W, while 0xB4 named an Apple laptop on that same port and
    # 0xAC reported an EPR-240W cable doing Apple PD fast charging. Three
    # independent TLVs agreeing is enough to trust the struct.
    parse_realtime(payload, state)
    return state


def parse_identity(payload: bytes, state: ChargerState) -> None:
    from .device import parse_identity_0029

    def walk(data: bytes):
        offset = 0
        if data and data[0] == 0x00:
            offset = 1
        elif len(data) > 5 and data[0] == 0x03 and data[1] == 0x00:
            offset = 6
        return ff09.parse_tlv(data, offset)

    parse_identity_0029(payload, state, walk)


def format_state(state: ChargerState) -> str:
    lines = [state.header()]
    if state.product_code:
        lines.append(f"  product {state.product_code}")
    if state.screen_brightness is not None:
        lines.append(f"  brightness {state.screen_brightness}%")
    if state.screensaver_id is not None:
        flags = state.screensaver_flags if state.screensaver_flags is not None else 0
        lines.append(f"  screensaver id {state.screensaver_id} flags 0x{flags:04X}")
    if state.total_output_power_w is not None:
        lines.append(f"  total out {state.total_output_power_w:.1f}W")
    for port in state.ports:
        lines.append(f"    {port.name}: {port.summary()}")
    return "\n".join(lines)


PROFILE = DeviceProfile(
    key="charger",
    name="Anker Prime 160W charger — A2687",
    name_prefix=DEVICE_NAME_PREFIX,
    # Without an Anker account ID on 0x0027 this firmware never starts its
    # pushed stream, and 0x020A cannot be built at all.
    needs_account_id=True,
    needs_realtime_probe=True,
    new_state=ChargerState,
    parse_realtime=parse_realtime,
    parse_snapshot=parse_snapshot,
    parse_identity=parse_identity,
    format_state=format_state,
    realtime_commands=REALTIME_COMMANDS,
    snapshot_commands=SNAPSHOT_COMMANDS,
)
