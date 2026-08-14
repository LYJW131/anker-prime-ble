"""One CLI for both devices.

    python -m anker_prime_ble scan                     # find what is in range
    python -m anker_prime_ble gatt <addr>              # services and characteristics
    python -m anker_prime_ble status <addr>            # decoded dashboard
    python -m anker_prime_ble watch <addr>             # only fields that move
    python -m anker_prime_ble session <addr> --record cap.jsonl
    python -m anker_prime_ble replay cap.jsonl --decode

`--device` picks the decoder; `scan` guesses it from the advertised name, so in
practice it can be left off unless both devices are in range and you mean the
other one.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import sys
import time
from typing import Optional

from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice

from . import charger, decode, ff09, powerbank, session as ble
from .device import DeviceProfile

log = logging.getLogger("anker")

PROFILES: dict[str, DeviceProfile] = {
    charger.PROFILE.key: charger.PROFILE,
    powerbank.PROFILE.key: powerbank.PROFILE,
}


# --- discovery -------------------------------------------------------------


async def cmd_scan(args: argparse.Namespace) -> None:
    seen: dict[str, float] = {}

    def report(device: BLEDevice, adv) -> None:
        name = device.name or adv.local_name or ""
        ff09_match = ble.is_ff09(adv)
        if not (ff09_match or args.all):
            return
        now = time.time()
        if now - seen.get(device.address, 0) < 5.0:
            return
        seen[device.address] = now
        mark = f"[{ble.guess_device(name):>9}]" if ff09_match else " " * 11
        print(f"{mark} {ble.describe(device, adv)}", flush=True)

    scanner = BleakScanner(detection_callback=report)
    deadline = None if args.loop else time.monotonic() + args.seconds
    print(
        "scanning… wake the device (press its button) so it advertises. "
        "A device already connected to a phone app will not show up.",
        flush=True,
    )
    await scanner.start()
    try:
        while deadline is None or time.monotonic() < deadline:
            await asyncio.sleep(0.5)
    finally:
        await scanner.stop()
    if not seen:
        print("nothing matched. Try --all to see every advertiser.", flush=True)


async def cmd_gatt(args: argparse.Namespace) -> None:
    device = await ble.resolve(args.address, args.wait)
    async with BleakClient(device, timeout=25.0) as client:
        print(f"connected to {device.address} ({device.name or '?'})\n")
        for service in client.services:
            print(f"service {service.uuid}  {service.description}")
            for char in service.characteristics:
                print(f"  char {char.uuid}  [{','.join(char.properties)}]")
                if "read" not in char.properties:
                    continue
                try:
                    value = bytes(await client.read_gatt_char(char))
                except Exception as exc:
                    print(f"       read failed: {exc}")
                    continue
                shown = value.hex().upper()
                if char.uuid.lower() in ble.DEVICE_INFO_CHARS or decode._is_text(value):
                    with contextlib.suppress(UnicodeDecodeError):
                        shown += f'  "{value.decode("ascii").strip()}"'
                print(f"       value: {shown}")


# --- live ------------------------------------------------------------------


def _profile(args: argparse.Namespace) -> DeviceProfile:
    return PROFILES[args.device]


async def _run(args: argparse.Namespace, mode: str) -> None:
    device = await ble.resolve(args.address, args.wait)
    profile = _profile(args)
    tracker = decode.Tracker()
    state = profile.new_state()
    start = time.time()

    def on_frame(command: int, payload: bytes) -> None:
        stamp = f"{time.time() - start:7.2f}s"
        changes = tracker.feed(command, payload)

        if command == 0x0029:
            profile.parse_identity(payload, state)
        elif command in profile.snapshot_commands:
            profile.parse_snapshot(payload, state)
        elif command in profile.realtime_commands:
            profile.parse_realtime(payload, state)

        if mode == "status":
            # Render on snapshots too: a charger with no account ID never sends
            # a realtime frame, and the snapshot is all it has to show.
            if command in profile.realtime_commands | profile.snapshot_commands:
                print("\n" + profile.format_state(state), flush=True)
                if args.unknown and state.unknown:
                    for key, value in sorted(state.unknown.items()):
                        print(f"    ? {key} = {value}", flush=True)
        elif mode == "watch":
            for change in changes:
                print(f"[{stamp}] {change}", flush=True)
        else:
            print(f"\n[{stamp}] RX 0x{command:04X}  ({len(payload)} bytes)", flush=True)
            print(decode.format_payload(payload), flush=True)

    sess = ble.Session(
        device=device,
        profile=profile,
        on_frame=on_frame,
        user_id=args.user_id,
        stage=args.stage,
        record=args.record,
    )
    try:
        await sess.run(args.seconds)
    finally:
        if mode != "status":
            print("\n" + "=" * 72, flush=True)
            print(sess.summary(), flush=True)
            print(f"frames decoded: {tracker.frames}", flush=True)
            print("\nlast value of every field seen:", flush=True)
            print(tracker.table(), flush=True)
        if args.record:
            print(f"\ncapture written to {args.record}", flush=True)


async def cmd_status(args: argparse.Namespace) -> None:
    await _run(args, "status")


async def cmd_watch(args: argparse.Namespace) -> None:
    await _run(args, "watch")


async def cmd_session(args: argparse.Namespace) -> None:
    await _run(args, "dump")


# --- offline ---------------------------------------------------------------


def cmd_replay(args: argparse.Namespace) -> None:
    """Re-decode a recorded capture. No radio, no device, no handshake.

    This is how the field maps were worked out and how they should be extended:
    change a decoder, replay, see whether every stored capture still makes sense.
    """
    profile = _profile(args)
    tracker = decode.Tracker()
    state = profile.new_state()
    first: Optional[float] = None

    with open(args.capture) as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            event = json.loads(line)
            if event.get("dir") != "rx" or "plain" not in event:
                continue
            first = first if first is not None else event["t"]
            payload = bytes.fromhex(event["plain"])
            command = event["cmd"]
            stamp = f"{event['t'] - first:7.2f}s"
            changes = tracker.feed(command, payload)

            if args.decode:
                if command == 0x0029:
                    profile.parse_identity(payload, state)
                elif command in profile.snapshot_commands:
                    profile.parse_snapshot(payload, state)
                    print(f"[{stamp}]\n{profile.format_state(state)}")
                elif command in profile.realtime_commands:
                    profile.parse_realtime(payload, state)
                    print(f"[{stamp}]\n{profile.format_state(state)}")
                continue
            if args.changes:
                for change in changes:
                    print(f"[{stamp}] {change}")
            else:
                print(f"\n[{stamp}] RX 0x{command:04X}  ({len(payload)} bytes)")
                print(decode.format_payload(payload))

    print("\n" + "=" * 72)
    print(f"frames: {tracker.frames}\n\nlast value of every field seen:")
    print(tracker.table())


# --- CLI -------------------------------------------------------------------


def _add_device_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--device",
        choices=sorted(PROFILES),
        default=os.environ.get("ANKER_DEVICE", "powerbank"),
        help="which decoder to use (default: powerbank, or $ANKER_DEVICE)",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="anker_prime_ble", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    scan = sub.add_parser("scan", help="list FF09 devices in range")
    scan.add_argument("--seconds", type=float, default=15.0)
    scan.add_argument("--loop", action="store_true", help="scan until Ctrl-C")
    scan.add_argument("--all", action="store_true", help="show every advertiser")
    scan.set_defaults(func=cmd_scan, is_async=True)

    gatt = sub.add_parser("gatt", help="dump services, characteristics, readable values")
    gatt.add_argument("address")
    gatt.add_argument("--wait", type=float, default=60.0)
    gatt.set_defaults(func=cmd_gatt, is_async=True)

    for name, func, helptext in (
        ("status", cmd_status, "live decoded dashboard"),
        ("watch", cmd_watch, "print only the fields that change"),
        ("session", cmd_session, "dump every frame in full"),
    ):
        cmd = sub.add_parser(name, help=helptext)
        cmd.add_argument("address")
        _add_device_arg(cmd)
        cmd.add_argument("--seconds", type=float, default=60.0)
        cmd.add_argument(
            "--wait", type=float, default=60.0, help="seconds to wait for it to advertise"
        )
        cmd.add_argument(
            "--stage", choices=("none", "preamble", "ecdh", "full"), default="full",
            help="how far into the handshake to go",
        )
        cmd.add_argument("--record", help="append the raw capture to this .jsonl")
        cmd.add_argument("--unknown", action="store_true",
                         help="also print undecoded TLVs")
        cmd.add_argument(
            "--user-id", default=os.environ.get("ANKER_USER_ID"),
            help="Anker account ID; the charger needs one (default: $ANKER_USER_ID)",
        )
        cmd.set_defaults(func=func, is_async=True)

    replay = sub.add_parser("replay", help="re-decode a recorded capture offline")
    replay.add_argument("capture")
    _add_device_arg(replay)
    replay.add_argument("--changes", action="store_true", help="only show movement")
    replay.add_argument("--decode", action="store_true",
                        help="render through the device decoder")
    replay.set_defaults(func=cmd_replay, is_async=False)

    return parser


def main(argv: Optional[list[str]] = None) -> None:
    logging.basicConfig(
        level=os.environ.get("ANKER_LOG", "INFO"),
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    args = build_parser().parse_args(argv)
    try:
        if args.is_async:
            asyncio.run(args.func(args))
        else:
            args.func(args)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
