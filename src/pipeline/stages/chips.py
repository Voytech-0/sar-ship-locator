"""Chips: small image cutouts around Detections, kept for review and later model training."""

from __future__ import annotations

import io
import struct
import zlib

import numpy as np
from numpy.typing import NDArray

CHIP_PX = 64
_DB_MIN, _DB_MAX = -25.0, 5.0  # display range of sigma0 in dB


def cut_chip(sigma0: NDArray[np.float32], pixel_box: tuple[int, int, int, int]) -> NDArray[np.float32]:
    """CHIP_PX x CHIP_PX cutout centred on the box; outside the raster or NaN becomes 0."""
    r0, c0, r1, c1 = pixel_box
    top, left = (r0 + r1) // 2 - CHIP_PX // 2, (c0 + c1) // 2 - CHIP_PX // 2
    chip = np.zeros((CHIP_PX, CHIP_PX), dtype=np.float32)
    rows, cols = sigma0.shape
    sr0, sc0 = max(top, 0), max(left, 0)
    sr1, sc1 = min(top + CHIP_PX, rows), min(left + CHIP_PX, cols)
    if sr1 > sr0 and sc1 > sc0:
        chip[sr0 - top : sr1 - top, sc0 - left : sc1 - left] = sigma0[sr0:sr1, sc0:sc1]
    return np.nan_to_num(chip, nan=0.0)


def chip_npy(chip: NDArray[np.float32]) -> bytes:
    buf = io.BytesIO()
    np.save(buf, chip, allow_pickle=False)
    return buf.getvalue()


def chip_png(chip: NDArray[np.float32]) -> bytes:
    """8-bit greyscale PNG, contrast-stretched in dB (stdlib only)."""
    db = 10.0 * np.log10(np.maximum(chip, 1e-4))
    img = np.clip((db - _DB_MIN) / (_DB_MAX - _DB_MIN) * 255.0, 0, 255).astype(np.uint8)
    raw = b"".join(b"\x00" + img[i].tobytes() for i in range(img.shape[0]))

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    header = struct.pack(">IIBBBBB", img.shape[1], img.shape[0], 8, 0, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )
