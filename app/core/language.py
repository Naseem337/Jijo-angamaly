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
    # words the bot itself tends to use in Manglish replies (so an English reply with them is caught)
    "nokkamo", "venamo", "venel", "ariyikkam", "ariyikkatte", "aakum", "aavum", "kaananamo", "kaanam",
    "varam", "varamo", "cheyyatte", "ningalkku", "thanne", "okke", "kure", "nammalude", "ivide", "avide",
    "avde", "unden", "aan", "aanallo", "kaanan", "vilikkam", "parayam", "ishtam", "pinne", "allenkil",
})

# Words that say nothing about the language: greetings, car names and car terms used by
# English and Manglish speakers alike. A message of only these keeps the previous language.
NEUTRAL_WORDS = frozenset("""
hi hii hai hello helo hey ok okay k kk oky thanks thank thanku thx tq ty yes yeah yep no nope pls plz please
bro sir madam hmm hm ha haha good gud fine super nice great wow done sure
car cars vandi budget emi km kms kmpl lakh lakhs lac lacs l k rs inr price prize rate cost loan finance
test drive testdrive automatic auto manual amt cvt gear petrol diesel cng ev electric hybrid suv muv sedan
hatchback hatch model models variant top base owner owners first second third insurance rc noc showroom
colour color white black red silver grey gray blue brown year mileage photo photos pic pics video
maruti suzuki hyundai toyota honda kia tata mahindra skoda volkswagen vw renault nissan mg ford jeep chevrolet
mercedes benz bmw audi porsche volvo lexus jaguar land rover datsun fiat mitsubishi isuzu citroen
swift dzire baleno alto wagon wagonr ertiga brezza celerio ciaz ignis ritz zen omni eeco xl s presso fronx
creta venue verna i10 i20 grand nios aura santro eon elantra tucson alcazar exter
innova crysta fortuner glanza etios liva corolla altis camry hycross urban cruiser hyryder
city amaze jazz wrv brv elevate civic xuv xuv300 xuv500 xuv700 scorpio thar bolero marazzo kuv
nexon punch tiago tigor harrier safari altroz hexa zest seltos sonet carens carnival
polo vento virtus taigun kushaq rapid octavia slavia kwid duster triber kiger magnite terrano
hector astor zs gloster ecosport figo aspire endeavour compass meridian
true choice anees makkaraparamba makaraparamba panangangara pattambi cochin kochi malappuram kerala
kozhikode calicut thrissur palakkad perinthalmanna manjeri tirur ernakulam
""".split())

# Common English words, so English text is recognised even when it has no car words.
ENGLISH_WORDS = frozenset("""
a an the i i'm im me my mine you your yours u ur he she it its we us our they them their this that these those
is am are was were be been being do does did doing done have has had having will would shall should can could
may might must not no yes and or but if then so because as at by for from in into of on to with without about
above below under over after before between up down out off again more most less least very too also just only
what which who whom whose when where why how all any both each few many much some such other another same own
there here than now today tomorrow yesterday tonight morning evening night week month year years day days time
want wants wanted need needs needed like looking look find found get got give send sent show see seen check
tell know think available availability buy buying sell selling sold book booking visit come coming go going
call contact number phone address location where's what's how's details detail information info interested
interest please thanks okay much cheap cheaper expensive best better good new old used second hand condition
run driven km under within around upto up range between budget price any other more options option one two
three four five six seven eight nine ten hundred thousand per month down payment exchange trade sell accept
open close closed timing timings hours weekend sunday monday tuesday wednesday thursday friday saturday
let me can't cant don't dont didn't doesn't isn't wasn't won't i'll i've you're it's that's there's
family seater seats seat colour still stock new arrived arrival arrive similar same kind type size big small
fuel economy service history accident free original paint tyres tyre engine battery loan approval documents
mean means sorry hello hi again wait ok sure alright right fine sounds great nice love really actually
today's latest which one please share list send pictures photos price final negotiable discount offer
""".split())


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


def _classify_latin(words: list[str]) -> tuple[int, int, int]:
    """(English words, Manglish markers, other unknown words), ignoring neutral words
    (greetings, car names, car terms) that both English and Manglish speakers use."""
    english = markers = unknown = 0
    for w in words:
        if w.isdigit() or w in NEUTRAL_WORDS:
            continue
        if w in MANGLISH_MARKERS:
            markers += 1
        elif w in ENGLISH_WORDS or w.rstrip("s") in ENGLISH_WORDS:
            english += 1
        else:
            unknown += 1
    return english, markers, unknown


def detect_language(text: str, default: Language = "en") -> Language:
    """Language *and script* to reply in.

    Malayalam script -> "ml". English letters: mostly real English words -> "en"; otherwise
    Malayalam typed in English letters -> "manglish" (spellings vary endlessly: "unden",
    "avde", "ullath", so anything that isn't English counts). Messages with only neutral
    words ("Hi", "ok", "crysta", "2019", emoji) keep `default`, the customer's last language.
    """
    script = detect_script(text)
    if script == "none":
        return default
    if script == "malayalam":
        return "ml"
    english, markers, unknown = _classify_latin(_words(text))
    if english == markers == unknown == 0:
        return default
    if markers and english < 3 * markers:
        return "manglish"
    return "manglish" if unknown > english else "en"


def is_language_neutral(text: str) -> bool:
    """True if the message says nothing about the language ("Hi", "ok", "crysta", emoji)."""
    script = detect_script(text)
    if script != "latin":
        return script == "none"
    return _classify_latin(_words(text)) == (0, 0, 0)


_VOICE_WORD = re.compile(r"\b(?:voice|voic|vois|voise|audio|audeo|vn)\b|വോയ്\u200c?സ്|വോയിസ്|വോയ്സ്|ഓഡിയോ|ശബ്ദ",
                         re.IGNORECASE)
_NO_VOICE = re.compile(r"\b(?:venda|vendaa|vendathilla|dont|don't|do not|no|not|text|type)\b|വേണ്ട|ടൈപ്പ്",
                       re.IGNORECASE)


def asks_for_voice(text: str) -> bool:
    """'voice itt paray', 'voice note ayakko', 'audio venam', 'വോയ്‌സ് ആയി പറയൂ' -> True;
    'voice venda', 'no voice, text please' -> False."""
    return bool(_VOICE_WORD.search(text)) and not _NO_VOICE.search(text)


LANGUAGE_NAMES = {"ml": "Malayalam script", "manglish": "Manglish (English letters)", "en": "English"}
_LATIN_WORD = re.compile(r"[a-z']+")
_CAR_ID = re.compile(r"\b[A-Z]{2,4}-[0-9A-Z]{3,}\b")


def reply_language_problem(reply: str, language: str) -> str | None:
    """Is a bot reply in the one language it should be? None if fine, else what's wrong
    (fed to one rewrite). Car names, car terms, ids and numbers are allowed in any reply."""
    text = _CAR_ID.sub(" ", reply)
    ml_letters = len(_MALAYALAM_CHAR.findall(text))
    latin = [w for w in _LATIN_WORD.findall(normalize_text(text)) if w.strip("'")]
    english, markers, unknown = _classify_latin(latin)
    want = LANGUAGE_NAMES.get(language, "English")
    if language in ("en", "manglish") and ml_letters:
        return f"The whole reply must be in {want}; it contains Malayalam script."
    if language == "ml":
        if not ml_letters:
            return "The whole reply must be in Malayalam script; it has none."
        if markers + unknown >= 2:
            return ("The whole reply must be in Malayalam script; it mixes in Manglish words. Only car "
                    "names, prices, car ids and common English car words may stay in English letters.")
        if english >= 8:
            return "The whole reply must be in Malayalam script; it contains English sentences."
    if language == "en" and markers >= 1:
        return "The whole reply must be in simple English; it contains Manglish words."
    if language == "manglish" and english >= 6 and markers == 0 and unknown == 0:
        return "The customer writes Manglish: reply in Manglish (Malayalam in English letters), not English."
    return None
