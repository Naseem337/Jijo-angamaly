"""Car photos for WhatsApp: WhatsApp image messages take JPEG/PNG only (not WebP), max 5 MB.

The dealer's photos (Carlet: WebP on S3, ~300 KB) are converted with ffmpeg and shrunk to
1280 px wide; a typical result is ~140 KB, done in ~0.1 s (measured 2026-10-20).
"""

from __future__ import annotations

import subprocess

from app.providers.audio import AudioError

MAX_WIDTH = 1280


class ImageError(AudioError):
    pass


def to_jpeg(data: bytes, ffmpeg: str) -> bytes:
    if data[:3] == b"\xff\xd8\xff" and len(data) < 1_000_000:
        return data  # already a small JPEG
    try:
        done = subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
             "-vf", f"scale='min({MAX_WIDTH},iw)':-2", "-q:v", "4", "-frames:v", "1",
             "-f", "image2", "-vcodec", "mjpeg", "pipe:1"],
            input=data, check=True, capture_output=True, timeout=30,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        detail = getattr(exc, "stderr", b"") or b""
        raise ImageError(f"ffmpeg could not convert the photo: {detail[:200]!r}") from exc
    if not done.stdout.startswith(b"\xff\xd8\xff"):
        raise ImageError("ffmpeg produced no JPEG")
    return done.stdout
