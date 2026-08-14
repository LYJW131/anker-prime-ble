"""The FF09 BLE transport shared across Anker Prime devices.

Framing, TLV bodies, AES-GCM, and the ECDH P-256 session handshake — everything
below the telemetry layer, and nothing above it. The power bank and the A2687
charger run this identically; only what they pack *inside* the TLVs differs, so
this module knows nothing about ports, batteries, or watts.

Derived from a Python port of `AnkerPrimeWebBle_A2687.js` written for the A2687
charger, reduced here to the parts that are device-independent.

Pure logic — no BLE, no I/O. `probe.py` supplies the transport.
"""

from __future__ import annotations

import struct
import time
from dataclasses import dataclass
from typing import Iterator, Optional

from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

# --- GATT ------------------------------------------------------------------

SERVICE_UUID = "8c850001-0302-41c5-b46e-cf057c562025"
WRITE_CHAR_UUID = "8c850002-0302-41c5-b46e-cf057c562025"
NOTIFY_CHAR_UUID = "8c850003-0302-41c5-b46e-cf057c562025"

# 16-bit 0xff09 expanded to the Bluetooth base UUID, as advertised. Anker uses
# it across the Prime line, so it identifies the family rather than a product.
ADVERTISED_SERVICE_UUID = "0000ff09-0000-1000-8000-00805f9b34fb"

# The A2687 charger's advertised name prefix, kept so a scan can tell the
# charger apart from everything else answering on ff09.
CHARGER_NAME_PREFIX = "ASHDJW"

# --- Crypto constants ------------------------------------------------------
#
# The initial key and IV are fixed in the firmware and identical on every unit;
# they only protect the handshake, which then swaps in an ECDH-derived session
# key. They are constants of the protocol, not anybody's secret.

INITIAL_KEY = bytes.fromhex("b8ff7422955d4eb6d554a2c470280559")
INITIAL_IV = bytes.fromhex("6ba3e3f2f3a60f2971ce5d1f")
GCM_AAD = bytes.fromhex("3322110077665544bbaa9988ffeeddcc")

CMD_0022_A3 = bytes.fromhex("808fffff")
CMD_0022_A5 = b"CST-8"
CMD_020A_A2 = bytes.fromhex("045553")

# --- Command groups / codes ------------------------------------------------

GROUP_SESSION = 0x01
GROUP_TELEMETRY = 0x0F

CMD_STATUS = 0x0200
CMD_REALTIME = 0x020A

FLAG_ENCRYPTED = 0x40
FLAG_ACK = 0x08


# --- TLV -------------------------------------------------------------------


def build_tlv(items: list[tuple[int, bytes]]) -> bytes:
    """[(type, value), ...] -> type|len|value concatenation."""
    out = bytearray()
    for tlv_type, value in items:
        if len(value) > 0xFF:
            raise ValueError(f"TLV 0x{tlv_type:02X} value too long: {len(value)}")
        out.append(tlv_type)
        out.append(len(value))
        out += value
    return bytes(out)


def parse_tlv(payload: bytes, offset: int = 0) -> Iterator[tuple[int, bytes]]:
    i = offset
    while i < len(payload) - 1:
        tlv_type = payload[i]
        length = payload[i + 1]
        if i + 2 + length > len(payload):
            return  # declared length runs past the packet; stop rather than guess
        yield tlv_type, payload[i + 2 : i + 2 + length]
        i += 2 + length


def tlv_offset(payload: bytes) -> int:
    """Decrypted payloads may carry a leading 0x00 status byte."""
    return 1 if payload and payload[0] == 0x00 else 0


@dataclass
class TypedValue:
    """A TLV value whose first byte is a type tag."""

    tag: int
    payload: bytes
    text: Optional[str] = None
    u: Optional[int] = None
    i: Optional[int] = None


def read_typed_value(raw: bytes) -> TypedValue:
    if not raw:
        return TypedValue(tag=0xFF, payload=b"")
    tag, payload = raw[0], raw[1:]
    out = TypedValue(tag=tag, payload=payload)
    if tag == 0x00:
        out.text = _ascii(payload).rstrip(".")
    elif tag == 0x01 and len(payload) >= 1:
        out.u = payload[0]
        out.i = struct.unpack_from("<b", payload)[0]
    elif tag == 0x02 and len(payload) >= 2:
        out.u = struct.unpack_from("<H", payload)[0]
        out.i = struct.unpack_from("<h", payload)[0]
    elif tag == 0x03 and len(payload) >= 4:
        out.u = struct.unpack_from("<I", payload)[0]
        out.i = struct.unpack_from("<i", payload)[0]
    return out


def _ascii(data: bytes) -> str:
    text = data.decode("utf-8", errors="replace")
    return "".join(c if 0x20 <= ord(c) < 0x7F else "." for c in text)


def epoch_bytes(offset_sec: int = 0) -> bytes:
    return struct.pack("<I", (int(time.time()) + offset_sec) & 0xFFFFFFFF)


# --- Framing ---------------------------------------------------------------


def _checksum(data: bytes) -> int:
    result = 0
    for byte in data:
        result ^= byte
    return result


def build_frame(group: int, command: int, ciphertext: bytes) -> bytes:
    """Wrap an encrypted body in the FF09 transport frame."""
    cmd_high = ((command >> 8) & 0xFF) | FLAG_ENCRYPTED
    cmd_low = command & 0xFF
    payload = bytes([0x03, 0x00, group, cmd_high, cmd_low]) + ciphertext
    total_len = len(payload) + 5
    message = b"\xff\x09" + struct.pack("<H", total_len) + payload
    return message + bytes([_checksum(message)])


@dataclass
class Frame:
    command: int
    encrypted: bool
    ack: bool
    body: bytes  # ciphertext when encrypted, else the raw remainder
    raw: bytes


def parse_frame(raw: bytes) -> Optional[Frame]:
    """Decode one complete FF09 frame. None if it is too short to classify."""
    if len(raw) < 10:
        return None
    payload = raw[4:-1]  # strip FF 09 len16 ... checksum
    if len(payload) < 5:
        return None
    cmd_high = payload[3]
    cmd_low = payload[4]
    command = ((cmd_high & ~(FLAG_ENCRYPTED | FLAG_ACK)) << 8) | cmd_low
    return Frame(
        command=command,
        encrypted=bool(cmd_high & FLAG_ENCRYPTED),
        ack=bool(cmd_high & FLAG_ACK),
        body=payload[5:],
        raw=raw,
    )


class FrameAssembler:
    """Reassembles FF09 frames from BLE notification chunks.

    Browser implementations assume one notification is one frame, which holds at
    the MTUs Chrome negotiates. CoreBluetooth and BlueZ can split them, so this
    buffers and cuts on the length field instead.
    """

    def __init__(self) -> None:
        self._buffer = bytearray()

    def feed(self, chunk: bytes) -> list[bytes]:
        self._buffer += chunk
        frames: list[bytes] = []
        while len(self._buffer) >= 4:
            if self._buffer[0] != 0xFF or self._buffer[1] != 0x09:
                del self._buffer[0]  # lost sync — drop a byte and retry
                continue
            frame_len = struct.unpack_from("<H", self._buffer, 2)[0]
            if frame_len < 10 or frame_len > 4096:
                del self._buffer[0]
                continue
            if len(self._buffer) < frame_len:
                break
            frames.append(bytes(self._buffer[:frame_len]))
            del self._buffer[:frame_len]
        return frames

    def reset(self) -> None:
        self._buffer.clear()


# --- Crypto ----------------------------------------------------------------


class CryptoContext:
    """AES-GCM context. Starts on the fixed initial key and switches to the
    ECDH-derived session key after the 0x0021 exchange."""

    def __init__(self) -> None:
        self._aesgcm = AESGCM(INITIAL_KEY)
        self._nonce = INITIAL_IV
        self.state = "Initial"

    def set_session(self, key: bytes, nonce: bytes) -> None:
        if len(key) != 16:
            raise ValueError(f"session key must be 16 bytes, got {len(key)}")
        if len(nonce) != 12:
            raise ValueError(f"session nonce must be 12 bytes, got {len(nonce)}")
        self._aesgcm = AESGCM(key)
        self._nonce = nonce
        self.state = "Session"

    def encrypt(self, plaintext: bytes) -> bytes:
        return self._aesgcm.encrypt(self._nonce, plaintext, GCM_AAD)

    def decrypt(self, ciphertext: bytes) -> bytes:
        return self._aesgcm.decrypt(self._nonce, ciphertext, GCM_AAD)


class EcdhSession:
    """Ephemeral P-256 key pair, fresh per connection."""

    def __init__(self) -> None:
        self._private = ec.generate_private_key(ec.SECP256R1())

    def public_coordinates(self) -> bytes:
        """The 64-byte X||Y sent as TLV 0xA1 in the 0x0021 request."""
        raw = self._private.public_key().public_bytes(
            Encoding.X962, PublicFormat.UncompressedPoint
        )
        if len(raw) != 65 or raw[0] != 0x04:
            raise ValueError(f"unexpected P-256 encoding ({len(raw)} bytes)")
        return raw[1:]

    def derive(self, device_coordinates: bytes) -> tuple[bytes, bytes]:
        """Device X||Y -> (aes_key_16, gcm_nonce_12)."""
        if len(device_coordinates) != 64:
            raise ValueError(
                f"device public key must be 64 bytes, got {len(device_coordinates)}"
            )
        peer = ec.EllipticCurvePublicKey.from_encoded_point(
            ec.SECP256R1(), b"\x04" + device_coordinates
        )
        shared = self._private.exchange(ec.ECDH(), peer)
        if len(shared) != 32:
            raise ValueError(f"unexpected shared secret length {len(shared)}")
        return shared[0:16], shared[16:28]


def find_device_public_key(payload: bytes) -> Optional[bytes]:
    """Pull the 64-byte device P-256 key out of a decrypted 0x0021 response."""
    for tlv_type, value in parse_tlv(payload, tlv_offset(payload)):
        if tlv_type == 0xA1 and len(value) == 64:
            return value
    return None


# --- Request builders (read-only) ------------------------------------------


def status_probe_tlv() -> list[tuple[int, bytes]]:
    return [(0xA1, b"\x21"), (0xFE, epoch_bytes())]


def realtime_probe_tlv(user_id: str) -> list[tuple[int, bytes]]:
    """The app's 0x020A request, which needs an Anker account ID.

    The power bank never needs this — it streams 0x0300 unprompted. Kept because
    the charger does, and because a device that stays silent is worth probing.
    """
    try:
        encoded_user_id = user_id.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError("user ID must contain ASCII characters only") from exc
    if len(encoded_user_id) != 40:
        raise ValueError("user ID must be exactly 40 ASCII characters")
    return [
        (0xA1, b"\x21"),
        (0xA2, CMD_020A_A2),
        (0xA3, b"\x04" + encoded_user_id),
        (0xA5, b"\x01\x01"),
        (0xFE, epoch_bytes()),
    ]


def handshake_steps() -> list[tuple[int, list[tuple[int, bytes]], bool]]:
    """(command, tlv, expects_response) for the session-establishing sequence.

    0x0021 is not included — it needs the live ECDH public key and its response
    drives the crypto switch, so the transport issues it directly.
    """
    ts1 = epoch_bytes()
    return [
        (0x0001, [(0xA1, ts1)], True),
        (0x0003, [(0xA1, ts1), (0xA3, b"\x20"), (0xA4, b"\x00\xf0")], True),
        (0x0029, [(0xA1, ts1)], True),
        (
            0x0005,
            [
                (0xA1, epoch_bytes()),
                (0xA3, b"\x20"),
                (0xA4, b"\x29\x01"),
                (0xA5, b"\x44"),
                (0xA6, b"\x02"),
            ],
            True,
        ),
    ]


def post_session_steps(
    user_id: Optional[str] = None,
) -> list[tuple[int, list[tuple[int, bytes]], bool]]:
    """Commands sent once the session key is live.

    On the power bank 0x0022 alone is enough to start the 0x0300 stream. The
    charger additionally needs 0x0027 carrying the Anker account ID, which is
    why `user_id` exists at all — never hard-code a captured value, since the
    device ties personalized screen state to that identity.
    """
    steps = [
        (
            0x0022,
            [(0xA1, epoch_bytes()), (0xA3, CMD_0022_A3), (0xA5, CMD_0022_A5)],
            False,
        ),
    ]
    if user_id:
        try:
            encoded_user_id = user_id.encode("ascii")
        except UnicodeEncodeError as exc:
            raise ValueError("user ID must contain ASCII characters only") from exc
        steps.append((0x0027, [(0xA1, epoch_bytes()), (0xA2, encoded_user_id)], False))
    return steps
