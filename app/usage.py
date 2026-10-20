"""How much AI the bot used, per day (counted by the agent on every customer turn).

    python -m app.usage --client truechoice            # last 7 days
    python -m app.usage --client truechoice --days 30

"cached" input tokens were billed at the provider's (much lower) cache price.
"""

from __future__ import annotations

import argparse
import sys
from datetime import timedelta

from app.agent.agent import TOKEN_COUNTERS
from app.core.client_config import load_client_config
from app.core.clock import SystemClock, ist_today
from app.runtime import build_repository
from app.settings import get_settings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.usage", description=__doc__.split("\n")[0])
    parser.add_argument("--client", required=True)
    parser.add_argument("--days", type=int, default=7)
    args = parser.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")

    settings = get_settings()
    config = load_client_config(args.client, settings.clients_dir)
    repo = build_repository(settings)
    today = ist_today(SystemClock())
    print(f"{'day':<12}{'AI calls':>10}{'tokens in':>12}{'(cached)':>11}{'tokens out':>12}")
    totals = dict.fromkeys(TOKEN_COUNTERS, 0)
    for back in range(args.days - 1, -1, -1):
        day = (today - timedelta(days=back)).isoformat()
        row = {name: repo.get_counter(config.client_id, f"llm_{name}", day) for name in TOKEN_COUNTERS}
        for name in TOKEN_COUNTERS:
            totals[name] += row[name]
        if any(row.values()):
            print(f"{day:<12}{row['calls']:>10,}{row['tokens_in']:>12,}{row['tokens_cached']:>11,}{row['tokens_out']:>12,}")
    print(f"{'total':<12}{totals['calls']:>10,}{totals['tokens_in']:>12,}{totals['tokens_cached']:>11,}{totals['tokens_out']:>12,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
