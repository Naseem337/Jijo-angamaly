"""Starter data: the two properties (PLACEHOLDER rooms and rates) and the owner login.

Used by scripts/seed_resort.py and, on hosts whose disk is wiped (Render free), at startup when
RESORT_AUTO_SEED=1. Does nothing for data that already exists.
"""

from __future__ import annotations

import os
import secrets

from app.resort.db import ResortRepo, hash_password
from app.resort.models import DashboardUser, Property, RoomType

PROPERTIES = [
    ("Pozhiyoram Resort", "Pozhiyoram, Kerala", "PLACEHOLDER description: replace with the real one."),
    ("Vibes Kanthalloor", "Kanthalloor, Munnar, Kerala", "PLACEHOLDER description: replace with the real one."),
]
# name, units, adults, children, max occupancy, base rate, weekend rate
ROOMS = [
    ("Standard Room (placeholder)", 4, 2, 1, 3, 3000, 3500),
    ("Deluxe Room (placeholder)", 3, 2, 2, 4, 4500, 5500),
    ("Family Suite (placeholder)", 2, 4, 2, 6, 7500, 9000),
]
OWNER_EMAIL = "owner@resort.local"


def seed(repo: ResortRepo) -> list[str]:
    """Returns human-readable lines about what was created (the generated password is included once)."""
    out: list[str] = []
    if repo.list_properties(active_only=False):
        out.append("Properties already exist: nothing seeded.")
    else:
        for name, city, description in PROPERTIES:
            prop = repo.save_property(Property(name, city=city, description=description, advance_pct=50))
            for room, units, adults, children, occupancy, base, weekend in ROOMS:
                repo.save_room_type(RoomType(prop.id, room, units, adults, children, occupancy, base, weekend,
                                             description="PLACEHOLDER"))
            out.append(f"Created {name} with {len(ROOMS)} placeholder room types.")
    if repo.get_user(OWNER_EMAIL) is None:
        given = os.environ.get("RESORT_OWNER_PASSWORD")
        password = given or secrets.token_urlsafe(9)
        repo.save_user(DashboardUser(OWNER_EMAIL, hash_password(password), name="Owner", role="owner"))
        out.append(f"Owner login: {OWNER_EMAIL}  password: {'(from RESORT_OWNER_PASSWORD)' if given else password}"
                   "  (shown once; change it in the dashboard)")
    else:
        out.append("Owner user already exists.")
    return out
