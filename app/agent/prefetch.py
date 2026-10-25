"""Look at the stock BEFORE the AI answers, when the message names a car, a budget or a fuel.

Why (live 2026-10-25): some models answered "Swift options und" without searching, which is a guess
presented as a fact, and then lost the thread. With the search done in code, every model sees the real
stock for this message, and the answer needs one AI call instead of two or three.
Only the customer's current message is read (rules, no AI); follow-ups like "ethra km?" fall back
to the conversation, as before.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from app.agent.tools import ToolContext, search_cars
from app.inventory import InventoryError
from app.leads.parse import rule_based

log = logging.getLogger(__name__)

MAX_MODELS = 2  # "Brezza and Ertiga" searches both


def _filters(want) -> dict[str, Any]:
    args: dict[str, Any] = {}
    for key, value in (("budget_min", want.budget_min), ("budget_max", want.budget_max), ("min_year", want.min_year),
                       ("fuel", want.fuel), ("transmission", want.gearbox), ("body_type", want.body_type),
                       ("max_km", want.max_km)):
        if value:
            args[key] = value
    return args


def stock_note(ctx: ToolContext, text: str, aliases: dict[str, list[str]]) -> tuple[str, bool] | None:
    """(note for the turn context, whether any car was found), or None if the message isn't about cars."""
    try:
        known = sorted({c.model for c in ctx.inventory.list_all_cars()})
    except InventoryError:
        known = []
    want = rule_based(text, aliases, known)
    if not (want.models or want.makes or want.budget_min or want.budget_max or want.fuel or want.body_type
            or want.gearbox):
        return None
    searches: list[dict[str, Any]] = []
    if want.models:
        searches = [{**_filters(want), "model": model} for model in want.models[:MAX_MODELS]]
    elif want.makes:
        searches = [{**_filters(want), "make": want.makes[0]}]
    else:
        searches = [_filters(want)]
    parts: list[str] = []
    found = False
    for args in searches:
        result = search_cars(ctx, args)
        found = found or json.loads(result.content).get("count", 0) > 0
        label = ", ".join(f"{k}={v}" for k, v in args.items())
        parts.append(f"search_cars({label}) -> {result.content}")
    log.info("Stock pre-search for %s: %s", ctx.phone, [list(a) for a in searches])
    return ("Stock search ALREADY DONE for this message (real stock; answer from it, don't guess, and don't search "
            "again unless they change what they want): " + " | ".join(parts)), found
