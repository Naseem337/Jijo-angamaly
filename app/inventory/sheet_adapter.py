"""Inventory from a Google Sheet that is shared as "anyone with the link can view".

The dealer's staff keep editing their sheet as usual; the bot downloads it as CSV and
re-reads it every `cache_seconds`. Columns are matched by header name (case and units
ignored), so extra columns are harmless and new ones (Transmission, Owners, Location,
Status…) are picked up automatically.

A row that can't be read (typo in the price, unknown fuel) is skipped with a warning,
so one bad cell never takes the bot down. If the download fails, the last good copy is
used for up to `stale_seconds`; after that InventoryError is raised (the alert engine
treats that as an outage and sends nothing).
"""

from __future__ import annotations

import csv
import io
import logging
import re
import time
from collections.abc import Callable
from typing import Any

import httpx
from pydantic import ValidationError

from app.inventory.aliases import ModelAliases
from app.inventory.base import Car, CarQuery, InventoryAdapter, InventoryError, apply_query

log = logging.getLogger(__name__)

# Car field -> header names it may appear under (compared after _header_key()).
COLUMNS: dict[str, tuple[str, ...]] = {
    "id": ("car id", "id", "stock id", "stock no", "reg no"),
    "make": ("make", "brand", "manufacturer"),
    "model": ("model",),
    "variant": ("variant", "trim", "version"),
    "year": ("year", "model year", "mfg year", "manufacturing year"),
    "fuel": ("fuel type", "fuel"),
    "transmission": ("transmission", "gearbox", "gear"),
    "km": ("odometer", "km", "kms", "km driven", "kilometers", "kilometres", "mileage driven"),
    "price": ("price", "selling price", "price inr", "price rs"),
    "color": ("color", "colour"),
    "owners": ("owners", "owner", "no of owners", "ownership"),
    "body_type": ("body type", "body", "type", "segment"),
    "registration": ("registration", "rto", "reg"),
    "location": ("location", "branch", "showroom", "yard"),
    "status": ("status", "availability"),
    "mileage_kmpl": ("expected mileage", "mileage", "fuel efficiency", "kmpl"),
    "tyre_condition_pct": ("tyre condition", "tire condition", "tyres", "tyre"),
    "highlights": ("highlights", "features", "notes"),
}
_INT_FIELDS = {"year", "km", "price", "owners", "tyre_condition_pct"}
_STATUS = {"available": "available", "in stock": "available", "": "available",
           "booked": "booked", "reserved": "booked", "on hold": "booked", "hold": "booked",
           "sold": "sold", "sold out": "sold", "delivered": "sold"}
_TRANSMISSION = {"manual": "manual", "mt": "manual", "automatic": "automatic", "auto": "automatic",
                 "at": "automatic", "amt": "automatic", "cvt": "automatic", "dct": "automatic"}


def _header_key(header: str) -> str:
    """'Odometer (km)' -> 'odometer', 'Price (INR)' -> 'price', 'No. of Owners' -> 'no of owners'."""
    text = re.sub(r"\(.*?\)", " ", header.lower())
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def _map_headers(headers: list[str]) -> dict[str, str]:
    """Car field -> the sheet's actual header."""
    keys = {_header_key(h): h for h in headers}
    mapping: dict[str, str] = {}
    for field, names in COLUMNS.items():
        for name in names:
            if name in keys and keys[name] not in mapping.values():
                mapping[field] = keys[name]
                break
    missing = [f for f in ("id", "make", "model", "year", "fuel", "km", "price") if f not in mapping]
    if missing:
        raise InventoryError(f"inventory sheet is missing required columns: {', '.join(missing)} "
                             f"(found: {', '.join(headers)})")
    return mapping


def _number(text: str) -> float:
    cleaned = re.sub(r"[^\d.]", "", text)
    if not cleaned:
        raise ValueError(f"not a number: {text!r}")
    return float(cleaned)


def _row_to_car(row: dict[str, str], mapping: dict[str, str]) -> Car:
    data: dict[str, Any] = {}
    for field, header in mapping.items():
        value = (row.get(header) or "").strip()
        if field == "status":
            data[field] = _STATUS.get(value.lower(), "available")
        elif field == "transmission":
            data[field] = _TRANSMISSION.get(value.lower())
        elif field == "highlights":
            data[field] = [h.strip() for h in re.split(r"[;,|]", value) if h.strip()]
        elif not value:
            continue
        elif field in _INT_FIELDS:
            data[field] = int(round(_number(value)))
        elif field == "mileage_kmpl":
            data[field] = _number(value)
        elif field == "body_type":
            body = value.lower()
            data[field] = body if body in ("hatchback", "sedan", "suv", "muv", "pickup") else "other"
        else:
            data[field] = value
    return Car.model_validate(data)


def parse_sheet_csv(text: str) -> tuple[list[Car], list[str]]:
    """Cars from CSV text, plus one warning per skipped row."""
    reader = csv.DictReader(io.StringIO(text))
    mapping = _map_headers(list(reader.fieldnames or []))
    cars: list[Car] = []
    warnings: list[str] = []
    seen: set[str] = set()
    for line, row in enumerate(reader, start=2):  # line 1 is the header
        if not any((v or "").strip() for v in row.values()):
            continue
        try:
            car = _row_to_car(row, mapping)
        except (ValueError, ValidationError) as exc:
            reason = exc.errors()[0]["msg"] if isinstance(exc, ValidationError) else str(exc)
            warnings.append(f"row {line} ({row.get(mapping['id'], '?')}): {reason}")
            continue
        if car.id in seen:
            warnings.append(f"row {line}: duplicate car id {car.id}, skipped")
            continue
        seen.add(car.id)
        cars.append(car)
    return cars, warnings


def sheet_csv_url(sheet_id: str, gid: str | int | None = None) -> str:
    """CSV export link. Without a gid Google returns the first tab (tab ids are not
    0, 1, 2…; take the number after `gid=` in the browser URL of the tab you want)."""
    url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv"
    return f"{url}&gid={gid}" if gid not in (None, "") else url


class GoogleSheetInventoryAdapter(InventoryAdapter):
    def __init__(self, csv_url: str, aliases: ModelAliases | None = None, *, cache_seconds: float = 300,
                 stale_seconds: float = 3600, http: httpx.Client | None = None,
                 monotonic: Callable[[], float] = time.monotonic) -> None:
        self.csv_url = csv_url
        self._aliases = aliases or ModelAliases()
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
                log.warning("Inventory sheet unreachable; using the copy from %.0f s ago", now - self._loaded_at)
                return self._cars
            raise
        self._cars, self._loaded_at = {c.id: c for c in cars}, now
        return self._cars

    def _download(self) -> list[Car]:
        try:
            resp = self._http.get(self.csv_url)
        except httpx.HTTPError as exc:
            raise InventoryError(f"inventory sheet download failed: {exc!r}") from exc
        if resp.status_code != 200 or "text/csv" not in resp.headers.get("content-type", ""):
            # A private sheet returns a Google sign-in page (HTML) instead of CSV.
            raise InventoryError(f"inventory sheet not readable (HTTP {resp.status_code}, "
                                 f"{resp.headers.get('content-type')}); is it shared as 'anyone with the link'?")
        cars, warnings = parse_sheet_csv(resp.content.decode("utf-8-sig"))
        for warning in warnings:
            log.warning("Inventory sheet: %s", warning)
        if not cars:
            raise InventoryError("inventory sheet has no readable cars")
        return cars

    def search_cars(self, query: CarQuery) -> list[Car]:
        return apply_query(self._load().values(), query, self._aliases)

    def list_all_cars(self) -> list[Car]:
        return [car for car in self._load().values() if car.is_available]

    def get_car(self, car_id: str) -> Car | None:
        return self._load().get(car_id)
