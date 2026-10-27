"""python -m app.resort.dashboard.create_user EMAIL PASSWORD --role owner [--name Name] [--property P-XXXX ...]"""

from __future__ import annotations

import argparse

from app.resort.dashboard.deps import get_repo
from app.resort.db import hash_password
from app.resort.models import DashboardUser


def main() -> None:
    ap = argparse.ArgumentParser(description="Create or update a dashboard user")
    ap.add_argument("email")
    ap.add_argument("password")
    ap.add_argument("--role", choices=["owner", "manager"], default="owner")
    ap.add_argument("--name", default="")
    ap.add_argument("--property", action="append", default=[], help="limit a manager to this property id (repeatable)")
    args = ap.parse_args()
    get_repo().save_user(DashboardUser(args.email.lower(), hash_password(args.password), args.name or args.email,
                                       args.role, args.property))
    print(f"Saved {args.role} {args.email.lower()}")


if __name__ == "__main__":
    main()
