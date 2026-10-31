"""Starter data: Pozhiyoram (Alleppey), Vibes Kanthalloor (switched off until its rooms and rates are entered) and
the owner login.

Used by scripts/seed_resort.py and, on hosts whose disk is wiped (Render free), at startup when
RESORT_AUTO_SEED=1. Does nothing for data that already exists, except tidy up databases made by an older version.
"""

from __future__ import annotations

import os
import re
import secrets
from datetime import date

from app.resort.db import ResortRepo, check_password, hash_password
from app.resort.models import DashboardUser, Property, RateRule, RoomType

ALLEPPEY = dict(
    name="Pozhiyoram Resort", city="Alleppey, Kerala", place="Pozhiyoram, Alleppey",
    description=("A peaceful 1-acre beachfront property in simple Kerala traditional style, no luxury, for guests who "
                 "want to unplug and listen to the waves. Every room has a beach view and all 9 rooms are "
                 "air-conditioned. Guests get direct 24/7 access to the shore. There is no in-house restaurant: "
                 "food can be ordered from Swiggy or Zomato and is delivered to the gate."),
    highlights=["Beach view from every room", "Direct 24/7 beach access", "All 9 rooms air-conditioned",
                "Peaceful 1-acre property"],
    contact_numbers=["+919497861924", "+919995855789"], extra_bed_rate=500, advance_pct=50,
    map_url="https://maps.app.goo.gl/4x1rKZhq8wh2Pugw8")
# room, units, adults, children, max occupancy, base rate; rates by date: (start, end, rate)
ALLEPPEY_ROOM = ("Beach View AC Room", 9, 3, 3, 3, 2500)
ALLEPPEY_SEASONS = [(date(2026, 12, 1), date(2026, 12, 14), 3000, "Dec"),
                    (date(2026, 12, 15), date(2027, 1, 5), 3500, "Christmas and New Year")]
# Kanthalloor: created switched off; the owner enters the real description, rooms and rates in the dashboard
KANTHALLOOR = ("Vibes Kanthalloor", "Kanthalloor, Munnar, Kerala", "Kanthalloor, Munnar")
# name, units, adults, children, max occupancy, base rate, weekend rate
ROOMS = [
    ("Standard Room", 4, 2, 1, 3, 3000, 3500),
    ("Deluxe Room", 3, 2, 2, 4, 4500, 5500),
    ("Family Suite", 2, 4, 2, 6, 7500, 9000),
]
DEFAULT_OWNER_EMAIL = "owner@resort.local"
# Stand-in wording that an older version wrote into descriptions and room names
OLD_MARK = re.compile(r"\s*\(placeholder\)|placeholder( description: replace with the real one\.)?", re.IGNORECASE)


def _clean(text: str) -> str:
    return OLD_MARK.sub("", text).strip()


def _fill_alleppey(repo: ResortRepo, pz: Property) -> str:
    """Put the real Alleppey data on `pz` and switch off any other room types it had."""
    for key, value in ALLEPPEY.items():
        setattr(pz, key, value)
    pz.active = True
    repo.save_property(pz)
    for old in repo.list_room_types(pz.id, active_only=False):
        if old.name != ALLEPPEY_ROOM[0]:  # switched off, not deleted: old bookings may point at it
            old.active = False
            repo.save_room_type(old)
    name, units, adults, children, occupancy, base = ALLEPPEY_ROOM
    rt = next((r for r in repo.list_room_types(pz.id, active_only=False) if r.name == name), None)
    if rt is None:
        rt = repo.save_room_type(RoomType(pz.id, name, units, adults, children, occupancy, base,
                                          description="Air-conditioned, beach view."))
        for start, end, rate, label in ALLEPPEY_SEASONS:
            repo.save_rate_rule(RateRule(rt.id, start, end, rate, label))
    return f"{pz.name}: {units} {name}s with seasonal rates, owner numbers and map set."


def _tidy_old(repo: ResortRepo, p: Property) -> bool:
    """Remove the stand-in text an older version stored, and close a property that still had it."""
    changed = False
    if OLD_MARK.search(p.description):
        p.description, p.active, changed = _clean(p.description), False, True
        repo.save_property(p)
    for rt in repo.list_room_types(p.id, active_only=False):
        if OLD_MARK.search(rt.name) or OLD_MARK.search(rt.description):
            rt.name, rt.description = _clean(rt.name), _clean(rt.description)
            repo.save_room_type(rt)
            changed = True
    return changed


def seed(repo: ResortRepo) -> list[str]:
    """Returns human-readable lines about what was done (the generated password is included once).

    Only Pozhiyoram (Alleppey) is open to guests. Kanthalloor is created switched off until its data is in."""
    out: list[str] = []
    props = repo.list_properties(active_only=False)
    if not props:
        out.append(_fill_alleppey(repo, Property(**ALLEPPEY)))
        name, city, place = KANTHALLOOR
        kt = repo.save_property(Property(name, city=city, place=place, advance_pct=50, active=False))
        for room, units, adults, children, occupancy, base, weekend in ROOMS:
            repo.save_room_type(RoomType(kt.id, room, units, adults, children, occupancy, base, weekend))
        out.append(f"Created {name} switched off: enter its real rooms and rates, then switch it on.")
    else:
        for p in props:
            if p.name == ALLEPPEY["name"] and OLD_MARK.search(p.description):
                out.append("Upgraded: " + _fill_alleppey(repo, p))
                _tidy_old(repo, repo.get_property(p.id))  # the switched-off old rooms lose the stand-in wording too
            elif _tidy_old(repo, p):
                out.append(f"{p.name}: old stand-in text removed, switched off until its data is entered.")
        if not out:
            out.append("Properties already exist: nothing seeded.")
    email = (os.environ.get("RESORT_OWNER_EMAIL") or DEFAULT_OWNER_EMAIL).strip().lower()
    given = os.environ.get("RESORT_OWNER_PASSWORD")
    owner = repo.get_user(email)
    if owner is None:
        password = given or secrets.token_urlsafe(9)
        repo.save_user(DashboardUser(email, hash_password(password), name="Owner", role="owner"))
        out.append(f"Owner login: {email}  password: {'(from RESORT_OWNER_PASSWORD)' if given else password}"
                   "  (shown once; change it in the dashboard)")
    elif given and not check_password(given, owner.password_hash):  # the setting is the source of truth
        owner.password_hash = hash_password(given)
        repo.save_user(owner)
        out.append(f"Owner password for {email} updated from RESORT_OWNER_PASSWORD.")
    else:
        out.append("Owner user already exists.")
    return out
