"""List saved yard-visitor leads.

    python -m app.leads --client truechoice
    python -m app.leads --client truechoice --status active

Phones are masked except the last 4 digits.
"""

from __future__ import annotations

import argparse
import sys

from app.core.client_config import load_client_config
from app.core.clock import SystemClock, to_ist
from app.core.phone import mask_phone
from app.runtime import build_repository
from app.settings import get_settings
from app.storage.models import LEAD_STATUSES


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.leads", description=__doc__.split("\n")[0])
    parser.add_argument("--client", required=True)
    parser.add_argument("--status", choices=LEAD_STATUSES)
    parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")

    settings = get_settings()
    config = load_client_config(args.client, settings.clients_dir)
    repo = build_repository(settings)
    expired = repo.expire_leads(config.client_id, SystemClock().now())
    leads = repo.list_leads(config.client_id, status=args.status, limit=args.limit)
    if expired:
        print(f"({expired} lead(s) just expired)")
    if not leads:
        print("No leads saved yet.")
        return 0
    print(f"{len(leads)} lead(s):")
    for lead in leads:
        added = to_ist(lead.created_at).strftime("%d %b")
        name = f" {lead.name}" if lead.name else ""
        print(f"  {mask_phone(lead.phone)}{name} | {lead.requirement.summary()} | status: {lead.status} | "
              f"stage: {lead.stage} | added {added}, expires {to_ist(lead.expires_at):%d %b %Y}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
