"""Inventory from a local JSON file (clients/<id>/inventory_mock.json) or a list of cars.

Used for local development and tests until a dealer's real API is connected.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from app.inventory.aliases import ModelAliases
from app.inventory.base import Car, CarQuery, InventoryAdapter, apply_query, load_cars_json


class MockInventoryAdapter(InventoryAdapter):
    def __init__(self, cars: Iterable[Car], aliases: ModelAliases | None = None) -> None:
        self._cars = {car.id: car for car in cars}
        self._aliases = aliases or ModelAliases()

    @classmethod
    def from_file(cls, path: Path, aliases: ModelAliases | None = None) -> "MockInventoryAdapter":
        return cls(load_cars_json(path), aliases)

    def search_cars(self, query: CarQuery) -> list[Car]:
        return apply_query(self._cars.values(), query, self._aliases)

    def list_all_cars(self) -> list[Car]:
        return [car for car in self._cars.values() if car.is_available]

    def get_car(self, car_id: str) -> Car | None:
        """Returns booked/sold cars too (with their status), so callers can say why."""
        return self._cars.get(car_id)
