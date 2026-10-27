"""Create the two starter properties (PLACEHOLDER rooms and rates) and the owner login.

    .venv\Scripts\python.exe scripts\seed_resort.py

Safe to run twice: it does nothing for data that already exists. Replace every placeholder in the
dashboard once the client sends the real data. The owner password comes from RESORT_OWNER_PASSWORD,
or is generated and printed ONCE.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.resort.db import ResortRepo  # noqa: E402
from app.resort.seed import seed  # noqa: E402
from app.settings import PROJECT_ROOT, load_dotenv  # noqa: E402


def main() -> None:
    load_dotenv()
    repo = ResortRepo(Path(os.environ.get("RESORT_DB_PATH") or PROJECT_ROOT / "data" / "resort.sqlite3"))
    print("\n".join(seed(repo)))


if __name__ == "__main__":
    main()
