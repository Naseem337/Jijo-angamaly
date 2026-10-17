from __future__ import annotations

import httpx
import pytest
import respx

from app.inventory import CarQuery, InventoryError
from app.inventory.sheet_adapter import GoogleSheetInventoryAdapter, parse_sheet_csv, sheet_csv_url

URL = sheet_csv_url("SHEET")
CSV = """Car ID,Make,Model,Year,Fuel Type,Odometer (km),Expected Mileage (km/l),Tyre Condition (%),Color,Price (INR)
CAR001,Maruti Suzuki,Swift,2019,Petrol,"42,000",18.9,48,White,"545,000"
CAR002,Hyundai,Creta,2021,Diesel,"30,500",19.2,80,Grey,"1,250,000"
CAR003,Toyota,Innova Crysta,2023,Diesel,"64,900",11.3,90,White,"1,305,000"
"""


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def csv_response(text: str = CSV) -> httpx.Response:
    return httpx.Response(200, text=text, headers={"content-type": "text/csv; charset=utf-8"})


def test_dealer_sheet_columns_are_mapped() -> None:
    cars, warnings = parse_sheet_csv(CSV)
    assert warnings == [] and [c.id for c in cars] == ["CAR001", "CAR002", "CAR003"]
    swift = cars[0]
    assert (swift.price, swift.km, swift.year, swift.fuel, swift.color) == (545000, 42000, 2019, "petrol", "White")
    assert (swift.mileage_kmpl, swift.tyre_condition_pct, swift.transmission) == (18.9, 48, None)
    summary = swift.to_summary()
    assert summary["transmission"] == "not listed" and summary["tyre_condition_pct"] == 48


def test_optional_columns_and_bad_rows() -> None:
    text = """Stock No,Brand,Model,Variant,Year,Fuel,KMs,Price,Transmission,Owners,Location,Status,Body Type
A1,Honda,City,ZX CVT,2021,petrol,26000,"₹12,40,000",CVT,1st,Kochi,Available,Sedan
A2,Honda,Amaze,S,2019,diesel,69000,595000,Manual,2,Thrissur,Sold,sedan
A3,Kia,Seltos,HTX,2020,steam,47000,1190000,manual,1,Kochi,,suv
A4,Tata,Nexon,XZ,twenty,petrol,44000,790000,manual,1,Kochi,booked,compact suv
A1,Honda,City,V,2018,petrol,54000,795000,manual,1,Kochi,,sedan
,,,,,,,,,,,,
"""
    cars, warnings = parse_sheet_csv(text)
    assert [c.id for c in cars] == ["A1", "A2"]
    city, amaze = cars
    assert (city.variant, city.transmission, city.owners, city.location, city.body_type) == \
        ("ZX CVT", "automatic", 1, "Kochi", "sedan")
    assert city.price == 1_240_000 and amaze.status == "sold"
    assert len(warnings) == 3  # A3 bad fuel, A4 bad year, duplicate A1
    assert any("A3" in w for w in warnings) and any("duplicate car id A1" in w for w in warnings)


def test_missing_required_column_is_a_clear_error() -> None:
    with pytest.raises(InventoryError, match="missing required columns: price"):
        parse_sheet_csv("Car ID,Make,Model,Year,Fuel Type,Odometer (km)\nX,Tata,Punch,2021,Petrol,1000\n")


@respx.mock
def test_adapter_caches_and_refreshes() -> None:
    route = respx.get(URL).mock(return_value=csv_response())
    clock = Clock()
    adapter = GoogleSheetInventoryAdapter(URL, cache_seconds=300, monotonic=clock)
    assert [c.id for c in adapter.search_cars(CarQuery(model="crysta"))] == ["CAR003"]
    assert len(adapter.list_all_cars()) == 3 and adapter.get_car("CAR002").model == "Creta"
    assert route.call_count == 1  # cached
    clock.t += 301
    route.mock(return_value=csv_response(CSV.replace("CAR003", "CAR009")))
    assert adapter.get_car("CAR009") is not None and adapter.get_car("CAR003") is None
    assert route.call_count == 2


@respx.mock
def test_outage_uses_last_good_copy_then_fails() -> None:
    route = respx.get(URL).mock(return_value=csv_response())
    clock = Clock()
    adapter = GoogleSheetInventoryAdapter(URL, cache_seconds=300, stale_seconds=3600, monotonic=clock)
    adapter.list_all_cars()
    route.mock(side_effect=httpx.ConnectError("offline"))
    clock.t += 600
    assert len(adapter.list_all_cars()) == 3  # stale copy
    clock.t += 3600
    with pytest.raises(InventoryError, match="download failed"):
        adapter.list_all_cars()


@respx.mock
def test_private_sheet_and_empty_sheet_are_errors() -> None:
    route = respx.get(URL).mock(return_value=httpx.Response(200, text="<html>Sign in</html>",
                                                             headers={"content-type": "text/html"}))
    with pytest.raises(InventoryError, match="anyone with the link"):
        GoogleSheetInventoryAdapter(URL).list_all_cars()
    route.mock(return_value=csv_response(CSV.splitlines()[0] + "\n"))
    with pytest.raises(InventoryError, match="no readable cars"):
        GoogleSheetInventoryAdapter(URL).list_all_cars()


def test_csv_url_with_and_without_tab() -> None:
    assert sheet_csv_url("ID") == "https://docs.google.com/spreadsheets/d/ID/export?format=csv"
    assert sheet_csv_url("ID", "12345").endswith("format=csv&gid=12345")


def test_unknown_transmission_does_not_hide_a_car() -> None:
    from app.inventory.base import apply_query
    cars, _ = parse_sheet_csv(CSV)
    assert len(apply_query(cars, CarQuery(transmission="automatic", body_type="suv", limit=10))) == 3
