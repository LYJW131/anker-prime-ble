"""BLE transport: discovery, one instrumented FF09 session, frame recording.

Device-agnostic. It runs the handshake, hands every decoded frame to a callback,
and writes a capture — what the frames *mean* is the device modules' problem.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Callable, Optional

from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice

from . import ff09
from .device import DeviceProfile

log = logging.getLogger("anker")

# Standard GATT strings worth reading before any proprietary traffic.
DEVICE_INFO_CHARS = {
    "00002a24-0000-1000-8000-00805f9b34fb": "model_number",
    "00002a25-0000-1000-8000-00805f9b34fb": "serial_number",
    "00002a26-0000-1000-8000-00805f9b34fb": "firmware_revision",
    "00002a27-0000-1000-8000-00805f9b34fb": "hardware_revision",
    "00002a28-0000-1000-8000-00805f9b34fb": "software_revision",
    "00002a29-0000-1000-8000-00805f9b34fb": "manufacturer",
}


def is_ff09(adv) -> bool:
    return any(
        u.lower() == ff09.ADVERTISED_SERVICE_UUID or u.lower().startswith("0000ff09")
        for u in (adv.service_uuids or [])
    )


def describe(device: BLEDevice, adv) -> str:
    name = device.name or adv.local_name or ""
    parts = [f"{device.address}  rssi={adv.rssi:>4}  name={name!r}"]
    if adv.service_uuids:
        parts.append(f"      uuids: {list(adv.service_uuids)}")
    if adv.manufacturer_data:
        rendered = {k: v.hex().upper() for k, v in adv.manufacturer_data.items()}
        parts.append(f"      mfg:   {rendered}")
    if adv.service_data:
        rendered = {k: v.hex().upper() for k, v in adv.service_data.items()}
        parts.append(f"      sdata: {rendered}")
    return "\n".join(parts)


def guess_device(name: str) -> str:
    """Which profile an advertised name belongs to.

    The charger has a stable ASHDJW prefix; the power bank advertises its bare
    serial, so anything else on ff09 is assumed to be one.
    """
    return "charger" if name.upper().startswith(ff09.CHARGER_NAME_PREFIX) else "powerbank"


async def resolve(address: str, wait: float = 60.0) -> BLEDevice:
    """Wait for the target to advertise, then return it the moment it does.

    A power bank only advertises while awake and goes back to sleep on its own,
    so a one-shot lookup keeps losing that race. Matches either the
    CoreBluetooth UUID or the advertised name, since the two are easy to confuse.
    """
    target = address.upper()
    loop = asyncio.get_running_loop()
    found: asyncio.Future[BLEDevice] = loop.create_future()

    def seen(device: BLEDevice, adv) -> None:
        if found.done():
            return
        name = (device.name or adv.local_name or "").upper()
        if device.address.upper() == target or (name and name == target):
            found.set_result(device)

    scanner = BleakScanner(detection_callback=seen)
    await scanner.start()
    try:
        log.info("waiting up to %.0fs for %s to advertise…", wait, address)
        return await asyncio.wait_for(found, wait)
    except TimeoutError:
        raise SystemExit(
            f"{address} never advertised in {wait:.0f}s — wake the device "
            "(press its button) and try again. If a phone app is connected to "
            "it, quit that first: a connected peripheral stops advertising."
        ) from None
    finally:
        await scanner.stop()


class Session:
    """One instrumented FF09 session: handshake, then log everything that lands."""

    def __init__(
        self,
        device: BLEDevice,
        profile: DeviceProfile,
        on_frame: Callable[[int, bytes], None],
        user_id: Optional[str] = None,
        auth_password: Optional[bytes] = None,
        stage: str = "full",
        record: Optional[str] = None,
        idle_timeout: float = 8.0,
    ) -> None:
        self.device = device
        self.profile = profile
        self.on_frame = on_frame
        self.user_id = user_id
        self.auth_password = auth_password
        self.stage = stage
        self.idle_timeout = idle_timeout
        self._crypto = ff09.CryptoContext()
        self._assembler = ff09.FrameAssembler()
        self._pending: Optional[tuple[int, asyncio.Future[bytes]]] = None
        self._client: Optional[BleakClient] = None
        self._write_uuid = ff09.WRITE_CHAR_UUID
        self._notify_uuid = ff09.NOTIFY_CHAR_UUID
        self._log = open(record, "a", buffering=1) if record else None
        self.commands_seen: dict[int, int] = {}
        # Monotonic stamp of the last decoded frame. The charger's pushed stream
        # goes quiet on its own and has to be re-armed, so a long capture needs
        # to notice silence rather than assume the device has nothing to say.
        self._last_frame_at = 0.0

    # -- plumbing --

    def _record(self, direction: str, command: int, **extra) -> None:
        if self._log is None:
            return
        self._log.write(
            json.dumps({"t": time.time(), "dir": direction, "cmd": command, **extra})
            + "\n"
        )

    def _pick_characteristics(self, client: BleakClient) -> None:
        """Prefer the known UUIDs; otherwise find a write+notify pair."""
        chars = {
            char.uuid.lower(): char
            for service in client.services
            for char in service.characteristics
        }
        if ff09.WRITE_CHAR_UUID in chars and ff09.NOTIFY_CHAR_UUID in chars:
            return
        writable = [
            c for c in chars.values()
            if {"write", "write-without-response"} & set(c.properties)
        ]
        notifying = [c for c in chars.values() if "notify" in c.properties]
        if not writable or not notifying:
            raise SystemExit(
                "no write+notify characteristic pair found — run `gatt` and look"
            )
        self._write_uuid = writable[0].uuid
        self._notify_uuid = notifying[0].uuid
        log.warning(
            "known UUIDs absent; using write=%s notify=%s",
            self._write_uuid,
            self._notify_uuid,
        )

    def _on_notify(self, _characteristic, data: bytearray) -> None:
        for raw in self._assembler.feed(bytes(data)):
            try:
                self._dispatch(raw)
            except Exception as exc:  # a bad packet must not kill the notify loop
                log.warning("dispatch failed on %s: %s", raw.hex().upper(), exc)

    def _dispatch(self, raw: bytes) -> None:
        frame = ff09.parse_frame(raw)
        if frame is None:
            log.debug("short frame: %s", raw.hex().upper())
            return
        payload = frame.body
        if frame.encrypted:
            try:
                payload = self._crypto.decrypt(frame.body)
            except Exception as exc:
                log.warning(
                    "decrypt failed 0x%04X (%s): %s",
                    frame.command, self._crypto.state, exc,
                )
                self._record("rx", frame.command, enc=True, ok=False,
                             raw=raw.hex().upper())
                return
        self.commands_seen[frame.command] = self.commands_seen.get(frame.command, 0) + 1
        self._record(
            "rx", frame.command, enc=frame.encrypted, ack=frame.ack, ok=True,
            plain=payload.hex().upper(), raw=raw.hex().upper(),
        )
        self._last_frame_at = time.monotonic()
        self.on_frame(frame.command, payload)
        if self._pending is not None:
            expected, future = self._pending
            if frame.command == expected and not future.done():
                future.set_result(payload)

    async def send(self, group: int, command: int, tlv: list[tuple[int, bytes]]) -> None:
        assert self._client is not None
        frame = ff09.build_frame(group, command, self._crypto.encrypt(ff09.build_tlv(tlv)))
        self._record("tx", command, group=group, raw=frame.hex().upper())
        log.info("tx 0x%04X", command)
        await self._client.write_gatt_char(self._write_uuid, frame, response=False)

    async def send_expect(
        self, group: int, command: int, tlv: list[tuple[int, bytes]],
        timeout: float = 6.0,
    ) -> Optional[bytes]:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[bytes] = loop.create_future()
        self._pending = (command, future)
        try:
            await self.send(group, command, tlv)
            return await asyncio.wait_for(future, timeout)
        except TimeoutError:
            log.warning("0x%04X: no reply in %.0fs", command, timeout)
            return None
        finally:
            self._pending = None

    # -- handshake --

    async def handshake(self) -> None:
        if self.stage == "none":
            log.info("stage=none: listening only, sending nothing")
            return

        for command, tlv, _ in ff09.handshake_steps():
            await self.send_expect(ff09.GROUP_SESSION, command, tlv)
            await asyncio.sleep(0.1)
        if self.stage == "preamble":
            return

        ecdh = ff09.EcdhSession()
        response = await self.send_expect(
            ff09.GROUP_SESSION, 0x0021, [(0xA1, ecdh.public_coordinates())]
        )
        if response is None:
            log.error("0x0021 unanswered — a different key exchange?")
            return
        device_key = ff09.find_device_public_key(response)
        if device_key is None:
            log.error("0x0021 reply carried no 64-byte P-256 key: %s", response.hex())
            return
        key, nonce = ecdh.derive(device_key)
        self._crypto.set_session(key, nonce)
        log.info("session key derived (ECDH P-256)")
        await asyncio.sleep(0.12)
        if self.stage == "ecdh":
            return

        # 0x0027 carries the account ID. Both devices need it — the charger to
        # start streaming at all, the power bank to keep the session past 26 s.
        account_id = self.user_id if self.profile.needs_account_id else None
        for command, tlv, _ in ff09.post_session_steps(
            account_id, password=self.auth_password
        ):
            await self.send(ff09.GROUP_SESSION, command, tlv)
            await asyncio.sleep(0.12)

        await self.arm_telemetry()

    async def arm_telemetry(self) -> None:
        """Ask for a snapshot and a realtime frame.

        Sent once at the end of the handshake, and again whenever the watchdog
        finds the stream quiet. Both are reads; neither changes a setting.
        """
        await self.send(ff09.GROUP_TELEMETRY, ff09.CMD_STATUS, ff09.status_probe_tlv())
        await asyncio.sleep(0.2)
        if not self.profile.needs_realtime_probe:
            return
        if self.user_id:
            await self.send(
                ff09.GROUP_TELEMETRY, ff09.CMD_REALTIME,
                ff09.realtime_probe_tlv(self.user_id),
            )
        else:
            log.warning(
                "%s needs an account ID for its stream — pass --user-id or set "
                "ANKER_USER_ID, or it will stay quiet", self.profile.name,
            )

    async def _watchdog(self, idle_timeout: float) -> None:
        """Re-arm the stream when it goes silent.

        The charger stops pushing on its own after a dozen frames or so. A
        capture that only listens then sits there recording nothing, which reads
        as "the device has no more to say" rather than "nobody asked again" —
        and quietly ruins any experiment that needs minutes of data, such as
        watching a temperature change. The power bank does not need this, but
        re-arming an already-live stream is harmless.
        """
        while True:
            await asyncio.sleep(1.0)
            if time.monotonic() - self._last_frame_at < idle_timeout:
                continue
            log.info("stream quiet for %.0fs; re-arming", idle_timeout)
            try:
                await self.arm_telemetry()
            except Exception as exc:
                log.warning("re-arm failed: %s", exc)
                return
            self._last_frame_at = time.monotonic()

    async def run(self, seconds: float) -> None:
        async with BleakClient(self.device, timeout=25.0) as client:
            self._client = client
            self._pick_characteristics(client)
            await client.start_notify(self._notify_uuid, self._on_notify)
            log.info("connected to %s, notifications on", self.device.address)
            await asyncio.sleep(0.3)
            await self.handshake()
            log.info("handshake stage '%s' done; listening %.0fs", self.stage, seconds)
            self._last_frame_at = time.monotonic()
            watchdog = asyncio.create_task(self._watchdog(self.idle_timeout))
            try:
                deadline = time.monotonic() + seconds
                while client.is_connected and time.monotonic() < deadline:
                    await asyncio.sleep(0.5)
            finally:
                watchdog.cancel()
                await asyncio.gather(watchdog, return_exceptions=True)
        if self._log:
            self._log.close()

    def summary(self) -> str:
        if not self.commands_seen:
            return "no frames decoded"
        parts = [f"0x{c:04X}x{n}" for c, n in sorted(self.commands_seen.items())]
        return "commands seen: " + "  ".join(parts)
