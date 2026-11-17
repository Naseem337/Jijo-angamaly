"""Property and room photos the owner uploads in the dashboard.

Files live next to the database (on Railway: the /data volume, so they survive redeploys) in a `photos` folder.
A photo is referenced in Property.photo_urls / RoomType.photo_urls as "/resort/media/<name>". Uploads are turned
into a JPEG of at most 1600 px with ffmpeg when it is available (WhatsApp wants JPEG/PNG under 5 MB); without
ffmpeg, JPEG and PNG under 5 MB are kept as they are.
"""

from __future__ import annotations

import os
import re
import secrets
import subprocess
import tempfile
from pathlib import Path

from app.providers.audio import find_ffmpeg

PREFIX = "/resort/media/"
MAX_UPLOAD = 15_000_000  # a phone photo before shrinking
MAX_SEND = 5_000_000  # WhatsApp's image limit
NAME = re.compile(r"^[a-f0-9]{24}\.(?:jpg|png)$")
KINDS = {b"\xff\xd8\xff": "jpg", b"\x89PNG": "png"}
PROJECT_ROOT = Path(__file__).resolve().parents[2]


class PhotoError(ValueError):
    pass


def photo_dir() -> Path:
    db = os.environ.get("RESORT_DB_PATH") or str(PROJECT_ROOT / "data" / "resort.sqlite3")
    folder = Path(db).resolve().parent / "photos"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _kind(data: bytes) -> str | None:
    return next((ext for magic, ext in KINDS.items() if data.startswith(magic)), None)


def _shrink(data: bytes) -> bytes | None:
    """Any image ffmpeg can read -> JPEG, longest side at most 1600 px. None if ffmpeg is missing or fails."""
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        return None
    with tempfile.TemporaryDirectory() as tmp:
        src, dst = Path(tmp) / "in", Path(tmp) / "out.jpg"
        src.write_bytes(data)
        cmd = [ffmpeg, "-y", "-loglevel", "error", "-i", str(src), "-frames:v", "1",
               "-vf", "scale='min(1600,iw)':'min(1600,ih)':force_original_aspect_ratio=decrease", "-q:v", "4", str(dst)]
        try:
            subprocess.run(cmd, check=True, timeout=60, capture_output=True)
        except (subprocess.SubprocessError, OSError):
            return None
        return dst.read_bytes() if dst.is_file() else None


def save_photo(data: bytes) -> str:
    """Store an uploaded image and return its reference ("/resort/media/<name>")."""
    if not data:
        raise PhotoError("The file is empty.")
    if len(data) > MAX_UPLOAD:
        raise PhotoError("That photo is too large (max 15 MB).")
    small = _shrink(data)
    if small:
        data, ext = small, "jpg"
    else:
        ext = _kind(data)
        if ext is None:
            raise PhotoError("Please upload a JPG or PNG photo.")
    if len(data) > MAX_SEND:
        raise PhotoError("That photo is too large to send on WhatsApp (max 5 MB). Please use a smaller one.")
    name = f"{secrets.token_hex(12)}.{ext}"
    (photo_dir() / name).write_bytes(data)
    return PREFIX + name


def local_path(ref: str) -> Path | None:
    """The file behind an uploaded photo reference, or None for an outside link / a bad name."""
    if not ref.startswith(PREFIX):
        return None
    name = ref[len(PREFIX):]
    if not NAME.match(name):
        return None
    path = photo_dir() / name
    return path if path.is_file() else None


def delete_photo(ref: str) -> None:
    path = local_path(ref)
    if path is not None:
        path.unlink(missing_ok=True)


def mime_of(path: Path) -> str:
    return "image/png" if path.suffix == ".png" else "image/jpeg"
