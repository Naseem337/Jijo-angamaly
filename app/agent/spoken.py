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

from app.core.money import number_to_malayalam, spoken_inr_malayalam, spoken_year_malayalam
from app.providers.llm import ChatMessage, LLMError, LLMProvider

MAX_SPOKEN_CHARS = 400

SPOKEN_INSTRUCTION = {
    "ml": """Rewrite the WhatsApp reply below as a short voice-note script for text-to-speech, in natural spoken Kerala Malayalam written in Malayalam script (never English letters for Malayalam words).
Rules:
- At most 3 short sentences, under 300 characters.
- Mention at most 2 cars; brand and model names may stay in English letters (Creta, Swift).
- Keep every price, year and km figure as digits exactly as in the reply (₹9,80,000, 2021, 42,000 km). Do not add any number that is not in the reply.
- No lists, car ids, emoji, asterisks or links.
- If the reply has more details than you can say, end with: വിവരങ്ങൾ മെസ്സേജിൽ അയച്ചിട്ടുണ്ട്.
Reply with the script only.""",
    "en": """Rewrite the WhatsApp reply below as a short, friendly voice-note script in simple Indian English for text-to-speech.
Rules:
- At most 3 short sentences, under 300 characters.
- Mention at most 2 cars by brand, model and year.
- Keep every price, year and km figure exactly as in the reply. Do not add any number that is not in the reply.
- No lists, car ids, emoji, asterisks or links.
- If the reply has more details than you can say, end with: I've sent the details in the message.
Reply with the script only.""",
}


def speech_language(language: str) -> str:
    """Reply language → script for the voice note: Manglish is spoken in Malayalam."""
    return "en" if language == "en" else "ml"


def draft_spoken(llm: LLMProvider, reply_text: str, language: str) -> str | None:
    """LLM step: the digit version of the voice script, or None if the LLM failed."""
    lang = speech_language(language)
    try:
        response = llm.complete(SPOKEN_INSTRUCTION[lang], [ChatMessage.user(reply_text)], None, 600)
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


def to_speech(text: str, language: str) -> str:
    """Make a script ready for TTS: no markup or car ids; for Malayalam, numbers as words."""
    out = _CAR_ID.sub("", text)
    out = _EMOJI.sub("", out).replace("*", "").replace("_", " ").replace("•", "")
    if speech_language(language) == "en":
        out = re.sub(r"₹\s*", "Rupees ", out)
        return re.sub(r"\s+", " ", out).strip()[:MAX_SPOKEN_CHARS]

    out = _MONEY.sub(lambda m: spoken_inr_malayalam(_value(m["a"] or m["b"], m["u"] or m["v"])), out)
    out = _KM.sub(lambda m: f"{_words(_value(m['n'], None))} കിലോമീറ്റർ", out)
    out = _YEAR.sub(lambda m: spoken_year_malayalam(int(m.group(1))), out)
    out = _ANY.sub(lambda m: _words(_value(m.group(0), None)), out)
    return re.sub(r"\s+", " ", out).strip()[:MAX_SPOKEN_CHARS]
