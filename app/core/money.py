"""Money and number helpers for English, Manglish and Malayalam.

Three jobs:
  1. Formatting   : 550000 -> "₹5,50,000" / "₹5.5 lakh"
  2. Parsing      : find every number in free text, whatever the language:
                    "8L", "8.5 lakhs", "Rs 5,50,000", "ettu lakh", "anjara lakh",
                    "എട്ട് ലക്ഷം", "അഞ്ചര ലക്ഷം", "അമ്പതിനായിരം", "45k km"
                    Used by the guardrails (no un-sourced prices) and by staff commands.
  3. Speaking     : 550000 -> "അഞ്ചര ലക്ഷം രൂപ", 2019 -> "രണ്ടായിരത്തി പത്തൊമ്പത്"
                    Used for spoken_text, because TTS reads digits badly.

The Malayalam vocabulary is not exhaustive. It covers what shows up in used-car chats
(1–99, halves, hundreds, thousands, lakhs, crores), and the tests pin down that coverage.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

LAKH = 100_000
CRORE = 10_000_000

# --------------------------------------------------------------------------- formatting


def format_inr(amount: float) -> str:
    """Indian digit grouping: 550000 -> '₹5,50,000', 12345678 -> '₹1,23,45,678'."""
    value = int(round(amount))
    digits = str(abs(value))
    if len(digits) > 3:
        head, tail = digits[:-3], digits[-3:]
        head = re.sub(r"(\d)(?=(\d{2})+$)", r"\1,", head)
        digits = f"{head},{tail}"
    return ("-" if value < 0 else "") + "₹" + digits


def _trim(x: float) -> str:
    """5.50 -> '5.5', 6.00 -> '6', 5.25 -> '5.25'."""
    return f"{x:.2f}".rstrip("0").rstrip(".")


def format_lakh(amount: float) -> str:
    """Short form used in chat: '₹5.5 lakh', '₹1.2 crore', small amounts in full."""
    if amount >= CRORE:
        return f"₹{_trim(amount / CRORE)} crore"
    if amount >= LAKH:
        return f"₹{_trim(amount / LAKH)} lakh"
    return format_inr(amount)


# --------------------------------------------------------------------------- normalisation

# Chillu letters are written two ways (atomic chillu vs consonant+virama[+ZWJ]).
# Map both onto consonant+virama so the vocabulary only needs one spelling.
_CHILLU = {"ൺ": "ണ്", "ൻ": "ന്", "ർ": "ര്", "ൽ": "ല്", "ൾ": "ള്", "ൿ": "ക്"}
_ZERO_WIDTH = dict.fromkeys(map(ord, "‌‍﻿"), None)


def normalize_text(text: str) -> str:
    """Lower-case, strip zero-width joiners, unify chillu forms, drop digit-group commas."""
    t = text.translate(_ZERO_WIDTH).lower()
    for chillu, expanded in _CHILLU.items():
        t = t.replace(chillu, expanded)
    t = re.sub(r"(?<=\d),(?=\d)", "", t)
    return t.replace("₹", " rs ")


_TOKEN_RE = re.compile(r"\d+(?:\.\d+)?|[a-z]+|[ഀ-ൿ]+")


def tokenize(text: str) -> list[str]:
    """Digits, Latin words and Malayalam words; everything else is a separator."""
    return _TOKEN_RE.findall(normalize_text(text))


# --------------------------------------------------------------------------- vocabulary

_VIRAMA = "്"
_U_SIGN = "ു"

_EN_WORDS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17,
    "eighteen": 18, "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40,
    "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
}

# Malayalam script, canonical spellings (chillu already expanded by normalize_text).
_ML_UNITS = {
    1: "ഒന്ന്", 2: "രണ്ട്", 3: "മൂന്ന്", 4: "നാല്", 5: "അഞ്ച്",
    6: "ആറ്", 7: "ഏഴ്", 8: "എട്ട്", 9: "ഒമ്പത്",
}
_ML_TEENS = {
    10: "പത്ത്", 11: "പതിനൊന്ന്", 12: "പന്ത്രണ്ട്", 13: "പതിമൂന്ന്", 14: "പതിനാല്",
    15: "പതിനഞ്ച്", 16: "പതിനാറ്", 17: "പതിനേഴ്", 18: "പതിനെട്ട്", 19: "പത്തൊമ്പത്",
}
_ML_TENS = {
    20: "ഇരുപത്", 30: "മുപ്പത്", 40: "നാല്പത്", 50: "അമ്പത്",
    60: "അറുപത്", 70: "എഴുപത്", 80: "എണ്പത്", 90: "തൊണ്ണൂറ്",
}
# Combining forms used before a following unit: 25 = ഇരുപത്തി + (y)അഞ്ച്.
_ML_TENS_COMB = {
    20: "ഇരുപത്തി", 30: "മുപ്പത്തി", 40: "നാല്പത്തി", 50: "അമ്പത്തി",
    60: "അറുപത്തി", 70: "എഴുപത്തി", 80: "എണ്പത്തി", 90: "തൊണ്ണൂറ്റി",
}
_ML_HUNDREDS = {
    1: "നൂറ്", 2: "ഇരുനൂറ്", 3: "മുന്നൂറ്", 4: "നാനൂറ്", 5: "അഞ്ഞൂറ്",
    6: "അറുനൂറ്", 7: "എഴുനൂറ്", 8: "എണ്ണൂറ്", 9: "തൊള്ളായിരം",
}
# Irregular "N thousand" words (2–10); others are built by rule in _thousand_word().
_ML_THOUSANDS_IRREGULAR = {
    1: "ആയിരം", 2: "രണ്ടായിരം", 3: "മൂവായിരം", 4: "നാലായിരം", 5: "അയ്യായിരം",
    6: "ആറായിരം", 7: "ഏഴായിരം", 8: "എണ്ണായിരം", 9: "ഒമ്പതിനായിരം", 10: "പതിനായിരം",
}
# Stems seen in front of "ായിരം" that don't follow the general rule.
_ML_THOUSAND_STEMS = {"മൂവ": 3, "അയ്യ": 5, "എണ്ണ": 8, "പതിന": 10}

# When a word ending in "ി" is followed by a vowel-initial word, the vowel glides to "യ".
_GLIDE = {
    "അ": "യ", "ആ": "യാ", "ഇ": "യി", "ഈ": "യീ", "ഉ": "യു",
    "ഊ": "യൂ", "എ": "യെ", "ഏ": "യേ", "ഒ": "യൊ", "ഓ": "യോ",
}


def _glue(prefix: str, word: str) -> str:
    """Join a combining form (ending in 'ി') with the next word, applying the y-glide."""
    first = word[:1]
    return prefix + (_GLIDE[first] + word[1:] if first in _GLIDE else word)


def _ml_below_100(n: int) -> str:
    if n in _ML_UNITS:
        return _ML_UNITS[n]
    if n in _ML_TEENS:
        return _ML_TEENS[n]
    tens, unit = divmod(n, 10)
    if unit == 0:
        return _ML_TENS[tens * 10]
    return _glue(_ML_TENS_COMB[tens * 10], _ML_UNITS[unit])


# Manglish (Malayalam typed in English letters). Several common spellings each.
_MANGLISH_WORDS = {
    "oru": 1, "onnu": 1, "randu": 2, "rendu": 2, "moonu": 3, "moonnu": 3, "munnu": 3,
    "naalu": 4, "nalu": 4, "anchu": 5, "anju": 5, "aaru": 6, "aru": 6, "ezhu": 7,
    "ettu": 8, "onpathu": 9, "ombathu": 9, "onbathu": 9, "pathu": 10,
    "pathinonnu": 11, "panthrandu": 12, "pathimoonu": 13, "pathinaalu": 14,
    "pathinanchu": 15, "pathinaaru": 16, "pathinezhu": 17, "pathinettu": 18,
    "pathonpathu": 19, "irupathu": 20, "muppathu": 30, "nalpathu": 40,
    "ambathu": 50, "anpathu": 50, "arupathu": 60, "ezhupathu": 70, "enpathu": 80,
    "thonnooru": 90, "nooru": 100,
}
_MANGLISH_TENS_COMB = {
    20: "irupathi", 30: "muppathi", 40: "nalpathi", 50: "ambathi",
    60: "arupathi", 70: "ezhupathi", 80: "enpathi", 90: "thonnootti",
}
_MANGLISH_UNIT_FORMS = {
    1: ["onnu", "yonnu"], 2: ["randu"], 3: ["moonu", "moonnu"], 4: ["naalu", "nalu"],
    5: ["anchu", "anju", "yanchu", "yanju"], 6: ["aaru", "yaaru"], 7: ["ezhu", "yezhu"],
    8: ["ettu", "yettu"], 9: ["onpathu", "yonpathu"],
}


def _build_word_values() -> dict[str, float]:
    """All single-token number words (all three languages) -> value."""
    values: dict[str, float] = dict(_EN_WORDS)

    # Malayalam script 1–99 plus "ഒരു" (one, adjective) and നൂറ് (100).
    ml: dict[str, float] = {"ഒരു": 1, normalize_text(_ML_HUNDREDS[1]): 100}
    for n in range(1, 100):
        ml[normalize_text(_ml_below_100(n))] = n
    for word, n in list(ml.items()):
        if word.endswith(_VIRAMA):
            stem = word[:-1]
            ml[stem + _U_SIGN] = n  # എട്ട് / എട്ടു
            ml[stem + "ര"] = n + 0.5  # അഞ്ചര = 5.5, ഒന്നര = 1.5
    values.update(ml)

    # Manglish words, compounds (irupathiyanchu = 25) and halves (anjara = 5.5).
    mg: dict[str, float] = dict(_MANGLISH_WORDS)
    for tens, comb in _MANGLISH_TENS_COMB.items():
        for unit, forms in _MANGLISH_UNIT_FORMS.items():
            for form in forms:
                mg[comb + form] = tens + unit
                if form[0] in "aeiou":
                    mg[comb[:-1] + form] = tens + unit  # contracted: irupathanchu
    for word, n in list(mg.items()):
        if word.endswith("u") and n < 100:
            mg[word[:-1] + "ara"] = n + 0.5
    values.update(mg)
    return values


_WORD_VALUES = _build_word_values()

_MULTIPLIERS: dict[str, int] = {
    **dict.fromkeys(["thousand", "thousands", "k", "aayiram", "ayiram", "aayirathi",
                     "ആയിരം", "ആയിരത്തി"], 1000),
    **dict.fromkeys(["lakh", "lakhs", "lac", "lacs", "l", "lk", "laksham", "lakham",
                     "lakshathi", "lakhathi", "ലക്ഷം", "ലക്ഷത്തി", "ലക്ഷ"], LAKH),
    **dict.fromkeys(["crore", "crores", "cr", "kodi", "കോടി"], CRORE),
}
_HUNDRED_WORDS = {"hundred"}
_HALF_WORDS = {"half"}
_CONNECTORS = {"and", "a"}
_CURRENCY_BEFORE = {"rs", "inr", "rupees", "rupee"}
_CURRENCY_AFTER = {"rs", "rupees", "rupee", "roopa", "rupa", "roopaykku", "rupaykku"}
_KM_WORDS = {"km", "kms", "kilometer", "kilometers", "kilometre", "kilometres", "kilometer"}
# "1.2 L petrol" is an engine, not ₹1.2 lakh.
_ENGINE_WORDS = {"petrol", "diesel", "engine", "turbo", "litre", "liter", "ltr", "cc", "cng",
                 "tsi", "tdi", "crdi", "vtvt", "mpi", "hybrid"}

# Glued forms: "അഞ്ചുലക്ഷം", "anjulakh", "രണ്ടായിരത്തി", "pathinayiram".
_ML_GLUED_SUFFIXES = ["ലക്ഷത്തി", "ലക്ഷം", "കോടി"]
_ML_THOUSAND_SUFFIXES = ["ായിരത്തി", "ായിരം"]
_LATIN_GLUED_SUFFIXES = ["lakhs", "lakh", "laksham", "lakham", "lacs", "lac",
                         "aayiram", "ayiram", "thousand", "crore", "kodi"]


# Case-inflected forms ("lakhinu", "lakhil", "ലക്ഷത്തിന്", "ആയിരത്തിന്") start with these.
_MULTIPLIER_PREFIXES = [("lakh", LAKH), ("laksh", LAKH), ("ലക്ഷ", LAKH),
                        ("aayira", 1000), ("ayira", 1000), ("ആയിര", 1000),
                        ("crore", CRORE), ("kodi", CRORE), ("കോടി", CRORE)]


def _multiplier(token: str) -> int | None:
    if token in _MULTIPLIERS:
        return _MULTIPLIERS[token]
    for prefix, value in _MULTIPLIER_PREFIXES:
        if token.startswith(prefix):
            return value
    return None


def _word_value(token: str) -> float | None:
    if re.fullmatch(r"\d+(?:\.\d+)?", token):
        return float(token)
    return _WORD_VALUES.get(token)


def _ml_stem_value(stem: str) -> float | None:
    """Value of a Malayalam stem that lost its final virama when glued to a suffix."""
    if stem in _ML_THOUSAND_STEMS:
        return _ML_THOUSAND_STEMS[stem]
    candidates = [stem, stem + _VIRAMA, stem + _U_SIGN]
    if stem.endswith("ിന"):  # ഇരുപതിനായിരം -> ഇരുപത്
        candidates.append(stem[:-2] + _VIRAMA)
    for cand in candidates:
        if cand in _WORD_VALUES:
            return _WORD_VALUES[cand]
    return None


def _latin_stem_value(stem: str) -> float | None:
    candidates = [stem, stem + "u"]
    if stem.endswith("in"):  # pathinayiram -> pathu
        candidates.append(stem[:-2] + "u")
    for cand in candidates:
        if cand in _WORD_VALUES:
            return _WORD_VALUES[cand]
    return None


def _split_glued(token: str) -> list[str]:
    """Split one glued token into [number, multiplier] tokens when possible."""
    if token in _WORD_VALUES or token in _MULTIPLIERS:
        return [token]
    if token in _ML_THOUSANDS_IRREGULAR_REVERSE:  # അയ്യായിരം = 5000
        return [str(_ML_THOUSANDS_IRREGULAR_REVERSE[token]), "ആയിരം"]
    for suffix in _ML_GLUED_SUFFIXES:
        if token.endswith(suffix) and len(token) > len(suffix):
            value = _ml_stem_value(token[: -len(suffix)])
            if value is not None:
                return [_fmt(value), suffix]
    for suffix in _ML_THOUSAND_SUFFIXES:
        if token.endswith(suffix) and len(token) > len(suffix):
            value = _ml_stem_value(token[: -len(suffix)])
            if value is not None:
                return [_fmt(value), "ആയിരത്തി" if suffix.endswith("ി") else "ആയിരം"]
    if token.isascii():
        for suffix in _LATIN_GLUED_SUFFIXES:
            if token.endswith(suffix) and len(token) > len(suffix):
                value = _latin_stem_value(token[: -len(suffix)])
                if value is not None:
                    return [_fmt(value), suffix]
    return [token]


_ML_THOUSANDS_IRREGULAR_REVERSE = {
    normalize_text(w): n for n, w in _ML_THOUSANDS_IRREGULAR.items()
}


def _fmt(value: float) -> str:
    return _trim(value)


# --------------------------------------------------------------------------- extraction

Unit = Literal["inr", "km", "none"]


@dataclass(frozen=True)
class ParsedNumber:
    """One number found in text. unit='none' means we can't tell (could be ₹, KM or a year)."""

    value: float
    unit: Unit
    raw: str


def _expanded_tokens(text: str) -> list[str]:
    tokens: list[str] = []
    for tok in tokenize(text):
        tokens.extend(_split_glued(tok))
    return tokens


def _is_currency_after(token: str) -> bool:
    return token in _CURRENCY_AFTER or token.startswith("രൂപ")


def _is_km(token: str) -> bool:
    return token in _KM_WORDS or token.startswith("കിലോമീ")


def extract_numbers(text: str) -> list[ParsedNumber]:
    """Every number phrase in `text`, with a best-effort unit."""
    tokens = _expanded_tokens(text)
    results: list[ParsedNumber] = []
    i = 0
    while i < len(tokens):
        if _word_value(tokens[i]) is None:
            i += 1
            continue

        start = i
        total = 0.0
        current: float | None = None
        last_mult: int | None = None
        prev_was_digit = False
        while i < len(tokens):
            tok = tokens[i]
            value = _word_value(tok)
            is_digit = value is not None and tok[0].isdigit()
            nxt = tokens[i + 1] if i + 1 < len(tokens) else ""

            if value is not None:
                if current is None and last_mult is not None and is_digit:
                    # "6.95 lakh, 28500 km" / "6.95 lakh, 28.5k km": digits after a lakh/crore
                    # start a new number, unless they are "5 lakh 50 thousand".
                    m = _multiplier(nxt)
                    after = tokens[i + 2] if i + 2 < len(tokens) else ""
                    if m is None or m >= last_mult or "." in tok or _is_km(after):
                        break
                if current is None:
                    current = value
                elif (not is_digit and not prev_was_digit and current >= 20
                      and current % 10 == 0 and current < 100 and value < 10):
                    current += value  # "twenty five"
                else:
                    break  # two separate numbers side by side
                prev_was_digit = is_digit
                i += 1
                continue
            if tok in _HUNDRED_WORDS and current is not None:
                current *= 100
            elif tok in _HALF_WORDS and current is not None:
                current += 0.5
            elif tok in _CONNECTORS and current is not None and nxt in (_HALF_WORDS | {"a"}):
                pass  # "five and a half"
            elif tok in _CONNECTORS and last_mult is not None and _word_value(nxt) is not None:
                pass  # "five lakh and fifty thousand"
            elif current is not None and (mult := _multiplier(tok)) is not None:
                if tok in ("l", "lk") and nxt in _ENGINE_WORDS:
                    break  # "1.2 L petrol"
                if last_mult is not None and mult >= last_mult:
                    break
                total += current * mult
                current, last_mult = None, mult
            else:
                break
            prev_was_digit = False
            i += 1

        value_total = total + (current or 0)
        before = tokens[start - 1] if start > 0 else ""
        after = tokens[i] if i < len(tokens) else ""
        unit: Unit = "none"
        if _is_km(after):
            unit = "km"
        elif last_mult is not None or before in _CURRENCY_BEFORE or _is_currency_after(after):
            unit = "inr"
        results.append(ParsedNumber(value_total, unit, " ".join(tokens[start:i])))
        if i == start:  # safety: always make progress
            i += 1
    return results


def extract_amounts(text: str) -> list[int]:
    """Rupee amounts that are clearly money (have a lakh/thousand/₹ marker)."""
    return [int(round(n.value)) for n in extract_numbers(text) if n.unit == "inr"]


def parse_budget(text: str) -> int | None:
    """Budget typed by staff or customers: '8L', '8.5 lakh', '800000', bare '8' (= 8 lakh)."""
    numbers = extract_numbers(text)
    for n in numbers:
        if n.unit == "inr":
            return int(round(n.value))
    for n in numbers:
        if n.unit == "none":
            if 0 < n.value < 100:
                return int(round(n.value * LAKH))
            if n.value >= 10_000:
                return int(round(n.value))
    return None


# --------------------------------------------------------------------------- speaking


def _thousand_word(k: int) -> str:
    """k thousand as one word: 2 -> രണ്ടായിരം, 20 -> ഇരുപതിനായിരം, 45 -> നാല്പത്തിയഞ്ചായിരം."""
    if k in _ML_THOUSANDS_IRREGULAR:
        return _ML_THOUSANDS_IRREGULAR[k]
    word = _ml_below_100(k)
    stem = word[:-1]  # drop virama
    if stem.endswith("ത"):  # ഇരുപത് -> ഇരുപതിനായിരം, പത്തൊമ്പത് -> പത്തൊമ്പതിനായിരം
        return stem + "ിനായിരം"
    return stem + "ായിരം"


def _combining(word: str) -> str:
    """Form used when more digits follow: ആയിരം -> ആയിരത്തി, നൂറ് -> നൂറ്റി, ലക്ഷം -> ലക്ഷത്തി."""
    if word.endswith("ം"):
        return word[:-1] + "ത്തി"
    if word.endswith("റ്"):
        return word[:-1] + "്റി"
    return word


def _join(head: str, rest: str) -> str:
    """Glue to a combining form when rest starts with a vowel (glide), else use a space."""
    if head.endswith("ി") and rest[:1] in _GLIDE:
        return _glue(head, rest)
    return f"{head} {rest}"


def _below_1000(n: int) -> str:
    hundreds, rest = divmod(n, 100)
    if hundreds == 0:
        return _ml_below_100(rest)
    word = _ML_HUNDREDS[hundreds]
    return word if rest == 0 else _join(_combining(word), _ml_below_100(rest))


def number_to_malayalam(n: int) -> str:
    """Integer -> Malayalam words, written for TTS (0 <= n < 1000 crore)."""
    if n < 0 or n >= 1000 * CRORE:
        raise ValueError(f"out of range: {n}")
    if n == 0:
        return "പൂജ്യം"
    crores, n = divmod(n, CRORE)
    lakhs, n = divmod(n, LAKH)
    thousands, rest = divmod(n, 1000)

    # (standalone form, combining form, may glue to the next word)
    parts: list[tuple[str, str, bool]] = []
    if crores:
        word = f"{_below_1000(crores)} കോടി" if crores > 1 else "ഒരു കോടി"
        parts.append((word, word, False))
    if lakhs:
        word = f"{_ml_below_100(lakhs)} ലക്ഷം" if lakhs > 1 else "ഒരു ലക്ഷം"
        parts.append((word, _combining(word), False))  # "ലക്ഷത്തി അമ്പതിനായിരം": keep a space
    if thousands:
        word = _thousand_word(thousands)
        parts.append((word, _combining(word), True))  # "രണ്ടായിരത്തിയഞ്ച്"
    if rest:
        word = _below_1000(rest)
        parts.append((word, word, True))

    text = parts[-1][0]
    for _standalone, combining, glue in reversed(parts[:-1]):
        text = _join(combining, text) if glue else f"{combining} {text}"
    return text


def spoken_inr_malayalam(amount: float) -> str:
    """₹ amount as natural spoken Malayalam: 550000 -> 'അഞ്ചര ലക്ഷം രൂപ'."""
    value = int(round(amount))
    if LAKH <= value < 100 * LAKH and value % LAKH == LAKH // 2:
        half = _ml_below_100(value // LAKH)[:-1] + "ര"  # അഞ്ച് -> അഞ്ചര
        return f"{half} ലക്ഷം രൂപ"
    return f"{number_to_malayalam(value)} രൂപ"


def spoken_year_malayalam(year: int) -> str:
    """2019 -> 'രണ്ടായിരത്തി പത്തൊമ്പത്'."""
    return number_to_malayalam(year)
