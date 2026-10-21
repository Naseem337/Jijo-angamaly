"""The agent's tools: JSON-schema specs for the LLM plus their implementations.

Every tool returns JSON text. Bad arguments come back as {"error": ...} with is_error set,
so the model can correct itself instead of the turn crashing. When the customer spoke a
voice note, save_enquiry / save_requirement don't write: they store a PendingAction and ask
the model to read the details back (see confirmation.py).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from app.agent.emi import estimate
from app.core.client_config import ClientConfig
from app.core.money import format_inr, format_lakh, parse_budget
from app.inventory import CarQuery, InventoryAdapter, InventoryError
from app.providers.llm import ToolCall, ToolSpec
from app.storage.models import Enquiry, PendingAction, Requirement
from app.storage.repository import Repository

SEARCH_LIMIT = 3  # the reply shows at most 3 cars; fewer rows = fewer tokens


@dataclass
class HandoffRequest:
    reason: str
    summary: str


@dataclass
class ToolContext:
    config: ClientConfig
    repo: Repository
    inventory: InventoryAdapter
    phone: str
    now: datetime
    language: str = "en"
    source: str = "text"  # "voice" holds save_* for confirmation
    handoff: HandoffRequest | None = None
    saved: list[str] = field(default_factory=list)  # e.g. ["enquiry:<id>"], for logs/tests
    photos: list[str] = field(default_factory=list)  # car ids whose main photo the router sends
    enforce_language: bool = True  # check the reply is in `language` (off for a new customer's "Hi")


@dataclass(frozen=True)
class ToolResult:
    content: str
    is_error: bool = False


class ToolArgError(ValueError):
    """Bad tool arguments; the message is shown to the model."""


# --------------------------------------------------------------------------- specs

_INT = {"type": "integer"}
_STR = {"type": "string"}

TOOL_SPECS: list[ToolSpec] = [
    ToolSpec(
        "search_cars",
        "Available cars, best 3 first. Filters optional; money in rupees.",
        {"type": "object", "properties": {
            "budget_max": _INT, "budget_min": _INT, "make": _STR, "model": _STR,
            "min_year": _INT, "max_year": _INT,
            "fuel": {"type": "string", "enum": ["petrol", "diesel", "cng", "electric", "hybrid"]},
            "transmission": {"type": "string", "enum": ["manual", "automatic"]},
            "body_type": {"type": "string", "enum": ["hatchback", "sedan", "suv", "muv"]},
            "max_km": _INT,
        }},
    ),
    ToolSpec(
        "get_car_details",
        "One car's details and availability, by id.",
        {"type": "object", "properties": {"car_id": _STR}, "required": ["car_id"]},
    ),
    ToolSpec(
        "emi_estimate",
        "Indicative EMI range for a car_id or a price (rupees). Not a loan offer.",
        {"type": "object", "properties": {
            "car_id": _STR, "price": _INT, "down_payment": _INT,
            "tenure_months": {**_INT, "description": "omit for several tenures"},
        }},
    ),
    ToolSpec(
        "save_enquiry",
        "Record a visit, test drive, call back or booking request.",
        {"type": "object", "properties": {
            "name": _STR,
            "interest": {**_STR, "description": "e.g. 'test drive TC-1002 Swift'"},
            "budget": _INT,
            "notes": {**_STR, "description": "time, exchange car, …"},
        }, "required": ["interest"]},
    ),
    ToolSpec(
        "save_requirement",
        "Save a wish for an alert when a matching car arrives. consent=true only after a clear yes.",
        {"type": "object", "properties": {
            "consent": {"type": "boolean"},
            "budget_max": _INT,
            "min_year": _INT,
            "make": _STR,
            "model": _STR,
            "fuel": {"type": "string", "enum": ["petrol", "diesel", "cng", "electric", "hybrid"]},
            "max_km": _INT,
            "notes": _STR,
        }, "required": ["consent"]},
    ),
    ToolSpec(
        "send_car_photo",
        "Send the main photo of up to 3 cars (by id) right after your reply.",
        {"type": "object", "properties": {"car_ids": {"type": "array", "items": _STR}},
         "required": ["car_ids"]},
    ),
    ToolSpec(
        "handoff_to_human",
        "Pass the chat to the sales team.",
        {"type": "object", "properties": {
            "reason": {**_STR, "description": "short, e.g. 'wants discount'"},
            "summary": {**_STR, "description": "one line: what they want"},
        }, "required": ["reason", "summary"]},
    ),
]


# --------------------------------------------------------------------------- argument helpers


def _money(args: dict[str, Any], key: str) -> int | None:
    """Rupee amount; tolerates '8 lakh', '8L', 8.5 (lakh) or '800000'."""
    value = args.get(key)
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ToolArgError(f"{key} must be an amount in rupees")
    if isinstance(value, (int, float)):
        amount = value * 100_000 if 0 < value < 100 else value  # 8 or 8.5 means lakh
        return int(round(amount))
    parsed = parse_budget(str(value))
    if parsed is None:
        raise ToolArgError(f"{key} must be an amount in rupees, got {value!r}")
    return parsed


def _int(args: dict[str, Any], key: str, lo: int, hi: int) -> int | None:
    value = args.get(key)
    if value is None or value == "":
        return None
    try:
        number = int(float(str(value).replace(",", "").replace("km", "").strip()))
    except ValueError as exc:
        raise ToolArgError(f"{key} must be a number, got {value!r}") from exc
    if not lo <= number <= hi:
        raise ToolArgError(f"{key} must be between {lo} and {hi}")
    return number


def _text(args: dict[str, Any], key: str) -> str | None:
    value = args.get(key)
    return str(value).strip() or None if value is not None else None


def _dump(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False)


# --------------------------------------------------------------------------- tools


def search_cars(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    query = CarQuery(
        budget_min=_money(args, "budget_min"),
        budget_max=_money(args, "budget_max"),
        min_year=_int(args, "min_year", 1990, 2100),
        max_year=_int(args, "max_year", 1990, 2100),
        make=_text(args, "make"),
        model=_text(args, "model"),
        fuel=_text(args, "fuel"),
        transmission=_text(args, "transmission"),
        body_type=_text(args, "body_type"),
        max_km=_int(args, "max_km", 0, 2_000_000),
        limit=SEARCH_LIMIT,
    )
    cars = ctx.inventory.search_cars(query)
    # One line per car instead of a JSON object each: same facts, about a third of the tokens.
    return ToolResult(_dump({"count": len(cars), "cars": [c.to_line() for c in cars]}))


def get_car_details(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    car_id = (_text(args, "car_id") or "").upper()
    car = ctx.inventory.get_car(car_id) if car_id else None
    if car is None:
        return ToolResult(_dump({"error": f"no car with id {car_id or '(missing)'}"}), is_error=True)
    details = car.to_summary()
    if not car.is_available:
        why = {"booked": "already booked by another customer", "sold": "sold",
               "coming_soon": "not ready for sale yet (under inspection); it may be available soon"}
        details["note"] = f"This car is {why.get(car.status, car.status)}. Do not offer it."
    return ToolResult(_dump(details))


def emi_estimate(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    price = _money(args, "price")
    car_id = _text(args, "car_id")
    if car_id:
        car = ctx.inventory.get_car(car_id.upper())
        if car is None:
            raise ToolArgError(f"no car with id {car_id}")
        price = car.price
    if not price:
        raise ToolArgError("give a car_id or a price")
    rules = ctx.config.business_rules.emi
    est = estimate(price, rules, down_payment=_money(args, "down_payment"),
                   tenure_months=_int(args, "tenure_months", 6, 84))
    disclaimer = rules.disclaimer_ml if ctx.language == "ml" else rules.disclaimer_en
    return ToolResult(_dump({
        "price": est.price, "price_text": format_inr(est.price),
        "down_payment": est.down_payment, "down_payment_text": format_inr(est.down_payment),
        "loan_amount": est.loan_amount, "loan_amount_text": format_lakh(est.loan_amount),
        "interest_rate_range_pct": [est.rate_min_pct, est.rate_max_pct],
        "options": [
            {"tenure_months": o.tenure_months, "emi_min": o.emi_min, "emi_max": o.emi_max,
             "emi_text": f"{format_inr(o.emi_min)} – {format_inr(o.emi_max)} per month"}
            for o in est.options
        ],
        "disclaimer": disclaimer,
    }))


def _hold_for_voice(ctx: ToolContext, tool: str, args: dict[str, Any]) -> ToolResult:
    ctx.repo.set_pending_action(PendingAction(ctx.config.client_id, ctx.phone, tool, args, ctx.now))
    return ToolResult(_dump({
        "status": "needs_confirmation",
        "instruction": "Not saved yet. Read these details back to the customer in one short "
                       "sentence and ask them to confirm with yes or no.",
        "details": args,
    }))


def save_enquiry(ctx: ToolContext, args: dict[str, Any], *, confirmed: bool = False) -> ToolResult:
    interest = _text(args, "interest")
    if not interest:
        raise ToolArgError("interest is required")
    budget = _money(args, "budget")
    if ctx.source == "voice" and not confirmed:
        return _hold_for_voice(ctx, "save_enquiry", args)
    name = _text(args, "name")
    if name:
        customer = ctx.repo.get_or_create_customer(ctx.config.client_id, ctx.phone, ctx.now)
        customer.name = name
        ctx.repo.save_customer(customer)
    enquiry = Enquiry(ctx.config.client_id, ctx.phone, ctx.now, name=name, interest=interest,
                budget=budget, notes=_text(args, "notes"), source=ctx.source)
    ctx.repo.create_enquiry(enquiry)
    ctx.saved.append(f"enquiry:{enquiry.id}")
    return ToolResult(_dump({"status": "saved", "message": "The sales team will contact the customer."}))


def save_requirement(ctx: ToolContext, args: dict[str, Any], *, confirmed: bool = False) -> ToolResult:
    if args.get("consent") is not True:
        raise ToolArgError("not saved: first ask the customer if we may WhatsApp them when a "
                           "matching car arrives, and call again with consent=true only if they agree")
    req = Requirement(
        ctx.config.client_id, ctx.phone, ctx.now,
        ctx.now + timedelta(days=ctx.config.business_rules.alerts.requirement_ttl_days),
        budget_max=_money(args, "budget_max"),
        min_year=_int(args, "min_year", 1990, 2100),
        make=_text(args, "make"),
        model=_text(args, "model"),
        fuel=_text(args, "fuel"),
        max_km=_int(args, "max_km", 0, 2_000_000),
        notes=_text(args, "notes"),
        source=ctx.source,
    )
    if not any([req.budget_max, req.min_year, req.make, req.model, req.fuel, req.max_km]):
        raise ToolArgError("give at least one of budget_max, model, make, fuel, min_year, max_km")
    if ctx.source == "voice" and not confirmed:
        return _hold_for_voice(ctx, "save_requirement", args)
    for existing in ctx.repo.list_requirements(ctx.config.client_id, "active", ctx.phone):
        if (existing.model, existing.make, existing.budget_max) == (req.model, req.make, req.budget_max):
            return ToolResult(_dump({"status": "already_saved"}))
    ctx.repo.set_consent(ctx.config.client_id, ctx.phone, source="voice" if ctx.source == "voice" else "chat",
                         recorded_by="customer", at=ctx.now)
    ctx.repo.create_requirement(req)
    ctx.saved.append(f"requirement:{req.id}")
    return ToolResult(_dump({
        "status": "saved",
        "message": f"We will WhatsApp the customer about matching cars for "
                   f"{ctx.config.business_rules.alerts.requirement_ttl_days} days. They can reply STOP anytime.",
    }))


MAX_PHOTOS_PER_TURN = 3  # cost control: one main photo per car, at most 3 cars per message


def send_car_photo(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    ids = args.get("car_ids")
    ids = [ids] if isinstance(ids, str) else ids
    if not isinstance(ids, list) or not ids:
        raise ToolArgError("give car_ids, e.g. ['TC-A72F0B']")
    queued, missing = [], []
    for raw in ids:
        car_id = str(raw).strip().upper()
        car = ctx.inventory.get_car(car_id)
        if car is None or not car.image_url:
            missing.append(car_id)
        elif car_id not in ctx.photos and len(ctx.photos) < MAX_PHOTOS_PER_TURN:
            ctx.photos.append(car_id)
            queued.append(car_id)
    return ToolResult(_dump({
        "will_send_photo_of": queued,
        **({"no_photo_for": missing} if missing else {}),
        "note": "One main photo per car is sent after your reply; don't describe it. More photos or a video: "
                "the team sends them (handoff_to_human if they want them).",
    }))


def handoff_to_human(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    ctx.handoff = HandoffRequest(_text(args, "reason") or "customer request",
                                 _text(args, "summary") or "")
    return ToolResult(_dump({
        "status": "handed_off",
        "instruction": "Tell the customer a member of the sales team will contact them "
                       "(today if the showroom is open, otherwise the next working morning).",
    }))


_TOOLS = {
    "search_cars": search_cars,
    "get_car_details": get_car_details,
    "emi_estimate": emi_estimate,
    "save_enquiry": save_enquiry,
    "save_requirement": save_requirement,
    "send_car_photo": send_car_photo,
    "handoff_to_human": handoff_to_human,
}


def run_tool(ctx: ToolContext, call: ToolCall) -> ToolResult:
    """Run one tool call. Never raises for bad input or a broken inventory feed."""
    if call.arguments_error:
        return ToolResult(_dump({"error": call.arguments_error}), is_error=True)
    tool = _TOOLS.get(call.name)
    if tool is None:
        return ToolResult(_dump({"error": f"unknown tool {call.name}"}), is_error=True)
    try:
        return tool(ctx, call.arguments)
    except ToolArgError as exc:
        return ToolResult(_dump({"error": str(exc)}), is_error=True)
    except InventoryError:
        return ToolResult(_dump({"error": "stock list is unavailable right now; offer a call "
                                          "from the sales team instead"}), is_error=True)


def commit_pending(ctx: ToolContext, action: PendingAction) -> ToolResult:
    """The customer confirmed a voice read-back: really save it now."""
    tool = {"save_enquiry": save_enquiry, "save_lead": save_enquiry,  # pre-2026-10-20 name
            "save_requirement": save_requirement}[action.tool]
    try:
        return tool(ctx, action.args, confirmed=True)
    except ToolArgError as exc:
        return ToolResult(_dump({"error": str(exc)}), is_error=True)
