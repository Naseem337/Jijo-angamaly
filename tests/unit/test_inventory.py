from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core.client_config import load_client_config
from app.inventory import (
    Car,
    CarQuery,
    InventoryAdapter,
    InventoryError,
    MockInventoryAdapter,
    ModelAliases,
    RealInventoryAdapter,
    build_inventory_adapter,
)
from app.inventory.base import load_cars_json

MOCK_FILE = Path(__file__).resolve().parents[2] / "clients" / "truechoice" / "inventory_mock.json"


@pytest.fixture
def adapter(clients_dir: Path) -> MockInventoryAdapter:
    config = load_client_config("truechoice", clients_dir)
    built = build_inventory_adapter(config)
    assert isinstance(built, MockInventoryAdapter)
    return built


def ids(cars: list[Car]) -> list[str]:
    return [car.id for car in cars]


def test_mock_file_has_30_valid_cars_with_booked_and_sold() -> None:
    cars = load_cars_json(MOCK_FILE)
    assert len(cars) == 30
    assert {car.status for car in cars} == {"available", "booked", "sold"}


@pytest.mark.parametrize(
    ("wanted", "expected"),
    [
        ("swift", {"TC-1001", "TC-1002"}),
        ("Maruti Swift", {"TC-1001", "TC-1002"}),
        ("crysta", {"TC-1018", "TC-1019"}),
        ("innova", {"TC-1018", "TC-1019"}),
        ("xuv 7oo", {"TC-1025"}),
        ("XUV-700", {"TC-1025"}),
        ("wagonr", {"TC-1005", "TC-1006"}),
        ("elite i20", {"TC-1013", "TC-1014"}),
        ("nexon", {"TC-1027", "TC-1028"}),
        ("nexon ev", {"TC-1028"}),
        ("swift dzire", {"TC-1001", "TC-1002", "TC-1003"}),
    ],
)
def test_model_names_and_aliases(adapter: MockInventoryAdapter, wanted: str, expected: set[str]) -> None:
    assert set(ids(adapter.search_cars(CarQuery(model=wanted, limit=50)))) == expected


def test_make_matches_partial_brand_names(adapter: MockInventoryAdapter) -> None:
    maruti = adapter.search_cars(CarQuery(make="maruti", limit=50))
    assert len(maruti) == 10
    assert all(car.make == "Maruti Suzuki" for car in maruti)


def test_budget_sorts_closest_to_budget_first(adapter: MockInventoryAdapter) -> None:
    cars = adapter.search_cars(CarQuery(budget_max=600_000, limit=3))
    assert ids(cars) == ["TC-1024", "TC-1001", "TC-1015"]  # 5.95L, 5.45L, 5.35L
    assert all(car.price <= 600_000 for car in cars)


def test_without_budget_newest_listing_first(adapter: MockInventoryAdapter) -> None:
    assert ids(adapter.search_cars(CarQuery(limit=2))) == ["TC-1025", "TC-1021"]


def test_combined_filters(adapter: MockInventoryAdapter) -> None:
    cars = adapter.search_cars(CarQuery(fuel="Diesel", min_year=2019, max_km=80_000, limit=50))
    assert set(ids(cars)) == {"TC-1017", "TC-1024", "TC-1025", "TC-1026"}
    automatic = adapter.search_cars(CarQuery(transmission="automatic", budget_max=800_000, limit=50))
    assert set(ids(automatic)) == {"TC-1002", "TC-1010"}


def test_booked_and_sold_cars_are_never_offered(adapter: MockInventoryAdapter) -> None:
    assert "TC-1029" not in ids(adapter.search_cars(CarQuery(model="tiago")))
    assert len(adapter.list_all_cars()) == 28
    booked = adapter.get_car("TC-1029")
    assert booked is not None and not booked.is_available
    assert adapter.get_car("TC-9999") is None


def test_summary_carries_formatted_price_for_the_agent(adapter: MockInventoryAdapter) -> None:
    summary = adapter.get_car("TC-1001").to_summary()
    assert summary["title"] == "2019 Maruti Suzuki Swift VXi"
    assert summary["price_text"] == "₹5,45,000 (₹5.45 lakh)"
    assert summary["km"] == 42000


def test_default_get_car_filters_list_all_cars() -> None:
    car = load_cars_json(MOCK_FILE)[0]

    class OnlyList(InventoryAdapter):
        def search_cars(self, query: CarQuery) -> list[Car]:
            return []

        def list_all_cars(self) -> list[Car]:
            return [car]

    assert OnlyList().get_car(car.id) == car
    assert OnlyList().get_car("other") is None


def test_alias_spelling_shared_by_two_models() -> None:
    aliases = ModelAliases({"swift": ["swift dzire"], "dzire": ["swift dzire"]})
    assert aliases.resolve("Swift Dzire") == {"swiftdzire", "swift", "dzire"}
    assert not aliases.car_matches("Maruti Suzuki", "Baleno", "Delta", "swift")


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("not json", "not valid JSON"),
        ('{"items": []}', '"cars" list'),
        ('{"cars": [{"id": "A", "make": "X", "model": "Y", "year": 2020, "price": 1, '
         '"km": 0, "fuel": "steam", "transmission": "manual"}]}', "car A is invalid"),
    ],
)
def test_bad_inventory_files_fail_with_a_clear_message(tmp_path: Path, content: str, message: str) -> None:
    path = tmp_path / "inv.json"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(InventoryError, match=message):
        load_cars_json(path)


def test_duplicate_ids_and_missing_file(tmp_path: Path) -> None:
    car = json.loads(MOCK_FILE.read_text(encoding="utf-8"))["cars"][0]
    path = tmp_path / "inv.json"
    path.write_text(json.dumps({"cars": [car, car]}), encoding="utf-8")
    with pytest.raises(InventoryError, match="duplicate car id TC-1001"):
        load_cars_json(path)
    with pytest.raises(InventoryError, match="not found"):
        load_cars_json(tmp_path / "missing.json")


def test_real_adapter_fails_loudly() -> None:
    adapter = RealInventoryAdapter({})
    for call in (lambda: adapter.search_cars(CarQuery()), adapter.list_all_cars,
                 lambda: adapter.get_car("x")):
        with pytest.raises(NotImplementedError, match="adapter: mock"):
            call()
