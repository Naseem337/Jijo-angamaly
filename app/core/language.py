"""Language and intent-word detection for Malayalam, English and Manglish.

Deterministic on purpose. Opt-outs and yes/no confirmations must not depend on the LLM.
  - detect_language()      -> "ml" | "manglish" | "en"
  - is_opt_out()           -> stop / venda / not interested … (but NOT "ee car venda")
  - is_opt_in()            -> START
  - classify_confirmation()-> "yes" | "no" | None   (voice read-back answers)
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Literal

from app.core.money import normalize_text

Language = Literal["ml", "manglish", "en"]


def _normalized(words: Iterable[str]) -> frozenset[str]:
    """Word sets are compared against normalize_text() output, so normalise them too."""
    return frozenset(normalize_text(w) for w in words)

_MALAYALAM_CHAR = re.compile(r"[ഀ-ൿ]")
_LATIN_CHAR = re.compile(r"[a-z]")
_WORD_RE = re.compile(r"[a-z']+|[ഀ-ൿ]+|\d+")

# Common Manglish words that are not normal English words.
MANGLISH_MARKERS = frozenset({
    "undo", "ondo", "unda", "und", "undu", "venam", "venda", "vendi", "venamenkil",
    "ethra", "ethre", "entha", "enthanu", "enthu", "aano", "ano", "aanu", "anu",
    "alle", "alla", "illa", "ille", "illey", "enikku", "enik", "ningal", "ningalude",
    "ente", "njan", "njn", "sheri", "seri", "kittumo", "kittum", "kitto", "vila",
    "eppo", "eppol", "evide", "evideya", "nokkam", "nokkan", "parayamo", "paranju",
    "cheyyam", "cheyyamo", "chetta", "chettan", "chechi", "mone", "kollam", "nalla",
    "kurachu", "kooduthal", "vandi", "kaashu", "roopa", "thazhe", "mukalil", "onnu",
    "aayi", "ayi", "tharumo", "tharam", "varum", "varumo", "pattumo", "pattum",
    "vechittundo", "edukkam", "edukkan", "athe", "aathe", "ippo", "ippol", "vere",
    "ithu", "athu", "ee", "aa", "lakhinu", "budgetil", "kalar", "vandiyude", "ayakkamo",
})

# --------------------------------------------------------------------------- opt-out / opt-in

# Words/phrases that mean "stop messaging me". All lower-case, chillu-normalised.
_OPT_OUT_PHRASES = [
    "stop", "unsubscribe", "opt out", "optout", "not interested", "no longer interested",
    "dont send", "don't send", "do not send", "stop sending", "no more messages",
    "no more alerts", "stop alerts", "venda", "ini venda", "eni venda", "msg venda",
    "message venda", "messages venda", "alert venda", "ayakkanda", "ayakkenda",
    "താല്പര്യമില്ല", "താല്പ്പര്യമില്ല", "താല്പര്യം ഇല്ല", "താല്പ്പര്യം ഇല്ല",
    "വേണ്ട", "ഇനി വേണ്ട", "മെസ്സേജ് വേണ്ട", "അയക്കണ്ട", "അയക്കേണ്ട", "അയയ്ക്കേണ്ട",
]
# Words allowed around an opt-out word in a short message ("stop pls", "ini venda chetta").
_FILLERS = _normalized({
    "pls", "plz", "please", "thanks", "thank", "you", "thx", "ok", "okay", "sir", "madam",
    "chetta", "chechi", "bro", "now", "ini", "eni", "i", "am", "im", "i'm", "me", "my",
    "to", "the", "all", "any", "more", "message", "messages", "msg", "msgs", "alert",
    "alerts", "updates", "ഇനി", "സാർ", "ചേട്ടാ", "നന്ദി", "മെസ്സേജ്", "എനിക്ക്", "ഇപ്പോൾ",
})
# In longer messages an opt-out word only counts next to a messaging word, so
# "ee car venda, vere undo?" ("not this car, any other?") is NOT an opt-out.
_MESSAGING_WORDS = _normalized({
    "message", "messages", "msg", "msgs", "alert", "alerts", "notification", "notifications",
    "whatsapp", "updates", "ayakkanda", "ayakkenda", "മെസ്സേജ്", "അലേര്ട്ട്", "അയക്കണ്ട",
    "അയക്കേണ്ട", "അയയ്ക്കേണ്ട",
})
_ALWAYS_OPT_OUT = ("unsubscribe", "opt out", "optout", "stop sending", "dont send",
                   "don't send", "do not send", "no more messages", "no more alerts",
                   "stop alerts")

_OPT_IN_WORDS = frozenset({"start", "subscribe", "resume", "restart", "unstop"})


def _words(text: str) -> list[str]:
    return _WORD_RE.findall(normalize_text(text))


def _contains_phrase(words: list[str], phrase: str) -> bool:
    target = _words(phrase)
    n = len(target)
    return any(words[i:i + n] == target for i in range(len(words) - n + 1))


def is_opt_out(text: str, extra_phrases: Iterable[str] = ()) -> bool:
    """True if the customer is asking us to stop sending them alerts/marketing."""
    words = _words(text)
    if not words:
        return False
    phrases = [*_OPT_OUT_PHRASES, *extra_phrases]
    matched = [p for p in phrases if _contains_phrase(words, p)]
    if not matched:
        return False
    if any(p in _ALWAYS_OPT_OUT for p in matched):
        return True
    # Short message made only of opt-out words + fillers: "venda", "stop pls", "not interested".
    opt_out_vocab = {w for p in phrases for w in _words(p)}
    if len(words) <= 5 and all(w in opt_out_vocab or w in _FILLERS for w in words):
        return True
    # Longer message: needs an explicit messaging word ("msg ayakkanda", "alerts venda").
    return any(w in _MESSAGING_WORDS for w in words)


def is_opt_in(text: str) -> bool:
    """'START' / 'start alerts' / 'resume': re-subscribe after an opt-out."""
    words = _words(text)
    return (bool(words) and len(words) <= 3 and words[0] in _OPT_IN_WORDS
            and all(w in _OPT_IN_WORDS or w in _FILLERS for w in words))


# --------------------------------------------------------------------------- yes / no

_YES = _normalized({
    "yes", "yeah", "yep", "ya", "yah", "ok", "okay", "okey", "k", "correct", "right",
    "sure", "confirm", "confirmed", "done", "sheri", "seri", "shari", "athe", "aathe",
    "athey", "aanu", "anu", "uvvu", "haan", "ha", "ശരി", "അതെ", "ഓക്കെ", "ഓക്കേ",
    "ശരിയാണ്", "ഉവ്വ്", "ആണ്",
})
_NO = _normalized({
    "no", "nope", "nah", "wrong", "incorrect", "alla", "illa", "thettu", "thettanu",
    "അല്ല", "ഇല്ല", "തെറ്റ്", "തെറ്റാണ്",
})


def classify_confirmation(text: str) -> Literal["yes", "no"] | None:
    """Answer to "Shall I save this? Budget 8 lakh, 2018+, Swift". None = not a plain yes/no."""
    words = _words(text)
    if not words or len(words) > 5:
        return None
    has_no = any(w in _NO for w in words)
    if has_no:
        return "no"
    if all(w in _YES or w in _FILLERS for w in words) and any(w in _YES for w in words):
        return "yes"
    return None


# --------------------------------------------------------------------------- language


def detect_script(text: str) -> Literal["malayalam", "latin", "none"]:
    ml = len(_MALAYALAM_CHAR.findall(text))
    latin = len(_LATIN_CHAR.findall(text.lower()))
    if ml == 0 and latin == 0:
        return "none"
    return "malayalam" if ml >= 3 and ml / (ml + latin) >= 0.2 else "latin"


def detect_language(text: str, default: Language = "en") -> Language:
    """Language *and script* to reply in.

    Malayalam script -> "ml"; Malayalam typed in English letters -> "manglish"; otherwise "en".
    Text with no letters (emoji, numbers) returns `default` (the customer's last language).
    """
    script = detect_script(text)
    if script == "none":
        return default
    if script == "malayalam":
        return "ml"
    words = [w for w in _words(text) if not w.isdigit()]
    markers = sum(1 for w in words if w in MANGLISH_MARKERS)
    if markers >= 2 or (markers == 1 and len(words) <= 4):
        return "manglish"
    return "en"
