"""The guest's mobile number, checked in code before a booking or payment link is made (owner, 2026-11-26).

Accepted: an Indian mobile (10 digits starting 6-9; +91 / 91 / 0 prefix allowed; spaces, dashes, dots and brackets
ignored), a foreign number with '+' and a country code (8-15 digits, E.164), or 'same' / 'same number' / 'this number'
(the guest's WhatsApp number). Rejected as fake: all one digit, a running sequence up or down, a repeating 2-digit
pattern, one digit 8+ times in a row, a wrong length, an Indian number not starting with 6-9.
"""

from __future__ import annotations

SAME_WORDS = ("same", "same number", "this number", "this", "same no", "this no", "whatsapp", "whatsapp number",
              "same whatsapp number", "this whatsapp number")
SAME = "same"


def _looks_fake(digits: str) -> bool:
    if len(set(digits)) == 1:
        return True
    up = "01234567890123456789"
    down = "98765432109876543210"
    if digits in up or digits in down:  # 1234567890, 6789012345, 9876543210
        return True
    if len(digits) >= 4 and digits == (digits[:2] * len(digits))[:len(digits)]:  # 9090909090, 1212121212
        return True
    run = 1
    for a, b in zip(digits, digits[1:]):
        run = run + 1 if a == b else 1
        if run >= 8:  # 9000000000 (owner rule: one digit 8+ times in a row)
            return True
    return False


def check(raw: str) -> str:
    """'same', '+91XXXXXXXXXX' / '+<country><number>' when the number is valid, '' when it is not."""
    text = " ".join(str(raw or "").lower().split()).strip(" .!")
    if text in SAME_WORDS:
        return SAME
    plus = text.startswith("+") or text.startswith("00")
    digits = "".join(ch for ch in text if ch.isdigit())
    if text.startswith("00"):
        digits = digits[2:]
    if any(ch.isalpha() for ch in text):
        return ""
    if plus and not digits.startswith("91"):  # a foreign number with its country code
        if 8 <= len(digits) <= 15 and not _looks_fake(digits[-8:]) and not _looks_fake(digits):
            return "+" + digits
        return ""
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    if len(digits) != 10 or digits[0] not in "6789" or _looks_fake(digits):
        return ""
    return "+91" + digits
