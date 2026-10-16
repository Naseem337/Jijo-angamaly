"""Phone number normalisation.

Everything we store is E.164 ("+919847012345"). Inputs we have to accept:
  - WhatsApp wa_id without '+':        "919847012345"
  - Local 10-digit mobile:              "9847012345"
  - Trunk-prefixed / spaced / dashed:   "098470 12345", "+91-98470-12345", "0091 98470 12345"
  - Foreign numbers (Gulf NRIs):        "+971501234567" -> kept with their own country code
"""

from __future__ import annotations

import re

import phonenumbers


class InvalidPhone(ValueError):
    """Raised when input cannot be turned into a valid phone number."""


def normalize_phone(raw: str, default_region: str = "IN") -> str:
    """Return the E.164 form of `raw` or raise InvalidPhone."""
    if not raw or not raw.strip():
        raise InvalidPhone("empty phone number")
    text = raw.strip()
    digits = re.sub(r"\D", "", text)

    if text.startswith("+"):
        candidate = "+" + digits
    elif digits.startswith("00"):
        candidate = "+" + digits[2:]  # international dialling prefix
    elif len(digits) == 12 and digits.startswith("91"):
        candidate = "+" + digits  # WhatsApp wa_id style
    elif len(digits) == 11 and digits.startswith("0"):
        candidate = digits[1:]  # Indian trunk prefix
    else:
        candidate = digits

    try:
        parsed = phonenumbers.parse(candidate, default_region)
    except phonenumbers.NumberParseException as exc:
        raise InvalidPhone(f"cannot parse phone {raw!r}: {exc}") from exc
    if not phonenumbers.is_valid_number(parsed):
        raise InvalidPhone(f"not a valid phone number: {raw!r}")
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


def try_normalize_phone(raw: str, default_region: str = "IN") -> str | None:
    """Like normalize_phone but returns None instead of raising."""
    try:
        return normalize_phone(raw, default_region)
    except InvalidPhone:
        return None


def to_wa_id(e164: str) -> str:
    """WhatsApp Cloud API wants recipients without the '+'."""
    return e164.lstrip("+")


def mask_phone(e164: str) -> str:
    """For logs: '+919847012345' -> '+91******2345'."""
    if len(e164) <= 7:
        return "*" * len(e164)
    return e164[:3] + "*" * (len(e164) - 7) + e164[-4:]
