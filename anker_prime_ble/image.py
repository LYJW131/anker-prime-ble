"""Crop a local picture to the official A2687 custom-cover JPEG.

The app ships a `375x375` crop overlay (`crop_cover_375x375.png`) and names
uploads `*.cropped_image.jpg`, but every official file fetched from this
account is **240×240**. Cloud `hash_code` is IEEE CRC-32 of those JPEG bytes
(`0x` + 8 hex digits) — matched on all five pictures.
"""

from __future__ import annotations

import io
import zlib
from pathlib import Path
from typing import Union

from .cloud import format_hash_code

PathLike = Union[str, Path]

SCREEN_SIZE = 240
JPEG_QUALITY = 85


def jpeg_hash_code(data: bytes) -> int:
    """IEEE CRC-32 of the JPEG file bytes, matching cloud `hash_code`."""
    return zlib.crc32(data) & 0xFFFFFFFF


def hash_hex(data: bytes) -> str:
    return format_hash_code(jpeg_hash_code(data))


def encode_screensaver_jpeg(
    source: PathLike | bytes,
    *,
    size: int = SCREEN_SIZE,
    quality: int = JPEG_QUALITY,
) -> bytes:
    """Center-crop to square, resize to `size`, emit a baseline JPEG.

    Needs Pillow. `covers list` / `covers select` do not import this module.
    """
    try:
        from PIL import Image
    except ImportError as exc:
        raise SystemExit(
            "Pillow is required to crop a cover. "
            "Install with: .venv/bin/pip install -r requirements.txt"
        ) from exc

    if isinstance(source, (bytes, bytearray)):
        image = Image.open(io.BytesIO(source))
    else:
        image = Image.open(source)
    image = image.convert("RGB")
    width, height = image.size
    side = min(width, height)
    left = (width - side) // 2
    top = (height - side) // 2
    image = image.crop((left, top, left + side, top + side))
    if image.size != (size, size):
        image = image.resize((size, size), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    image.save(buf, format="JPEG", quality=quality, optimize=True, subsampling=0)
    return buf.getvalue()
