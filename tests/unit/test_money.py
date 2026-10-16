"""Number parsing/speaking in English, Manglish and Malayalam.

These are the building blocks for the guardrails ("never state a price that
search_cars didn't return"), so the coverage here is deliberately wide.
"""

import pytest

from app.core.money import (
    LAKH,
    extract_amounts,
    extract_numbers,
    format_inr,
    format_lakh,
    number_to_malayalam,
    parse_budget,
    spoken_inr_malayalam,
    spoken_year_malayalam,
)

# --------------------------------------------------------------------------- formatting


@pytest.mark.parametrize("amount, expected", [
    (999, "₹999"), (1000, "₹1,000"), (550000, "₹5,50,000"),
    (12345678, "₹1,23,45,678"), (-25000, "-₹25,000"),
])
def test_format_inr(amount: int, expected: str) -> None:
    assert format_inr(amount) == expected


@pytest.mark.parametrize("amount, expected", [
    (550000, "₹5.5 lakh"), (600000, "₹6 lakh"), (525000, "₹5.25 lakh"),
    (12000000, "₹1.2 crore"), (45000, "₹45,000"),
])
def test_format_lakh(amount: int, expected: str) -> None:
    assert format_lakh(amount) == expected


# --------------------------------------------------------------------------- extraction


@pytest.mark.parametrize("text, expected", [
    # English / shorthand
    ("Price is Rs 5,50,000", 550000),
    ("₹5,50,000 only", 550000),
    ("budget 8L", 800000),
    ("8.5 lakhs max", 850000),
    ("around 8 lac", 800000),
    ("50k down payment", 50000),
    ("five lakh fifty thousand", 550000),
    ("five and a half lakh", 550000),
    ("1.2 crore", 12000000),
    ("650000 rupees", 650000),
    # Manglish
    ("ettu lakh vare", 800000),
    ("anjara lakh", 550000),
    ("anchara lakhinu", 550000),   # dative "for 5.5 lakh"
    ("ആറ് ലക്ഷത്തിന്", 600000),     # dative in Malayalam script
    ("randu lakh", 200000),
    ("irupathiyanchu lakh", 2500000),
    ("pathinayiram", 10000),
    ("ambathinayiram", 50000),
    ("anjulakh", 500000),
    # Malayalam script
    ("എട്ട് ലക്ഷം", 800000),
    ("എട്ടു ലക്ഷം രൂപ", 800000),
    ("അഞ്ചര ലക്ഷം", 550000),
    ("അഞ്ചുലക്ഷം", 500000),
    ("അഞ്ച് ലക്ഷത്തി അമ്പതിനായിരം", 550000),
    ("ഇരുപത്തിയഞ്ച് ലക്ഷം", 2500000),
    ("അയ്യായിരം", 5000),
    ("ഒന്നര ലക്ഷം", 150000),
    ("ഒരു കോടി", 10000000),
])
def test_extract_amounts(text: str, expected: int) -> None:
    assert extract_amounts(text) == [expected]


def test_spoken_output_round_trips_through_parser() -> None:
    # Whatever we *say* must be parseable by the guardrail.
    for amount in [150000, 550000, 800000, 2500000, 645000]:
        assert extract_amounts(spoken_inr_malayalam(amount)) == [amount]


def test_engine_size_is_not_money() -> None:
    nums = extract_numbers("Swift 1.2 L petrol, 2019 model")
    assert extract_amounts("Swift 1.2 L petrol") == []
    assert [n.value for n in nums] == [1.2, 2019]


def test_km_is_not_money() -> None:
    nums = extract_numbers("45k km driven, 45,000 kms")
    assert [(n.value, n.unit) for n in nums] == [(45000, "km"), (45000, "km")]
    assert extract_amounts("45k km") == []


def test_year_and_plain_numbers_have_unknown_unit() -> None:
    nums = extract_numbers("2019 model, 2 owners")
    assert [(n.value, n.unit) for n in nums] == [(2019, "none"), (2, "none")]


def test_multiple_amounts_in_one_message() -> None:
    text = "Swift ₹5.5 lakh, Baleno 6.8L, Creta പത്ത് ലക്ഷം"
    assert extract_amounts(text) == [550000, 680000, 1000000]


def test_text_without_numbers() -> None:
    assert extract_numbers("hello, any swift available?") == []
    assert extract_numbers("ലക്ഷം") == []


# --------------------------------------------------------------------------- budget parsing


@pytest.mark.parametrize("text, expected", [
    ("8L", 8 * LAKH), ("8", 8 * LAKH), ("8.5", 850000), ("800000", 800000),
    ("8 lakh", 8 * LAKH), ("ettu lakh", 8 * LAKH), ("abc", None), ("500", None),
])
def test_parse_budget(text: str, expected: int | None) -> None:
    assert parse_budget(text) == expected


# --------------------------------------------------------------------------- speaking


@pytest.mark.parametrize("n, expected", [
    (5, "അഞ്ച്"),
    (19, "പത്തൊമ്പത്"),
    (22, "ഇരുപത്തിരണ്ട്"),
    (25, "ഇരുപത്തിയഞ്ച്"),
    (105, "നൂറ്റിയഞ്ച്"),
    (1000, "ആയിരം"),
    (5000, "അയ്യായിരം"),
    (20000, "ഇരുപതിനായിരം"),
    (45000, "നാല്പത്തിയഞ്ചായിരം"),
    (100000, "ഒരു ലക്ഷം"),
    (550000, "അഞ്ച് ലക്ഷത്തി അമ്പതിനായിരം"),
])
def test_number_to_malayalam(n: int, expected: str) -> None:
    assert number_to_malayalam(n) == expected


def test_spoken_prices_and_years() -> None:
    assert spoken_inr_malayalam(550000) == "അഞ്ചര ലക്ഷം രൂപ"
    assert spoken_inr_malayalam(800000) == "എട്ട് ലക്ഷം രൂപ"
    assert spoken_inr_malayalam(150000) == "ഒന്നര ലക്ഷം രൂപ"
    assert spoken_year_malayalam(2019) == "രണ്ടായിരത്തി പത്തൊമ്പത്"
    assert spoken_year_malayalam(2005) == "രണ്ടായിരത്തിയഞ്ച്"


def test_spoken_text_contains_no_digits() -> None:
    for n in [1, 99, 2021, 345678, 9876543]:
        assert not any(ch.isdigit() for ch in number_to_malayalam(n))
