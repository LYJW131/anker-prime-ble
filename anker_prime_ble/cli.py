"""One CLI for both devices.

    python -m anker_prime_ble scan                     # find what is in range
    python -m anker_prime_ble gatt <addr>              # services and characteristics
    python -m anker_prime_ble status <addr>            # decoded dashboard
    python -m anker_prime_ble watch <addr>             # only fields that move
    python -m anker_prime_ble session <addr> --record cap.jsonl
    python -m anker_prime_ble replay cap.jsonl --decode
    python -m anker_prime_ble covers list              # cloud custom-cover directory
    python -m anker_prime_ble covers select <addr> --seq 1
    python -m anker_prime_ble covers upload photo.jpg  # crop + register; BLE pixels still need a capture

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
from pathlib import Path
from typing import Optional

from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice

from . import charger, cloud, cover, decode, ff09, powerbank, session as ble
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


# --- covers (cloud + BLE select) -------------------------------------------


def _charger_sn(args: argparse.Namespace) -> str:
    sn = getattr(args, "sn", None) or os.environ.get("ANKER_CHARGER_SN")
    if not sn:
        raise SystemExit("pass --sn or set ANKER_CHARGER_SN")
    return sn


def _cloud_session(args: argparse.Namespace) -> cloud.CloudSession:
    return cloud.login_from_env(
        phone=getattr(args, "phone", None),
        password=getattr(args, "password", None),
        sms_code=getattr(args, "sms_code", None),
        token=getattr(args, "token", None),
        user_id=getattr(args, "user_id", None),
        host=getattr(args, "host", None),
        ab=getattr(args, "ab", None),
        cache=getattr(args, "cache", None),
    )


def cmd_covers_list(args: argparse.Namespace) -> None:
    session = _cloud_session(args)
    pictures = session.list_manual(_charger_sn(args))
    print(f"total {len(pictures)}  uid={session.user_id}")
    for pic in pictures:
        title = pic.name or "-"
        print(f"  seq {pic.seq:<2}  id {pic.id:<6}  {pic.hash_hex()}  {title}  {pic.short_url}")


def cmd_covers_hash(args: argparse.Namespace) -> None:
    from . import image

    data = Path(args.file).read_bytes() if args.file != "-" else sys.stdin.buffer.read()
    if args.file != "-" and not args.raw:
        data = image.encode_screensaver_jpeg(args.file)
        print(f"cropped {len(data)} bytes  {image.hash_hex(data)}")
        if args.out:
            Path(args.out).write_bytes(data)
            print(f"wrote {args.out}")
        return
    from .image import hash_hex

    print(hash_hex(data))


def cmd_covers_token(args: argparse.Namespace) -> None:
    session = _cloud_session(args)
    extra = json.loads(args.body) if args.body else None
    token = session.get_up_token(extra)
    print("data keys:", ", ".join(sorted(token)) or "(empty)")
    for key, value in token.items():
        shown = value
        if isinstance(value, str) and len(value) > 24:
            shown = value[:6] + "…" + value[-6:]
        print(f"  {key}: {shown}")


def cmd_covers_upload(args: argparse.Namespace) -> None:
    from . import image

    jpeg = image.encode_screensaver_jpeg(args.file, quality=args.quality)
    digest = image.hash_hex(jpeg)
    print(f"cropped {len(jpeg)} bytes  {digest}  {args.file}")
    if args.out:
        Path(args.out).write_bytes(jpeg)
        print(f"wrote {args.out}")
    if args.prepare_only:
        return

    session = _cloud_session(args)
    try:
        picture, _ = cover.register_local_image(
            session, _charger_sn(args), args.file, name=args.name, jpeg=jpeg
        )
    except cloud.CloudError as exc:
        raise SystemExit(f"cloud register failed: {exc}") from exc
    print(f"registered seq {picture.seq} id {picture.id} {picture.hash_hex()} {picture.name or '-'}")
    if not args.address:
        print("cloud row only — pass the BLE address to push pixels (0x0220/0x0221).")
        return
    args.user_id = args.user_id or session.user_id
    asyncio.run(_covers_push(args, picture, jpeg))


async def _covers_push(args: argparse.Namespace, picture: cloud.Picture, jpeg: bytes) -> None:
    if not args.user_id:
        raise SystemExit("the charger needs an account ID — pass --user-id or log in")
    device = await ble.resolve(args.address, getattr(args, "wait", 60.0))
    last: list[Optional[int]] = [None]

    def on_frame(command: int, payload: bytes) -> None:
        if command in (charger.CMD_SET_SCREENSAVER, charger.CMD_TRANSFER_START, charger.CMD_TRANSFER_DATA):
            print(f"ACK 0x{command:04X} {payload[:8].hex().upper()}", flush=True)
        if command in charger.SNAPSHOT_COMMANDS | charger.REALTIME_COMMANDS:
            current = charger.screensaver_id_from_payload(payload)
            if current is not None and current != last[0]:
                print(f"E1 {last[0]} -> {current}", flush=True)
                last[0] = current

    sess = ble.Session(
        device=device,
        profile=charger.PROFILE,
        on_frame=on_frame,
        user_id=args.user_id,
        idle_timeout=20.0,
    )

    async def after_handshake(_sess: ble.Session) -> None:
        await cover.transfer_pixels(sess, jpeg, picture)
        await sess.arm_telemetry()

    await sess.run(getattr(args, "seconds", 20.0), after_handshake=after_handshake)
    if last[0] == picture.id:
        print(f"screen now id {picture.id}")
    else:
        print(f"screen still id {last[0]} (wanted {picture.id})")


async def cmd_covers_select(args: argparse.Namespace) -> None:
    session: Optional[cloud.CloudSession] = None
    picture_id = args.id
    hash_code = cloud.parse_hash_code(args.hash) if args.hash is not None else None
    if args.seq is not None or picture_id is None or hash_code is None:
        session = _cloud_session(args)
        pictures = session.list_manual(_charger_sn(args))
        if args.seq is not None:
            match = next((p for p in pictures if p.seq == args.seq), None)
            if match is None:
                have = ", ".join(str(p.seq) for p in pictures) or "none"
                raise SystemExit(f"no seq {args.seq} in the cloud list (have {have})")
        elif picture_id is not None:
            match = next((p for p in pictures if p.id == picture_id), None)
            if match is None:
                raise SystemExit(f"id {picture_id} is not in the cloud list")
        else:
            raise SystemExit("pass --seq, or both --id and --hash")
        picture_id = match.id
        hash_code = match.hash_code
        print(f"cloud seq {match.seq} id {match.id} {match.hash_hex()} {match.name or '-'}")
    if picture_id is None or hash_code is None:
        raise SystemExit("pass --seq, or both --id and --hash")

    if not args.address:
        raise SystemExit("pass the BLE address or set ANKER_CHARGER")

    user_id = args.user_id or os.environ.get("ANKER_USER_ID")
    if not user_id:
        if session is None:
            session = _cloud_session(args)
        user_id = session.user_id
    if not user_id:
        raise SystemExit("the charger needs an account ID — pass --user-id or log in")

    device = await ble.resolve(args.address, args.wait)
    last: list[Optional[int]] = [None]
    ack = {"seen": False}

    def on_frame(command: int, payload: bytes) -> None:
        if command == charger.CMD_SET_SCREENSAVER:
            ack["seen"] = True
            print(f"ACK 0x021F {payload.hex().upper()}", flush=True)
        if command in charger.SNAPSHOT_COMMANDS | charger.REALTIME_COMMANDS:
            current = charger.screensaver_id_from_payload(payload)
            if current is not None and current != last[0]:
                print(f"E1 {last[0]} -> {current}", flush=True)
                last[0] = current

    sess = ble.Session(
        device=device,
        profile=charger.PROFILE,
        on_frame=on_frame,
        user_id=user_id,
        idle_timeout=20.0,
    )

    async def after_handshake(_sess: ble.Session) -> None:
        deadline = time.monotonic() + 8.0
        while last[0] is None and time.monotonic() < deadline:
            await asyncio.sleep(0.1)
        print(f"current id {last[0]}; writing {picture_id} {cloud.format_hash_code(hash_code)}")
        await sess.send(
            ff09.GROUP_TELEMETRY,
            charger.CMD_SET_SCREENSAVER,
            charger.screensaver_select_tlv(picture_id, hash_code),
        )
        await asyncio.sleep(1.0)
        await sess.arm_telemetry()

    await sess.run(args.seconds, after_handshake=after_handshake)
    if last[0] == picture_id:
        print(f"screen now id {picture_id}")
    else:
        print(
            f"screen still id {last[0]} (wanted {picture_id}). "
            "ACK is not success; pixels may never have been pushed."
        )


# --- CLI -------------------------------------------------------------------


def _add_device_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--device",
        choices=sorted(PROFILES),
        default=os.environ.get("ANKER_DEVICE", "powerbank"),
        help="which decoder to use (default: powerbank, or $ANKER_DEVICE)",
    )


def _add_cloud_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--sn", default=os.environ.get("ANKER_CHARGER_SN"), help="charger serial ($ANKER_CHARGER_SN)")
    parser.add_argument("--phone", default=os.environ.get("ANKER_PHONE"), help="login phone / email ($ANKER_PHONE)")
    parser.add_argument("--password", default=os.environ.get("ANKER_PASSWORD"), help="login password ($ANKER_PASSWORD)")
    parser.add_argument("--sms-code", default=os.environ.get("ANKER_SMS_CODE"), help="SMS code ($ANKER_SMS_CODE)")
    parser.add_argument("--token", default=os.environ.get("ANKER_AUTH_TOKEN"), help="reuse an auth token")
    parser.add_argument("--user-id", default=os.environ.get("ANKER_USER_ID"), help="account id ($ANKER_USER_ID)")
    parser.add_argument("--host", default=os.environ.get("ANKER_HOST"), help="API origin")
    parser.add_argument("--ab", default=os.environ.get("ANKER_AB", "CN"), help="region: CN, COM, EU")
    parser.add_argument(
        "--cache",
        default=os.environ.get("ANKER_AUTH_CACHE"),
        help="0600 JSON file for auth_token+user_id ($ANKER_AUTH_CACHE); avoids login rate limits",
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

    covers = sub.add_parser("covers", help="list, upload, or select custom screensaver pictures")
    covers_sub = covers.add_subparsers(dest="covers_cmd", required=True)

    covers_list = covers_sub.add_parser("list", help="print the cloud custom-cover directory")
    _add_cloud_args(covers_list)
    covers_list.set_defaults(func=cmd_covers_list, is_async=False)

    covers_hash = covers_sub.add_parser("hash", help="crop a file to 240x240 and print hash_code")
    covers_hash.add_argument("file")
    covers_hash.add_argument("--raw", action="store_true", help="hash the file bytes without cropping")
    covers_hash.add_argument("--out", help="write the cropped JPEG here")
    covers_hash.set_defaults(func=cmd_covers_hash, is_async=False)

    covers_token = covers_sub.add_parser("token", help="probe get_app_up_token_general (prints redacted keys)")
    _add_cloud_args(covers_token)
    covers_token.add_argument("--body", help="raw JSON body to send instead of the guess list")
    covers_token.set_defaults(func=cmd_covers_token, is_async=False)

    covers_upload = covers_sub.add_parser("upload", help="crop and register a local image in the cloud")
    covers_upload.add_argument("file")
    _add_cloud_args(covers_upload)
    covers_upload.add_argument("--name", help="display title")
    covers_upload.add_argument("--quality", type=int, default=85)
    covers_upload.add_argument("--out", help="write the cropped JPEG here")
    covers_upload.add_argument("--prepare-only", action="store_true", help="crop and hash only; do not call the cloud")
    covers_upload.add_argument(
        "--address",
        nargs="?",
        default=os.environ.get("ANKER_CHARGER"),
        help="if set, BLE-push pixels after register ($ANKER_CHARGER)",
    )
    covers_upload.add_argument("--wait", type=float, default=60.0)
    covers_upload.add_argument("--seconds", type=float, default=20.0)
    covers_upload.set_defaults(func=cmd_covers_upload, is_async=False)

    covers_select = covers_sub.add_parser("select", help="BLE 0x021F: show an already-uploaded picture")
    covers_select.add_argument(
        "address",
        nargs="?",
        default=os.environ.get("ANKER_CHARGER"),
        help="BLE address or advertised name ($ANKER_CHARGER)",
    )
    _add_cloud_args(covers_select)
    covers_select.add_argument("--seq", type=int, help="1-based slot from the cloud list")
    covers_select.add_argument("--id", type=int, dest="id", help="cloud picture id")
    covers_select.add_argument("--hash", help="hash_code (0x…); required with --id unless listed")
    covers_select.add_argument("--wait", type=float, default=60.0)
    covers_select.add_argument("--seconds", type=float, default=16.0)
    covers_select.set_defaults(func=cmd_covers_select, is_async=True)

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
