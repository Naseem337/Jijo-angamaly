"""Does a car fit what a customer is waiting for? Pure code, no LLM (zero tokens).

A wish is a LeadRequirement: a yard visitor's LEAD, or a chat requirement converted with
`wish_from_requirement`. Every criterion the customer gave must hold, with one allowance
(PLAN.md §5 rule 4): a *near* match may be up to `budget_over_pct` over budget OR up to
`years_older` older than asked, never both. Alerts are stricter than chat search: a car
whose gearbox or body type isn't listed does not match a wish that names one.
"""

from __future__ import annotations

from typing import Literal

from app.core.client_config import NearMatchRules
from app.core.money import format_lakh
from app.inventory import Car
from app.inventory.aliases import ModelAliases, compact
from app.storage.models import LeadRequirement, Requirement

Fit = Literal["match", "near"]

FUEL_NAMES = {"cng": "CNG", "electric": "Electric", "hybrid": "Hybrid", "petrol": "Petrol", "diesel": "Diesel"}


def wish_from_requirement(req: Requirement) -> LeadRequirement:
    """A chat requirement (save_requirement tool) in the same shape as a LEAD."""
    return LeadRequirement(budget_max=req.budget_max, min_year=req.min_year, fuel=req.fuel, max_km=req.max_km,
                           makes=[req.make] if req.make else [], models=[req.model] if req.model else [])


def _fixed_criteria_hold(car: Car, want: LeadRequirement, aliases: ModelAliases) -> bool:
    """Everything except budget_max and min_year (those may allow a near match)."""
    if want.budget_min is not None and car.price < want.budget_min:
        return False
    if want.max_km is not None and car.km > want.max_km:
        return False
    if want.max_owners is not None and car.owners > want.max_owners:
        return False
    if want.fuel and car.fuel != want.fuel.strip().lower():
        return False
    if want.gearbox and car.transmission != want.gearbox.strip().lower():
        return False
    if want.body_type and car.body_type != want.body_type.strip().lower():
        return False
    if want.makes and not any(compact(m) in compact(car.make) or compact(car.make) in compact(m)
                              for m in want.makes if m.strip()):
        return False
    if want.models and not any(aliases.car_matches(car.make, car.model, car.variant, m) for m in want.models):
        return False
    return True


def match_car(car: Car, want: LeadRequirement, aliases: ModelAliases, near: NearMatchRules) -> Fit | None:
    """'match', 'near' or None."""
    if not car.is_available or want.is_empty() or not _fixed_criteria_hold(car, want, aliases):
        return None
    over_budget = want.budget_max is not None and car.price > want.budget_max
    too_old = want.min_year is not None and car.year < want.min_year
    if not over_budget and not too_old:
        return "match"
    if over_budget and too_old:
        return None
    if over_budget:
        return "near" if car.price <= want.budget_max * (1 + near.budget_over_pct / 100) else None
    return "near" if car.year >= want.min_year - near.years_older else None


def near_label(car: Car, want: LeadRequirement) -> str:
    if want.budget_max is not None and car.price > want.budget_max:
        return "slightly above your budget"
    return "a little older than you asked"


def car_line(car: Car, fit: Fit, want: LeadRequirement) -> str:
    """One template parameter (no newlines allowed):
    'Maruti Suzuki Swift VXi 2019, 38,000 km, Petrol, ₹5.5 lakh, at Pattambi (TC-A72F0B)'."""
    name = " ".join(p for p in (car.make, car.model, car.variant, str(car.year)) if p)
    parts = [name, f"{car.km:,} km", FUEL_NAMES.get(car.fuel, car.fuel), format_lakh(car.price)]
    if fit == "near":
        parts[-1] += f" ({near_label(car, want)})"
    if car.location:
        parts.append(f"at {car.location}")
    return ", ".join(parts) + f" ({car.id})"


def rank_key(car: Car, fit: Fit, want: LeadRequirement) -> tuple:
    """Exact before near, then the price closest to the budget, then the newest."""
    gap = abs(want.budget_max - car.price) if want.budget_max else 0
    return (fit != "match", gap, -car.year, car.id)
