"""Inventory adapters. build_inventory_adapter() picks one from a client's config."""

from __future__ import annotations

from app.core.client_config import ClientConfig
from app.inventory.aliases import ModelAliases
from app.inventory.base import Car, CarQuery, InventoryAdapter, InventoryError
from app.inventory.carlet_adapter import CarletInventoryAdapter
from app.inventory.mock_adapter import MockInventoryAdapter
from app.inventory.real_adapter import RealInventoryAdapter
from app.inventory.sheet_adapter import GoogleSheetInventoryAdapter, sheet_csv_url

__all__ = [
    "Car",
    "GoogleSheetInventoryAdapter",
    "CarQuery",
    "CarletInventoryAdapter",
    "InventoryAdapter",
    "InventoryError",
    "MockInventoryAdapter",
    "ModelAliases",
    "RealInventoryAdapter",
    "build_inventory_adapter",
]


def build_inventory_adapter(config: ClientConfig) -> InventoryAdapter:
    options = config.inventory.options
    aliases = ModelAliases(config.business_rules.model_aliases)
    if config.inventory.adapter == "mock":
        path = config.config_dir / options.get("path", "inventory_mock.json")
        return MockInventoryAdapter.from_file(path, aliases)
    if config.inventory.adapter == "google_sheet":
        sheet_id = options.get("sheet_id")
        if not sheet_id:
            raise InventoryError("inventory.adapter is google_sheet but no sheet_id is set")
        return GoogleSheetInventoryAdapter(sheet_csv_url(sheet_id, options.get("gid")), aliases,
                                           cache_seconds=float(options.get("cache_seconds", 300)),
                                           columns=options.get("columns") or {})
    if config.inventory.adapter == "carlet":
        return CarletInventoryAdapter(options.get("base_url") or "https://api.carlet.in", aliases,
                                      id_prefix=options.get("id_prefix", "TC-"),
                                      showrooms=options.get("showrooms") or [],
                                      cache_seconds=float(options.get("cache_seconds", 300)))
    return RealInventoryAdapter(options)
