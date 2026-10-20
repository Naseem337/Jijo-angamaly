"""Inventory from a dealer's Carlet app backend (https://api.carlet.in), read-only.

True Choice's mobile app runs on Carlet. Its public car list is the same stock customers see
in the app, so the bot reads it instead of a spreadsheet. We never write to it, and our own
data (chats, leads, alerts) stays in our own database.

Checked live on 2026-10-20 (GET /api/car/list, no auth, 174 cars, ~160 KB, all at once):
  {"success": true, "message": null, "data": [ {
     "_id": "6ad7…", "showroom": "Makkaraparamba", "brand": "maruti suzuki", "model": "ciaz zeta",
     "rate": 595000, "maximumLoan": 525000, "makeYear": 2018, "regDate": "2018-07-12T18:30:00.000Z",
     "bodyType": "sedan" | "hatchback" | "suv" | "compact suv" | "mpv",
     "owners": "1".."4+", "insuranceValidity": "2027-01-22T…", "kmsDriven": 38000, "color": "white",
     "fuel": "Petrol" | "Diesel" | "CNG + Petrol" | "Electric", "transmission": "manual" | "automatic",
     "status": "showroom", "image": {"original": "https://…s3…/x.webp", "thumbnail": "…"} } ] }
  - There is no single-car endpoint (/api/car/<id> is 404), so get_car() uses the cached list.
  - `?showroom=` needs the exact name ("Makkaraparamba"); we filter locally instead.
  - Photos are WebP; WhatsApp image messages need JPEG/PNG (convert before sending).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import date, datetime
from typing import Any

import httpx
from pydantic import ValidationError

from app.inventory.aliases import ModelAliases
from app.inventory.base import Car, CarQuery, InventoryAdapter, InventoryError, apply_query

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.carlet.in"
LIST_PATH = "/api/car/list"

_STATUS = {"showroom": "available", "available": "available", "in stock": "available", "in showroom": "available",
           "booked": "booked", "reserved": "booked", "on hold": "booked", "sold": "sold", "delivered": "sold",
           "inspection": "coming_soon", "under inspection": "coming_soon", "workshop": "coming_soon",
           "in workshop": "coming_soon", "coming soon": "coming_soon"}
_BODY = {"hatchback": "hatchback", "sedan": "sedan", "suv": "suv", "compact suv": "suv", "muv": "muv",
         "mpv": "muv", "pickup": "pickup"}


# Variant / trim codes written in capitals ("Swift VDI", "Creta SX", "City ZX CVT").
_UPPER = {"lxi", "vxi", "zxi", "ldi", "vdi", "zdi", "sx", "ex", "vx", "gx", "zx", "lx", "ax", "mx", "amt",
          "cvt", "dct", "at", "mt", "crdi", "tdi", "tsi", "dci", "vtec", "abs", "cng", "ev", "suv", "mpv",
          "rxe", "rxl", "rxt", "rxz", "xe", "xm", "xt", "xz", "hte", "htk", "htx", "gtx", "o", "(o)", "dx", "sx(o)",
          "4x4", "4x2", "awd"}


def pretty(name: str) -> str:
    """'maruti suzuki' → 'Maruti Suzuki', 'swift vdi' → 'Swift VDI', 'xuv700' → 'XUV700', 'i20' stays."""
    words = []
    for word in name.split():
        if word.lower().rstrip("+") in _UPPER:
            words.append(word.upper())
        elif word[:1] == "i" and word[1:].isdigit():
            words.append(word)
        elif any(ch.isdigit() for ch in word) and any(ch.isalpha() for ch in word):
            words.append(word.upper())
        else:
            words.append(word[:1].upper() + word[1:])
    return " ".join(words)


def _fuel(value: str) -> str:
    low = value.lower()
    if "cng" in low:
        return "cng"
    if "electric" in low or low == "ev":
        return "electric"
    if "hybrid" in low:
        return "hybrid"
    return low.strip()


def _date(value: Any) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except ValueError:
        return None


def short_id(raw_id: str, prefix: str) -> str:
    return f"{prefix}{raw_id[-6:].upper()}"


def parse_cars(rows: list[dict[str, Any]], *, id_prefix: str = "TC-",
               showrooms: list[str] | None = None) -> tuple[list[Car], list[str]]:
    """Carlet rows → Cars (+ one warning per skipped row). Unknown status = not offered."""
    wanted = {s.strip().lower() for s in (showrooms or []) if s.strip()}
    ids = [str(r.get("_id", "")) for r in rows]
    use_short = len({i[-6:] for i in ids}) == len(ids)  # fall back to full ids on a clash
    cars: list[Car] = []
    warnings: list[str] = []
    for row in rows:
        raw_id = str(row.get("_id") or "")
        showroom = str(row.get("showroom") or "").strip()
        if wanted and showroom.lower() not in wanted:
            continue
        status = _STATUS.get(" ".join(str(row.get("status") or "").lower().split()))
        if status is None:
            warnings.append(f"car {raw_id}: unknown status {row.get('status')!r}, not offered")
            continue
        insurance = _date(row.get("insuranceValidity"))
        image = row.get("image") if isinstance(row.get("image"), dict) else {}
        try:
            car = Car(
                id=short_id(raw_id, id_prefix) if use_short else raw_id,
                make=pretty(str(row.get("brand") or "")),
                model=pretty(str(row.get("model") or "")),
                year=int(row.get("makeYear")),
                price=int(row.get("rate")),
                km=int(row.get("kmsDriven") or 0),
                fuel=_fuel(str(row.get("fuel") or "")),
                transmission=str(row.get("transmission") or "").lower() or None,
                owners=int(str(row.get("owners") or "1").rstrip("+")),
                color=str(row.get("color") or "").capitalize(),
                body_type=_BODY.get(str(row.get("bodyType") or "").lower(), "other"),
                location=f"{showroom} showroom" if showroom else "",
                status=status,
                max_loan=int(row["maximumLoan"]) if row.get("maximumLoan") else None,
                insurance_valid_till=insurance,
                image_url=image.get("original") or None,
                source_id=raw_id,
            )
        except (ValueError, TypeError, ValidationError) as exc:
            reason = exc.errors()[0]["msg"] if isinstance(exc, ValidationError) else str(exc)
            warnings.append(f"car {raw_id}: {reason}")
            continue
        cars.append(car)
    return cars, warnings


class CarletInventoryAdapter(InventoryAdapter):
    """The whole list is fetched (one call, ~160 KB) and cached; filtering is local, with the
    same rules as every other adapter. On an outage the last good copy is used for up to
    `stale_seconds`, after that InventoryError (the alert engine then sends nothing)."""

    def __init__(self, base_url: str = DEFAULT_BASE_URL, aliases: ModelAliases | None = None, *,
                 id_prefix: str = "TC-", showrooms: list[str] | None = None, cache_seconds: float = 300,
                 stale_seconds: float = 3600, http: httpx.Client | None = None,
                 monotonic: Callable[[], float] = time.monotonic) -> None:
        self.url = base_url.rstrip("/") + LIST_PATH
        self._aliases = aliases or ModelAliases()
        self._id_prefix = id_prefix
        self._showrooms = list(showrooms or [])
        self._cache_seconds = cache_seconds
        self._stale_seconds = stale_seconds
        self._http = http or httpx.Client(timeout=20, follow_redirects=True)
        self._monotonic = monotonic
        self._cars: dict[str, Car] = {}
        self._loaded_at: float | None = None

    def _load(self) -> dict[str, Car]:
        now = self._monotonic()
        if self._loaded_at is not None and now - self._loaded_at < self._cache_seconds:
            return self._cars
        try:
            cars = self._download()
        except InventoryError:
            if self._loaded_at is not None and now - self._loaded_at < self._stale_seconds:
                log.warning("Carlet API unreachable; using the copy from %.0f s ago", now - self._loaded_at)
                return self._cars
            raise
        self._cars, self._loaded_at = {c.id: c for c in cars}, now
        return self._cars

    def _download(self) -> list[Car]:
        try:
            resp = self._http.get(self.url)
        except httpx.HTTPError as exc:
            raise InventoryError(f"Carlet API request failed: {exc!r}") from exc
        if resp.status_code != 200:
            raise InventoryError(f"Carlet API HTTP {resp.status_code}: {resp.text[:200]}")
        try:
            body = resp.json()
        except ValueError as exc:
            raise InventoryError("Carlet API did not return JSON") from exc
        rows = body.get("data") if isinstance(body, dict) else None
        if not isinstance(rows, list) or (isinstance(body, dict) and body.get("success") is False):
            raise InventoryError(f"unexpected Carlet API response: {str(body)[:200]}")
        cars, warnings = parse_cars(rows, id_prefix=self._id_prefix, showrooms=self._showrooms)
        for warning in warnings:
            log.warning("Carlet stock: %s", warning)
        if not cars:
            raise InventoryError("Carlet API returned no usable cars")
        return cars

    def search_cars(self, query: CarQuery) -> list[Car]:
        return apply_query(self._load().values(), query, self._aliases)

    def list_all_cars(self) -> list[Car]:
        return [car for car in self._load().values() if car.is_available]

    def get_car(self, car_id: str) -> Car | None:
        cars = self._load()
        key = car_id.strip().upper()
        return cars.get(key) or next((c for c in cars.values() if c.source_id == car_id.strip()), None)
