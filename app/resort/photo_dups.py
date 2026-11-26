"""Photo ids, duplicates and 'Common' photos (owner, 2026-11-27; no image recognition).

Every photo's stable id is its reference ("/resort/media/<name>"). Two photos are duplicates when they have the same id,
the same uploaded file name (case and folder ignored: "courtyard.jpg" in two room types) or the same file bytes (sha256;
exact copies only). A photo ticked "Common" in the dashboard belongs to the resort (courtyard, beach, entrance), not to
a room: it goes with the resort's own photos, once per chat, and never as a room's photo. The resort's own photo list
(Property.photo_urls) is always common. Nothing here deletes a photo by itself.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from pathlib import PurePath
from typing import Any

log = logging.getLogger(__name__)


@dataclass
class Place:
    ref: str
    property_id: str
    property_name: str
    room_type_id: str = ""  # "" = the resort's own photos
    room_type_name: str = ""
    number: str = ""  # a room number's own photos

    @property
    def where(self) -> str:
        if not self.room_type_id:
            return f"{self.property_name}: resort photos"
        return f"{self.property_name} / {self.room_type_name}" + (f" / room {self.number}" if self.number else "")


@dataclass
class Group:
    places: list[Place] = field(default_factory=list)
    why: set[str] = field(default_factory=set)  # same id | same name | same file


def clean_name(filename: str) -> str:
    """The uploaded file's own name, without its folder, in lower case ('' when unknown)."""
    return PurePath((filename or "").replace("\\", "/")).name.strip().lower()


def sha_of_file(ref: str) -> str:
    from app.resort import photos as ph
    path = ph.local_path(ref)
    if path is None:
        return ""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def places(repo: Any) -> list[Place]:
    """Every photo in the dashboard, each time it appears (resort photos, room types, room numbers; switched-off ones
    too, so the owner sees every copy)."""
    out: list[Place] = []
    for p in repo.list_properties(active_only=False):
        out += [Place(r, p.id, p.name) for r in p.photo_urls or []]
        for rt in repo.list_room_types(p.id, False):
            out += [Place(r, p.id, p.name, rt.id, rt.name) for r in rt.photo_urls or []]
            for num, refs in (rt.number_photos or {}).items():
                out += [Place(r, p.id, p.name, rt.id, rt.name, num) for r in refs or []]
    return out


def infos(repo: Any) -> dict[str, dict[str, Any]]:
    """Every photo's info; a missing checksum is worked out once from the file and saved."""
    known = repo.photo_infos()
    for pl in places(repo):
        if not (known.get(pl.ref) or {}).get("sha"):
            if sha := sha_of_file(pl.ref):
                repo.save_photo_info(pl.ref, sha=sha)
                known[pl.ref] = {**known.get(pl.ref, {"name": "", "common": False}), "sha": sha}
    return known


def keys(ref: str, info: dict[str, dict[str, Any]]) -> set[str]:
    """What makes two photos the same: the id, the file name, the file bytes."""
    i = info.get(ref) or {}
    out = {"id:" + ref}
    if i.get("name"):
        out.add("name:" + i["name"])
    if i.get("sha"):
        out.add("file:" + i["sha"])
    return out


def is_common(ref: str, info: dict[str, dict[str, Any]], prop: Any = None) -> bool:
    return bool((info.get(ref) or {}).get("common")) or bool(prop is not None and ref in (prop.photo_urls or []))


def common_photos(repo: Any, prop: Any, info: dict[str, dict[str, Any]] | None = None) -> list[str]:
    """The resort's common photos: its own photo list, then every room photo ticked Common, without duplicates."""
    info = info if info is not None else infos(repo)
    refs = list(prop.photo_urls or [])
    for rt in repo.list_room_types(prop.id):
        refs += [r for r in [*rt.photo_urls, *(x for v in (rt.number_photos or {}).values() for x in v)]
                 if (info.get(r) or {}).get("common")]
    out: list[str] = []
    seen: set[str] = set()
    for r in refs:
        k = keys(r, info)
        if k & seen:
            continue
        seen |= k
        out.append(r)
    return out


def groups(repo: Any) -> list[Group]:
    """Duplicate groups: photos joined by the same id, name or file (a photo in two places counts too)."""
    info = infos(repo)
    all_places = places(repo)
    parent = list(range(len(all_places)))

    def root(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    first: dict[str, int] = {}
    why: dict[tuple[int, int], str] = {}
    for i, pl in enumerate(all_places):
        for k in keys(pl.ref, info):
            if k in first:
                a, b = root(first[k]), root(i)
                if a != b:
                    parent[b] = a
                why[(min(first[k], i), max(first[k], i))] = {"id": "same photo", "name": "same name",
                                                             "file": "same file"}[k.split(":", 1)[0]]
            else:
                first[k] = i
    found: dict[int, Group] = {}
    for i, pl in enumerate(all_places):
        found.setdefault(root(i), Group()).places.append(pl)
    for (a, _), reason in why.items():
        found[root(a)].why.add(reason)
    return [g for g in found.values() if len(g.places) > 1]


def worth_listing(g: Group) -> bool:
    """A group the owner should see (owner, 2026-11-27): copies across different room types (or the resort photos), or
    twice in the same room number's list. One room type sharing a set over its room numbers (101/102) is intended."""
    if len({(pl.property_id, pl.room_type_id) for pl in g.places}) > 1:
        return True
    spots = [(pl.property_id, pl.room_type_id, pl.number) for pl in g.places]
    return len(spots) != len(set(spots))


def listed_groups(repo: Any) -> list[Group]:
    """The duplicate groups shown on the Duplicate photos page."""
    return [g for g in groups(repo) if worth_listing(g)]


def where_else(repo: Any, ref: str) -> list[str]:
    """Other places with a duplicate of this photo (for the upload warning)."""
    info = infos(repo)
    mine = keys(ref, info)
    return list(dict.fromkeys(pl.where for pl in places(repo) if pl.ref != ref and keys(pl.ref, info) & mine))


class SentPhotos(set):
    """The photos sent in this chat. `ref in sent` is True when the same photo, or a duplicate of it (same name or
    file), was sent."""

    def __init__(self, refs: Any = (), info: dict[str, dict[str, Any]] | None = None) -> None:
        super().__init__(refs)
        self.info = info or {}

    def __contains__(self, ref: object) -> bool:
        if set.__contains__(self, ref):
            return True
        if not isinstance(ref, str):
            return False
        mine = keys(ref, self.info) - {"id:" + ref}
        return bool(mine) and any(mine & keys(s, self.info) for s in set.__iter__(self))

    def copy(self) -> SentPhotos:
        return SentPhotos(set.__iter__(self), self.info)
