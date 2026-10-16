"""Search a client's inventory from the terminal.

    python -m app.inventory --client truechoice --model swift --budget 7L
    python -m app.inventory --client truechoice --fuel diesel --min-year 2019
"""

from __future__ import annotations

import argparse
import sys

from app.core.client_config import load_client_config
from app.core.money import format_lakh, parse_budget
from app.inventory import CarQuery, build_inventory_adapter
from app.settings import get_settings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.inventory", description=__doc__.split("\n")[0])
    parser.add_argument("--client", required=True)
    parser.add_argument("--model", help='e.g. "swift", "crysta", "xuv 7oo"')
    parser.add_argument("--make")
    parser.add_argument("--budget", help='max price, e.g. "7L", "8.5 lakh", "750000"')
    parser.add_argument("--min-year", type=int)
    parser.add_argument("--fuel")
    parser.add_argument("--transmission")
    parser.add_argument("--max-km", type=int)
    parser.add_argument("--limit", type=int, default=10)
    args = parser.parse_args(argv)

    budget = parse_budget(args.budget) if args.budget else None
    if args.budget and budget is None:
        parser.error(f"could not read budget {args.budget!r}")

    sys.stdout.reconfigure(encoding="utf-8")  # ₹ on Windows consoles
    config = load_client_config(args.client, get_settings().clients_dir)
    adapter = build_inventory_adapter(config)
    cars = adapter.search_cars(CarQuery(
        budget_max=budget, min_year=args.min_year, make=args.make, model=args.model,
        fuel=args.fuel, transmission=args.transmission, max_km=args.max_km, limit=args.limit,
    ))
    print(f"{len(adapter.list_all_cars())} cars available; {len(cars)} shown")
    for car in cars:
        print(f"  {car.id}  {car.title:<40} {format_lakh(car.price):>13}  "
              f"{car.km:>7,} km  {car.fuel:<8} {car.transmission}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
