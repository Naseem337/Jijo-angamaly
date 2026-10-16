import pytest

from app.core.phone import InvalidPhone, mask_phone, normalize_phone, to_wa_id, try_normalize_phone

EXPECTED = "+919847012345"


@pytest.mark.parametrize("raw", [
    "9847012345",          # local 10-digit
    "09847012345",         # trunk prefix
    "919847012345",        # WhatsApp wa_id
    "+91 98470 12345",     # spaced
    "+91-98470-12345",     # dashed
    "0091 9847012345",     # international prefix
    " (+91) 98470-12345 ",
])
def test_indian_formats_normalise_to_plus91(raw: str) -> None:
    assert normalize_phone(raw) == EXPECTED


def test_foreign_number_keeps_its_country_code() -> None:
    # Gulf NRIs are common customers in Kerala.
    assert normalize_phone("+971 50 123 4567") == "+971501234567"


@pytest.mark.parametrize("raw", ["", "   ", "12345", "abc", "+91 12345"])
def test_invalid_numbers_raise(raw: str) -> None:
    with pytest.raises(InvalidPhone):
        normalize_phone(raw)
    assert try_normalize_phone(raw) is None


def test_wa_id_and_mask() -> None:
    assert to_wa_id(EXPECTED) == "919847012345"
    assert mask_phone(EXPECTED) == "+91******2345"
