"""Test data for the agent's scratch databases (scripts/eval_agent.py and scripts/chat_resort.py --agent), never the real one.

The seed resorts have no photos and no room numbers, so the owner's photo order (glimpse -> general photos -> each room's
'*Room <number> - <room type>*' label + its photos) could not be seen. This adds, for every resort (switched on):
3 general photos, and for every room type room numbers (101, 102 ... / 201 ... by the owner's order), one photo of each
room number of its own and 2 room-type photos (fake images, saved like dashboard uploads next to the scratch database).
"""

from __future__ import annotations

from typing import Any

from app.resort import photos as ph

PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 64


def add_test_photos(repo: Any) -> None:
    for n, p in enumerate(repo.list_properties(active_only=False)):
        p.active = True
        p.photo_urls = [ph.save_photo(PNG + f"p{n}-{i}".encode()) for i in range(3)]
        repo.save_property(p)
        for m, rt in enumerate(repo.list_room_types(p.id)):
            if not rt.room_list:
                rt.room_list = [f"{(m + 1) * 100 + i + 1}" for i in range(max(1, rt.units))]
            rt.number_photos = {num: [ph.save_photo(PNG + f"r{n}-{m}-{num}".encode())] for num in rt.room_list}
            rt.photo_urls = [ph.save_photo(PNG + f"r{n}-{m}-{i}".encode()) for i in range(2)]
            repo.save_room_type(rt)
