from __future__ import annotations

from datetime import date

import httpx
import pytest
import respx

from app.inventory import CarletInventoryAdapter, CarQuery, InventoryError, ModelAliases
from app.inventory.carlet_adapter import parse_cars, pretty

URL = "https://api.carlet.in/api/car/list"


def row(_id: str, **kw) -> dict:
    base = {"_id": _id, "showroom": "Makkaraparamba", "model": "ciaz zeta", "brand": "maruti suzuki",
            "rate": 595000, "maximumLoan": 525000, "makeYear": 2018, "regDate": "2018-07-12T18:30:00.000Z",
            "bodyType": "sedan", "owners": "3", "insuranceValidity": "2027-01-22T18:30:00.000Z",
            "kmsDriven": 38000, "color": "white", "status": "showroom", "fuel": "Petrol",
            "transmission": "automatic",
            "image": {"original": "https://carlet-used-cars.s3.ap-south-1.amazonaws.com/cars/original/x.webp",
                      "thumbnail": "https://carlet-used-cars.s3.ap-south-1.amazonaws.com/cars/thumbnail/x.webp"}}
    return {**base, **kw}


ROWS = [
    row("6ad7273fbd787869d5a72f0b"),
    row("6acf6a165b5fbeac911e6434", showroom="Pattambi", model="swift vdi", rate=550000, fuel="Diesel",
        transmission="manual", bodyType="hatchback", owners="1"),
    row("6acf6a165b5fbeac911e0001", model="xuv700 ax7", brand="mahindra", rate=1890000, bodyType="compact suv",
        fuel="CNG + Petrol", owners="4+"),
    row("6acf6a165b5fbeac911e0002", status="sold"),
    row("6acf6a165b5fbeac911e0003", status="sent to auction"),
]


def ok(rows=ROWS) -> httpx.Response:
    return httpx.Response(200, json={"success": True, "message": None, "data": rows})


def test_carlet_rows_become_cars() -> None:
    cars, warnings = parse_cars(ROWS)
    by_id = {c.id: c for c in cars}
    ciaz = by_id["TC-A72F0B"]
    assert (ciaz.make, ciaz.model, ciaz.title) == ("Maruti Suzuki", "Ciaz Zeta", "2018 Maruti Suzuki Ciaz Zeta")
    assert (ciaz.price, ciaz.km, ciaz.year, ciaz.owners, ciaz.fuel, ciaz.transmission) == \
        (595000, 38000, 2018, 3, "petrol", "automatic")
    assert (ciaz.location, ciaz.max_loan, ciaz.insurance_valid_till) == ("Makkaraparamba showroom", 525000,
                                                                          date(2027, 1, 22))
    assert ciaz.image_url.endswith("/original/x.webp") and ciaz.source_id == "6ad7273fbd787869d5a72f0b"
    xuv = by_id["TC-1E0001"]
    assert (xuv.model, xuv.body_type, xuv.fuel, xuv.owners) == ("XUV700 AX7", "suv", "cng", 4)
    assert by_id["TC-1E0002"].status == "sold"
    assert "TC-1E0003" not in by_id and "unknown status 'sent to auction'" in warnings[0]


def test_compact_line_carries_showroom_loan_and_insurance() -> None:
    ciaz = parse_cars(ROWS)[0][0]
    line = ciaz.to_line()
    assert line.startswith("TC-A72F0B | 2018 Maruti Suzuki Ciaz Zeta | ₹5,95,000 (₹5.95 lakh) | 38,000 km")
    assert "Makkaraparamba showroom" in line and "insurance till Jan 2027" in line and "max loan ₹5,25,000" in line


def test_showroom_filter_and_pretty_names() -> None:
    cars, _ = parse_cars(ROWS, showrooms=["pattambi"])
    assert [c.model for c in cars] == ["Swift VDI"]
    assert [pretty(n) for n in ("i20 asta", "xuv700", "grand i10 nios", "wr-v")] == \
        ["i20 Asta", "XUV700", "Grand i10 Nios", "Wr-v"]


@respx.mock
def test_adapter_search_details_and_cache() -> None:
    route = respx.get(URL).mock(return_value=ok())
    clock = [0.0]
    stock = CarletInventoryAdapter(aliases=ModelAliases({"swift": ["maruti swift"]}), monotonic=lambda: clock[0])
    assert [c.id for c in stock.search_cars(CarQuery(model="maruti swift"))] == ["TC-1E6434"]
    assert len(stock.list_all_cars()) == 3  # sold and unknown-status cars are never offered
    assert stock.get_car("tc-a72f0b").model == "Ciaz Zeta"
    assert stock.get_car("6ad7273fbd787869d5a72f0b").id == "TC-A72F0B"  # the app's own id works too
    assert stock.get_car("TC-1E0002").status == "sold"
    assert route.call_count == 1
    clock[0] = 301
    stock.list_all_cars()
    assert route.call_count == 2


@respx.mock
def test_outage_uses_last_copy_then_fails() -> None:
    route = respx.get(URL).mock(return_value=ok())
    clock = [0.0]
    stock = CarletInventoryAdapter(monotonic=lambda: clock[0])
    stock.list_all_cars()
    route.mock(side_effect=httpx.ConnectError("down"))
    clock[0] = 600
    assert len(stock.list_all_cars()) == 3
    clock[0] = 4000
    with pytest.raises(InventoryError, match="request failed"):
        stock.list_all_cars()


@respx.mock
@pytest.mark.parametrize("response", [
    httpx.Response(500, text="boom"),
    httpx.Response(200, json={"success": False, "message": "maintenance", "data": None}),
    httpx.Response(200, json={"success": True, "data": []}),
    httpx.Response(200, text="<html>not json</html>"),
])
def test_bad_responses_are_errors(response) -> None:
    respx.get(URL).mock(return_value=response)
    with pytest.raises(InventoryError):
        CarletInventoryAdapter().list_all_cars()
