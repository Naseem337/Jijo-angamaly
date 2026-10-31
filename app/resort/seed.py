"""Starter data: Pozhiyoram (Alleppey, real data), Kanthalloor (PLACEHOLDER rooms and rates) and the owner login.

Used by scripts/seed_resort.py and, on hosts whose disk is wiped (Render free), at startup when
RESORT_AUTO_SEED=1. Does nothing for data that already exists.
"""

from __future__ import annotations

import os
import secrets
from datetime import date

from app.resort.db import ResortRepo, hash_password
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
# Kanthalloor: still placeholders until the owner's real data is entered
KANTHALLOOR = ("Vibes Kanthalloor", "Kanthalloor, Munnar, Kerala", "Kanthalloor, Munnar",
               "PLACEHOLDER description: replace with the real one.")
# name, units, adults, children, max occupancy, base rate, weekend rate
ROOMS = [
    ("Standard Room (placeholder)", 4, 2, 1, 3, 3000, 3500),
    ("Deluxe Room (placeholder)", 3, 2, 2, 4, 4500, 5500),
    ("Family Suite (placeholder)", 2, 4, 2, 6, 7500, 9000),
]
OWNER_EMAIL = "owner@resort.local"


def _fill_alleppey(repo: ResortRepo, pz: Property) -> str:
    """Put the real Alleppey data on `pz` (new, or an older placeholder copy) and replace its placeholder rooms."""
    for key, value in ALLEPPEY.items():
        setattr(pz, key, value)
    pz.active = True
    repo.save_property(pz)
    for old in repo.list_room_types(pz.id, active_only=False):
        if "PLACEHOLDER" in old.description:  # switched off, not deleted: old bookings may point at it
            old.active = False
            repo.save_room_type(old)
    name, units, adults, children, occupancy, base = ALLEPPEY_ROOM
    rt = repo.save_room_type(RoomType(pz.id, name, units, adults, children, occupancy, base,
                                      description="Air-conditioned, beach view."))
    for start, end, rate, label in ALLEPPEY_SEASONS:
        repo.save_rate_rule(RateRule(rt.id, start, end, rate, label))
    return f"{pz.name}: {units} {name}s with seasonal rates, owner numbers and map set."


def seed(repo: ResortRepo) -> list[str]:
    """Returns human-readable lines about what was done (the generated password is included once).

    Only Pozhiyoram (Alleppey) is open to guests. Kanthalloor is created switched off until its real data is in."""
    out: list[str] = []
    props = repo.list_properties(active_only=False)
    if not props:
        out.append(_fill_alleppey(repo, Property(**ALLEPPEY)))
        name, city, place, description = KANTHALLOOR
        kt = repo.save_property(Property(name, city=city, place=place, description=description, advance_pct=50,
                                         active=False))
        for room, units, adults, children, occupancy, base, weekend in ROOMS:
            repo.save_room_type(RoomType(kt.id, room, units, adults, children, occupancy, base, weekend,
                                         description="PLACEHOLDER"))
        out.append(f"Created {name} switched off (placeholder rooms): enter the real data, then switch it on.")
    else:
        for p in props:  # an older database that still has the placeholder properties
            if p.name == ALLEPPEY["name"] and "PLACEHOLDER" in p.description:
                out.append("Upgraded: " + _fill_alleppey(repo, p))
            elif "PLACEHOLDER" in p.description and p.active:
                p.active = False
                repo.save_property(p)
                out.append(f"{p.name} switched off until its real data is entered.")
        if not out:
            out.append("Properties already exist: nothing seeded.")
    if repo.get_user(OWNER_EMAIL) is None:
        given = os.environ.get("RESORT_OWNER_PASSWORD")
        password = given or secrets.token_urlsafe(9)
        repo.save_user(DashboardUser(OWNER_EMAIL, hash_password(password), name="Owner", role="owner"))
        out.append(f"Owner login: {OWNER_EMAIL}  password: {'(from RESORT_OWNER_PASSWORD)' if given else password}"
                   "  (shown once; change it in the dashboard)")
    else:
        out.append("Owner user already exists.")
    return out
