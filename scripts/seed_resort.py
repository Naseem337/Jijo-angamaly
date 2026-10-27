"""Create the two starter properties (PLACEHOLDER rooms and rates) and the owner login.

    .venv\\Scripts\\python.exe scripts\\seed_resort.py

Safe to run twice: it does nothing if properties already exist. Replace every placeholder in the
dashboard once the client sends the real data. The owner password comes from RESORT_OWNER_PASSWORD,
or is generated and printed ONCE.
"""

from __future__ import annotations

import os
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.resort.db import ResortRepo, hash_password  # noqa: E402
from app.resort.models import DashboardUser, Property, RoomType  # noqa: E402
from app.settings import PROJECT_ROOT, load_dotenv  # noqa: E402

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


def main() -> None:
    load_dotenv()
    repo = ResortRepo(Path(os.environ.get("RESORT_DB_PATH") or PROJECT_ROOT / "data" / "resort.sqlite3"))
    if repo.list_properties(active_only=False):
        print("Properties already exist: nothing seeded.")
    else:
        for name, city, description in PROPERTIES:
            prop = repo.save_property(Property(name, city=city, description=description, advance_pct=50))
            for room, units, adults, children, occupancy, base, weekend in ROOMS:
                repo.save_room_type(RoomType(prop.id, room, units, adults, children, occupancy, base, weekend,
                                             description="PLACEHOLDER"))
            print(f"Created {name} with {len(ROOMS)} placeholder room types.")
    email = "owner@resort.local"
    if repo.get_user(email) is None:
        password = os.environ.get("RESORT_OWNER_PASSWORD") or secrets.token_urlsafe(9)
        repo.save_user(DashboardUser(email, hash_password(password), name="Owner", role="owner"))
        shown = "(from RESORT_OWNER_PASSWORD)" if os.environ.get("RESORT_OWNER_PASSWORD") else password
        print(f"Owner login: {email}  password: {shown}  (shown once; change it in the dashboard)")
    else:
        print("Owner user already exists.")


if __name__ == "__main__":
    main()
