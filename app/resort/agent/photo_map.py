"""Every photo's room (owner, 2026-11-26).

A photo's id is its upload reference ('/resort/media/<24 hex>.jpg', unique per upload). Where the dashboard keeps it
says what it shows: Property.photo_urls = the resort in general ('resort'), RoomType.number_photos[n] = room n of that
type, RoomType.photo_urls = that room type. When the agent sends a photo or a room label, its WhatsApp id is saved with
that room (repo.save_photo_send), so a swipe-reply finds the room even if the dashboard changes later.
"""

from __future__ import annotations

import re
from typing import Any

RESORT = "resort"  # room_type_id of a general resort photo
LABEL = re.compile(r"\*(?:Room (\w+) - )?(.+?)\*")


def photo_room(repo: Any, ref: str) -> tuple[str, str, str] | None:
    """(property_id, room_type_id or 'resort', room number or '') for a photo id, or None when no one has it."""
    for p in repo.list_properties(active_only=False):
        for rt in repo.list_room_types(p.id, False):
            for num, refs in (rt.number_photos or {}).items():
                if ref in refs:
                    return p.id, rt.id, str(num)
            if ref in (rt.photo_urls or []):
                return p.id, rt.id, ""
        if ref in (p.photo_urls or []):
            return p.id, RESORT, ""
    return None


def label_room(repo: Any, text: str) -> tuple[str, str, str] | None:
    """(property_id, room_type_id, room number or '') for a room label '*Room 108 - Beach View Standard*'."""
    m = LABEL.fullmatch(text.strip())
    if not m:
        return None
    for p in repo.list_properties(active_only=False):
        for rt in repo.list_room_types(p.id, False):
            if rt.name == m.group(2).strip():
                return p.id, rt.id, m.group(1) or ""
    return None


def check(repo: Any) -> list[str]:
    """What the owner should fix so every photo maps to one room: a photo in two places, a room number's photos for a
    number not in the room list, a room type with no photo at all."""
    seen: dict[str, list[str]] = {}
    lines: list[str] = []
    for p in repo.list_properties(active_only=False):
        for ref in p.photo_urls or []:
            seen.setdefault(ref, []).append(f"{p.name}: general resort photos")
        for rt in repo.list_room_types(p.id, False):
            where = f"{p.name} / {rt.name}"
            for ref in rt.photo_urls or []:
                seen.setdefault(ref, []).append(f"{where} (room type, no room number)")
            for num, refs in (rt.number_photos or {}).items():
                for ref in refs:
                    seen.setdefault(ref, []).append(f"{where} / Room {num}")
                if refs and rt.room_list and str(num) not in [str(n) for n in rt.room_list]:
                    lines.append(f"{where}: photos for Room {num}, which is not in the room list")
            if rt.active and not rt.photo_urls and not any((rt.number_photos or {}).values()):
                lines.append(f"{where}: no photos")
    for ref, places in seen.items():
        if len(set(places)) > 1:
            lines.append(f"Photo {ref.rsplit('/', 1)[-1]} is in more than one place: {'; '.join(sorted(set(places)))}")
    return lines
