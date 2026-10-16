"""Placeholder for a dealer's real inventory API.

No API docs from True Choice yet (PLAN.md §9, Q14), so every call fails loudly instead
of silently returning no cars. When the docs arrive: implement the three methods,
raise InventoryError on any HTTP/parse failure, and reuse base.apply_query if the
API can't filter by itself.
"""

from __future__ import annotations

from typing import Any

from app.inventory.base import Car, CarQuery, InventoryAdapter

_MESSAGE = (
    "The real inventory API is not connected yet (no API docs). "
    "Set `inventory.adapter: mock` in clients/<client_id>/client.yaml."
)


class RealInventoryAdapter(InventoryAdapter):
    def __init__(self, options: dict[str, Any]) -> None:
        self.options = options

    def search_cars(self, query: CarQuery) -> list[Car]:
        raise NotImplementedError(_MESSAGE)

    def list_all_cars(self) -> list[Car]:
        raise NotImplementedError(_MESSAGE)

    def get_car(self, car_id: str) -> Car | None:
        raise NotImplementedError(_MESSAGE)
