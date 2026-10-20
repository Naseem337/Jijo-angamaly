"""The spoken version of a reply, for voice notes (PLAN.md §3 "spoken_text").

1. The LLM rewrites the (already guardrail-checked) text reply as a short script for a
   voice note, keeping every number as digits.
2. That digit version goes through the same guardrails as any reply, so speech can't
   carry a figure the text didn't have.
3. Digits become words here, deterministically: TTS reads digit strings badly, and
   Malayalam number words are exactly what app.core.money already produces.
Sarvam's Malayalam voice needs Malayalam script, so Manglish customers get Malayalam
script in the voice note (their text reply stays Manglish).
"""

from __future__ import annotations

import re

from app.core.money import number_to_malayalam, spoken_inr_malayalam
from app.providers.llm import ChatMessage, LLMError, LLMProvider

MAX_SPOKEN_CHARS = 400

SPOKEN_INSTRUCTION = {
    "ml": """Rewrite the WhatsApp reply below as a short voice note, the way a friendly Kerala car salesman would actually say it out loud to a customer. Write it in Malayalam script (never English letters for Malayalam words) for text-to-speech.
Make it sound like a real person talking, not a news reader:
- Start naturally, e.g. "ആ, ഉണ്ട്!", "ശരി,", "നോക്കട്ടെ..." (vary it).
- Address the customer exactly as the reply does ("സാർ"/"മാഡം" or nothing); never by name, never ചേട്ടാ/ചേച്ചി.
- Short spoken phrases with commas and "..." for natural pauses; it's fine to drop formal words.
- Spoken words, not written ones: ഉണ്ട് (not ലഭ്യമാണ്), പറ്റും (not സാധിക്കും), വണ്ടി/കാർ (not വാഹനം), പിന്നെ (not കൂടാതെ), വിളിക്കും (not ബന്ധപ്പെടുന്നതാണ്).
- English words Keralites use are good, written in Malayalam script: മോഡൽ, കണ്ടീഷൻ, ടെസ്റ്റ് ഡ്രൈവ്, ബജറ്റ്, ഓണർ.
Rules:
- At most 3 short sentences, under 300 characters.
- Mention at most 2 cars. Car brand and model names ALWAYS stay exactly in English letters as written (i20, XUV700, Creta, Alto 800): never translate them, never write them in Malayalam script, never turn their numbers into Malayalam words.
- Keep every price, year and km figure as digits exactly as in the reply (₹9,80,000, 2021, 42,000 km). Do not add any number that is not in the reply.
- No lists, car ids, emoji, asterisks or links.
Reply with the script only.""",
    "en": """Rewrite the WhatsApp reply below as a short voice note, the way a friendly Kerala car salesman would say it out loud in simple Indian English: relaxed, natural pauses (commas, "..."), not formal, not a news reader.
Rules:
- At most 3 short sentences, under 300 characters.
- Mention at most 2 cars by brand, model and year.
- Keep every price, year and km figure exactly as in the reply. Do not add any number that is not in the reply.
- No lists, car ids, emoji, asterisks or links.
Reply with the script only.""",
}


def speech_language(language: str) -> str:
    """Reply language → script for the voice note: Manglish is spoken in Malayalam."""
    return "en" if language == "en" else "ml"


DETAILS_NOTE = {
    "ml": "The car details are ALSO sent as a text message: mention at most 2 cars briefly, then end with: "
          "വിവരങ്ങൾ മെസ്സേജിൽ അയച്ചിട്ടുണ്ട്.",
    "en": "The car details are ALSO sent as a text message: mention at most 2 cars briefly, then end with: "
          "I've sent the details in the message.",
}
WHOLE_REPLY_NOTE = ("This voice note is the WHOLE reply (no text message is sent), so say everything important "
                    "in it, briefly. Don't say anything was sent in a message.")


def draft_spoken(llm: LLMProvider, reply_text: str, language: str, style: str = "",
                 details_in_text: bool = False) -> str | None:
    """LLM step: the digit version of the voice script, or None if the LLM failed.

    details_in_text: the car details also go out as text, so the voice only summarises;
    otherwise the voice note is the whole reply."""
    lang = speech_language(language)
    instruction = SPOKEN_INSTRUCTION[lang] + "\n" + (DETAILS_NOTE[lang] if details_in_text else WHOLE_REPLY_NOTE)
    if style.strip():
        instruction += f"\nTone and dialect: {style.strip()}"
    try:
        response = llm.complete(instruction, [ChatMessage.user(reply_text)], None, 600)
    except LLMError:
        return None
    text = response.text.strip()
    return text if text and not response.tool_calls and response.stop_reason != "refusal" else None


# --------------------------------------------------------------------------- digits → words

_CAR_ID = re.compile(r"\(?\b(?:CAR|TC-?)\s?\d+\)?", re.IGNORECASE)
_EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿️]")
_NUM = r"\d(?:[\d,]*\d)?(?:\.\d+)?"  # never ends on a comma ("₹9,80,000, 60,400 km")
_MONEY = re.compile(
    rf"(?:₹|\brs\.?|\binr)\s*(?P<a>{_NUM})(?:\s*(?P<u>lakhs?|ലക്ഷം|crores?|കോടി))?(?:\s*രൂപ)?"
    rf"|(?P<b>{_NUM})\s*(?P<v>lakhs?|ലക്ഷം|crores?|കോടി)(?:\s*രൂപ)?",
    re.IGNORECASE,
)
_KM = re.compile(rf"(?P<n>{_NUM})\s*(?:km|kms|kilometres?|kilometers?|കി\.?\s?മീ\.?|കിലോമീറ്റർ)", re.IGNORECASE)
_YEAR = re.compile(r"(?<![\d,.])(19[89]\d|20\d\d)(?![\d,.])")
_ANY = re.compile(_NUM)


def _value(number: str, unit: str | None) -> float:
    value = float(number.replace(",", ""))
    if unit:
        unit = unit.lower()
        value *= 10_000_000 if unit.startswith(("crore", "കോടി")) else 100_000
    return value


def _words(value: float) -> str:
    if value.is_integer():
        return number_to_malayalam(int(value))
    whole, frac = f"{value:.2f}".rstrip("0").split(".")
    return f"{number_to_malayalam(int(whole))} പോയിന്റ് {' '.join(number_to_malayalam(int(d)) for d in frac)}"


_EN_ONES = ["", "വൺ", "ടൂ", "ത്രീ", "ഫോർ", "ഫൈവ്", "സിക്സ്", "സെവൻ", "എയ്റ്റ്", "നയൻ", "ടെൻ", "ഇലവൻ",
            "ട്വൽവ്", "തേർട്ടീൻ", "ഫോർട്ടീൻ", "ഫിഫ്റ്റീൻ", "സിക്സ്റ്റീൻ", "സെവന്റീൻ", "എയ്റ്റീൻ", "നയന്റീൻ"]
_EN_TENS = ["", "", "ട്വന്റി", "തേർട്ടി", "ഫോർട്ടി", "ഫിഫ്റ്റി", "സിക്സ്റ്റി", "സെവന്റി", "എയ്റ്റി", "നയന്റി"]


def _en_below_100(n: int) -> str:
    if n < 20:
        return _EN_ONES[n]
    return " ".join(w for w in (_EN_TENS[n // 10], _EN_ONES[n % 10]) if w)


def year_as_spoken(year: int) -> str:
    """Model years the way Keralites say them, in English: 2021 → "ട്വന്റി ട്വന്റി വൺ"."""
    if 2000 <= year <= 2009:
        return "ടൂ തൗസൻഡ്" + (f" {_EN_ONES[year - 2000]}" if year > 2000 else "")
    return f"{_en_below_100(year // 100)} {_en_below_100(year % 100)}"


def km_as_spoken(km: float) -> str:
    """Odometer the way people say it: 60,400 km → "അറുപതിനായിരം കിലോമീറ്റർ" (thousands; the text has the exact figure)."""
    if km >= 10_000:
        km = round(km / 1000) * 1000
    return f"{_words(km)} കിലോമീറ്റർ"


def price_as_spoken(amount: float) -> str:
    """₹9,80,000 → "ഒമ്പത് ലക്ഷത്തി എണ്പതിനായിരം" (people drop "രൂപ" for lakh amounts)."""
    words = spoken_inr_malayalam(amount)
    return words.removesuffix(" രൂപ") if amount >= 100_000 else words


def to_speech(text: str, language: str) -> str:
    """Make a script ready for TTS: no markup or car ids; for Malayalam, numbers as words."""
    out = _CAR_ID.sub("", text)
    out = _EMOJI.sub("", out).replace("*", "").replace("_", " ").replace("•", "")
    if speech_language(language) == "en":
        out = re.sub(r"₹\s*", "Rupees ", out)
        return re.sub(r"\s+", " ", out).strip()[:MAX_SPOKEN_CHARS]

    out = _MONEY.sub(lambda m: price_as_spoken(_value(m["a"] or m["b"], m["u"] or m["v"])), out)
    out = _KM.sub(lambda m: km_as_spoken(_value(m["n"], None)), out)
    out = _YEAR.sub(lambda m: year_as_spoken(int(m.group(1))), out)
    out = _ANY.sub(_number_or_car_name, out)
    return re.sub(r"\s+", " ", out).strip()[:MAX_SPOKEN_CHARS]


_LATIN_WORD_BEFORE = re.compile(r"([A-Za-z][A-Za-z\-]*)\s+$")


def _number_or_car_name(m: re.Match[str]) -> str:
    """Numbers become Malayalam words, except inside car names: 'i20', 'XUV700', '4X4' and
    'Alto 800' stay as written, so the voice says them in English ("i twenty", not "i ഇരുപത്")."""
    text, start, end = m.string, m.start(), m.end()
    before, after = text[:start], text[end:end + 1]
    if (before[-1:].isascii() and before[-1:].isalpha()) or (after.isascii() and after.isalpha()):
        return m.group(0)  # glued to letters: part of a model name
    if _LATIN_WORD_BEFORE.search(before):
        return m.group(0)  # right after an English word: "Alto 800", "Series 3"
    return _words(_value(m.group(0), None))
