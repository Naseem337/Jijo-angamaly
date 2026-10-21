"""Run the alert engine once: tell waiting customers about newly arrived matching cars.

    python -m app.alerts --client truechoice --dry-run      # check everything, send nothing
    python -m app.alerts --client truechoice                # really send (09:00–20:00 IST only)
    python -m app.alerts --client truechoice --dry-run --pretend-new TC-A72F0B
                                                            # treat a car in stock as just arrived

The first run only records the cars already in stock, so they are never alerted.
Phones are masked except the last 4 digits. Uses no AI tokens.
"""

from __future__ import annotations

import argparse
import logging
import sys

from app.runtime import Runtime


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.alerts", description=__doc__.split("\n")[0])
    parser.add_argument("--client", required=True)
    parser.add_argument("--dry-run", action="store_true", help="do every check but send nothing")
    parser.add_argument("--pretend-new", default="", metavar="CAR_IDS",
                        help="comma-separated car ids to treat as just arrived (for testing)")
    args = parser.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

    client = Runtime.from_settings().get(args.client)
    if client is None:
        print(f"Unknown client '{args.client}'")
        return 2
    pretend = frozenset(i.strip() for i in args.pretend_new.split(",") if i.strip())
    r = client.alerts.run(dry_run=args.dry_run, pretend_new=pretend)

    print(f"Alert run for {args.client}{' (DRY RUN, nothing sent)' if r.dry_run else ''}")
    if r.aborted:
        print(f"  ABORTED: {r.aborted}. Nothing was changed.")
        return 1
    print(f"  stock: {r.cars} cars, {r.new_cars} new since the last run, {r.recent_cars} arrived in the last "
          f"{client.config.business_rules.alerts.new_car_days} days")
    if r.seeded:
        print(f"  first run: recorded the {r.seeded} cars already in stock; they will never be alerted")
    print(f"  customers waiting: {r.waiting}" + (f" ({r.expired} lead(s)/requirement(s) just expired)"
                                                if r.expired else ""))
    if r.deferred:
        print(f"  NOT SENT NOW: {r.deferred}")
    for line in r.lines:
        print(f"  - {line}")
    print(f"  result: {r.sent} sent, {r.failed} failed, {sum(r.skipped.values())} skipped")
    return 1 if r.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
