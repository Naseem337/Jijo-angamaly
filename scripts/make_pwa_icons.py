"""Draws the Resort Desk app icons (gradient, sun and sea) as PNG files with no extra libraries.

    python scripts/make_pwa_icons.py
"""
import math
import struct
import zlib
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "app" / "resort" / "dashboard" / "static"
A, B, C = (124, 92, 255), (179, 107, 255), (255, 106, 162)


def mix(p, q, k):
    return tuple(round(p[i] + (q[i] - p[i]) * k) for i in range(3))


def pixel(x, y, n):
    u, v = x / n, y / n
    k = (u + v) / 2
    bg = mix(A, B, k * 2) if k < 0.5 else mix(B, C, (k - 0.5) * 2)
    cx, cy, r = 0.5, 0.40, 0.17  # the sun
    if math.hypot(u - cx, v - cy) <= r:
        return (255, 255, 255)
    wave = 0.64 + 0.035 * math.sin(u * 2 * math.pi * 2.2)  # two soft waves, all inside the maskable safe zone
    for i, alpha in enumerate((1.0, 0.55)):
        if wave + i * 0.09 <= v <= wave + i * 0.09 + 0.045 and 0.2 <= u <= 0.8:
            return mix(bg, (255, 255, 255), alpha)
    return bg


def png(n: int) -> bytes:
    raw = b"".join(b"\x00" + bytes(c for x in range(n) for c in pixel(x, y, n)) for y in range(n))
    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", n, n, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


for name, size in (("icon-192.png", 192), ("icon-512.png", 512), ("apple-touch-icon.png", 180)):
    (OUT / name).write_bytes(png(size))
    print("wrote", name)
