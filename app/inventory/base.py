"""Inventory interface: the Car record, the search query, and the InventoryAdapter base.

Every client's stock comes through an InventoryAdapter (mock JSON today, the dealer's
real API later). The agent calls search_cars(); the alert engine calls list_all_cars()
for new arrivals and get_car() to re-check availability right before sending.

Filtering lives here (apply_query), so an adapter whose API can't filter can still
fetch everything and filter locally with exactly the same rules as the mock.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.core.money import format_inr, format_lakh
from app.inventory.aliases import ModelAliases, compact

Fuel = Literal["petrol", "diesel", "cng", "electric", "hybrid"]
Transmission = Literal["manual", "automatic"]
BodyType = Literal["hatchback", "sedan", "suv", "muv", "pickup", "other"]
CarStatus = Literal["available", "booked", "sold", "coming_soon"]  # coming_soon: e.g. under inspection


class InventoryError(RuntimeError):
    """The inventory source is unreachable or returned data we can't use."""


class Car(BaseModel):
    """One car in a dealer's stock. Prices are whole rupees; km is the odometer reading."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    id: str = Field(min_length=1)
    make: str = Field(min_length=1)
    model: str = Field(min_length=1)
    variant: str = ""
    year: int = Field(ge=1990, le=2100)
    price: int = Field(gt=0)
    km: int = Field(ge=0)
    fuel: Fuel
    transmission: Transmission | None = None  # None = not listed (e.g. the sheet has no column)
    owners: int = Field(1, ge=1)
    color: str = ""
    body_type: BodyType = "other"
    registration: str = ""  # where it is registered: RTO code ("KL-07") or city ("Delhi")
    location: str = ""
    status: CarStatus = "available"
    listed_on: date | None = None
    highlights: list[str] = []
    mileage_kmpl: float | None = None  # expected fuel efficiency, km per litre
    tyre_condition_pct: int | None = Field(None, ge=0, le=100)

    @field_validator("fuel", "transmission", "body_type", "status", mode="before")
    @classmethod
    def _lower(cls, v: Any) -> Any:
        return v.strip().lower() if isinstance(v, str) else v

    @property
    def title(self) -> str:
        """'2019 Maruti Suzuki Swift VXi'."""
        return " ".join(p for p in (str(self.year), self.make, self.model, self.variant) if p)

    @property
    def is_available(self) -> bool:
        return self.status == "available"

    def to_line(self) -> str:
        """Compact form for search results (the LLM reads it; guardrails find every number in it):
        'TC-1002 | 2021 Maruti Suzuki Swift ZXi AMT | ₹6,95,000 (₹6.95 lakh) | 28,500 km | petrol |
        automatic | 1 owner | White | reg Delhi | tyres 80% | 18.9 km/l'"""
        parts = [self.id, self.title, f"{format_inr(self.price)} ({format_lakh(self.price)})", f"{self.km:,} km",
                 self.fuel, self.transmission or "gearbox not listed", f"{self.owners} owner"]
        parts += [p for p in (self.color, self.body_type if self.body_type != "other" else "") if p]
        if self.registration:
            parts.append(f"reg {self.registration}")
        if self.tyre_condition_pct is not None:
            parts.append(f"tyres {self.tyre_condition_pct}%")
        if self.mileage_kmpl is not None:
            parts.append(f"{self.mileage_kmpl:g} km/l")
        parts += list(self.highlights)
        return " | ".join(parts)

    def to_summary(self) -> dict[str, Any]:
        """What the LLM sees in a search_cars result. The guardrails later check that any
        price, year or km the agent states appears here, so keep the formatted forms."""
        return {
            "id": self.id,
            "title": self.title,
            "year": self.year,
            "price": self.price,
            "price_text": f"{format_inr(self.price)} ({format_lakh(self.price)})",
            "km": self.km,
            "fuel": self.fuel,
            "transmission": self.transmission or "not listed",
            "owners": self.owners,
            "color": self.color,
            "body_type": self.body_type,
            "registration": self.registration,
            "location": self.location,
            "status": self.status,
            "highlights": list(self.highlights),
            **({"mileage_kmpl": self.mileage_kmpl} if self.mileage_kmpl is not None else {}),
            **({"tyre_condition_pct": self.tyre_condition_pct} if self.tyre_condition_pct is not None else {}),
        }


@dataclass(frozen=True)
class CarQuery:
    """Search filters. Every field is optional; None means 'any'."""

    budget_min: int | None = None
    budget_max: int | None = None
    min_year: int | None = None
    max_year: int | None = None
    make: str | None = None
    model: str | None = None  # "swift", "crysta", "xuv 7oo", even "maruti swift"
    fuel: str | None = None
    transmission: str | None = None
    body_type: str | None = None
    max_km: int | None = None
    limit: int = 5


def _matches(car: Car, q: CarQuery, aliases: ModelAliases) -> bool:
    if q.budget_min is not None and car.price < q.budget_min:
        return False
    if q.budget_max is not None and car.price > q.budget_max:
        return False
    if q.min_year is not None and car.year < q.min_year:
        return False
    if q.max_year is not None and car.year > q.max_year:
        return False
    if q.max_km is not None and car.km > q.max_km:
        return False
    if q.fuel and car.fuel != q.fuel.strip().lower():
        return False
    # Unknown transmission / body type don't exclude a car: the agent sees "not listed"
    # and can say it will check, which beats hiding half the stock of a sparse sheet.
    if q.transmission and car.transmission and car.transmission != q.transmission.strip().lower():
        return False
    if q.body_type and car.body_type != "other" and car.body_type != q.body_type.strip().lower():
        return False
    if q.make:
        wanted, have = compact(q.make), compact(car.make)
        if wanted not in have and have not in wanted:  # "maruti" vs "maruti suzuki"
            return False
    if q.model and not aliases.car_matches(car.make, car.model, car.variant, q.model):
        return False
    return True


def apply_query(cars: Iterable[Car], q: CarQuery, aliases: ModelAliases | None = None) -> list[Car]:
    """Available cars matching every filter, best first, at most q.limit.

    With a budget, the cars closest to (but within) the budget come first, because that
    is what customers usually mean. Without one, the newest listings come first.
    """
    aliases = aliases or ModelAliases()
    found = [c for c in cars if c.is_available and _matches(c, q, aliases)]
    if q.budget_max is not None:
        found.sort(key=lambda c: (-c.price, -c.year, c.id))
    else:
        found.sort(key=lambda c: (c.listed_on or date.min, c.year, c.id), reverse=True)
    return found[: max(q.limit, 0)]


def load_cars_json(path: Path) -> list[Car]:
    """Read a {"cars": [...]} file. Raises InventoryError naming the bad car and field."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise InventoryError(f"inventory file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise InventoryError(f"{path} is not valid JSON: {exc}") from exc
    rows = data.get("cars") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise InventoryError(f'{path} must contain an object with a "cars" list')

    cars: list[Car] = []
    seen: set[str] = set()
    for index, row in enumerate(rows):
        label = row.get("id", f"#{index}") if isinstance(row, dict) else f"#{index}"
        try:
            car = Car.model_validate(row)
        except ValidationError as exc:
            raise InventoryError(f"{path}: car {label} is invalid:\n{exc}") from exc
        if car.id in seen:
            raise InventoryError(f"{path}: duplicate car id {car.id}")
        seen.add(car.id)
        cars.append(car)
    return cars


class InventoryAdapter(ABC):
    """One dealer's stock. Implementations raise InventoryError when the source fails,
    never return an empty list instead (the alert engine treats 0 cars as an outage)."""

    @abstractmethod
    def search_cars(self, query: CarQuery) -> list[Car]:
        """Available cars matching the query, best first."""

    @abstractmethod
    def list_all_cars(self) -> list[Car]:
        """Every *available* car (used by the alert engine to find new arrivals)."""

    def get_car(self, car_id: str) -> Car | None:
        """Fresh lookup of one car. None, or a car whose is_available is False, both mean
        'do not offer it'. Adapters with a single-car endpoint should override this."""
        return next((c for c in self.list_all_cars() if c.id == car_id), None)
