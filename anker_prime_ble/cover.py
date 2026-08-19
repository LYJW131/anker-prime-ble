"""Glue for the custom-cover pipeline: cloud register + BLE transfer + select.

    local image → 240×240 JPEG → OSS + add_manual → 0x021F → 0x0220 → 0x0221 chunks

Official add-cover capture (2026-08-19): one 0x021F (new id 45470), one 0x0220
start, then 163× 0x0221 of 156 JPEG bytes. First chunk is `FFD8 … JFIF`.
The charger ACKs 0x0221 every 10 chunks (`A2` = next index) and once more
with the total. Firmware keeps four pixel slots; a fifth add overwrites one.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from . import charger, cloud, ff09
from .cloud import CloudSession, Picture
from .image import encode_screensaver_jpeg, jpeg_hash_code

log = logging.getLogger("anker.cover")


def register_local_image(
    session: CloudSession,
    sn: str,
    source,
    *,
    name: Optional[str] = None,
    jpeg: Optional[bytes] = None,
) -> tuple[Picture, bytes]:
    """Crop, upload to OSS, call add_manual, return the new list row + JPEG bytes."""
    payload = jpeg if jpeg is not None else encode_screensaver_jpeg(source)
    digest = jpeg_hash_code(payload)
    before = {pic.id for pic in session.list_manual(sn)}

    token = session.get_up_token()
    img_url = session.upload_jpeg(payload, token)
    log.info("uploaded %d bytes hash=%s url=%s", len(payload), cloud.format_hash_code(digest), img_url)
    added = session.add_manual(sn, img_url, digest)
    log.info("add_manual returned keys %s", list((added.get("data") or added).keys()) if isinstance(added, dict) else type(added))

    after = session.list_manual(sn)
    fresh = [pic for pic in after if pic.id not in before]
    match = [pic for pic in after if pic.hash_code == digest]
    picture = (fresh or match or after)[-1] if (fresh or match or after) else None
    if picture is None:
        raise cloud.CloudError("add_manual succeeded but the list is still empty", body=added)
    if name and picture.name != name:
        session.set_name(sn, picture.id, name)
        picture = Picture(
            id=picture.id,
            seq=picture.seq,
            name=name,
            hash_code=picture.hash_code,
            img_url=picture.img_url,
            short_url=picture.short_url,
            raw=picture.raw,
        )
    return picture, payload


async def transfer_pixels(sess, jpeg: bytes, picture: Picture, *, select_first: bool = True) -> None:
    """Push JPEG bytes the way the official app does: 0x021F, 0x0220, 0x0221…"""
    chunks = charger.screensaver_chunks(jpeg)
    log.info(
        "transfer id=%s hash=%s bytes=%d chunks=%d",
        picture.id,
        cloud.format_hash_code(picture.hash_code),
        len(jpeg),
        len(chunks),
    )
    if select_first:
        await sess.send_expect(
            ff09.GROUP_TELEMETRY,
            charger.CMD_SET_SCREENSAVER,
            charger.screensaver_select_tlv(picture.id, picture.hash_code),
        )
        await asyncio.sleep(0.15)
    ack = await sess.send_expect(
        ff09.GROUP_TELEMETRY,
        charger.CMD_TRANSFER_START,
        charger.screensaver_transfer_start_tlv(
            picture.id,
            picture.hash_code,
            len(jpeg),
            chunk_count=len(chunks),
        ),
    )
    if ack is None:
        raise RuntimeError("0x0220 unanswered")
    await asyncio.sleep(0.2)
    every = charger.SCREENSAVER_ACK_EVERY
    for seq, chunk in enumerate(chunks):
        items = charger.screensaver_chunk_tlv(seq, chunk)
        last = seq + 1 == len(chunks)
        if last or (seq + 1) % every == 0:
            reply = await sess.send_expect(
                ff09.GROUP_TELEMETRY, charger.CMD_TRANSFER_DATA, items, timeout=5.0
            )
            log.info("0x0221 ack seq=%d %s", seq, (reply or b"")[:10].hex())
        else:
            await sess.send(ff09.GROUP_TELEMETRY, charger.CMD_TRANSFER_DATA, items)
            await asyncio.sleep(0.04)
    await asyncio.sleep(0.4)


def select_tlv(picture: Picture):
    return charger.screensaver_select_tlv(picture.id, picture.hash_code)
