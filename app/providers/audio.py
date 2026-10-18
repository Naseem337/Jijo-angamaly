"""Audio helpers: voice-note duration (pure Python) and ffmpeg-based splitting/conversion.

ffmpeg is optional for voice-in: notes up to 30 s go to speech-to-text as they are, and
only longer ones need ffmpeg to be cut into pieces. Voice-out (step 8) needs it to make
WhatsApp-compatible OGG/Opus. Locally it lives in %USERPROFILE%\\tools; on Lambda it comes
from a layer at /opt/bin.
"""

from __future__ import annotations

import os
import shutil
import struct
import subprocess
import tempfile
from pathlib import Path


class AudioError(RuntimeError):
    pass


def find_ffmpeg() -> str | None:
    candidates = [os.environ.get("FFMPEG_PATH"), shutil.which("ffmpeg"),
                  str(Path.home() / "tools" / "ffmpeg.exe"), "/opt/bin/ffmpeg"]
    return next((c for c in candidates if c and Path(c).is_file()), None)


def ogg_duration_seconds(data: bytes) -> float | None:
    """Length of an OGG/Opus voice note, read from its last page header (no decoding).

    WhatsApp voice notes are OGG/Opus, whose granule position counts 48 kHz samples.
    Returns None for anything that isn't OGG.
    """
    if not data.startswith(b"OggS"):
        return None
    last = data.rfind(b"OggS")
    if last < 0 or last + 14 > len(data) or data[last + 4] != 0:  # byte 4 = stream version, always 0
        return None
    granule = struct.unpack_from("<q", data, last + 6)[0]
    if granule <= 0:
        return None
    pre_skip = 0
    head = data.find(b"OpusHead")
    if 0 <= head and head + 12 <= len(data):
        pre_skip = struct.unpack_from("<H", data, head + 10)[0]
    return max(granule - pre_skip, 0) / 48_000


def split_to_wav_chunks(data: bytes, chunk_seconds: float, ffmpeg: str) -> list[bytes]:
    """Cut audio into mono 16 kHz WAV pieces of at most `chunk_seconds` (what STT likes)."""
    with tempfile.TemporaryDirectory(prefix="voice-") as tmp:
        src = Path(tmp) / "input"
        src.write_bytes(data)
        pattern = str(Path(tmp) / "part%03d.wav")
        try:
            subprocess.run(
                [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(src), "-ac", "1",
                 "-ar", "16000", "-f", "segment", "-segment_time", str(chunk_seconds), pattern],
                check=True, capture_output=True, timeout=60,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
            detail = getattr(exc, "stderr", b"") or b""
            raise AudioError(f"ffmpeg could not split the audio: {detail[:300]!r}") from exc
        parts = sorted(Path(tmp).glob("part*.wav"))
        if not parts:
            raise AudioError("ffmpeg produced no audio")
        return [p.read_bytes() for p in parts]  # temp files are deleted with the folder
