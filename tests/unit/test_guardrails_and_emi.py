from __future__ import annotations

import json

import pytest

from app.agent.emi import estimate, monthly_emi
from app.agent.guardrails import check_reply, facts_from
from app.core.client_config import EmiRules
from app.core.money import extract_numbers

SWIFT = json.dumps({"id": "TC-1002", "title": "2021 Maruti Suzuki Swift ZXi AMT", "year": 2021,
                    "price": 695000, "price_text": "₹6,95,000 (₹6.95 lakh)", "km": 28500})
FACTS = facts_from(texts=["Swift undo 7 lakhinu thazhe?"], tool_results=[SWIFT], years=[2026])


@pytest.mark.parametrize("reply", [
    "*2021 Maruti Suzuki Swift ZXi AMT* – ₹6,95,000 (₹6.95 lakh), 28,500 km, petrol (TC-1002)",
    "Ningalude 7 lakh budgetil ee Swift und: ₹6.95 lakh, 28.5k km",
    "ee Swift ₹6.9 lakh-nu aanu",  # within rounding of 6.95 lakh
    "I can't offer discounts; the sales team will discuss the price with you.",
    "XUV700 AX5 7S, Innova 2.4 VX, i20 Asta, 1.5 IVT, 7 seater, 1 owner",
    "Ningalkku 2026 model venamenkil njangal ariyikkam",
])
def test_sourced_or_harmless_replies_pass(reply: str) -> None:
    assert check_reply(reply, FACTS) == []


@pytest.mark.parametrize(("reply", "kind"), [
    ("Swift ₹5.2 lakh-nu und", "unsourced_amount"),
    ("ee Swift ettu lakh-nu kittum", "unsourced_amount"),
    ("Swift 8L-nu tharam", "unsourced_amount"),
    ("അഞ്ചര ലക്ഷം രൂപയ്ക്ക് കിട്ടും", "unsourced_amount"),
    ("Rs 5,50,000 only", "unsourced_amount"),
    ("only 12,000 km driven", "unsourced_km"),
    ("it's a 2019 model", "unsourced_year"),
    ("We can give you a discount on this one", "promise"),
    ("Your loan will be approved for sure", "promise"),
    ("This is our best price", "promise"),
    ("50k off if you book today", "promise"),
    ("ലോൺ ഉറപ്പായും കിട്ടും", "promise"),
])
def test_unsourced_figures_and_promises_are_caught(reply: str, kind: str) -> None:
    assert kind in [v.kind for v in check_reply(reply, FACTS)]


def test_price_followed_by_km_is_two_numbers() -> None:
    parsed = extract_numbers("₹6.95 lakh), 28,500 km")
    assert [(n.value, n.unit) for n in parsed] == [(695000, "inr"), (28500, "km")]
    assert [n.value for n in extract_numbers("5 lakh 50 thousand")] == [550000]
    assert [n.value for n in extract_numbers("1 crore 20 lakh")] == [12_000_000]


RULES = EmiRules(annual_rate_min_pct=10.5, annual_rate_max_pct=14.0,
                 tenure_months_options=[36, 60], default_down_payment_pct=20,
                 disclaimer_en="indicative", disclaimer_ml="ഏകദേശം")


def test_monthly_emi_formula() -> None:
    assert monthly_emi(100_000, 12.0, 12) == pytest.approx(8884.88, abs=0.01)
    assert monthly_emi(120_000, 0, 12) == 10_000
    assert monthly_emi(0, 12, 12) == 0


def test_estimate_uses_default_down_payment_and_rounds() -> None:
    est = estimate(695_000, RULES)
    assert (est.down_payment, est.loan_amount) == (139_000, 556_000)
    assert [o.tenure_months for o in est.options] == [36, 60]
    five_years = est.options[1]
    assert five_years.emi_min % 100 == 0 and five_years.emi_min < five_years.emi_max
    assert (five_years.emi_min, five_years.emi_max) == (12_000, 12_900)


def test_estimate_single_tenure_and_clamped_down_payment() -> None:
    est = estimate(500_000, RULES, down_payment=900_000, tenure_months=24)
    assert est.loan_amount == 0 and est.options[0].emi_min == 0
