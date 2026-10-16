"""Inventory adapters. build_inventory_adapter() picks one from a client's config."""

from __future__ import annotations

from app.core.client_config import ClientConfig
from app.inventory.aliases import ModelAliases
from app.inventory.base import Car, CarQuery, InventoryAdapter, InventoryError
from app.inventory.mock_adapter import MockInventoryAdapter
from app.inventory.real_adapter import RealInventoryAdapter

__all__ = [
    "Car",
    "CarQuery",
    "InventoryAdapter",
    "InventoryError",
    "MockInventoryAdapter",
    "ModelAliases",
    "RealInventoryAdapter",
    "build_inventory_adapter",
]


def build_inventory_adapter(config: ClientConfig) -> InventoryAdapter:
    options = config.inventory.options
    if config.inventory.adapter == "mock":
        path = config.config_dir / options.get("path", "inventory_mock.json")
        aliases = ModelAliases(config.business_rules.model_aliases)
        return MockInventoryAdapter.from_file(path, aliases)
    return RealInventoryAdapter(options)
