"""The guest conversation: plain free text in any language, answered like a person at the front desk.

No menus, lists or buttons. The AI reads the guest's message, replies in the guest's own language and
style (English, Malayalam, Manglish, Hindi/Hinglish, Tamil/Tanglish) and asks for what is missing: place,
check-in date, nights, persons, rooms. It never decides a fact: prices, capacity, availability and the
booking all come from the tools below, which run the same code the dashboard uses.

Per-guest state in repo.get_state(phone):
    st["bk"] = {property_id, check_in, check_out, adults, children,
                chosen: {room_type_id, rooms, extra_beds, total, advance, turn}}
    st["turn"] = counts the guest's messages; a booking can only be created on a LATER turn than the
                 one where the summary was shown, so the guest's "yes" is always a separate message.
"""

from __future__ import annotations

import difflib
import json
import logging
import re
import time
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from typing import Any

from app.core.clock import to_ist
from app.core.language import _classify_latin, _words, is_language_neutral
from app.core.phone import try_normalize_phone
from app.providers.llm import ChatMessage, LLMError, ToolCall, ToolSpec
from app.resort import availability as av
from app.resort import leads
from app.resort.db import SoldOut
from app.resort.i18n import S as _STRINGS, status_name, t
from app.resort.models import Booking, Property, RoomType
from app.resort.payments import PaymentError

log = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 6
MAX_REPLY_TOKENS = 700
HISTORY = 20  # real messages the AI sees (photo rows do not count)
SESSION_GAP = timedelta(hours=24)  # after a pause this long, a greeting starts a new conversation; anything else continues
LINK = re.compile(r"https?://\S+")
STYLE_LABELS = {"en": "English", "ml": "Malayalam script", "hi": "Hindi script", "ta": "Tamil script",
                "manglish": "Manglish (Malayalam in English letters)", "hinglish": "Hinglish (Hindi in English letters)",
                "tanglish": "Tanglish (Tamil in English letters)"}
GREETING = re.compile(r"^\W*((hi+|hello+|hey+|hai+|hlo|hola|namaste|namaskar\w*|vanakkam|good\s*(morning|afternoon|evening))"
                      r"|നമസ്കാരം|ഹായ്|ഹലോ|வணக்கம்|ஹாய்|நமஸ்தே|नमस्ते|हाय|हेलो|हैलो)"
                      r"(?:[\s,]+(?:sir|madam|mam|maam|bro|brother|chetta|chettan|chechi|dear|there|team|ji|anna|bhai|"
                      r"bhaiya|machane|macha|da|all|everyone|friend))?\W*$", re.IGNORECASE)
# Styles the AI may report. Fixed messages (payment, ID) use the script language; Latin-script styles get English.
STYLES = ("en", "ml", "manglish", "hi", "hinglish", "ta", "tanglish")
# Replies are ALWAYS in English letters (user, 2026-11-11): a guest who writes or speaks Malayalam / Tamil / Hindi gets
# Manglish / Tanglish / Hinglish, never the native script.
LATIN_STYLE = {"ml": "manglish", "ta": "tanglish", "hi": "hinglish"}
STYLE_LANG = {"en": "en", "ml": "ml", "hi": "hi", "ta": "ta", "manglish": "mn", "hinglish": "hn", "tanglish": "tn"}
# "English please" / "Tamil la pesunga" / "Malayalathil" / "Hindi mein baat karo": that language stays until the guest
# asks for another one or writes a long message clearly in another language (owner, 2026-11-18)
LANG_REQUEST = {
    "en": re.compile(r"\b(?:english\s*(?:please|pls|plz)|in english|speak english|reply in english|"
                     r"english\s*(?:il|la|mein|me)\s*(?:parayamo|pesunga|sollunga|bolo|baat|reply))\b", re.IGNORECASE),
    "tanglish": re.compile(r"\btamil\s*-?\s*(?:la|le|il|lae)?\s*(?:pesunga|pesu|sollunga|please|pls|reply|venum)\b|"
                           r"\bin tamil\b|\btamil-?la\b", re.IGNORECASE),
    "manglish": re.compile(r"\bmalayalam\s*(?:il|ithil|thil)?\s*(?:parayamo|paranja|please|pls|reply|venam)\b|"
                           r"\bmalayalathil\b|\bin malayalam\b|\bmalayalam il\b", re.IGNORECASE),
    "hinglish": re.compile(r"\bhindi\s*(?:mein|me|main)?\s*(?:baat|bolo|boliye|please|pls|reply|karo)\b|\bin hindi\b",
                           re.IGNORECASE),
}
INDIC = re.compile(r"[\u0900-\u097f\u0b80-\u0bff\u0d00-\u0d7f]")
ACK_WORDS = frozenset("""ok okay okk okey okie k kk kay thanks thank thankyou thanx thx tnx tq ty you so much lot a very fine cool
    noted got it sure alright all right yes yeah yep ya yup haha hahaha lol hmm hm mm mmm oh ohh ah aah aha wow nice great good
    super awesome perfect lovely beautiful excellent superb welcome then will see later now bro sir madam da dear anna chetta
    chechi sheri sari shari shaari seri athe aanu nannayi kollam adipoli polichu mathi ശരി ഓക്കെ ഓകെ താങ്ക്സ് നന്ദി കൊള്ളാം
    ठीक है धन्यवाद शुक्रिया अच्छा சரி நன்றி""".split())
# Resort words, months and place names: they say nothing about the language (the shared word list is for cars, so
# "Pozhiyoram rooms available?" used to look like Manglish and an English guest got a Manglish reply).
RESORT_NEUTRAL = frozenset("""
room rooms resort resorts stay stays night nights per person persons people pax adult adults kid kids child children
deluxe suite suites villa villas cottage cottages pool beach backwater backwaters houseboat lake view ac nonac
checkin checkout check bed beds extra breakfast food meals wifi parking family couple honeymoon trip tour package
booking book booked advance balance upi gpay phonepe paytm razorpay link payment id
jan feb mar apr may jun jul aug sep sept oct nov dec january february march april june july august september october
november december mon tue wed thu fri sat sun weekend weekday
pozhiyoram vibes kanthalloor alleppey alappuzha allepey aleppey munnar kerala kochi cochin kumarakom vagamon idukki
""".split())


def latin_style(text: str, previous: str = "en") -> str:
    """'manglish' or 'en' for a message in English letters. Only real Manglish words make it Manglish; place names,
    resort words and other names never do, so an English guest always gets English. A message with nothing to go
    on ('Alleppey', '2 divasam', 'Asha') keeps the guest's language so far."""
    if MANGLISH_WORDS.search(text):
        return "manglish"
    words = [w for w in _words(text) if w not in RESORT_NEUTRAL]
    names = {w.lower().strip(".,!?") for w in text.split()[1:] if w[:1].isupper()}  # 'Asha', 'Thrissur' mid-sentence
    english, markers, unknown = _classify_latin([w for w in words if w not in names])
    if markers:
        return "manglish"
    # No Malayalam word at all: never Manglish. Words the English list does not know ('pets', 'unmarried', 'ppl',
    # 'nov') once made 'Are pets allowed?' Manglish, and English guests got Malayalam replies (2026-11-20).
    if english:
        return "en"
    return previous if previous in ("en", "manglish") else "en"  # '2 divasam', 'Asha, Kochi'


MAX_GENERAL_PHOTOS = None  # no limit (client, 2026-11-19): every general photo uploaded in the dashboard goes
# The AI asking for the stay details itself (any language in English letters): replaced by the owner's form.
DATES_QUESTION = re.compile(
    r"check[\s-]?in|check[\s-]?out|\bdates?\b|which day|\bwhen\b|how many (?:people|persons|guests|nights|days|of you|friends|members|adults|kids|children|are coming)"
    r"|\bstaying\b|\bnights\b|\bpax\b|ethra (?:per|peru|aal|divasam|day|night)|\beppol|\bennu\b|kitne (?:log|din)"
    r"|\bkab\b|evlo (?:per|peru|naal|days?)|evvalavu (?:per|naal)|ethana(?:i)? (?:per|peru|naal)|\beppo\b|\beppodhu\b"
    r"|kitni raat|kab se|\bta?reekh|\btarikh|\bthethi|\bthedhi|which year|what year|\b20\d\d or 20\d\d\b"
    r"|you said \d+ (?:person|people|night|day)|\bdetails?\b", re.IGNORECASE)  # 'Details onnu parayamo?' (2026-11-21)
BOOKING_WISH = re.compile(r"\b(?:rooms?|resorts?|stay|book\w*|cottages?|accommodation|vacancy|availab\w*|kamra)\b|"
                          r"റൂം|മുറി|റിസോർട്ട്|ബുക്ക്|ரூம்|அறை|ரிசார்ட்|புக்|रूम|कमरा|रिसॉर्ट|बुक", re.IGNORECASE)
NOT_NOW = re.compile(r"\b(?:later|will|thanks|thank|pinne|sheri|ok|okay|nice|good|was|were|cancel\w*|super|great|"
                     r"beautiful|lovely|awesome|amazing|wow|adipoli|kollam|kidu|semma|mast|badhiya)\b", re.IGNORECASE)
ASKING = re.compile(r"\b(?:share|tell|send|give|let me know|please|pls|kindly|confirm|need|needed|require|venam|"
                    r"parayamo|parayu|parayoo|ariyanam|ariyan|ariyenam|ariyende|ariyikk"
                    r"\w*|batao|bataiye|bataye|batayein|bataiyega|sollunga|sollungal|sollu|anuppunga|kodunga|"
                    r"ayakkamo|ayakku)\b", re.IGNORECASE)
FILLER = re.compile(r"(?:(?:got it|sure|okay|ok|alright|noted|fine|great|thanks|thank you|sheri|shari)[\s,.!]*"
                    r"(?:sir|madam)?[\s,.!]*)*", re.IGNORECASE)
_MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov",
                                       "dec"), 1)}
_MON = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?"
_DAY_MON = re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s*(?:of\s+)?{_MON}", re.IGNORECASE)
_MON_DAY = re.compile(rf"\b{_MON}\s*(\d{{1,2}})(?:st|nd|rd|th)?\b"  # 'dec 12', never 'december 2 people'
                      r"(?!\s*(?:persons?|people|pax|per\b|peru|adults?|guests?|members|nights?|days?|aal|log|rooms?))",
                      re.IGNORECASE)
_NUMERIC = re.compile(r"\b(\d{1,2})[/.-](\d{1,2})(?:[/.-](\d{2,4}))?\b")
_DAY_ONLY = re.compile(r"(?:\b(?:till|to|until|upto|up to|thru|through)|-|\bcheck(?:ed|ing)?[\s-]?out(?:\s+(?:in|on|at|by))?"
                       r"\s*[:=-]?)\s*(\d{1,2})(?:st|nd|rd|th)?\b"
                       r"(?!\s*(?:[/.-]\d|person|people|pax|per\b|adult|night|room|guest|member|aal|days?))", re.IGNORECASE)
_PERSONS_LOOSE = re.compile(r"\b(?:for|of|we are|we're|total)\s+(\d{1,2})\b(?!\s*(?:nights?|days?|rooms?|dec|jan|feb|"
                            r"mar|apr|may|jun|jul|aug|sep|oct|nov|th|st|nd|rd))|\b(\d{1,2})\s*(?:of us|members)\b",
                            re.IGNORECASE)
CLOSING = re.compile(r"^(?:anything else|is there anything else|vere enth[ae]n?[gk]?[ie]lum|vere entha|let me know|"
                     r"feel free|enth[ae]n?[gk]?[ie]lum|any (?:other )?(?:doubts?|questions?)|do you have any|kuch aur|"
                     r"aur kuch|vera edhavadhu|hope this helps)[^.?!]*[.?!]*$", re.IGNORECASE)
OWNER_ASK = re.compile(r"\b(?:owner|manager|talk to (?:a |the )?(?:person|human|someone|staff)|call me|"
                       r"vilikk\w*|phone cheyy\w*|baat karni|pesanum)\b", re.IGNORECASE)
CHANGE_ASK = re.compile(r"\b(?:change|modify|postpone|prepone|reschedule|shift|cancel\w*|refund|maatt\w*|matt\w*|"
                        r"cancel cheyy\w*|badal\w*)\b", re.IGNORECASE)
_UNTIL = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\s*(?:vare|vareyum|varey|tak|tk|varai|varaikkum|varaikum|"
                    r"(?:nu|nn|ku|kku|ke|la)\s+(?:pokum|pogum|pokunnu|po|checkout|check out|leave|leaving|return|thirichu|kilambum|"
                    r"jayenge|niklenge))\b",
                    re.IGNORECASE)  # '22 vare'
_STAY_LENGTH = re.compile(r"\d+\s*(?:nights?|days?|d?h?ivas\w*|disam|dhisam|ra+thri\w*|din|raat\w*|iravu|naal\w*)\b|\bone night\b|\bovernight\b|"
                          r"\b(?:a|one|oru|ek|single|two|randu|rendu|do|three|moonu|moonnu|teen)\s+(?:nights?|days?|d?h?ivas\w*|disam|raat\w*|din|naal\w*)\b",
                          re.IGNORECASE)
_NIGHTS = re.compile(r"\b(\d{1,2})\s*(?:nights?|ra+thri|raat|raatein|iravu|days?|d?h?ivas\w*|disam|dhisam|din|naal\w*)\b", re.IGNORECASE)
# 'Person 3' at the start of a line, '3 persons', 'persons : 3'. Never across a line break: 'To 23\nPerson 3' was once
# read as 23 persons (live chat, 2026-11-19).
_PERSONS = re.compile(r"(?:number of persons|no of persons|number of people|persons|pax|guests|people)[ \t]*(?:[:=,-]|is|are)?[ \t]*(\d{1,2})\b(?![ \t]*(?:rooms?|nights?|days?)\b)|(?:we are|we're|we r|njangal|njmal|nangal|hum)[ \t]+(\d{1,2})\b|\b(\d{1,2})[ \t]*(?:persons?|"
                      r"people|ppl|pple|pax|per\b|peru|adults?|members|guests?|aal\w*|log)\b|"
                      r"(?:^|\n)[ \t]*(?:persons?|people|pax|guests?|adults?|members)[ \t]*[:=-]?[ \t]*(\d{1,2})[ \t]*(?:$|\n)",
                      re.IGNORECASE)


def _upcoming(day: int, month: int, today: date, year: int | None = None) -> date | None:
    """The day/month on or after today. A date that passed only days ago ('nov 20' typed just after midnight on 21 Nov)
    stays in this year, so the guest is told it has passed; it is never quietly moved a year ahead (live chat,
    2026-11-21: 'nov 20 to 30' became 2027 and the bot then kept asking for the dates)."""
    for y in ([year] if year else [today.year, today.year + 1]):
        try:
            d = date(y, month, day)
        except ValueError:
            continue
        if d >= today or year or 0 < (today - d).days <= 14:
            return d
    return None


# Month, person, night and "until" words in Malayalam, Tamil and Hindi script (typed or from a voice note), turned into
# the English-letter words parse_stay already reads (2026-11-18: "ഡിസംബർ 12 മുതൽ 15 വരെ 3 പേർ" read nothing before).
_NATIVE_STAY_WORDS = [
    ("jan", "ജനുവരി|ஜனவரி|जनवरी"), ("feb", "ഫെബ്രുവരി|பிப்ரவரி|फ़रवरी|फरवरी"), ("mar", "മാർച്ച്|மார்ச்|मार्च"),
    ("apr", "ഏപ്രിൽ|ஏப்ரல்|अप्रैल"), ("may", "മേയ്|മെയ്|மே|मई"), ("jun", "ജൂൺ|ஜூன்|जून"), ("jul", "ജൂലൈ|ஜூலை|जुलाई"),
    ("aug", "ഓഗസ്റ്റ്|ആഗസ്റ്റ്|ஆகஸ்ட்|अगस्त"), ("sep", "സെപ്റ്റംബർ|செப்டம்பர்|सितंबर|सितम्बर"),
    ("oct", "ഒക്ടോബർ|அக்டோபர்|अक्टूबर|अक्तूबर"), ("nov", "നവംബർ|நவம்பர்|नवंबर|नवम्बर"),
    ("dec", "ഡിസംബർ|டிசம்பர்|दिसंबर|दिसम्बर"),
    ("per", "പേരുണ്ട\\S*|പേരാണ്|പേരുണ്ട്|പേർ|പേര്|ആളുകൾ|ആൾക്കാർ|ആളുണ്ട\\S*|ആൾ|பேர்|நபர்கள்|நபர்|लोगों|लोग|व्यक्ति|आदमी|जन"),
    ("check in", "ചെക്കിന്|ചെക്കിൻ|ചെക്ക് ഇൻ|ചെക്ക്-ഇൻ|ചെക്ക്ഇൻ|ചെക്ക് ഇന്"),
    ("check out", "ചെക്ക്ഔട്ട്|ചെക്ക് ഔട്ട്|ചെക്കൗട്ട്|ചെക്ക്-ഔട്ട്|ചെക്ക് ഔട്ട്"),
    ("number of", "നമ്പർ ഓഫ്|നമ്പര് ഓഫ്"),
    ("persons", "പേഴ്സൺസ്|പേഴ്സൺ|പെഴ്സൺസ്|പേഴ്സൻസ്"),
    ("people", "പീപ്പിൾ|പീപ്പിള്"),
    ("rooms", "റൂമുകൾ|റൂംസ്|റൂമ്|റൂം|മുറികൾ|മുറി|ரூம்கள்|ரூம்|அறைகள்|अरै|रूम्स|रूम|कमरे|कमरा"),
    ("nights", "രാത്രി|ராத்திரி|இரவு|इरवु|रातें|रात"), ("vare", "വരെ|வரை|तक"),
    ("days", "ദിവസം|ദിവസത്തേക്ക്|ദിവസ|நாள்|நாட்கள்|दिन"), ("kid", "കുട്ടി\\S*|குழந்தை\\S*|बच्च\\S*"),
    ("one", "ഒരു|ஒரு|एक"),
]
_NATIVE_STAY = [(word, re.compile(pattern)) for word, pattern in _NATIVE_STAY_WORDS]


_MONTH_NAMES = {"jan": "january", "feb": "february", "mar": "march", "apr": "april", "may": "may", "jun": "june",
                "jul": "july", "aug": "august", "sep": "september", "oct": "october", "nov": "november",
                "dec": "december"}
_NOT_MONTH = {"now", "new", "nor", "not", "noon", "dev", "den", "day", "max", "man", "mat", "jam", "fan", "fed",
              "few", "per", "pax", "out", "apt", "art", "sea", "set", "see", "son", "sun", "mon", "may", "jun", "jul",
              "aug", "the", "and", "for", "two", "one", "six", "ten", "can", "nil", "nos", "dey", "dear", "dec"}
_NEAR_DAY = re.compile(r"\b([a-z]{3,9})\.?\s*(\d{1,2})(?:st|nd|rd|th)?\b|\b(\d{1,2})(?:st|nd|rd|th)?\s*([a-z]{3,9})\b",
                       re.IGNORECASE)


def _month_typo(word: str) -> str | None:
    """'niv' -> 'nov', 'decmber' -> 'dec', 'jnuary' -> 'jan' (one letter off a month name), else None."""
    w = word.lower()
    if w in _NOT_MONTH or w[:3] in _MONTHS:
        return None
    for abbr, full in _MONTH_NAMES.items():
        if len(w) == 3 and w[0] == abbr[0] and sum(a != b for a, b in zip(w, abbr)) == 1:
            return abbr  # 'niv' -> 'nov', 'dex' -> 'dec'
        if len(w) >= 5 and w[0] == full[0] and difflib.SequenceMatcher(None, w, full).ratio() >= 0.8:
            return abbr  # 'decmber', 'jnuary', 'novemebr'
    return None


_NUMBER_WORDS = {"two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
                 "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
                 "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20, "randu": 2, "rendu": 2, "moonu": 3,
                 "munnu": 3, "naalu": 4, "nalu": 4, "anju": 5, "aaru": 6, "ezhu": 7, "ettu": 8, "ombathu": 9,
                 "pathu": 10, "teen": 3, "chaar": 4, "char": 4, "paanch": 5, "panch": 5}
_NUMBER_WORD = re.compile(r"\b(" + "|".join(_NUMBER_WORDS) + r")\b", re.IGNORECASE)
# 'one' only next to a person / room word ('one room', 'one person'): 'which one', 'one moment' stay words
_ONE = re.compile(r"\bone(?=[ \t]+(?:persons?|people|adults?|rooms?|nights?|days?)\b)|"
                  r"(?<=\bpersons )one\b|(?<=\bpeople )one\b", re.IGNORECASE)


_ORDINALS = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7, "eighth": 8,
             "ninth": 9, "tenth": 10, "eleventh": 11, "twelfth": 12, "thirteenth": 13, "fourteenth": 14,
             "fifteenth": 15, "sixteenth": 16, "seventeenth": 17, "eighteenth": 18, "nineteenth": 19, "twentieth": 20,
             "twenty first": 21, "twenty-first": 21, "twenty second": 22, "twenty-second": 22, "twenty third": 23,
             "twenty-third": 23, "twenty fourth": 24, "twenty-fourth": 24, "twenty fifth": 25, "twenty-fifth": 25,
             "twenty sixth": 26, "twenty-sixth": 26, "twenty seventh": 27, "twenty-seventh": 27, "twenty eighth": 28,
             "twenty-eighth": 28, "twenty ninth": 29, "twenty-ninth": 29, "thirtieth": 30, "thirty first": 31,
             "thirty-first": 31,
             "ഒന്നാം": 1, "രണ്ടാം": 2, "മൂന്നാം": 3, "നാലാം": 4, "അഞ്ചാം": 5, "ആറാം": 6, "ഏഴാം": 7, "എട്ടാം": 8,
             "ഒമ്പതാം": 9, "ഒൻപതാം": 9, "പത്താം": 10}
# English ordinals only next to a month or a date word ('February first', 'the fifth', 'to fifth'): 'first floor'
# and 'second week' stay words; Malayalam ordinals are only used for dates
_ORDINAL_WORD = re.compile(
    r"\b((?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+|the\s+|to\s+|till\s+|until\s+|from\s+)("
    + "|".join(sorted((k for k in _ORDINALS if k.isascii()), key=len, reverse=True))
    + r")\b|(" + "|".join(k for k in _ORDINALS if not k.isascii()) + ")", re.IGNORECASE)
# '5-ാം തീയതി വരെ' / '5ാം തീയതി' / '5th date' / 'the 5th': the plain day number
_DAY_SUFFIX = re.compile(r"(\d{1,2})\s*-?\s*ാം|\s*-?\s*(?:തീയതി|തിയതി|തീയ്യതി|തിയ്യതി|theeyathi|thiyathi|tharikh)|"
                         r"\bthe\s+(?=\d)|(?<=\d(?:st|nd|rd|th))\s+date\b", re.IGNORECASE)


def _spoken_numbers(text: str) -> str:
    """Numbers said in words in a voice note ('number of persons two', 'three people', 'moonu per') as digits
    (live chat 2026-11-21: 'number of persons 2' from a voice note was never read), ordinals ('February first to
    fifth', 'ഒന്നാം ... അഞ്ചാം തീയതി') and '5-ാം തീയതി വരെ' as plain day numbers."""
    text = _ONE.sub("1", text)
    text = _ORDINAL_WORD.sub(lambda m: (m.group(1) or "") + str(_ORDINALS[(m.group(2) or m.group(3)).lower()]), text)
    text = _DAY_SUFFIX.sub(lambda m: m.group(1) or " ", text)
    return _NUMBER_WORD.sub(lambda m: str(_NUMBER_WORDS[m.group(1).lower()]), text)


def _latin_stay(text: str) -> str:
    text = _spoken_numbers(text)
    for word, rx in _NATIVE_STAY:
        text = rx.sub(f" {word} ", text)

    def fix(m: re.Match) -> str:  # a month misspelt next to a day number ('Niv 22', '22 decmber')
        word = m.group(1) or m.group(4)
        abbr = _month_typo(word)
        return m.group(0).replace(word, abbr, 1) if abbr else m.group(0)

    return _NEAR_DAY.sub(fix, text)


MSG_SEP = "\x1e"  # between the guest's messages in _recent_guest_text
_DAY_AFTER = re.compile(r"\b(?:day after tomorrow|mattannal|matannal|mattanal|mattannaal|mattanaal|matannaal|"
                        r"mattenal|marunaal|parso|parson)\b|മറ്റന്നാൾ|परसों",
                        re.IGNORECASE)
_TOMORROW = re.compile(r"\b(?:tomorrow|tmrw|tmr|tomo|naale|nale|naaley|naalai|naalaikku|nalaiku|kal)\b|നാളെ|நாளை|कल",
                       re.IGNORECASE)
_TODAY = re.compile(r"\b(?:today|tonight|innu|innale raathri|aaj|indru|inniki|innaikku|innikku)\b|"
                    r"(?<!check )(?<!check)\binn\b(?! of\b)|ഇന്ന്|இன்று|आज",
                    re.IGNORECASE)
_WORD_NUM = {"a": 1, "one": 1, "oru": 1, "ek": 1, "single": 1, "two": 2, "randu": 2, "rendu": 2, "do": 2, "three": 3,
             "moonu": 3, "moonnu": 3, "munnu": 3, "teen": 3}
_WORD_NIGHTS = re.compile(r"\b(a|one|oru|ek|single|two|randu|rendu|do|three|moonu|moonnu|munnu|teen)\s+"
                          r"(?:nights?|days?|d?h?ivas\w*|disam|dhisam|raat\w*|din|naal\w*|ra+thri\w*)\b|\b(overnight)\b",
                          re.IGNORECASE)
FORM_START = "Please share these details"
ROOMS_OFFER_ASK = re.compile(r"\b(\d{1,2})\s*rooms?\b[^.?!\n]*\?", re.IGNORECASE)
ROOMS_MATH = re.compile(r"\b\d+\s*(?:rooms?|room\(s\)|extra beds?|beds?)\b|\bextra beds?\b|"
                        r"\b(?:rooms?|beds?)\s*(?:needed|venam|venum|chahiye)\b", re.IGNORECASE)
# 'Price etreya', 'Prive ethra', 'how much', 'rate?': the price of the stay. Payment words (advance, discount, refund)
# stay with the AI. 'ethra' alone only in a short message ('ethra per' asks how many people).
PRICE_Q = re.compile(r"\b(?:price|prices|prize|prise|prive|pricee|rate|rates|tariff|cost|how much|charges?|rent|vila|"
                     r"kitna|kitne ka|evlo|evvalavu)\b|വില|எவ்வளவு|कितना", re.IGNORECASE)
PRICE_SHORT = re.compile(r"^\W*(?:ethra|etra|ethraya|etreya|ethreya|ethrayaa|ethra aakum|ethra aavum|ethrayaakum)\W*$",
                         re.IGNORECASE)
NOT_PRICE = re.compile(r"discount|offer|reduce|advance|balance|refund|cancel|paid|extra bed|early", re.IGNORECASE)
# bills and deposits the AI made up ('Electricity bill-um property-il settle cheyyanam', live chat 2026-11-21): only
# when the guest asked about them
CHARGE_TALK = re.compile(r"electric\w*|current bill|power bill|water bill|utilit\w*|\bdeposit\w*|\bkarant\w*",
                         re.IGNORECASE)
# how the code picks rooms is internal: never 'The resort will assign the room based on its own priority' (owner,
# 2026-11-21)
INTERNAL_RULE = re.compile(r"\bpriority\b|\bautomatic\w*|\bthe (?:system|code|tool|bot)\b|\b(?:assign|allot|allocat)\w*\b[^.?!\n]{0,40}\brooms?\b|"
                           r"\brooms?\b[^.?!\n]{0,40}\b(?:assign|allot|allocat)\w*|"
                           r"\b(?:resort|owner|team|we)\b[^.?!\n]{0,30}\b(?:decide|choose|pick)s?\b[^.?!\n]{0,30}\brooms?\b",
                           re.IGNORECASE)
STALLING = re.compile(r"\b(?:one moment|just a moment|a moment|let me (?:check|see|confirm)|i will check|"
                      r"i'll check|please wait|wait a (?:moment|minute|sec)|checking now|"
                      r"give me a (?:moment|minute|sec)|ready aakk\w*|nokkatte|check cheyy(?:atte|am|aam|unnu)|"
                      r"onnu nokk\w*|wait cheyy\w*|kurachu samayam|details ready|prepare cheyy\w*|"
                      r"i will (?:prepare|arrange|send)|i'll (?:prepare|arrange|send))\b", re.IGNORECASE)
LOCATION_ASK = re.compile(r"\blocation\b|\bwhich (?:place|resort|property|town)\b|\bethu (?:sthalam|place|location)\b|"
                          r"\bevide(?:ya|yaa|yanu|aanu)?\b.*\b(?:venam|vendath|stay)\b|സ്ഥലം|லொகேஷன்|लोकेशन",
                          re.IGNORECASE)
_OUT_CUE = re.compile(r"(?:till|until|upto|up to|to|check[\s-]?out\s*:?|vare|tak|varai|-)\s*$", re.IGNORECASE)


def parse_stay(text: str, today: date) -> dict[str, Any]:
    """The stay details over the guest's messages (split by MSG_SEP), read one message at a time: a newer check-in
    date replaces an older one ('dec 12' then later 'today' = today, live chat 2026-11-19), a lone 'till 15' / 'check
    out : 15 dec' / '2 nights' completes the dates already given, and the latest number of persons wins."""
    out: dict[str, Any] = {}
    for seg in text.split(MSG_SEP):
        got = _parse_one(seg, today)
        if "check_in" in got:
            lone_later = ("check_out" not in got and out.get("check_in") and "check_out" not in out
                          and got["check_in"] > out["check_in"])
            if (got.get("out_only") or lone_later) and out.get("check_in") and got["check_in"] > out["check_in"]:
                out["check_out"] = got["check_in"]  # '29th Nov' then '30th Nov' = 29 to 30 Nov
            else:
                out["check_in"] = got["check_in"]
                out.pop("check_out", None)
                if "check_out" in got:
                    out["check_out"] = got["check_out"]
        elif got.get("nights") and out.get("check_in"):
            out["check_out"] = out["check_in"] + timedelta(days=got["nights"])
        elif got.get("until_day") and out.get("check_in"):  # 'dec 12' ... later '3 persons, till 15'
            ci, day = out["check_in"], got["until_day"]
            month, year = (ci.month, ci.year) if day > ci.day else ((ci.month % 12) + 1, ci.year + (ci.month == 12))
            try:
                out["check_out"] = date(year, month, day)
            except ValueError:
                pass
        if "persons" in got:
            out["persons"] = got["persons"]
        if "children" in got:
            out["children"] = got["children"]
        if "rooms" in got:
            out["rooms"] = got["rooms"]
            out.pop("more_rooms", None)
        if "more_rooms" in got:  # 'one more room': added on top later (_stay_known), the latest message wins
            out["more_rooms"] = got["more_rooms"]
            out.pop("rooms", None)
            out.pop("less_rooms", None)
        if "less_rooms" in got:
            out["less_rooms"] = got["less_rooms"]
            out.pop("rooms", None)
            out.pop("more_rooms", None)
    return out


_KIDS = re.compile(r"\b(\d{1,2}|a|one|oru|ek|two|randu|rendu|do)\s*(?:small\s+)?(?:kids?|child(?:ren)?|kutti\w*|"
                   r"kuzhandhai\w*|bachch?[ae]|bachch?on|bab(?:y|ies))\b", re.IGNORECASE)


_ROOMS_ASKED = re.compile(r"(?<!to )(?<!till )(?<!-)(?<!- )\b(\d{1,2}|one|two|three|four|five|six|oru|randu|rendu|moonu|moonnu|naalu|anju|"
                          r"ek|do|teen|char|paanch)[ \t]*"
                          # '2 cottage rooms venam', '2 deluxe rooms', 'randu cottage venam' (owner, 2026-11-22)
                          r"(?:(?:cottage|tiny|kerala|deluxe|standard|basic|beach|view|family|ac|non-ac|double|"
                          r"premium|super)[ \t]+){0,3}(?:rooms?|cottages?|muri\w*|kamre|kamra|araigal)\b|"
                          r"\b(?:number of |no of )?rooms?[ \t]*(?:[:=,-]|is)?[ \t]*(\d{1,2})\b(?![ \t]*(?:persons?|people|"
                          r"nights?|days?|pax)\b)", re.IGNORECASE)  # 'rooms 2' from a voice note (no ':')
_ROOM_WORD_NUM = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "oru": 1, "randu": 2, "rendu": 2,
                  "moonu": 3, "moonnu": 3, "naalu": 4, "anju": 5, "ek": 1, "do": 2, "teen": 3, "char": 4, "paanch": 5}


_MORE_ROOMS = re.compile(
    r"\b(\d{1,2}|one|oru|two|randu|rendu)?[ \t]*(?:more|extra|additional|koode|kude|koodi|kudi)[ \t]*rooms?\b|"
    r"\b(\d{1,2}|one|oru|two|randu|rendu)?[ \t]*rooms?[ \t]*(?:koode|kude|koodi|kudi|more|extra|additional|also|too)\b|"
    r"\b(?:another|add|plus)(?:[ \t]+(?:a|one|oru|(\d{1,2})|(two|randu|rendu)))?[ \t]+(?:more[ \t]+)?rooms?\b",
    re.IGNORECASE)  # 'add 1 room also', 'add 2 rooms', '1 room also' (live chat 2026-11-21)
_MORE_WORD = {"one": 1, "oru": 1, "two": 2, "randu": 2, "rendu": 2}


# the code's rooms sentences in every language ('For 5 persons: 2 room(s) + 1 extra bed(s) ...')
ROOMS_LINE_RX = [re.compile("^" + re.sub(r"\\\{\w+\\\}", ".+?", re.escape(v)) + "$", re.DOTALL)
                 for k in ("rooms_plain", "rooms_beds", "rooms_note") for v in _STRINGS[k].values()]


# 'same date', 'same dates', 'athe date', 'wahi date': the dates of the stay already booked / talked about
SAME_DATES = re.compile(r"\b(?:same|athe|ade|wahi|vahi|adhe)\s+(?:dates?|days?|dateil|date-il|tharikh)\b|"
                        r"അതേ\s*(?:ഡേറ്റ്|തീയതി)", re.IGNORECASE)


_LESS_ROOMS = re.compile(
    r"\b(?:remove|reduce|less|minus|cancel|drop|kurakk\w*|kuraykk\w*|kuracch\w*)\b[^.?!\n]{0,15}?"
    r"\b(\d{1,2}|one|oru|two|randu)?[ \t]*rooms?\b|"
    r"\b(\d{1,2}|one|oru|two|randu)[ \t]*rooms?[ \t]*(?:less|kuravu|kurachu|venda|vendaa|mathi\s+alla)\b",
    re.IGNORECASE)


# new dates for the SAME enquiry ('change to jan 2', 'make it jan 2 to 5', 'jan 2 aakkam') vs a second stay
DATE_CHANGE = re.compile(r"\b(?:change\w*|make it|instead|shift|aakk\w*|maatt\w*|badal\w*|pannunga)\b|ആക്ക",
                         re.IGNORECASE)
ANOTHER_STAY = re.compile(r"\b(?:another|new|separate|second|other|vere|veroru|innoru|dusra)\s+(?:stay|booking|trip|"
                          r"enquiry|visit)\b", re.IGNORECASE)


def _less_rooms_in(text: str) -> int:
    """'remove one room' / '1 room less' / '1 room venda' -> rooms taken away (owner, 2026-11-21)."""
    m = _LESS_ROOMS.search(text)
    if not m:
        return 0
    n = (m.group(1) or m.group(2) or "1").lower()
    return int(n) if n.isdigit() else _MORE_WORD.get(n, 1)


def _more_rooms_in(text: str) -> int:
    """'one more room' / '1 room koode venam' / 'add a room' -> 1 extra room on top of those already quoted."""
    m = _MORE_ROOMS.search(text)
    if not m:
        return 0
    n = (m.group(1) or m.group(2) or m.group(3) or m.group(4) or "1").lower()
    return int(n) if n.isdigit() else _MORE_WORD.get(n, 1)


def _rooms_in(text: str) -> int:
    found = [m.group(1) or m.group(2) for m in _ROOMS_ASKED.finditer(text)]
    if not found:
        return 0
    n = found[-1].lower()
    return int(n) if n.isdigit() else _ROOM_WORD_NUM.get(n, 0)


def _kids_in(text: str) -> int:
    m = _KIDS.search(text)
    if not m:
        return 0
    n = m.group(1).lower()
    return int(n) if n.isdigit() else _WORD_NUM.get(n, 1)


def _nights_in(text: str) -> int:
    if m := _NIGHTS.search(text):
        return int(m.group(1))
    if m := _WORD_NIGHTS.search(text):
        return 1 if m.group(2) else _WORD_NUM[m.group(1).lower()]
    return 0


def _parse_one(text: str, today: date) -> dict[str, Any]:
    """Check-in, check-out and persons from what the guest typed, in any order or spread over messages:
    'Check in :dec 12 / Check out :dec 15 / Number of persons :3', '12 dec to 14 dec, 2 people', 'dec 12 ... 3 persons,
    till 15', '25/12 2 nights 4 pax', 'dec 12 se 15 tak 3 log', '12 dec la irundhu 15 varai 3 per', and the same in
    Malayalam / Tamil / Hindi script. Only what is clearly there is returned (no guessing)."""
    text = _latin_stay(text)
    found: list[tuple[int, date]] = []
    for rx, days in ((_DAY_AFTER, 2), (_TOMORROW, 1), (_TODAY, 0)):  # 'today', 'naale', 'aaj', 'day after tomorrow'
        for m in rx.finditer(text):
            found.append((m.start(), today + timedelta(days=days)))
        text = rx.sub(lambda m: " " * len(m.group(0)), text)
    for m in _DAY_MON.finditer(text):
        if d := _upcoming(int(m.group(1)), _MONTHS[m.group(2).lower()[:3]], today):
            found.append((m.start(), d))
    for m in _MON_DAY.finditer(text):
        if d := _upcoming(int(m.group(2)), _MONTHS[m.group(1).lower()[:3]], today):
            found.append((m.start(), d))
    for m in _NUMERIC.finditer(text):
        year = int(m.group(3)) if m.group(3) else None
        if year and year < 100:
            year += 2000
        if 1 <= int(m.group(2)) <= 12 and (d := _upcoming(int(m.group(1)), int(m.group(2)), today, year)):
            found.append((m.start(), d))
    found = sorted({pos: d for pos, d in found}.items())  # one date per place in the text
    dates: list[date] = []
    for _, d in found:
        if not dates or d != dates[-1]:
            dates.append(d)
    out: dict[str, Any] = {}
    if dates:
        out["check_in"] = dates[0]
        later = [d for d in dates[1:] if d > dates[0]]
        if later:
            out["check_out"] = later[0]
        elif m := next((x for rx in (_DAY_ONLY, _UNTIL) for x in rx.finditer(text[found[0][0]:])
                        if int(x.group(1)) != dates[0].day), None):  # 'dec 12 ... till 15', '22 vare', '14 nu pokum'
            ci, day = dates[0], int(m.group(1))
            month, year = (ci.month, ci.year) if day > ci.day else ((ci.month % 12) + 1, ci.year + (ci.month == 12))
            try:
                out["check_out"] = date(year, month, day)
            except ValueError:
                pass
        elif nights := _nights_in(text):
            out["check_out"] = dates[0] + timedelta(days=nights)
        if len(dates) == 1 and "check_out" not in out and _OUT_CUE.search(text[:found[0][0]]):
            out["out_only"] = True  # 'check out : 15 dec' / 'till dec 15' on its own completes earlier dates
    elif nights := _nights_in(text):
        out["nights"] = nights
    elif m := next((x for rx in (_DAY_ONLY, _UNTIL) for x in rx.finditer(text)), None):
        out["until_day"] = int(m.group(1))
    persons = [int(next(g for g in m.groups() if g)) for m in _PERSONS.finditer(text)]
    if not persons and (bare := re.fullmatch(r"\s*(\d{1,2})\s*[.!]?\s*", text)):
        persons = [int(bare.group(1))]  # '3' on its own, answering 'Number of persons :'
    if persons and 0 < persons[-1] <= 500:  # no 40 cap (owner, 2026-11-20); 500 only keeps a year out
        out["persons"] = persons[-1]  # the latest number they gave
    if kids := _kids_in(text):
        out["children"] = kids  # 'pinne oru kuttiyum' / '2 kids': children on top of the persons
    if less := _less_rooms_in(text):
        out["less_rooms"] = less  # rooms taken AWAY from the ones quoted / held (owner, 2026-11-21)
    elif more := _more_rooms_in(text):
        out["more_rooms"] = more  # rooms ADDED to the ones already quoted (owner, 2026-11-21: 'add rooms')
    elif 0 < (rooms := _rooms_in(text)) <= 200:
        out["rooms"] = rooms  # '4 rooms': the group wants that many rooms (owner, 2026-11-19)
    return out


_MOBILE = re.compile(r"(?<![\d+])(?:\+?91[\s-]?)?[6-9]\d{4}[\s-]?\d{5}(?!\d)")  # a whole number, never 10 digits of 15
_SAME_NO = re.compile(r"\b(?:same|this|my whatsapp|whatsapp|ithe|ee|yahi|yehi|isi|wahi|idhe|idhey|indha|intha|ithu|idhu)"
                      r"\s*(?:number|no\b\.?|num|nbr|nmbr)|^\s*same\s*[.!]*\s*$|"
                      r"(?:ഇതേ|ഈ|ഇത്|இதே|இந்த|இது|यही|इसी|यह|ये)\s*(?:നമ്പർ|നമ്പര്|நம்பர்|எண்|नंबर|नम्बर)",
                      re.IGNORECASE)
_NOT_NAME = {"my", "name", "is", "number", "mobile", "phone", "no", "and", "same", "this", "sir", "its", "it", "i",
             "am", "here", "ok", "okay", "yes", "the", "whatsapp", "num", "nbr", "contact", "please", "pls", "use",
             "ente", "peru", "per", "aanu", "same.", "mera", "naam", "hai", "number.", "fine", "good", "take", "call",
             "on", "to", "me", "at", "for", "is.", "thanks", "thank", "you", "mathi", "ithe", "ee", "aa",
             # Tamil / Hindi / Malayalam fillers in English letters (2026-11-18)
             "en", "enoda", "ennoda", "peyar", "per", "thaan", "dhaan", "than", "idhu", "ithu", "idhe", "intha", "indha",
             "yahi", "yehi", "isi", "wahi", "ye", "yeh", "ka", "ki", "ke", "hain", "hu", "hoon", "nte", "ende", "enda",
             "naan", "nan", "sir.", "ji", "saar", "number,", "mobile:", "name:"}


_NATIVE_NOT_NAME = {"ഇതേ", "ഈ", "നമ്പർ", "നമ്പര്", "ആണ്", "എന്റെ", "പേര്", "இதே", "இந்த", "நம்பர்", "என்", "பெயர்",
                    "யही", "यही", "इसी", "नंबर", "नम्बर", "है", "मेरा", "नाम", "ये", "यह"}


def contact_in(text: str) -> tuple[str, str] | None:
    """(name or '', mobile or 'same') when the guest's message gives a mobile number or says 'same number' / 'this
    number'. None otherwise. The name is what is left once the number and filler words are taken out (1-4 words)."""
    phone = _MOBILE.search(text)
    same = None if phone else _SAME_NO.search(text)
    if not phone and not (same and len(text.split()) <= 8):
        return None
    mobile = (try_normalize_phone(phone.group(0)) or "") if phone else "same"
    if not mobile:
        return None
    rest = (text[:phone.start()] + " " + text[phone.end():]) if phone else _SAME_NO.sub(" ", text)
    # names in English letters only (owner rule, 2026-11-18): a Malayalam word like "എന്ത്" ("what") was once saved
    # as a guest's name. Without a Latin name the WhatsApp profile name is used, or the bot asks for it.
    words = [w for w in re.findall(r"[A-Za-z][A-Za-z.']*", rest) if w.lower().strip(".,") not in _NOT_NAME]
    name = " ".join(words) if 1 <= len(words) <= 4 else ""
    return name, mobile


_MONEY = re.compile(r"₹|\brs\.?\s?\d|\brupees?\b|\binr\b|\badvance\b|\d{1,3},\d{3}|\bbalance\b|\bbakki\b|"
                    r"\bbaaki\b|\bpending\b|\bmeedhi\b", re.IGNORECASE)  # balance talk only when asked (2026-11-19)
_PRICE_ASK = re.compile(r"price|rate|cost|how much|charge|amount|advance|total|tariff|ethra|ethrayaa|evlo|kitna|"
                        r"kitne|kimat|keemat|daam|kiraya|vila|vilai|kaasu|rupee|₹|budget|cheap|discount|offer|reduce|pay|"
                        r"എത്ര|വില|റേറ്റ്|ചാർജ്|எவ்வளவு|விலை|கட்டணம்|ரேட்|कितना|कितने|दाम|किराया|कीमत|रेट|पैसे",
                        re.IGNORECASE)


def no_unasked_prices(reply: str, guest_text: str) -> str:
    """The client never wants prices said unless the guest asked (2026-11-18): sentences with an amount, total, advance or
    balance are dropped when the guest's message is not about money. The price reaches them in the glimpse and the
    enquiry summary."""
    if _PRICE_ASK.search(guest_text) or not _MONEY.search(reply):
        return reply
    keep = [s for s in re.split(r"(?<=[.?!])\s+|\n+", reply) if s.strip() and not _MONEY.search(s)]
    return " ".join(keep).strip()


# The AI asking for the guest's name and / or mobile number in its own words, any language in English letters
# (both name AND mobile: a name-only question, once the number is saved, is new and stays)
CONTACT_ASK = re.compile(r"\b(?:name|naam|peru|peyar|perum)(?:-?um)?\s*(?:,|and|aur|&|-?um)?\s*(?:mobile|phone|contact)\b|"
                         r"\b(?:mobile|phone)(?:\s*number)?(?:-?um)?\s*(?:,|and|aur|&|-?um)\s*(?:name|naam|peru|peyar)\b",
                         re.IGNORECASE)
# "sir" in any form the AI writes it: sir, sir ji, saar (Tanglish), madam, ma'am (said at most once per conversation)
LOG_LINE = re.compile(r"\[(?:invoice|photos? sent|photo|image|template|restart|no reply)[^\]]*\]"
                      r"(?:\s*\S+\.(?:pdf|jpe?g|png|webp))?", re.IGNORECASE)
PAYMENT_ASK = re.compile(r"\b(?:pay\w*|paid|link|advance|refund|razorpay|upi|gpay|transaction|amount|balance)\b",
                         re.IGNORECASE)
STALE_PAY = re.compile(r"\b(?:payment|pay|paid|link|advance)\b|confirm aak", re.IGNORECASE)
FORM_LABELS = re.compile(r"check[\s-]?in\s*:|check[\s-]?out\s*:|(?:number of )?persons\s*:|(?:number of )?rooms\s*:",
                         re.IGNORECASE)
QUESTION_WORD = re.compile(r"\b(?:what|how|when|where|which|why|who|is|are|do|does|can|price|rate|cost|ethra|evlo|"
                           r"kitna|entha|enthu|enna|yenna|kya|evide|enga|kahan)\b", re.IGNORECASE)
NUDGE = re.compile(r"\b(?:respond|reply|answer|anyone|any update|update|waiting|there\?|marupadi|parayu|"
                   r"bolo|sollunga)\b", re.IGNORECASE)
# words that are only booking details, greetings or fillers: a message made of these is not a question to answer
_DETAIL_WORDS = set("""
for to from till until the a an in on at of and or with is it are am i we us our my me you your this that these those
there here be do does did can could would will shall pls please plz ok okay yes no sir madam bro hi hello hai hey
room rooms bed beds bedroom booking book booked need want wanted needed looking available availability free vacancy
vacant check checkin checkout out date dates day days night nights stay persons person people ppl pax per adults adult
kids kid child children members guests guest total only also just any some one two three four five six seven eight
nine ten jan january feb february mar march apr april may jun june jul july aug august sep sept september oct october
nov november dec december today tomorrow tonight weekend week next this coming th st nd rd photos photo pics pictures
images image show send see please share details resort resorts place location same number mobile name
undo und unde undoo ano aano alle alley allo illa ille illey venam veno venda kittumo kittum aanu aan aayirikkum
venum irukka iruka kidaikkuma chahiye hai hain kya milega mil sakta sakti
nale naale innu inn kal aaj naalai mattannal parso varum pokum vare muthal tak ninnu
""".split())
# a question: '?', an English question word that STARTS the message ('ok will book later' is not one), or a
# Malayalam / Tamil / Hindi question word anywhere ('pool undo', 'parking evide')
_QUESTION_SIGNAL = re.compile(r"\?|^\W*(?:what|how|when|where|which|why|who|is|are|do|does|can|could|will|any|whats)\b|"
                              r"\b(?:ethra|entha|enthu|evide|engane|enna|evlo|enga|eppadi|kya|kitna|kahan|kaise|kab|"
                              r"undo|ano|aano|ille|alle)\b", re.IGNORECASE)


def other_question(text: str, extra_detail_words: set[str] = frozenset()) -> bool:
    """True when the guest asks something besides booking details ('Do you have rooms with a sea view?', '25-27 Dec,
    3 people, is parking free?', 'pool undo?'). Pure details ('25 Dec 3 ppl', 'Alleppey.?', 'rooms undo?') are False.
    Such a question is answered by the AI like an agent, and the fixed booking step still goes (owner, 2026-11-20)."""
    if not _QUESTION_SIGNAL.search(text or ""):
        return False
    words = [w for w in re.findall(r"[a-z]+", (text or "").lower()) if len(w) >= 3]
    return any(w not in _DETAIL_WORDS and w not in extra_detail_words for w in words)


# a sentence about whether rooms are free or about the guest's dates: only the fixed steps say these, never the
# AI beside them (it once said 'Rooms are available' / 'fits 3 people' against the code's own check)
ROOM_FACT = re.compile(r"\brooms?\b[^.?!]{0,30}\b(?:available|free|booked|full|vacant)\b|"
                       r"\b(?:available|free|booked|vacant)\b[^.?!]{0,20}\brooms?\b|\bfully booked\b|\bsold out\b|"
                       r"\bavailability\b|\bfits?\b[^.?!]{0,20}\b(?:people|persons|guests|pax|\d)|"
                       r"check[\s-]?in date|check[\s-]?out date|available alla|free alla|\brooms? und\b",
                       re.IGNORECASE)
YES_REPLY = re.compile(r"^\W*(?:yes|yeah|yep|ya|yaa|s|sure|ok\w*|go ahead|proceed|book|book it|do it|fine|done|"
                       r"sheri|seri|sari|athe|aam|aanu|mathi|cheyyu|cheytho|haan|ha|haa|theek|thik|aamaa|ama|seri da)"
                       r"(?:\s+(?:please|pls|sir|bro|cheyyu|cheytho|karo|pannunga|go ahead|book))*\W*$", re.IGNORECASE)
SOLD_OUT_CLAIM = re.compile(r"fully booked|sold out|no rooms?\b|not available|full(?:y)? (?:on|for)|all (?:the )?rooms "
                            r"(?:are )?(?:booked|full)|rooms? (?:are |is )?full|booked out|onnum (?:illa|ella)|"
                            r"available alla|free alla|illa(?:llo)? rooms?", re.IGNORECASE)
SEE_ASK = re.compile(r"\b(?:see|show|view|look at|kaanikk\w*|kaanan|kanikk\w*|dikha\w*|paakka|kaattu)\b",
                     re.IGNORECASE)
PHOTO_ASK = re.compile(r"\b(?:photos?|pics?|pictures?|images?|fotos?|padam|padangal|video)\b|ഫോട്ടോ|படம்|फोटो",
                       re.IGNORECASE)
# "other room photos" / "vere room" / "dusra room": the next free room type by the owner's priority
OTHER_ROOM = re.compile(r"\b(?:other|another|different|next|vere|veroru|matte|mattu|innoru|vera|dusra|doosra|aur)\b",
                        re.IGNORECASE)
# 'any other rooms?' / 'multiple rooms' / 'rooms that are available': a request to SEE more rooms
ROOM_OPTIONS_ASK = re.compile(r"\b(?:other|another|different|more|multiple|vere|veroru|matte|innoru|vera|dusra|aur)\s+"
                              r"(?:rooms?|room types?|options?|cottages?|types?)\b", re.IGNORECASE)
ALL_ROOMS = re.compile(r"\b(?:all|multiple|every|available|ellam|ella|ellaa|saare|sab|sabhi)\b", re.IGNORECASE)
GENERAL_PHOTO_ASK = re.compile(r"\b(?:resort|property|outside|outdoor|beach|place|area|surroundings?|campus|building|"
                               r"common|sea|view of the)\b", re.IGNORECASE)
ROOM_WORD = re.compile(r"\b(?:rooms?|cottages?|bed\s*rooms?|suites?)\b", re.IGNORECASE)
ROOM_NAME_FILLER = {"room", "rooms", "view", "the", "with", "and", "type", "suite", "cottage", "villa"}
WISH_CORE = {"room", "rooms", "resort", "resorts", "booking", "book", "stay", "availability", "available", "cottage",
             "kamra", "accommodation"}
WISH_WORDS = WISH_CORE | {"a", "an", "any", "is", "are", "there", "i", "we", "need", "want", "wanted", "looking", "for",
                          "please", "pls", "plz", "sir", "hi", "hello", "venam", "venum", "veno", "undo", "und", "unda",
                          "kittumo", "chahiye", "hai", "kya", "milega", "vendum", "irukka", "iruka", "do", "you", "have",
                          "to", "the", "some", "can", "get", "me", "my", "ok", "now", "today", "one", "ondo", "aano",
                          "cheyyan", "cheyyanam", "enquiry", "details", "info", "information", "in", "at", "on",
                          "near", "il", "le", "mein", "la", "le", "from", "needed", "required", "require", "wants",
                          "urgent", "urgently", "free", "vacant", "vacancy", "kittan", "kittanam", "booking", "reserve",
                          "reservation", "inquiry", "chahte", "chahiye", "ah", "aa", "thanks", "thank", "chey", "cheyy",
                          "cheyyu", "cheyyo", "cheyyamo", "cheyyanam", "pannu", "pannanum", "pannunga", "karo", "karna",
                          "karni", "kar", "do", "make", "it", "ready"}
# the guest PICKS a room ('I only need cottage', 'ok book cottage', 'can I get room 103', 'I want other room'):
# switched in code with the dates already known, never the form again (owner, 2026-11-22)
# 'is there 2 rooms in kerala cottage' / '2 cottage rooms undo': a count with a named room is a pick of that room
# (live chat 2026-11-22: the count was read, the cottage was not, and Standard was booked)
_ROOMS_OF = r"\b(?:\d+|one|two|three|four|five|oru|randu|moonu|naalu|anju)\s*(?:rooms?|cottages?)\b"
CHOICE_ASK = re.compile(r"\b(?:want|wanted|need|book|booking|take|prefer|like|liked|only|just|choose|select|go with|"
                        r"go for|give me|instead|change|switch|shift|that one|this one|venam|venum|veno|mathi|madhi|"
                        r"mathy|matram|mathram|maathram|edukk\w*|kittumo|chahiye|lena|vendum|podhum|ok|okay|fine|"
                        r"confirm)\b|\bcan (?:i|we) (?:get|have|take|book)\b|" + _ROOMS_OF, re.IGNORECASE)
STRONG_CHOICE = re.compile(r"\b(?:book|booking|take|prefer|only|instead|mathi|madhi|matram|mathram|choose|go with|"
                           r"go for|switch|change|shift)\b|\bneed (?:a |the |that )?(?:rooms?|cottage)\b|"
                           r"\bcan (?:i|we) (?:get|have|take|book)\b|" + _ROOMS_OF, re.IGNORECASE)
NOT_WANT = re.compile(r"\b(?:don'?t|dont|do not|no need|not|remove|without|except|venda|vendaa|vendathilla|vendam|"
                      r"nahi|nahin|illathe)\b", re.IGNORECASE)
# a swipe-reply to a room photo that picks it or asks if it is free ('this one', 'ithu venam', 'is this free?');
# 'nice view' / 'beautiful' only gets the AI's answer (owner, 2026-11-22)
PHOTO_PICK = re.compile(r"\b(?:this|ithu|ith|idhu|ee|yeh|ye wala|that one|athu|adhu|free|available|availability|"
                        r"undo|kittumo|kittuo|book|booking)\b", re.IGNORECASE)
# room numbers are not the guest's concern (owner, 2026-11-22): told only when the guest named one or asked for them
ROOM_NUMBERS_SAID = re.compile(r"\broom\s*(?:numbers?|nos?)\b|\brooms?\s*#?\s*\d{3}\b|\b\d{3}\s*(?:,|/|&|and)\s*\d{3}\b",
                               re.IGNORECASE)
ROOM_NUMBER_ASKED = re.compile(r"\b\d{3}\b|\broom\s*(?:numbers?|nos?)\b", re.IGNORECASE)
ROOM_NUMBER = re.compile(r"\b(?:room|rm|no|number)\.?\s*#?\s*(\d{2,4})\b|#\s*(\d{2,4})\b", re.IGNORECASE)
# words that do not tell room types apart ('a room in Kerala' is not the Tiny Kerala Cottage)
CHOICE_FILLER = {"room", "rooms", "view", "the", "with", "and", "type", "kerala", "india", "floor", "first", "ground",
                 "second", "bedroom", "bed", "beds", "beach", "sea", "non", "at"}
RESTART = re.compile(r"^\s*(?:restart|reset|start again|start over|new chat)\s*[.!]*\s*$", re.IGNORECASE)
RESTART_MARK = "[restart]"  # stored in the chat; the AI's view of the conversation begins after it
SIR = re.compile(r"\b(?:sir|saar|sar|madam|ma'?am)\b(?:\s*ji\b)?", re.IGNORECASE)
MAX_ROOM_PHOTOS = None  # no limit (client, 2026-11-19): every photo uploaded for the room in the dashboard goes

# The owner wants no essays (2026-11-17): more words than this, outside the room/price lines, gets one shorter rewrite.
_LIST_LINE = re.compile(r"₹|\bRs\.?\s?\d|https?://|^\s*(?:[-*•]|\d+[.)])\s", re.IGNORECASE)


# Other names guests use for a town (small spelling mistakes are caught by close_word).
PLACE_ALIASES = {"alleppey": ("alappuzha", "alapuzha", "alappey", "alappi", "alappee", "aleppee"),
                 "alappuzha": ("alleppey",)}
# Town names typed in Malayalam / Tamil / Hindi script (or from a voice note) -> the English-letter name the dashboard has
NATIVE_PLACES = [(re.compile(p), name) for p, name in (
    ("ആലപ്പുഴ|ആലപ്പി|ആലപ്പീ|അലപ്പുഴ|അലപ്പി|അലപ്പീ|ஆலப்புழா|ஆலப்புழை|ஆலப்பி|अलेप्पी|अलप्पुझा|अलाप्पुझा", "alleppey"),
    ("കാന്തല്ലൂർ|കാന്തലൂർ|காந்தலூர்|காந்தல்லூர்|कांतलूर|कान्तलूर", "kanthalloor"),
    ("മൂന്നാർ|மூணார்|மூன்னார்|मुन्नार", "munnar"), ("പൊഴിയോരം|பொழியோரம்|पोझियोरम", "pozhiyoram"))]


def english_letters_only(text: str, lang: str = "en") -> str:
    """The last guard before ANY text goes to a guest (owner, 2026-11-18: "strictly no text in regional-language
    alphabets, only English letters"). Lines with Malayalam / Tamil / Devanagari letters are left out (a summary line
    'Guest - अनु' included); if nothing is left, a short ask to type it again. Native script may only ever be heard
    in a voice reply's audio, never read in text."""
    if not INDIC.search(text):
        return text
    kept = [line for line in text.split("\n") if not INDIC.search(line)]
    if any(line.strip() for line in kept):
        return "\n".join(kept).strip("\n")
    return t("say_again", lang)


def latin_places(text: str) -> str:
    for rx, name in NATIVE_PLACES:
        text = rx.sub(f" {name} ", text)
    return text


def close_word(said: str, name: str) -> bool:
    """'allepy' ~ 'alleppey', 'kanthaloor' ~ 'kanthalloor', 'pozhiyorum' ~ 'pozhiyoram': the same word give or take a
    letter or two (client, 2026-11-17). Short words must match exactly, so 'room' is never taken for a place."""
    if said == name:
        return True
    if min(len(said), len(name)) < 5 or said[0] != name[0]:
        return False
    if difflib.SequenceMatcher(None, said, name).ratio() >= 0.75:
        return True
    # doubled letters and y/i/ee endings from speech-to-text: 'aalappee' ~ 'alleppey' (live chat 2026-11-21)
    a, b = (re.sub(r"(.)\1+", r"\1", re.sub(r"(?:ee|ey|y|i)$", "i", w)) for w in (said, name))
    return len(a) >= 4 and difflib.SequenceMatcher(None, a, b).ratio() >= 0.8


def chatty_words(reply: str) -> int:
    """Words in the talk part of a reply; room lines with a price, list items and links don't count."""
    return sum(len(line.split()) for line in reply.splitlines() if not _LIST_LINE.search(line))


# Words a sentence cannot end on: the guest is still typing (a second message is about to arrive)
DANGLING = frozenset("""oru and or the a an to for of in on at with my i we is are was will can about from if but because so
    njan njangal enikku ente ningal nammal ee aa pinne allenkil athinu ennal avide evide ithinte athinte""".split())

NO_REPLY = "[no reply]"
LANG_NAMES = {"ml": "Malayalam", "ta": "Tamil", "hi": "Hindi"}
SCRIPTS = {"ml": re.compile(r"[\u0d00-\u0d7f]"), "ta": re.compile(r"[\u0b80-\u0bff]"), "hi": re.compile(r"[\u0900-\u097f]")}
# "there is no offer" / "I cannot reduce" in any of our languages: wrong when the guest only asked about offers
NO_OFFER = re.compile(
    r"\bno offers?\b|\bno discounts?\b|offers?\s+(?:\w+\s+)?(?:ഒന്നും|எதுவும்|kuch|nahi|nahin|illa|illai|ilya|ilai)\b|"
    r"(?:ഓഫർ|ஆஃபர்|ऑफर|offers?)\s*(?:ഇപ്പോൾ\s*)?(?:ഒന്നും\s*)?(?:ഇല്ല|இல்ல|இல்லை|नहीं|illa|illai|nahi|ilya)|"
    r"(?:cannot|can't|cant|not able to|unable to) (?:reduce|decrease|lower)|കുറയ്ക്കാൻ (?:പറ്റില്ല|കഴിയില്ല)|"
    r"குறைக்க முடியாது|कम नहीं (?:कर सकत[ेा]|हो सकता)|kurakkan pattilla|kuraikka mudiyaadhu|discount nahi", re.IGNORECASE)
# Hindi / Malayalam typed in English letters: a Hinglish guest must not be answered in Manglish
HINGLISH_WORDS = re.compile(r"\b(?:milega|milegi|kya|kitne|kitna|raat|chahiye|karna|kar do|theek|nahi|hai|hain|aap|mujhe|hum|log)\b", re.IGNORECASE)
MANGLISH_WORDS = re.compile(r"\b(?:und|undo|unda|aanu|aano|ethra|venam|venda|cheyyanam|cheyyam|cheyyamo|ippol|aakum|"
                            r"evide|ningal\w*|parayamo|parayu|mathi|enikk\w*|innu|naale|mattannal|tharo|tharamo|"
                            r"vilikk\w*|kittumo|pattumo|kaanan|varunnund|varunnilla|koode|ivide|aduthu|enthokke|"
                            # everyday Malayalam typed in English letters (2026-11-20: 'Alleppey il 10 nu 2 divasam'
                            # must stay Manglish now that unknown words no longer count as Manglish)
                            r"il|nu|nn|inu|ilekk\w*|divasam\w*|dhivasam\w*|disam|rathri|raathri|perkku|aalukal|"
                            r"varum|varaam|pokum|vare|muthal|alla|alle|illa|ille|ano|aanenkil|aayirikkum|indavum|"
                            r"njan|njangal|njmal|nammal|ente|entha|enthu|engane|evideya|sheri|kollam|kure|kurachu|"
                            r"onnu|randu|moonnu|naalu|kayyil|kodukkam|kodukkanam|ayakkamo|ayakku|tharam|nokkam|"
                            r"poyi|vannu|kazhinju|ninnu|kittum|pinne|oru|ee|aa)\b",
                            re.IGNORECASE)
# Tamil typed in English letters (2026-11-18: "room irukka? evlo price?" was read as English and got English back)
TANGLISH_WORDS = re.compile(r"\b(?:irukka|irukku|iruku|irukkum|irukkuma|evlo|evvalavu|ethana|ethanai|venum|vendum|"
                            r"sollunga|sollu|naanga|nanga|enga|unga|ungal\w*|enakku|enaku|pannunga|pannanum|panna|"
                            r"kudunga|kodunga|romba|mudiyuma|aaguma|aagum|thanguvom|varuvom|varom|varanum|illai|"
                            r"inga|anga|eppadi|epdi|vanakkam|irundhu|irunthu|varai|varaikkum|pakkam|thaan|"
                            r"dhaan|kattanum|vaanga|ponga|aamaa|illainga|ethukku|yenna|enna|maathanum|maathunga|"
                            r"ku|inniki|innaiku|naalaikku|naalaiku|pesunga|venaam|mudiyaadhu|paakka|edam|"
                            r"pakkathula|varanga|varuvanga)\b",
                            re.IGNORECASE)
# A Malayalam word that slipped into a Tanglish / Hinglish reply and survived the rewrite -> the guest's own word
_STRAY_ML = {"tanglish": {"und": "irukku", "undo": "irukka", "aanu": "thaan", "venam": "venum", "ippol": "ippo",
                          "ethra": "evlo", "mathi": "podhum"},
             "hinglish": {"und": "hai", "undo": "hai kya", "aanu": "hai", "venam": "chahiye", "ippol": "abhi",
                          "ethra": "kitna", "mathi": "chalega"}}
LINK_ASK = re.compile(r"\blink\b|\bpay\w*|\bpaid\b|\badvance\b|razorpay|upi|gpay", re.IGNORECASE)
# 'The link opens a secure Razorpay page where you can use ...' -> 'You can use ...'
PAY_PAGE_TALK = re.compile(r"\b(?:the\s+)?(?:payment\s+)?link\s+(?:opens|takes you to|leads to)\s+(?:a\s+|the\s+)?"
                           r"(?:secure\s+)?(?:razorpay\s+)?(?:payment\s+)?page[,]?\s*(?:where\s+)?(?:you\s+can\s+(?:use|pay\s+"
                           r"(?:by|with|using))\s+)?", re.IGNORECASE)
LINK_REMINDER = re.compile(r"payment link (?:is )?(?:still )?(?:active|open|valid)|link is still|link ippozhum|"
                           r"if you would like to go ahead", re.IGNORECASE)
SORRY_FILLER = re.compile(r"\bi wish i could\b|\bi hear you\b|\bi understand (?:your|that)\b|\bunfortunately\b|"
                          r"\bi am sorry\b|\bsorry,? but\b", re.IGNORECASE)
PHONE_DIGITS = re.compile(r"\d{5}\s?\d{5}")
OWNER_WORD = re.compile(r"\bowner\b|\bspeak with\b|\bcall\b|\bvilikk\w*", re.IGNORECASE)
ASKS_REDUCE = re.compile(r"reduc|discount|lower|less|last price|final price|negotiat|bargain|best price|kurach|kurakk|kuraikk|"
                         r"kam kar|chhoot|குறை|കുറ|कम कर|छूट|ഡിസ്കൗണ്ട്|തള്ള", re.IGNORECASE)


def looks_unfinished(text: str) -> bool:
    """A message that stops mid-sentence ('njan udheshikkunath oru', 'we are planning to', 'for 3 people,'): the next
    message will finish it, so the bot gives the guest time before answering."""
    s = text.strip().lower()
    if not s or "?" in s or s.endswith(("!", "।")):
        return False
    if s.endswith(("...", "..", ",", ";", ":", "-", "…")):
        return True
    words = s.split()
    return words[-1].strip(".,") in DANGLING and not s.endswith(".")


OK_SPELLING = re.compile(r"o+k+(?:a+y*|e+y*|i+e*|y+)?|k+")


def is_ack(text: str) -> bool:
    """Only an acknowledgement: 'ok', 'okay thanks', 'sheri', '👍'. Nothing to answer."""
    s = text.strip().lower()
    if not s:
        return False
    if not re.search(r"\w", s):  # emoji or punctuation only
        return True
    words = [w.strip(".,!?;:()-'\"") for w in s.split() if re.search(r"\w", w)]  # not \w: it would cut Malayalam vowel signs
    # every spelling of ok counts: 'Oky', 'okyy', 'oki', 'okkk', 'okaay' (live chat 2026-11-21: 'Oky' got 'type that again')
    return 0 < len(words) <= 5 and all(w in ACK_WORDS or OK_SPELLING.fullmatch(w) for w in words)


def display_phone(e164: str) -> str:
    """+919497861924 -> 9497861924 (how Indians write it); other countries keep the +."""
    return e164[3:] if e164.startswith("+91") and len(e164) == 13 else e164


def _day(d: date) -> str:
    return f"{d.day} {d.strftime('%b')} {d.year}"


def _money(n: int) -> str:
    return f"₹{n:,}"


@dataclass
class _Turn:
    """What one guest message did: tools may queue the payment link or take over the chat."""

    phone: str
    st: dict[str, Any]
    pay_message: str = ""
    summary_message: str = ""
    handed_off: bool = False
    after: list[Any] = field(default_factory=list)  # fixed messages after the AI's line: text, or a list of photos
    resend: bool = False  # the guest asked for their payment link again
    silent: bool = False  # a fixed step message replaces the AI's own line this turn
    photos: list[str] = field(default_factory=list)  # uploaded photo references to send after the reply
    text: str = ""  # the guest's message this turn
    priced: bool = False  # a tool worked out a price this turn (the only source of a price the AI may state)
    none_found: bool = False  # check_availability found no room this turn (the only source of 'fully booked')
    room_switched: bool = False  # the guest picked a room type this turn (handled in code)


LANGUAGE_FIELD = {"type": "string", "enum": list(STYLES),
                  "description": "the language style of the guest's latest message (Manglish = Malayalam in English letters)"}

EARLY_FIELD = {"type": "boolean", "description": "true only if the guest asked for early check-in"}

TOOLS = [
    ToolSpec(
        "ask_location",
        "When the guest wants a resort, a booking or rooms and has not said which location, call this: it sends the "
        "owner's fixed location question. Never write a location question yourself. Your reply must then be exactly "
        "[no reply].",
        {"type": "object", "properties": {}}),
    ToolSpec(
        "send_photos",
        "Send the guest the owner's photos. ONLY when the guest's message itself asks for photos, pictures, pics or "
        "images; never for a town or resort name alone. Which photos go is decided automatically by the owner's "
        "rules; reply exactly [no reply].",
        {"type": "object", "properties": {
            "place": {"type": "string", "description": "the resort or town"},
            "room_type": {"type": "string", "description": "only if they asked to see one room type"}},
         "required": ["place"]}),
    ToolSpec(
        "ask_stay_details",
        "Send the guest the booking form (Check in / Check out / Number of persons) for the resort they chose. Use it "
        "once the resort is known and the guest has NOT yet given ALL of check-in, check-out and persons (also to ask "
        "for a missing part). If they already "
        "gave them, call check_availability instead. The form is sent automatically; reply exactly [no reply].",
        {"type": "object", "properties": {"place": {"type": "string", "description": "the resort the guest chose"}},
         "required": ["place"]}),
    ToolSpec(
        "check_availability",
        "Check which rooms are free for the guest's stay and what they cost. Call it as soon as you know the place, "
        "the check-in date, the number of nights (or check-out date) and the number of guests. Give the rooms "
        "count too when the guest has said it.",
        {"type": "object", "properties": {
            "place": {"type": "string", "description": "the place the guest wants, e.g. Alleppey or Munnar"},
            "check_in": {"type": "string", "description": "YYYY-MM-DD"},
            "check_out": {"type": "string", "description": "YYYY-MM-DD (or give nights instead)"},
            "nights": {"type": "integer"},
            "adults": {"type": "integer", "description": "total persons as the guest said them (never assume 1)"},
            "children": {"type": "integer", "description": "only when the guest mentioned children"},
            "rooms": {"type": "integer", "description": "rooms the guest asked for, if any"},
            "language": LANGUAGE_FIELD},
         "required": ["place", "check_in", "adults"]}),
    ToolSpec(
        "prepare_booking",
        "Lock the room type and figures when the guest asks for a particular room type, extra beds or early "
        "check-in. Does not book anything and sends nothing.",
        {"type": "object", "properties": {
            "room_type": {"type": "string", "description": "room type name as shown by check_availability"},
            "rooms": {"type": "integer"},
            "extra_beds": {"type": "integer", "description": "extra beds the guest asked for (default 0)"},
            "early_checkin": EARLY_FIELD,
            "language": LANGUAGE_FIELD},
         "required": ["room_type"]}),
    ToolSpec(
        "create_booking",
        "Create the booking as soon as rooms are available and the guest has given their name and mobile number. "
        "The enquiry summary and the payment link are sent automatically; your reply must then be exactly [no reply].",
        {"type": "object", "properties": {
            "guest_name": {"type": "string", "description": "the name the guest gave"},
            "mobile": {"type": "string", "description": "the mobile number the guest gave ('same' if they said this number)"},
            "rooms": {"type": "integer", "description": "only if the guest asked for a number of rooms"},
            "extra_beds": {"type": "integer", "description": "only if the guest asked for extra beds"},
            "early_checkin": EARLY_FIELD,
            "from_city": {"type": "string", "description": "the city they are coming from, only if they said it"},
            "language": LANGUAGE_FIELD},
         "required": ["guest_name", "mobile"]}),
    ToolSpec(
        "change_enquiry",
        "The guest changes the stay already checked, before OR after the payment link: picks a room type or a room "
        "number ('cottage venam', 'room 103', 'the other room'), removes a room type ('no standard room'), adds or "
        "drops rooms, or changes persons or dates. Pass ONLY what changed; everything else (dates, persons, name, "
        "mobile) is remembered. Rooms are re-checked and, when the payment link was already sent, a new summary and "
        "link go automatically. Never send the form or ask the dates again for a change. Reply exactly [no reply].",
        {"type": "object", "properties": {
            "room_type": {"type": "string", "description": "the room type they want, as named in the FACTS"},
            "room_number": {"type": "string", "description": "a room number they asked for, e.g. 103"},
            "remove_room_type": {"type": "string", "description": "a room type they do NOT want"},
            "other_room": {"type": "boolean", "description": "true when they want a different room type than offered"},
            "rooms": {"type": "integer", "description": "the new total number of rooms"},
            "add_rooms": {"type": "integer", "description": "rooms to add ('one more room' = 1)"},
            "remove_rooms": {"type": "integer", "description": "rooms to take away ('1 room venda' = 1)"},
            "adults": {"type": "integer", "description": "the new total number of persons"},
            "children": {"type": "integer"},
            "check_in": {"type": "string", "description": "new check-in, YYYY-MM-DD"},
            "check_out": {"type": "string", "description": "new check-out, YYYY-MM-DD"},
            "language": LANGUAGE_FIELD}}),
    ToolSpec(
        "send_payment_link",
        "Send the guest their payment link again (they lost it, it did not open, or they ask for it). Only works "
        "while their booking is still on hold. The link is sent automatically after your reply.",
        {"type": "object", "properties": {"language": LANGUAGE_FIELD}}),
    ToolSpec(
        "recall_old_chat",
        "Read what was said in this guest's EARLIER conversations (before today's chat). Use it only when the guest "
        "refers to something said or sent before ('as I told you', 'you said earlier', 'the link you sent', 'my last "
        "enquiry', 'that room we talked about'). Never for greetings, small talk or a fresh request.",
        {"type": "object", "properties": {}}),
    ToolSpec(
        "my_bookings",
        "The guest's own recent bookings with status, amounts and the owner's contact. Use when they ask about "
        "an existing booking or payment, or want to cancel (if_cancelled_today has the policy charge and refund).",
        {"type": "object", "properties": {}}),
    ToolSpec(
        "change_name",
        "The guest corrects their name ('my name is not Naseem, it is Shibil', 'name correction: Shibil') after the "
        "enquiry summary or the booking: the name is changed on their booking and the guest is told automatically. "
        "Never start the booking again for a name correction. Reply exactly [no reply].",
        {"type": "object", "properties": {
            "name": {"type": "string", "description": "the correct name, in English letters (e.g. Shibil)"}},
         "required": ["name"]}),
    ToolSpec(
        "talk_to_owner",
        "The owner gets a short summary and will contact the guest; the bot keeps answering. Use when the guest asks for "
        "the owner / a person / a call, complains, wants a refund or exception outside the policy, makes a price "
        "counter-offer, or wants to change a booking that is already PAID. Changes before payment (room type, rooms, "
        "persons, dates) are never for the owner: call change_enquiry. The guest is told automatically; reply "
        "exactly [no reply].",
        {"type": "object", "properties": {
            "reason": {"type": "string", "description": "one short line for the owner, e.g. 'wants to cancel RB-1D9E49, "
                                                        "asks full refund'"}},
         "required": ["reason"]}),
]


class ChatMixin:
    # ------------------------------------------------------------------ the prompt

    def _places(self) -> list[Property]:
        return self.repo.list_properties()

    @staticmethod
    def _place_label(p: Property) -> str:
        return p.place or p.name

    def _rooms_facts(self, p: Property) -> list[str]:
        lines = []
        for rt in self.repo.list_room_types(p.id):
            line = (f"  ROOM TYPE '{rt.name}': {rt.units} room(s), max {rt.max_occupancy} guests per room "
                    f"({rt.max_adults} adults, {rt.max_children} children)")
            if rt.room_list:
                line += f", room numbers {', '.join(rt.room_list)} (only to understand a guest who names one; never tell them)"
            extras = " ".join(x for x in (rt.description, rt.amenities) if x and "PLACEHOLDER" not in x)
            lines.append(line + "." + (f" {extras}" if extras else ""))
        return lines

    def _resort_names(self) -> list[str]:
        """Every resort by its full name, open or not ('Vibes Kanthalloor', 'Pozhiyoram Resort, Alleppey')."""
        names = []
        for p in self.repo.list_properties(active_only=False):
            town = (p.city or "").split(",")[0].strip()
            names.append(p.name if not town or town.lower() in p.name.lower() else f"{p.name}, {town}")
        return names

    def _towns_text(self) -> str:
        """'Alleppey or Kanthalloor', 'Alleppey, Kanthalloor or Munnar': every resort in the dashboard (a new one shows
        up by itself), for the owner's 'Sure, Kindly confirm the Location 📍' question."""
        towns = self._towns()
        return towns[0] if len(towns) == 1 else ", ".join(towns[:-1]) + " or " + towns[-1]

    def _towns(self) -> list[str]:
        """Where the resorts are, from the dashboard: 'Alleppey', 'Kanthalloor' (city's first part, or the name)."""
        towns: list[str] = []
        for p in self.repo.list_properties(active_only=False):
            town = (p.city or p.place or p.name).split(",")[0].strip()
            if town and town.lower() not in (x.lower() for x in towns):
                towns.append(town)
        return towns

    def _sent_photos(self, phone: str) -> set[str]:
        """Photos already sent in the current conversation (never send one twice)."""
        rows = self.repo.list_chat(phone, limit=120)
        return {m.text[len("[photo] "):] for m in rows[self._session_start(rows):]
                if m.direction == "out" and m.text.startswith("[photo] ")}

    def _general_photos(self, phone: str, prop: Property) -> list[str]:
        """The property's general photos (outside, beach, common areas) not yet sent: they go to EVERY guest right
        after the glimpse (client, 2026-11-17), in the owner's order, all of them."""
        sent = self._sent_photos(phone)
        return [r for r in prop.photo_urls if r not in sent][:MAX_GENERAL_PHOTOS]

    @staticmethod
    def _room_named(text: str, rooms: list[RoomType]) -> RoomType | None:
        """The room type the guest named ('deluxe photos', 'premium room', 'basic'), by its distinctive words."""
        words = set(re.findall(r"[a-z]{3,}", (text or "").lower()))
        best, score = None, 0
        for rt in rooms:
            names = set(re.findall(r"[a-z]{3,}", rt.name.lower())) - ROOM_NAME_FILLER
            hits = sum(1 for n in names if any(close_word(w, n) for w in words))
            if hits > score:  # ties keep the higher-priority one
                best, score = rt, hits
        return best

    def _photo_plan(self, phone: str, st: dict[str, Any], prop: Property, text: str,
                    wanted: str = "") -> tuple[list[tuple[RoomType | None, list[str]]], str, str]:
        """Which photos answer a photo request (owner, 2026-11-19), in code, never mixed:
        - a named room type: ALL its photos if it is free on the guest's dates, else 'room_not_free';
        - resort / beach / outside photos: the GENERAL photos only;
        - 'other room' / 'any other rooms': the next FREE room by priority not shown yet, else 'no_other_room';
        - 'all / available rooms': every free room not shown yet, one room after another;
        - plain 'photos': the offered room (top priority and free), else the next free room not shown yet.
        Returns ([(room or None for general, photos)], problem key, room name for the problem)."""
        rooms = self.repo.list_room_types(prop.id)  # the owner's priority order
        bk = st.get("bk") or {}
        dated = bk.get("property_id") == prop.id and bk.get("check_in") and bk.get("check_out")
        # every free room type, also when the guest picked one ('other room?' must still find the others)
        free = [rt for rt, _ in self._fits(prop, {**bk, "pref": None})] if dated else rooms
        sent = self._sent_photos(phone)

        def unsent(refs: list[str]) -> list[str]:
            return [r for r in dict.fromkeys(refs) if r not in sent][:MAX_ROOM_PHOTOS]

        named = self._room_named(wanted, rooms) or self._room_named(text, rooms)
        if named:
            if named not in free:
                return [], "room_not_free", named.name
            refs = unsent(self._room_refs(named, st))
            return ([(named, refs)] if refs else []), ("" if refs else "photos_sent"), ""
        if GENERAL_PHOTO_ASK.search(text or "") and not ROOM_WORD.search(text or ""):
            refs = unsent(prop.photo_urls)
            return ([(None, refs)], "", "") if refs else ([], "photos_sent", "")
        if not free:
            return [], "no_other_room", ""
        chosen_id = (bk.get("chosen") or {}).get("room_type_id") or bk.get("pref") or st.get("room_pref")
        current = next((rt for rt in free if rt.id == chosen_id), free[0])
        refs_of = {rt.id: self._room_refs(rt, st) for rt in free}
        others = [rt for rt in free if rt is not current and unsent(refs_of[rt.id])]
        # before any dates every room is shown (owner, 2026-11-21: photos are not restricted); 'not free on your
        # dates' is only said once there ARE dates
        none_left = "no_other_room" if dated else "photos_sent"
        if not dated and not others and not unsent(refs_of[current.id]) and (general := unsent(prop.photo_urls)):
            return [(None, general)], "", ""  # every room shown: 'send all photos' / 'more' gets the resort's photos
        other_free = [rt for rt in free if rt is not current]
        if ALL_ROOMS.search(text or ""):
            todo = [rt for rt in [current, *others] if unsent(refs_of[rt.id])]
            if todo:
                return [(rt, unsent(refs_of[rt.id])) for rt in todo], "", ""
            if dated and other_free and OTHER_ROOM.search(text or ""):
                return [], "other_free", other_free[0].name  # 'any other room available?': it IS free (2026-11-22)
            return [], none_left, ""
        if OTHER_ROOM.search(text or ""):
            if others:
                return [(others[0], unsent(refs_of[others[0].id]))], "", ""
            if dated and other_free:
                # another room IS free, its photos already went (or it has none): never 'no other room is free'
                # (2026-11-22, two room types were free)
                return [], "other_free", other_free[0].name
            return [], none_left, ""
        if unsent(refs_of[current.id]):
            return [(current, unsent(refs_of[current.id]))], "", ""
        if others:
            return [(others[0], unsent(refs_of[others[0].id]))], "", ""
        return [], "photos_sent", ""

    def _next_numbers(self, rt: RoomType, st: dict[str, Any], rooms: int = 1) -> list[str]:
        """The room numbers the guest would get: the first free ones by the owner's order on their dates (the same rule
        as when they pay). Without dates: the first numbers in the list."""
        if not rt.room_list:
            return []
        bk = st.get("bk") or {}
        if not (bk.get("check_in") and bk.get("check_out")):
            return rt.room_list[:rooms]
        ci, co, now = date.fromisoformat(bk["check_in"]), date.fromisoformat(bk["check_out"]), self.clock.now()
        probe = Booking(rt.property_id, rt.id, "", ci, co, 1, 0, rooms, 0, 0, now)
        return av.allocate_rooms(rt, probe, self.repo.blocking_bookings(rt.id, ci, co), now) or rt.room_list[:rooms]

    def _room_refs(self, rt: RoomType, st: dict[str, Any], rooms: int = 1) -> list[str]:
        """Photos of the exact room the guest would get (owner, 2026-11-19: 108 and 109 can look different), else the
        room type's photos, else any room number's photos."""
        own = [r for n in self._next_numbers(rt, st, rooms) for r in rt.number_photos.get(n, [])]
        if own:
            return list(dict.fromkeys(own))
        if rt.photo_urls:
            return list(rt.photo_urls)
        return list(dict.fromkeys(r for n in rt.room_list for r in rt.number_photos.get(n, [])))

    def _photo_place(self, st: dict[str, Any], text: str) -> Property | None:
        bk = st.get("bk") or {}
        named = self._named_place(text)
        place_id = bk.get("property_id") or st.get("form_place")
        prop = self.repo.get_property(place_id) if place_id else None
        if prop is None:  # no resort picked yet: only the one they named (owner, 2026-11-21: never guess the
            prop = named  # location for photos; the location question goes first)
        if prop is None or (named and named.id != prop.id):
            return None
        return prop

    def _hello_back(self, phone: str, st: dict[str, Any]) -> None:
        """A 'hi' in the middle of a chat (owner, 2026-11-22: 'say hi and fetch the customer details, never silence'):
        'Hi. Your enquiry: Alleppey, 13 Jan to 17 Jan 2027, 3 persons, 2 room(s).' (or their confirmed booking), then
        the booking step still open. Nothing known at all: the open step with 'Hi', else the owner's greeting."""
        lang = st.get("lang", "en")
        bk = st.get("bk") or {}
        held = [] if bk.get("check_in") else self._open_hold(phone)
        hello = ""
        if bk.get("check_in") and bk.get("check_out") and bk.get("adults"):
            prop = self.repo.get_property(bk.get("property_id") or "")
            rooms = (bk.get("chosen") or {}).get("rooms") or bk.get("rooms")
            stay = [self._place_label(prop) if prop else "",
                    f"{_day(date.fromisoformat(bk['check_in']))} to {_day(date.fromisoformat(bk['check_out']))}",
                    f"{bk['adults']} person(s)" + (f" + {bk['children']} child(ren)" if bk.get("children") else ""),
                    f"{rooms} room(s)" if rooms else ""]
            hello = t("hello_back", lang, stay=", ".join(x for x in stay if x))
        elif held:
            prop = self.repo.get_property(held[0].property_id)
            stay = [self._place_label(prop) if prop else "",
                    f"{_day(held[0].check_in)} to {_day(held[0].check_out)}",
                    f"{sum(b.adults for b in held)} person(s)", f"{sum(b.rooms for b in held)} room(s)"]
            hello = t("hello_back", lang, stay=", ".join(x for x in stay if x))
        elif paid := [b for b in self.repo.list_bookings(phone=phone, limit=10)
                      if b.status in ("paid", "checked_in") and b.check_out >= self._today()]:
            prop = self.repo.get_property(paid[0].property_id)
            hello = t("hello_booked", lang, place=self._place_label(prop) if prop else "the resort",
                      dates=f"{_day(paid[0].check_in)} to {_day(paid[0].check_out)}")
        step = self._open_step(phone, st)
        if step and step == (self.repo.last_outbound_text(phone) or ""):  # just above: the short pointer to it
            step = t("fill_form", lang) if step == t("form", lang) else t("contact_again", lang) \
                if step == t("ask_contact", lang) else step
        if step and step.startswith("Sure, "):  # 'Hi. Kindly confirm the Location', not 'Hi. Sure, ...'
            step = step[6:7].upper() + step[7:]
        if hello:
            self._text(phone, hello, repeat_ok=True)
            if step:
                self._text(phone, step, repeat_ok=True)
        elif step:
            self._text(phone, f"Hi. {step}", repeat_ok=True)
        else:
            self._text(phone, t("greet", lang), repeat_ok=True)

    def _open_step(self, phone: str, st: dict[str, Any]) -> str | None:
        """The booking step still open (form, name and mobile...). The location question ONLY when no resort is picked
        yet and the guest has no booking going: a guest with a payment link out once got 'Kindly confirm the Location'
        after the photos (live chat 2026-11-20)."""
        if step := self.next_step(phone, st):
            return step
        bk = st.get("bk") or {}
        if st.get("form_place") or bk.get("property_id") or any(
                b.status in ("hold", "paid", "checked_in") for b in self.repo.list_bookings(phone=phone, limit=5)):
            return None
        return t("ask_location", st.get("lang", "en"), towns=self._towns_text())

    def _known_place(self, st: dict[str, Any]) -> Property | None:
        """The resort this conversation already picked (the location answer, or the room check). Once known, the
        location question is NEVER sent again in any path (live chat 2026-11-20: 'Ok' got it twice)."""
        pid = st.get("form_place") or (st.get("bk") or {}).get("property_id")
        return self.repo.get_property(pid) if pid else None

    @staticmethod
    def _code_facts_only(st: dict[str, Any], guest_text: str, reply: str) -> str:
        """The AI's reply without what only the code may say (live chat 2026-11-20): its own room / extra-bed maths
        once rooms were checked (the code's rooms sentence goes instead when the guest asked about rooms), and 'let
        me check, one moment' promises (the code answers at once, nothing comes later)."""
        parts = [x for x in re.split(r"(?<=[.?!])\s+|\n+", reply) if x.strip()]
        checked = bool(st.get("checked")) and bool((st.get("bk") or {}).get("check_in"))
        asked_charge = bool(CHARGE_TALK.search(guest_text))
        keep = [x for x in parts if not STALLING.search(x) and not INTERNAL_RULE.search(x)
                and not (checked and ROOMS_MATH.search(x)) and (asked_charge or not CHARGE_TALK.search(x))]
        out = " ".join(keep).strip()
        if checked and len(keep) < len(parts) and (line := st.get("rooms_line")) \
                and re.search(r"\brooms?\b|\bbeds?\b|muri|kamr", guest_text, re.IGNORECASE) and line not in out:
            out = (out + "\n" + line).strip()
        return out

    def _price_now(self, phone: str, st: dict[str, Any], text: str) -> str | None:
        """'Price etreya' once the dates and persons are checked: the price of THAT stay, from the code (live chat
        2026-11-21: the AI asked for the dates again although the guest had given them). None = not a price question,
        or the stay is not checked yet (then the AI / the form asks what is missing)."""
        bk = st.get("bk") or {}
        if not (PRICE_Q.search(text) or PRICE_SHORT.match(text)) or NOT_PRICE.search(text):
            return None
        if not (st.get("checked") and bk.get("check_in") and bk.get("check_out") and bk.get("adults")):
            # the payment link is out (the stay is kept on the held booking, not in bk): 'whats the cost for standard
            # room' gets that room's price for the SAME stay, never the form or 'switch?' (live chat 2026-11-22)
            return self._held_price(phone, st, text)
        ctx = _Turn(phone, st, text=text)
        st.setdefault("turn", 0)
        before = bk.get("chosen")
        args: dict[str, Any] = {"rooms": bk.get("rooms") or 0} if bk.get("rooms") else {}
        if (photo_room := getattr(self, "_quoted_rooms", {}).get(phone)) and (rt := self.repo.get_room_type(photo_room)):
            args["room_type"] = rt.name  # 'price?' swiped on a room's photo: the price of THAT room (owner, 2026-11-22)
        elif (prop0 := self.repo.get_property(bk.get("property_id") or "")) and \
                (named := self._room_in_words(text, self.repo.list_room_types(prop0.id))):
            args["room_type"] = named.name  # 'cost for standard room': the price of THAT room
        out = self._tool_prepare(ctx, args)
        if "error" in out:
            out = self._tool_prepare(ctx, {})
        beds_chosen = int((bk.get("chosen") or {}).get("extra_beds") or 0)
        if before is None:  # only the price was asked: the booking flow (name ask next) is unchanged
            bk.pop("chosen", None)
        else:
            bk["chosen"] = before
        lang = st.get("lang", "en")
        if "error" in out:
            for message in ctx.after:  # e.g. the rooms were just taken: the fixed 'not available' line
                self._text(phone, message)
            self._save(phone, st)
            return "price" if ctx.after else None
        ci, co = date.fromisoformat(bk["check_in"]), date.fromisoformat(bk["check_out"])
        beds = int(out.get("extra_beds") or beds_chosen)
        night = ""
        if (prop := self.repo.get_property(bk.get("property_id") or "")) and (rt := next(
                (r for r in self.repo.list_room_types(prop.id) if r.name == out["room_type"]), None)):
            night = self._night_line(prop, rt, ci, co, lang)
        total_line = t("price_answer", lang, rooms=out["rooms"], room=out["room_type"],
                       beds=t("price_beds", lang, beds=beds) if beds else "", dates=av.fmt_range(ci, co),
                       nights=(co - ci).days, total=_money(int(out["total"])))
        self._text(phone, "\n".join(x for x in (night, total_line) if x), repeat_ok=True)
        step = self._open_step(phone, st)
        if step and step != (self.repo.last_outbound_text(phone) or ""):
            self._text(phone, step)  # back to the booking (the name ask), never twice in a row
        self._save(phone, st)
        return "price"

    def _night_line(self, prop: Property, rt: RoomType, ci: date, co: date, lang: str) -> str:
        """'Beach View Standard Room: ₹4,000 per room per night.' (owner, 2026-11-22: a rate question gets the price
        per night). Only when every night costs the same (rates that change are never shown): else '' (total only)."""
        rates = {r for _, r in av.quote(prop, rt, self.repo.list_rate_rules(rt.id), ci, co, 1).per_night}
        return t("price_night", lang, room=rt.name, rate=_money(rates.pop())) if len(rates) == 1 else ""

    def _held_price(self, phone: str, st: dict[str, Any], text: str) -> str | None:
        """A price question while the payment link is out: the price of the room the guest names (else the held one)
        for the stay on that link, per night and in total. Only told: the booking and the link are not changed."""
        held = self._held_stay(phone)
        if held is None:
            return None
        prop = self.repo.get_property(held["property_id"])
        if prop is None:
            return None
        rooms = self.repo.list_room_types(prop.id)
        photo_room = getattr(self, "_quoted_rooms", {}).get(phone)
        rt = next((r for r in rooms if r.id == photo_room), None) or self._room_in_words(text, rooms) or \
            next((r for r in rooms if r.id in held["room_type_ids"]), None)
        if rt is None:
            return None
        ci, co = date.fromisoformat(held["check_in"]), date.fromisoformat(held["check_out"])
        lang = st.get("lang", "en")
        free = self.repo.free_rooms(rt, ci, co, self.clock.now(), phone)
        beds_ok = bool(prop.extra_bed_rate)
        plan = av.plan_rooms(rt, held["adults"], held["children"], free, int(held.get("rooms") or 0), beds_ok) \
            or av.plan_rooms(rt, held["adults"], held["children"], free, 0, beds_ok)
        night = self._night_line(prop, rt, ci, co, lang)
        if not plan:  # that room is not free for their dates: the nightly rate, and that it is taken
            self._text(phone, "\n".join(x for x in (night, t("rooms_none", lang)) if x), repeat_ok=True)
            return "price"
        n = plan[0]
        beds = self._beds_for(prop, rt, held["adults"], held["children"], n)
        _, _, total, _ = self._price(prop, rt, ci, co, n, beds, False)
        total_line = t("price_answer", lang, rooms=n, room=rt.name,
                       beds=t("price_beds", lang, beds=beds) if beds else "", dates=av.fmt_range(ci, co),
                       nights=(co - ci).days, total=_money(total))
        self._text(phone, "\n".join(x for x in (night, total_line) if x), repeat_ok=True)
        return "price"

    @staticmethod
    def _rooms_line_in(st: dict[str, Any], text: str) -> bool:
        return bool(line := st.get("rooms_line")) and line in text

    def _asks_location(self, sentence: str) -> bool:
        """'Ningalude location confirm cheyyamo, Alleppey aano?', 'Which place sir?': a question about the location."""
        s = sentence.lower()
        return "?" in s and (bool(LOCATION_ASK.search(s)) or any(town.lower() in s for town in self._towns()))

    @staticmethod
    def _label_sets(rt: RoomType, refs: list[str]) -> list[tuple[str, list[str]]]:
        """A room type's photos split by room number, each with its label: [('Room 108 - Beach View Standard', [..]),
        ('Room 109 - Beach View Standard', [..])]. Photos of no number go under the room type's name (owner,
        2026-11-20: a text between two rooms' photos says which room and number they are)."""
        out: list[tuple[str, list[str]]] = []
        left = list(refs)
        for num in rt.room_list:
            mine = [r for r in left if r in set(rt.number_photos.get(num, []))]
            if mine:
                out.append((f"*Room {num} - {rt.name}*", mine))
                left = [r for r in left if r not in mine]
        if left:
            out.append((f"*{rt.name}*", left))
        return out

    def _queue_room_photos(self, ctx: _Turn, rt: RoomType, refs: list[str]) -> None:
        """Each room's label as a text, then that room's photos, in the turn's order."""
        for label, part in self._label_sets(rt, refs):
            ctx.after.append(label)
            ctx.after.append(part)

    def _send_plan(self, phone: str, plan: list[tuple[RoomType | None, list[str]]]) -> int:
        """Each room's photos together, a text with the room number and name before them (a guest seeing two rooms
        knows which is which). General photos go without a label."""
        sent = 0
        for rt, refs in plan:
            if rt is None:
                sent += self._send_photos(phone, refs)
                continue
            for label, part in self._label_sets(rt, refs):
                self._text(phone, label, repeat_ok=True)
                sent += self._send_photos(phone, part)
        return sent

    def _photos_now(self, phone: str, st: dict[str, Any], text: str) -> str | None:
        """A photo request at any stage, answered in code (never the AI): the photos by _photo_plan, then the open
        booking step. None = another resort was named (the AI answers)."""
        prop = self._photo_place(st, text)
        if prop is None and self._known_place(st) is None and self._named_place(text) is None:
            # no location yet: ask it first; the photos go right after the answer (owner, 2026-11-21)
            st["photo_wish"] = text
            self._save(phone, st)
            self._text(phone, t("ask_location", st.get("lang", "en"), towns=self._towns_text()), repeat_ok=True)
            return "location"
        if prop is None:
            return None
        lang = st.get("lang", "en")
        plan, problem, room = self._photo_plan(phone, st, prop, text)
        if problem == "photos_sent" and not self._sent_photos(phone):
            return None  # no photo uploaded yet: nothing was 'sent above'; the next step / the AI answers instead
        if problem:
            self._text(phone, t(problem, lang, room=room), repeat_ok=True)
            return "photos"
        if (st.get("checked") and OTHER_ROOM.search(text or "") and plan and plan[0][0] is not None
                and not PHOTO_ASK.search(text or "")):
            # 'any other room avail?': say plainly which other room is free, then its photos (a guest once got 'the
            # owner will assign whichever is best', live chat 2026-11-20)
            self._text(phone, t("other_room_free", lang, room=plan[0][0].name), repeat_ok=True)
        if not self._send_plan(phone, plan):
            self._text(phone, t("photos_sent", lang), repeat_ok=True)
        step = self._open_step(phone, st)
        if step == t("form", lang) and not (st.get("bk") or {}).get("check_in"):
            # photos before any dates (owner, 2026-11-21): say WHY the dates are needed, once per conversation
            self._text(phone, t("dates_for_availability", lang))
        if step and step != (self.repo.last_outbound_text(phone) or ""):
            self._text(phone, step, repeat_ok=True)  # back to the booking: the step still open, if any
        return "photos"

    def _tool_photos(self, ctx: _Turn, a: dict[str, Any]) -> dict[str, Any]:
        """The AI's photo tool follows exactly the same rules as the code path (_photo_plan)."""
        if not (PHOTO_ASK.search(ctx.text) or ROOM_OPTIONS_ASK.search(ctx.text) or OTHER_ROOM.search(ctx.text)
                or SEE_ASK.search(ctx.text)
                or (re.fullmatch(r"\W*(?:any\s+)?(?:more|innum|iniyum|vere|aur|still|next)\W*", ctx.text, re.IGNORECASE)
                    and self._sent_photos(ctx.phone))):  # 'more?' right after photos = more photos
            # photos only when the guest asks for them (live chat 2026-11-21: 'a room ... send the details' got room
            # photos and nothing else, no location question)
            return {"error": "the guest did not ask for photos: send none. Answer what they said; a room request "
                             "with no place known: call ask_location."}
        if self._known_place(ctx.st) is None and self._named_place(ctx.text) is None:
            # the guest never said which resort: the location question first, photos after the answer
            ctx.st["photo_wish"] = ctx.text
            ctx.after.append(t("ask_location", ctx.st.get("lang", "en"), towns=self._towns_text()))
            ctx.silent = True
            return {"ok": True, "note": "the location question is sent first; the photos follow the guest's answer. "
                                        "Reply exactly [no reply]."}
        prop = self._find_property(a.get("place", "")) or self._photo_place(ctx.st, ctx.text)
        if prop is None:
            return {"error": "unknown place", "places": [self._place_label(p) for p in self._places()]}
        plan, problem, room = self._photo_plan(ctx.phone, ctx.st, prop, ctx.text, str(a.get("room_type") or ""))
        lang = ctx.st.get("lang", "en")
        if problem:
            ctx.after.append(t(problem, lang, room=room))
            ctx.silent = True
            return {"error": "the guest is told automatically. Reply exactly [no reply]."}
        for rt, refs in plan:  # queued, so the turn knows something was sent (never a stray extra question)
            if rt is None:
                ctx.after.append(refs)
            else:
                self._queue_room_photos(ctx, rt, refs)
        ctx.silent = True
        return {"ok": True, "note": "the photos are sent automatically; reply exactly [no reply]"}

    def _send_photos(self, phone: str, refs: list[str], caption: str = "") -> int:
        from app.resort import photos as ph
        sent = 0
        for ref in refs:
            path = ph.local_path(ref)
            if path is None:
                continue
            try:
                media_id = self.wa.upload_media(path.read_bytes(), ph.mime_of(path), path.name)
                wamid = self.wa.send_image(phone, media_id, caption if not sent else "")
            except Exception as exc:  # one bad photo must not stop the chat
                log.warning("Photo send failed: %s", exc)
                continue
            # the WhatsApp id is kept: a swipe-reply to this photo tells which room it shows (owner, 2026-11-22)
            self._log(phone, f"[photo] {ref}", wamid=wamid if isinstance(wamid, str) else "")
            sent += 1
        return sent

    @staticmethod
    def _place_words(p: Property) -> set[str]:
        names = set(re.findall(r"[a-z]{4,}", f"{p.name} {p.city} {p.place}".lower())) - {"resort", "kerala", "vibes"}
        return names | {alias for n in names for alias in PLACE_ALIASES.get(n, ())}

    def _named_place(self, text: str) -> Property | None:
        """An open resort the guest named by town or name, spelling mistakes and all ('Alleppey', 'allepy', 'aleppey',
        'Alappuzha', 'pozhiyoram', 'kanthaloor'), else None (client, 2026-11-17)."""
        words = set(re.findall(r"[a-z]{4,}", latin_places(text).lower()))
        hits = [p for p in self._places() if any(close_word(w, n) for w in words for n in self._place_words(p))]
        return hits[0] if len(hits) == 1 else None

    def _location_answer(self, phone: str, st: dict[str, Any], text: str) -> str | None:
        """The guest answered our location question with just a place: send the form at once (no AI guesswork)."""
        last = self.repo.last_outbound_text(phone) or ""
        if "Kindly confirm the Location" not in last and last != self._towns_text() + " ?":
            return None
        if len(text.split()) > 4:
            return None
        # only a short, place-like answer ('alloy', 'kanth', 'xyz') is read as a place or gets the re-ask; a real
        # question ('ethra aanu rate?', 'pool undo?') goes to the normal handling (Step 3, 2026-11-18)
        place_like = (len(text.split()) <= 2 and "?" not in text and not QUESTION_WORD.search(text)
                      and not (MANGLISH_WORDS.search(text) or TANGLISH_WORDS.search(text) or HINGLISH_WORDS.search(text))
                      and not _classify_latin(_words(text))[0])  # 'confirm' / 'book' are words, not a misspelt town
        prop = self._named_place(text) or (self._place_by_start(text) if place_like else None)
        if prop is None and (closed := self._closed_place(text, place_like)):
            # a resort that is switched off in the dashboard (Kanthalloor today): bookings by phone, never a re-ask
            numbers = ", ".join(display_phone(n) for n in (closed.contact_numbers or self._all_contact_numbers()))
            self._text(phone, t("closed_place", st.get("lang", "en"), place=(closed.city or closed.place or closed.name).split(",")[0].strip(), numbers=numbers))
            return "closed_place"
        if prop is None:
            if not place_like or PHOTO_ASK.search(text) or re.search(r"\d", text) or GREETING.match(text) \
                    or is_ack(text) or NOT_NOW.search(text) or RESTART.match(text):
                return None  # not a place answer: the normal handling goes on
            # not understood: ask once more, short, never silence (owner, 2026-11-18)
            again = self._towns_text() + " ?"
            if (self.repo.last_outbound_text(phone) or "") == again:
                return None  # asked twice already: let the AI help
            self._text(phone, again, repeat_ok=True)
            return "location"
        st["form_place"] = prop.id
        self._save(phone, st)
        if wish := st.pop("photo_wish", None):  # they asked for photos before the location: now they go
            self._save(phone, st)
            if self._photos_now(phone, st, wish):
                return "photos"
        self._text(phone, t("form", st.get("lang", "en")))
        return "form"

    def _since_last_reply(self, phone: str) -> str:
        """Everything the guest wrote since our last message (their latest message is already stored)."""
        texts: list[str] = []
        for m in reversed(self._session_rows(phone)):
            if m.direction == "out":
                break
            texts.append(m.text)
        return "\n".join(reversed(texts))

    def _name_in_chat(self, phone: str) -> str:
        """The newest name the guest typed with a number in this conversation ('Shibil 859002727' gives Shibil even
        when that number was wrong), else ''."""
        rows = self._session_rows(phone)
        asks = {t(k, lg) for k in ("ask_contact", "ask_name") for lg in ("en", "mn", "tn", "hn")}
        for i in range(len(rows) - 1, -1, -1):
            m = rows[i]
            if m.direction != "in":
                continue
            words = [w for w in re.findall(r"[A-Za-z][A-Za-z.']*", m.text) if w.lower().strip(".") not in _NOT_NAME]
            if re.search(r"\d{6,}", m.text) and 1 <= len(words) <= 3:
                return " ".join(words)
            # 'Naseem' on its own, then the number in the next message (live chat 2026-11-19: the old saved name was
            # used instead): a short Latin-letter answer right after we asked for the name is the name
            prev = next((r for r in reversed(rows[:i]) if r.direction == "out"), None)
            if (prev is not None and prev.text in asks and not re.search(r"\d|\?", m.text) and 1 <= len(words) <= 3
                    and len(words) == len(m.text.split()) and not is_ack(m.text) and not GREETING.match(m.text)):
                return " ".join(words)
        return ""

    def _never_silent(self, ctx: _Turn, text: str) -> str | None:
        """The AI said nothing to a REAL message (a question, a request, booking details). Free models sometimes stay
        silent; the owner wants every real message answered (2026-11-19). The next fixed step goes, chosen in code.
        Casual messages ('ok', 'thanks', 'will tell my family') may stay quiet: returns None for them."""
        phone, st = ctx.phone, ctx.st
        lang = st.get("lang", "en")
        asked = "?" in (self.repo.last_outbound_text(phone) or "")
        if ((is_ack(text) and "?" not in text and not asked) or (NOT_NOW.search(text) and "?" not in text)) \
                and not CHANGE_ASK.search(text):
            return None  # an 'ok' to OUR question is an answer: it is never left silent (live chat 2026-11-20)
        now = self.clock.now()
        held = next((b for b in self.repo.list_bookings(phone=phone, limit=3) if b.status == "hold"
                     and b.hold_expires_at and b.hold_expires_at > now and b.payment_link_url), None)
        if held is not None and not BOOKING_WISH.search(text):
            # a payment link is out and nothing else answered ('Already sent'): point to that link, never the dates
            # form again (live chat 2026-11-21)
            mins = max(1, int((held.hold_expires_at - now).total_seconds() // 60))
            self._text(phone, t("pending", lang, id=held.id, minutes=mins, url=held.payment_link_url), repeat_ok=True)
            return "answered"
        wish = (BOOKING_WISH.search(text) or PHOTO_ASK.search(text) or contact_in(text) is not None
                or bool(parse_stay(text, self._today())) or self._named_place(text) is not None)
        # a question, a request, booking details, or one unclear word ('asdfgh'); remarks like 'nice place' may stay quiet
        # a reply to a question WE asked is always real ('Yeah, I want a deluxe' to the location question got
        # silence, live chat 2026-11-21)
        real = wish or asked or "?" in text or bool(QUESTION_WORD.search(text)) or bool(OWNER_ASK.search(text)) \
            or bool(CHANGE_ASK.search(text)) or len(text.split()) == 1 or bool(NUDGE.search(text))
        if not real:
            return None
        last = self.repo.last_outbound_text(phone) or ""
        if OWNER_ASK.search(text):
            self.handoff(phone, st, f"guest asked: {text[:120]}")
            return "handoff"
        if CHANGE_ASK.search(text) and any(b.status in ("paid", "checked_in", "hold")
                                           for b in self.repo.list_bookings(phone=phone, limit=5)):
            msg = t("owner_will_help", lang)  # the owner was alerted when the message came in
        elif closed := self._closed_place(text, True):
            numbers = ", ".join(display_phone(n) for n in (closed.contact_numbers or self._all_contact_numbers()))
            msg = t("closed_place", lang, place=(closed.city or closed.place or closed.name).split(",")[0].strip(), numbers=numbers)
        elif step := self.next_step(phone, st):
            # the open step, or a short pointer to it when it is the message just above (never 'type that again'
            # after the name ask, 2026-11-21)
            msg = step if step != last else (t("fill_form", lang) if step == t("form", lang) else t(
                "contact_again", lang) if step == t("ask_contact", lang) else self._towns_text() + " ?"
                if "Kindly confirm the Location" in step else t("say_again", lang))
        elif wish and not (st.get("bk") or {}).get("check_in"):
            if prop := self._named_place(text) or self._known_place(st):
                st["form_place"] = prop.id
                self._save(phone, st)
                msg = self._missing_form(lang, parse_stay(text, self._today()))
            else:
                msg = t("ask_location", lang, towns=self._towns_text())
        else:
            # nothing fixed fits: the AI answers it like an agent; 'please type that once more' only if it can't
            if self.answer_question(phone, st, text) == "answered":
                return "answered"
            msg = t("say_again", lang)
        if msg == last:
            msg = t("say_again", lang) if msg != t("say_again", lang) else ""
        if msg:
            self._text(phone, msg, repeat_ok=True)
        return "answered"

    def _ask_missing(self, ctx: _Turn, got: dict[str, Any]) -> None:
        """The form with only the missing lines; if that exact form is already the last message, 'Please share the
        details above.' instead. Never nothing: the 'never twice in a row' rule once turned this into silence."""
        lang = ctx.st.get("lang", "en")
        msg = self._missing_form(lang, got)
        if msg == (self.repo.last_outbound_text(ctx.phone) or ""):
            msg = t("fill_form", lang)
        if msg not in ctx.after:
            ctx.after.append(msg)
        ctx.silent = True

    def _closed_place(self, text: str, place_like: bool) -> Property | None:
        """A resort switched off in the dashboard that the guest named ('Kanthalloor', 'kanthaloor', 'kanth')."""
        closed = [p for p in self.repo.list_properties(active_only=False) if not p.active]
        words = set(re.findall(r"[a-z]{3,}", text.lower()))
        hits = [p for p in closed if any(close_word(w, n) or (place_like and len(w) >= 3 and n.startswith(w[:3]))
                                         for w in words for n in self._place_words(p))]
        return hits[0] if len(hits) == 1 else None

    def _negotiation_guard(self, phone: str, guest_text: str, reply: str) -> str:
        """A request for a lower price (owner, 2026-11-21): the first time only the value is said (owner numbers and
        payment-link reminders are dropped); when they ask again, the owner's numbers always go. A payment-link
        reminder the guest did not ask for is dropped from any reply. How the payment page works is never told
        (owner, 2026-11-21: no 'Razorpay', no 'secure'; only 'You can use UPI, card, net banking or wallet')."""
        parts = []
        for x in (x for x in re.split(r"(?<=[.?!])\s+|\n+", reply) if x.strip()):
            if re.search(r"razorpay|\bsecure", x, re.IGNORECASE):
                if not re.search(r"\bupi\b|\bcards?\b|net ?banking|wallet", x, re.IGNORECASE):
                    continue  # only about the payment page: dropped
                x = PAY_PAGE_TALK.sub("You can use ", x)
                x = re.sub(r"\s*\b(?:via |through |on |using )?(?:a |the )?(?:secure\s+)?razorpay(?:\s+(?:page|link|"
                           r"gateway))?\b", "", x, flags=re.IGNORECASE)
                x = re.sub(r"\bsecure(?:ly)?\s+", "", x, flags=re.IGNORECASE).strip()
                x = x[:1].upper() + x[1:]
            parts.append(x)
        if not LINK_ASK.search(guest_text):
            parts = [x for x in parts if not LINK_REMINDER.search(x)]
        if ASKS_REDUCE.search(guest_text):
            parts = [x for x in parts if not SORRY_FILLER.search(x)]  # 'I hear you, and I wish I could help.'
            earlier = sum(1 for m in self._session_rows(phone)[:-1] if m.direction == "in" and ASKS_REDUCE.search(m.text))
            numbers = [display_phone(n) for n in self._all_contact_numbers()]
            if not earlier:
                parts = [x for x in parts if not PHONE_DIGITS.search(x) and not OWNER_WORD.search(x)]
            elif numbers and not any(PHONE_DIGITS.search(x) for x in parts):
                parts.append(t("owner_numbers", "en", numbers=" / ".join(numbers)))
        return " ".join(parts).strip()

    def _all_contact_numbers(self) -> list[str]:
        return [n for p in self.repo.list_properties(active_only=False) for n in p.contact_numbers][:2]

    def _place_by_start(self, text: str) -> Property | None:
        """An answer to the location question that starts like one of our towns ('alloy', 'allep', 'kanth'): that one,
        when only one town starts with those first 3 letters."""
        word = next(iter(re.findall(r"[a-z]{3,}", text.lower())), "")
        if not word:
            return None
        hits = [p for p in self._places() if any(n.startswith(word[:3]) for n in self._place_words(p))]
        return hits[0] if len(hits) == 1 else None

    def _own_location_question(self, reply: str) -> bool:
        """'Sure, which location sir - Alleppey or Kanthalloor?': a question naming two or more of our towns, which
        must always go out in the owner's fixed words (client, 2026-11-18)."""
        towns = self._towns()
        named = sum(1 for town in towns if town.lower() in reply.lower())
        # only the owner's EXACT message passes; the AI's own copy around it ('Sure, I can help with that. Kindly
        # confirm the Location - Alleppey or Kanthalloor?') is replaced too (live chat 2026-11-21)
        exact = {t("ask_location", lg, towns=self._towns_text()).strip() for lg in ("en", "mn", "tn", "hn")}
        return len(towns) > 1 and named >= 2 and "?" in reply and reply.strip() not in exact

    def _own_dates_ask(self, st: dict[str, Any], reply: str) -> tuple[bool, str]:
        """'What is your check-in date?', 'Please share your check in and check out dates.', 'How many people will be
        staying?': the AI asking for the stay details in its own words before it has the dates. Returns (asked, the
        rest of the reply). The owner's form ALWAYS goes instead, strictly (client, 2026-11-18)."""
        if (st.get("bk") or {}).get("check_in"):
            return False, reply
        if reply.strip() in {t("form", "en"), t("form", "mn")} or len(FORM_LABELS.findall(reply)) >= 2:  # its own form
            return True, ""
        keep, asked = [], False
        for sentence in re.split(r"(?<=[.?!])\s+|\n+", reply):
            if DATES_QUESTION.search(sentence) and (sentence.rstrip().endswith("?") or ASKING.search(sentence)):
                asked = True
            elif re.search(r"\bavailab|\bfree\b", sentence, re.IGNORECASE):  # 'Rooms are available' before any dates
                continue
            elif sentence.strip():
                keep.append(sentence.strip())
        rest = " ".join(keep)
        if FILLER.fullmatch(rest):  # 'Got it.', 'Sure.', 'Okay sir.' alone say nothing
            rest = ""
        return asked, rest

    def _tool_prices_only(self, ctx: _Turn, reply: str) -> str:
        """A price, total or advance only from a tool this turn (2026-11-18: an English guest asking 'how much is it?'
        got a made-up Rs 2,000 a night). Amounts the AI wrote without a tool are dropped; when the stay is known the
        real price for those dates is put in their place."""
        if ctx.priced or not _MONEY.search(reply):
            return reply
        keep = [s for s in re.split(r"(?<=[.?!])\s+|\n+", reply) if s.strip() and not _MONEY.search(s)]
        bk = ctx.st.get("bk") or {}
        prop = self.repo.get_property(bk["property_id"]) if bk.get("property_id") else None
        if prop and bk.get("check_in") and bk.get("check_out"):
            fits = self._fits(prop, bk)
            if fits:
                rt, options = fits[0]
                ci, co = date.fromisoformat(bk["check_in"]), date.fromisoformat(bk["check_out"])
                q = av.quote(prop, rt, self.repo.list_rate_rules(rt.id), ci, co, options[0])
                per = round(q.total / max(1, q.nights * options[0]))
                keep.insert(0, t("price_line", ctx.st.get("lang", "en"), room=rt.name, per=_money(per),
                                 total=_money(q.total), nights=q.nights, s="s" if q.nights != 1 else ""))
        return " ".join(keep).strip()

    def _no_repeat_ask(self, phone: str, reply: str) -> str:
        """Once the fixed 'Please share your name and mobile number' went, the AI does not ask it again in its own
        words ('Name aur mobile number share kijiye', 'Ningalude name-um mobile number-um ayakkamo?'): nothing is said
        twice (client rule). The rest of its answer stays."""
        asks = {t(k, lang) for k in ("ask_contact", "ask_name") for lang in ("en", "mn")}
        if reply in asks or not any(m.direction == "out" and m.text in asks for m in self._session_rows(phone)):
            return reply
        keep = [s for s in re.split(r"(?<=[.?!])\s+|\n+", reply) if s.strip() and not CONTACT_ASK.search(s)]
        return " ".join(keep).strip()

    def _strict_form(self, ctx: _Turn, rest: str) -> None:
        """Instead of the AI's own ask for dates: what it said besides (if anything), then the next fixed step. If the
        guest already gave the dates and persons across their messages, rooms are checked straight away; else the
        form goes (or the location question first, when the resort is not known yet)."""
        st, phone = ctx.st, ctx.phone
        lang = st.get("lang", "en")
        if rest:
            self._text(phone, self._ease_sir(phone, rest))
        now = self.clock.now()
        if not st.get("bk") and not BOOKING_WISH.search(ctx.text or "") and any(
                b.status == "hold" and b.hold_expires_at and b.hold_expires_at > now
                for b in self.repo.list_bookings(phone=phone, limit=3)):
            return  # a payment link is out: the booking is not started again (live chat 2026-11-21)
        prop = self._known_place(st) or self._named_place(
            ctx.text) or (self._places()[0] if len(self._places()) == 1 else None)
        if prop is None:
            self._text(phone, t("ask_location", lang, towns=self._towns_text()))
            return
        st["form_place"] = prop.id
        got = self._stay_known(phone, st)
        if got.get("check_in") and got.get("check_out") and got.get("persons"):
            self._tool_check(ctx, {"place": self._place_label(prop), "check_in": got["check_in"].isoformat(),
                                   "check_out": got["check_out"].isoformat(), "adults": got["persons"],
                                   "children": got.get("children", 0), "rooms": got.get("rooms", 0),
                                   "language": st.get("style")})
            self._save(phone, st)
            return  # rooms available / not, glimpse, photos and the name ask follow from ctx.after
        self._save(phone, st)
        if any(m in ctx.after for m in (t("form", "en"), t("form", "mn"))):  # once per turn
            return
        form = self._missing_form(lang, got)  # the owner's form, only the lines still missing (never silence)
        last = self.repo.last_outbound_text(phone) or ""
        if form == last:
            form = t("fill_form", lang)  # the same form is just above: point to it
            if form == last:
                return
        self._text(phone, form, repeat_ok=True)

    @staticmethod
    def _missing_form(lang: str, got: dict[str, Any]) -> str:
        """'Please share these details:' with only the lines the guest has not given yet ('dec 20 nu' leaves Check out and
        Number of persons). With nothing given it is the full form."""
        lines = t("form", lang).split("\n")
        have = {"check in": "check_in", "check out": "check_out", "number of persons": "persons",
                "number of rooms": "rooms"}  # rooms is optional: left blank, 2 persons per room are planned
        keep = [ln for ln in lines if not any(ln.lower().startswith(k) and got.get(v) for k, v in have.items())]
        return "\n".join(keep)

    def _no_stale_payment(self, phone: str, guest_text: str, reply: str) -> str:
        """A guest whose booking is paid, with nothing waiting for payment, is never told to complete a payment or
        offered the link again, unless they asked about payment themselves (2026-11-18)."""
        if not reply or PAYMENT_ASK.search(guest_text) or guest_text.startswith(("[image]", "[The guest sent an image")):
            return reply
        now = self.clock.now()
        rows = self.repo.list_bookings(phone=phone, limit=5)
        if any(b.status == "hold" and b.hold_expires_at and b.hold_expires_at > now for b in rows):
            return reply  # a booking really waits for payment: the AI may talk about it
        if (self.st_of(phone).get("bk") or {}).get("chosen"):
            return reply  # a booking is being made right now
        keep = [s for s in re.split(r"(?<=[.?!])\s+|\n+", reply) if s.strip() and not STALE_PAY.search(s)]
        return " ".join(keep).strip()

    def next_step(self, phone: str, st: dict[str, Any]) -> str | None:
        """The owner's flow step still open in this conversation, as its fixed message, or None:
        location question -> form -> (rooms, glimpse, photos) -> name and mobile -> summary and payment link."""
        lang = st.get("lang", "en")
        now = self.clock.now()
        if any(b.status == "hold" and b.hold_expires_at and b.hold_expires_at > now
               for b in self.repo.list_bookings(phone=phone, limit=3)):
            return None  # the payment link is out; nothing more to ask
        bk = st.get("bk") or {}
        if bk.get("chosen"):
            # the figures are locked but the booking needs the name and mobile: that is still the open step (an AI ask
            # held back as a repeat once left the guest with 'please type that again', 2026-11-21)
            return None if bk.get("mobile") else t("ask_contact", lang)
        if bk.get("check_in") and st.get("checked"):
            return t("ask_contact", lang)
        if st.get("form_place") and not bk.get("check_in"):
            return t("form", lang)
        if self._known_place(st) is not None:
            return None  # the resort is picked: never the location question again (live chat 2026-11-20)
        asked_location = t("ask_location", lang, towns=self._towns_text())
        if any(m.direction == "out" and m.text == asked_location for m in self._session_rows(phone)):
            return asked_location
        return None

    def st_of(self, phone: str) -> dict[str, Any]:
        return self.repo.get_state(phone)

    def booking_wish(self, phone: str, st: dict[str, Any], text: str) -> str | None:
        """'Rooms?', 'room venam', 'need a room', 'resort booking': the owner's next fixed step in code, never an AI guess
        (the AI once answered 'Rooms?' with an old booking's payment talk, 2026-11-18). Location question, or the form
        when the place is already known or named."""
        words = re.findall(r"[^\W\d_]+", text.lower())
        if not words or len(words) > 7 or not any(w in WISH_CORE for w in words):
            return None
        if any(w not in WISH_WORDS and not self._named_place(w) for w in words):
            return None  # it says more than 'I want a room' (a question about the rooms): the AI answers
        if (st.get("bk") or {}).get("check_in"):
            return None  # a booking is in progress: the AI carries on from there
        lang = st.get("lang", "en")  # the language was read in flow._dispatch, before any step answers
        prop = self._named_place(text)  # the owner always asks the location first, unless the guest named it
        if prop is None and (known := self._known_place(st)):
            # the place is known and the form already went in this chat ('ok book' after the form): the open step,
            # never the location question again (N01, Step 3)
            st["form_place"] = known.id
            self._save(phone, st)
            form = t("form", lang)
            self._text(phone, t("fill_form", lang) if self._form_was_last(phone) else form, repeat_ok=True)
            return "form"
        if prop is None:
            self._text(phone, t("ask_location", lang, towns=self._towns_text()), repeat_ok=True)
            self._save(phone, st)
            return "location"
        st["form_place"] = prop.id
        self._save(phone, st)
        self._text(phone, t("form", lang), repeat_ok=True)
        return "form"

    def _form_was_last(self, phone: str) -> bool:
        return (self.repo.last_outbound_text(phone) or "") in {t("form", "en"), t("form", "mn")}

    def _recent_guest_text(self, phone: str) -> str:
        """The guest's messages in this conversation, oldest first (their stay details may be spread over several)."""
        rows = self._session_rows(phone)
        # a fresh enquiry starts after the last room check / summary (a form re-sent in between must never lose
        # 'Person 3' given just before it, live chat 2026-11-19)
        marks = {t(k, lg) for k in ("rooms_ok", "rooms_none") for lg in ("en", "mn", "tn", "hn")}
        # the code's rooms sentence also closes a check ('2 rooms mathi' re-checks without 'rooms available'): a
        # '1 room koode' before it is never added twice (live chat replay, 2026-11-21)
        start = max((i for i, m in enumerate(rows) if m.direction == "out" and (
            m.text in marks or m.text.startswith("Following are the glimpse of your enquiry")
            or any(rx.match(m.text) for rx in ROOMS_LINE_RX))), default=-1)
        rows = [m for m in rows[start + 1:] if m.direction == "in"][-HISTORY:]
        return f"\n{MSG_SEP}\n".join(m.text for m in rows)[-1500:]

    def _stay_known(self, phone: str, st: dict[str, Any]) -> dict[str, Any]:
        """What the guest has told us about the stay: the messages since the last room check, on top of the details
        that check used (a child added later keeps the dates; new dates keep the persons)."""
        got = parse_stay(self._recent_guest_text(phone), self._today())
        bk = st.get("bk") or {}
        if not bk.get("adults") and (held := self._held_stay(phone)) and (
                not bk.get("check_in") or bk.get("check_in") == held["check_in"]):
            # the payment link is out: its stay is what we know ('same date', 'same persons' never ask again)
            bk = {**held, **{k: v for k, v in bk.items() if v}}
        if bk.get("check_in") and "check_in" not in got:
            got["check_in"] = date.fromisoformat(bk["check_in"])
            if bk.get("check_out") and "check_out" not in got:
                got["check_out"] = date.fromisoformat(bk["check_out"])
        if bk.get("adults") and "persons" not in got:
            got["persons"] = bk["adults"]
        if bk.get("children") and "children" not in got:
            got["children"] = bk["children"]
        if bk.get("rooms") and "rooms" not in got:
            got["rooms"] = bk["rooms"]
        if more := got.pop("more_rooms", 0):  # 'one more room': on top of the rooms quoted at the last check
            parts = (st.get("checked") or "").split("|")
            have = int(bk.get("rooms") or 0) or (int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else 0)
            got["rooms"] = have + more if have else more  # nothing quoted yet (a new booking): just those rooms
        if less := got.pop("less_rooms", 0):
            parts = (st.get("checked") or "").split("|")
            have = int(bk.get("rooms") or 0) or (int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else 0)
            if have:
                got["rooms"] = max(1, have - less)
        return got

    def _open_hold(self, phone: str) -> list[Booking]:
        """The guest's unpaid enquiry whose payment link is still open (all its parts for a stay over two room types)."""
        now = self.clock.now()
        holds = [b for b in self.repo.list_bookings(phone=phone, status="hold", limit=10)
                 if b.hold_expires_at and b.hold_expires_at > now]
        if not holds:
            return []
        newest = max(holds, key=lambda b: b.created_at)
        return [b for b in holds if b is newest or (newest.payment_link_id and b.payment_link_id == newest.payment_link_id)]

    def _held_stay(self, phone: str) -> dict[str, Any] | None:
        """The stay on the guest's open payment link, in the shape of st['bk'] (plus the held room type ids), or None.
        After the link goes out st['bk'] is cleared, so this is where the dates, persons and rooms are remembered."""
        held = self._open_hold(phone)
        if not held:
            return None
        first = held[0]
        note = re.search(r"Mobile: ([+\d][\d ]{6,})", first.notes or "")
        return {"property_id": first.property_id, "check_in": first.check_in.isoformat(),
                "check_out": first.check_out.isoformat(), "adults": sum(b.adults for b in held),
                "children": sum(b.children for b in held), "rooms": sum(b.rooms for b in held),
                "guest_name": first.guest_name or "",
                "mobile": try_normalize_phone(note.group(1)) if note else phone,
                "room_type_ids": [b.room_type_id for b in held]}

    def _upcoming_booking(self, phone: str) -> Booking | None:
        """The guest's latest booking that is still ahead (paid, on hold or checked in), else None."""
        today = self._today()
        live = [b for b in self.repo.list_bookings(phone=phone, limit=10)
                if b.status in ("paid", "hold", "checked_in") and b.check_out >= today]
        return max(live, key=lambda b: b.created_at, default=None)

    def _tool_location(self, ctx: _Turn, a: dict[str, Any]) -> dict[str, Any]:
        if prop := self._named_place(ctx.text) or self._known_place(ctx.st):  # named or picked already: the form follows
            return self._tool_form(ctx, {"place": self._place_label(prop)})
        ctx.after.append(t("ask_location", ctx.st.get("lang", "en"), towns=self._towns_text()))
        ctx.silent = True
        return {"ok": True, "note": "the location question is sent automatically; reply exactly [no reply]"}

    def _closed_facts(self) -> list[str]:
        """Resorts the guest may pick that have no online booking yet: the owner takes those."""
        out = []
        for p in self.repo.list_properties(active_only=False):
            if p.active:
                continue
            out.append(f"RESORT '{p.name}': currently not available. If the guest picks it, say only, in one short line, that it is currently not available")
        return out

    def _facts(self) -> str:
        out = self._closed_facts()
        for p in self._places():
            numbers = ", ".join(display_phone(n) for n in p.contact_numbers) or "not set"
            out.append(f"PLACE '{self._place_label(p)}' (property name {p.name})")
            if p.description and "PLACEHOLDER" not in p.description:
                out.append(f"  About: {p.description}")
            if p.highlights:
                out.append(f"  Good points: {'; '.join(p.highlights)}")
            if p.map_url:
                out.append(f"  Location (Google Maps link, send it when the guest asks where it is): {p.map_url}")
            out.append(f"  Check-in {p.checkin_time}, check-out {p.checkout_time}. To confirm a booking the guest pays "
                       f"{p.advance_pct}% online now; the rest is paid at the property.")
            if p.extra_bed_rate:
                out.append(f"  Extra bed: {_money(p.extra_bed_rate)} per bed per night.")
            if p.early_checkin_time and p.early_checkin_rate:
                out.append(f"  Early check-in (only if asked): from {self._short_time(p.early_checkin_time)}, "
                           f"{_money(p.early_checkin_rate)} per room.")
            if p.meal_plan:
                out.append(f"  Meal plan: {p.meal_plan}.")
            if p.policies:
                out.append(f"  Policies: {p.policies}")
            out.append(f"  Owner's phone/WhatsApp numbers: {numbers}")
            out += self._rooms_facts(p)
        return "\n".join(out)

    def _guest_memory(self, phone: str, fresh: bool) -> str:
        """What our records say about this guest (not the chat): name and earlier bookings, newest first."""
        if not phone:
            return "GUEST RECORDS\n- none"
        now, today = self.clock.now(), self._today()
        guest = self.repo.get_guest(phone)
        lines = ["GUEST RECORDS (from our database)"]
        lines.append(f"- Name: {guest.name} (never address them by name)" if guest and guest.name
                     else "- Name: not known yet")
        rows = self.repo.list_bookings(phone=phone, limit=4)
        for b in rows:
            prop = self.repo.get_property(b.property_id)
            place = self._place_label(prop) if prop else "?"
            when = "upcoming" if b.check_in >= today else ("in progress" if b.check_out > today else "past")
            status = status_name(b.status, "en")
            note = ""
            if b.status == "hold":
                live = bool(b.hold_expires_at and b.hold_expires_at > now)
                status = "ON HOLD, waiting for payment" if live else "expired (unpaid)"
                note = "" if live else " - finished, ignore it"
            lines.append(f"- {b.check_in.isoformat()} to {b.check_out.isoformat()}, {place}, {b.rooms} room(s), "
                         f"{status}, {when}{note}")
        if not rows:
            lines.append("- No earlier booking: a first-time guest.")
        lines.append("- This is a NEW conversation after a long gap." if fresh
                     else "- This is the same conversation continuing.")
        return "\n".join(lines)

    @staticmethod
    def _language_note(st: dict[str, Any], text: str) -> str:
        """The language of THIS reply, decided in code from the guest's latest message, so earlier chats cannot
        pull the AI into mixing languages."""
        own = st.get("gs", "")
        asked = next((code for code, rx in LANG_REQUEST.items() if rx.search(text)), None)
        strong = False  # the message itself clearly shows a language (script or our word lists)
        if asked:
            style = asked  # 'English please' / 'Tamil la pesunga': that language, and it stays (st["lang_lock"])
        elif own in LANG_NAMES:
            style, strong = LATIN_STYLE[own], True  # Malayalam / Tamil / Hindi script -> Manglish / Tanglish / Hinglish
        elif is_language_neutral(text):
            style = st.get("style") or "en"  # 'ok' / 'hi' / a name: the guest's own language so far
        elif (tamil := len(TANGLISH_WORDS.findall(text))) and tamil >= len(MANGLISH_WORDS.findall(text)) \
                and not HINGLISH_WORDS.search(text):
            style, strong = "tanglish", True
        elif MANGLISH_WORDS.search(text) and not HINGLISH_WORDS.search(text):
            style, strong = "manglish", True
        elif HINGLISH_WORDS.search(text) and not MANGLISH_WORDS.search(text):
            style, strong = "hinglish", True
        else:
            style = latin_style(text, st.get("style") or "en")
            strong = style == "en" and _classify_latin(_words(text))[0] >= 4  # a long, clearly English message
            if st.get("style") in ("tanglish", "hinglish") and not MANGLISH_WORDS.search(text) and (
                    style == "manglish" or _classify_latin(_words(text))[0] < 3):
                style = st["style"]  # a Tamil / Hindi guest's short or unknown words are not Malayalam or English
        if style not in STYLE_LABELS:
            style = "en"
        lock = asked or st.get("lang_lock")
        if lock and not asked and style != lock:
            if strong and len(_words(text)) >= 4:
                lock = None  # a long message clearly in another language: the guest switched
            else:
                style = lock  # short / neutral / unclear messages keep the language the guest asked for
        if lock:
            st["lang_lock"] = lock
        else:
            st.pop("lang_lock", None)
        if asked or lock or not is_language_neutral(text):
            st["style"] = style
            if own not in LANG_NAMES or lock:  # English letters: the fixed messages (payment, confirmation) follow
                st["lang"] = STYLE_LANG[style]
        extra = " Plain English words like room, booking and payment are fine inside it." if style != "en" else ""
        return (f"REPLY LANGUAGE (decided from the guest's latest message): {STYLE_LABELS[style]}. Write the whole "
                f"reply in it and no other language.{extra}")

    def _system_prompt(self, st: dict[str, Any], phone: str = "", text: str = "", fresh: bool = False) -> str:
        today = self._today()
        return (
            f"You are the WhatsApp booking assistant of {self._brand()}. You talk like a friendly, sensible person at "
            "the front desk, never like a form or a menu. " + self.config.speaking_style + "\n\n"
            "BE CONCISE (the owner's rule)\n"
            "- Answer exactly what was asked, in as few words as it needs, politely. Nothing extra: no reminders "
            "(payment link, booking), no offers, no explanations they did not ask for, no closing lines.\n\n"
            "YOUR PURPOSE (strict)\n"
            "- You ONLY help with this resort: the rooms, the stay, the booking, the payment, getting there and the "
            "places around. Anything else (news, other businesses, general questions, jokes, personal talk, other "
            "hotels): one polite line that you can help only with stays at the resort, then nothing more.\n"
            "- Never talk about how you work: no 'automatically', 'the system', 'tools', 'steps' or 'messages are "
            "sent'. Never explain the booking process unless asked.\n"
            "- Only say what you are sure of from the FACTS, the tool results and this chat. Never invent charges, "
            "bills, rules or facts. If a message makes no sense to you, ask the guest to say it again in a few words.\n"
            "- Every reply must make sense on its own to a guest reading it on WhatsApp.\n\n"
            f"Today is {today.isoformat()} ({today.strftime('%A')}), India time.\n\n"
            "LANGUAGE\n"
            "- Work out the guest's language from their message. Never ask which language they want.\n"
            "- Reply in the same language but ALWAYS in English letters, never in Malayalam, Tamil or Hindi script: "
            "English -> English; Malayalam (typed in any script, or spoken) -> Manglish; Tamil -> Tanglish; Hindi -> "
            "Hinglish. If they switch, you switch. Short words like 'ok' or 'hi' keep the language so far.\n"
            "- Use ONE language per reply, the guest's own. Malayalam typed in English letters ('avde engane und', "
            "'room undo', 'evide', 'ethra aaya') is Manglish: answer in Manglish, in English letters only, NEVER in "
            "Malayalam script. Malayalam script gets Manglish too. English gets English. Never mix languages in "
            "one reply; plain English words like room, booking, check-in, payment and price are fine "
            "inside Manglish. Example: guest 'avde parking undo' -> 'Und, parking und.'\n"
            "- Hindi typed in English letters ('kitne log', 'room milega', 'theek hai') is Hinglish: answer in Hinglish "
            "(Hindi in English letters), NEVER in Malayalam or Manglish. Tamil in English letters ('irukka', 'venum') "
            "is Tanglish: answer in Tanglish.\n"
            "- The booking summary is sent automatically, always in English; your own lines stay in the guest's "
            "language.\n"
            "- ONLY the guest's LATEST message decides the language. Earlier messages in this chat, and earlier "
            "chats, may be in other languages: ignore them completely and never carry their words or script into "
            "your reply. If one message mixes two languages, answer in the language most of its words are in. The "
            "line \"REPLY LANGUAGE\" at the end of these instructions is the language to use now.\n"
            "- Pass the language style in the `language` field when you call check_availability.\n\n"
            "HOW TO TALK (the owner's rule: NO essays)\n"
            "- Answer what the guest asked, simply and fully, like a quick WhatsApp reply from a real person who "
            "thinks. Short when the answer is short; a little longer when they ask something that needs it (places "
            "to see, how to reach). No introductions, no repeating, no closing lines like 'let me know if you need "
            "anything'.\n"
            "- Asked about rooms? Never ask the dates or persons in your own words: call ask_location (resort not known) "
            "or ask_stay_details (resort known) and reply [no reply]. Asked the "
            "location? Send the Maps link with a few words. Asked a yes/no question? Start with yes or no.\n"
            "- Say \"sir\" only once, at the very start of the chat (\"madam\" only if they are clearly a woman); never "
            "again after that, and never in the middle of the chat. "
            "Never bro, dear, da, chetta, chechi, machane or their name.\n"
            "- Rooms: the room offered first is picked automatically. The guest may choose ANY room type or room "
            "number at any time: when they ask which rooms there are, name the room types from the FACTS (prices "
            "only if asked). Never say a room cannot be chosen, and never send a room change to the owner. Never tell "
            "room numbers: 'is there 2 rooms in X?' -> 'Yes, 2 rooms available.' (a number only if the guest named it).\n"
            "- Ask only ONE thing at a time, simply. No emoji piles (one is fine).\n"
            "- Do not paste the property description. Do not mention offers, discounts or 'special' deals. Just "
            "state the price.\n"
            "- Never repeat details the guest already gave, except in the final summary.\n"
            "- Never ask again what they already told you.\n"
            "- Always polite, concise and to the point.\n"
            "- You are the resort's booking agent: answer every question about the resort, the stay and the trip "
            "(parking, food, pool, beach, Wi-Fi, AC, check-in time, ID, distance, how to reach, places to see nearby, "
            "weather, best season) helpfully, like a friendly person at the front desk. Places to see, how to reach, "
            "weather and best season: answer from what you know about the town (name 3-4 well-known places), never "
            "'the owner will guide you'. For the resort's own rules and services (pets, alcohol, extra services) use "
            "the FACTS; if a fact is not there, say the owner will confirm and give the owner's numbers. Only for things that have nothing to "
            "do with the trip (news, jokes, coding) say politely in one line that you help with stays at the resort.\n"
            "- When the guest's message also asks a question, ANSWER that question in one or two short lines even "
            "when a tool tells you to reply [no reply]: the booking messages still go automatically after your answer. "
            "Never repeat those booking messages yourself.\n"
            "- A price, total or advance only from a tool result in THIS turn (call check_availability); never from "
            "memory or the welcome message.\n"
            "- When the guest's message carries a note that they swiped to reply to an earlier message, answer about "
            "THAT message.\n"
            "- VERY SIMPLE: easy everyday words a school child understands, short sentences, no fancy or formal "
            "phrases ('kindly', 'we would be delighted', 'nestled', 'serene'), no sales talk. Example: guest 'is it near "
            "the beach?' -> 'Yes, right on the beach.'\n"
            "- Reply only when it is necessary: a question, a request, a booking detail, a payment problem, a complaint, "
            "or an answer to something you asked. For anything else send no reply at all, answer with exactly "
            "[no reply]: 'ok' or 'thanks' that answer nothing (an 'ok' / 'yes' to a question YOU just asked is an "
            "answer: act on it at once, e.g. book or check what you offered), a thumbs up, compliments, jokes, 'will tell my family', 'let me check and "
            "tell you', 'will see', a closing remark. Do not chase the guest or fill the silence. If they want more, they "
            "will write. It is fine for the guest to have the last message in the chat.\n"
            "- If the guest's message is cut off or you cannot tell what they mean (for example 'njan udheshikkunath "
            "oru'), do not guess: ask them in a few words to go on, in their own language.\n"
            "- Messages that arrive one after another belong together. A short message (a place, a date, a number, "
            "'yes') answers your last question or continues what they said before: join them using the chat so far. "
            "If the guest wrote 'avde engane und' and then 'Alleppey', they are asking how Alleppey is: answer that, "
            "do not start over or ask the same thing again.\n\n"
            "BOOKING, in this order, skipping what the guest already told you\n"
            "1. Which location. When the guest wants a resort, a booking or rooms and has not said where, call "
            f"ask_location (it asks: {' or '.join(self._towns())}) and reply [no reply]. A town or resort name "
            f"means that resort ({'; '.join(self._resort_names())}). Guests often misspell names (allepy, aleppey, "
            "alappuzha, kanthaloor, poziyoram): a close spelling IS that place. Carry on; never correct their spelling "
            "or ask which place they meant.\n"
            "2. As soon as the resort is known and the guest has not given their dates and persons, call "
            "ask_stay_details: it sends the form (Check in / Check out / Number of persons). Reply [no reply]. NEVER ask for "
            "dates, nights or persons in your own words (not even only the missing part): call ask_stay_details again "
            "and the form is sent. If a date has no year, use the next time "
            "it comes. Never assume the number of persons or nights.\n"
            "3. When you have check-in, check-out (or nights) and persons, call check_availability. Rooms: use the "
            "fewest rooms the group needs unless the guest asked for more. Whether rooms are free, the resort's "
            "details, the photos and the request for name and mobile number are all sent automatically: reply "
            "[no reply].\n"
            "4. Never ask which room type. Never tell the guest how rooms are picked: no 'priority', no 'the resort / "
            "owner will assign or decide the room'. ANY change to the stay after the room check, before or after "
            "the payment link (a room type or room number they pick, a room type they do not want, more or fewer "
            "rooms, persons or dates): call change_enquiry with only what changed. The CURRENT ENQUIRY below has "
            "everything already known; never ask for it again.\n"
            "- Never state prices, totals or the advance while checking availability; the price reaches the guest "
            "in the resort's details and the enquiry summary. Give a price only if the guest asks for it.\n"
            "- A name correction after the summary or the booking ('my name is not X, it is Y'): call change_name "
            "with the new name in English letters. Never ask for the dates again for that.\n"
            "- After a booking is confirmed: 'ok' / 'thanks' gets one short warm line ('Thank you, see you soon!'), "
            "never 'How can I help you'. A guest who mentions their booked dates ('see you on 21 Jan') is talking "
            "about THAT booking: reply warmly in one short line; never start a new booking.\n"
            "5. When the guest gives their name and mobile number, call create_booking right away (no 'shall I "
            "confirm?'). The enquiry summary and the payment link are sent automatically: reply [no reply]. Never "
            "write the summary, the link, the advance amount or a percent yourself. If they say 'this number' "
            "or 'same number', pass mobile 'same'. If they give only the number (or 'same number') without a name, "
            "call create_booking anyway with an empty name: their WhatsApp name is used.\n"
            "- Photos: only when the guest asks for photos, call send_photos and reply [no reply]; the photos are "
            "chosen automatically.\n"
            "- ID proof: if asked, say only 'Please bring your ID proof while coming.' Never name ID types and never ask "
            "for an ID photo on WhatsApp.\n"
            "- Early check-in: only if the guest asks. Tell them the time and charge from the FACTS and pass "
            "early_checkin true to create_booking if they want it.\n"
            "- Never ask the same thing twice.\n"
            "- Before the payment is received, NEVER say the room or booking is booked, confirmed, reserved or done. "
            "Say it will be confirmed once the payment is complete. The confirmation message arrives on its own.\n"
            "- If rooms are not free, the guest is told automatically with the nearest free dates or the extra-bed "
            "way; help them with that. Never suggest a resort that is not in the FACTS.\n\n"
            "MONEY AND FACTS\n"
            "- Never tell the guest that rates change, that their dates are peak or high season, or that a price went "
            "up. Give only the total for their stay, and a per-night price only when the tool provides one. Never "
            "quote a price from memory: only from check_availability or prepare_booking. If they ask the price before "
            "you know their dates, call ask_stay_details.\n"
            "- Every price, date, room count and availability you state must come from the tool results or the "
            "FACTS below. Never calculate your own totals. Never invent the resort's amenities or policies; a resort "
            "fact you do not have: the owner will tell them (give the owner's number).\n"
            "- Never invent charges, bills (electricity, water, utility), deposits, fees or rules that are not in the "
            "FACTS. If a message makes no sense (often a voice note heard wrongly), ask the guest to say it again in a "
            "few words; never guess what they meant.\n"
            "- Rooms and extra beds (the owner's rule): 2 persons per room. Fewer rooms only when the guest asks for "
            "fewer: then up to 3 per room, the 3rd on an extra bed at the FACTS price. When the guest asks for a "
            "number of rooms, call check_availability with that rooms number; the code adds the beds and the cost.\n\n"
            "PAYMENT\n"
            "- Asked how to pay: say only 'You can use UPI, card, net banking or wallet.' Never mention Razorpay, "
            "'secure', the payment page or how it works. Only the advance is paid online; the balance is paid at the "
            "property (say it only if asked).\n"
            "- If they say the link is lost, did not open or they want it again, call send_payment_link. If the hold "
            "ran out, offer to book again (check the dates again). Explain any payment doubt simply and patiently. "
            "A confirmation message arrives here automatically as soon as the payment goes through.\n"
            "- PAYMENT SCREENSHOT: when the guest sends an image, look at it. If it is a payment screenshot (UPI / "
            "bank / Razorpay), note the amount and the status you can see, call my_bookings (it asks Razorpay), and "
            "then: if my_bookings says the booking is paid, say nothing more (the confirmation was already sent); if "
            "still waiting, thank them and say the booking will be confirmed automatically as soon as Razorpay shows "
            "the payment, usually within a minute or two, and that they need not pay again. NEVER confirm a booking "
            "from a screenshot alone: only Razorpay confirms. If the screenshot shows a failed payment, tell them it "
            "did not go through and use send_payment_link. If it is not a payment (a photo, a document), answer "
            "naturally about what it shows or say the team will look at it.\n"
            "- If money was deducted but they got no confirmation, tell them not to pay again, that it normally "
            "shows within a few minutes, and give the owner's number. Never promise a refund.\n\n"
            "CANCELLATION, CHANGES AND REFUNDS\n"
            "- You cannot cancel or change a booking or promise a refund: the owner does it. For a cancellation state "
            "only the policy charge (see GUEST MEMORY).\n\n"
            "NEGOTIATION\n"
            "- Asking about offers, deals or the price is only a question, NOT a request to reduce. Just answer: the "
            "price for their dates (no dates yet: call ask_stay_details). Never say there is no offer, never "
            "say you cannot reduce, never mention discounts, unless they ask for a lower price themselves.\n"
            "- The FIRST time the guest asks for a lower price (reduce, discount, last price, 'kurachu tharamo', "
            "'koodiyo'): in one or two short, warm, respectful lines show it is worth it, with the real good points "
            "from the FACTS (for example the direct beach access and the sea-view AC rooms). NEVER say 'no discount', "
            "'no offer' or 'I cannot reduce', never 'I wish I could help', no owner numbers, no payment-link reminder, "
            "nothing else.\n"
            "- Only when they ask AGAIN: politely say the owner can talk to them directly and give the owner's "
            "number(s). Nothing else.\n\n"
            "GUEST MEMORY\n"
            "- You remember every guest. After the FACTS you get their records (name, earlier bookings with dates, place and status). The chat shown is today's conversation; older chat is kept and you can read it with the recall_old_chat tool.\n"
            "- Look things up only when needed, to save effort: a guest who asks about an earlier booking, a payment, a link, a stay date or something said before gets the real answer (use the records, my_bookings and/or recall_old_chat). A guest who only says hi, makes small talk or jokes gets a short, polite, friendly reply with no lookup.\n"
            "- Earlier bookings: if they ask about one, answer from the records or my_bookings in plain words: dates, place, rooms, whether it is confirmed, how much was paid and what is left to pay at the property. Paid and upcoming: say you can see it and be warm about their coming stay. Past: a warm thanks or welcome back. ON HOLD and still valid: tell them it is waiting for payment and offer the link (send_payment_link). Expired, cancelled or failed: say so kindly and offer to book again. Payment queries are answered politely and respectfully, never defensively.\n"
            "- A guest who already has a booking and asks for rooms or other dates wants a NEW booking: help with it. "
            "Never ask 'is this a new one?'. A request to CHANGE the dates or rooms of an existing booking is NOT a new booking: say in one short line that the owner will help (the owner is alerted automatically). To CANCEL: call my_bookings and tell them in one short line the charge from if_cancelled_today (e.g. '25% charge, Rs X refunded') and that the owner will confirm; for anything outside the policy call talk_to_owner. With only past stays, add at most one warm welcome-back line.\n"
            "- A new booking always takes its dates, rooms and price from what the guest says now and from the tools, never from an earlier booking. Never paste an old payment link: only the send_payment_link tool sends one, and only for a booking still on hold.\n"
            "- Warm and easy: one or two short lines, no jargon.\n"
            "- Never address them by name.\n\n"
            "FACTS\n" + self._facts() + "\n\n" + self._guest_memory(phone, fresh) + "\n\n"
            + self._chosen_note(st, phone) + self._language_note(st, text))

    def _chosen_note(self, st: dict[str, Any], phone: str = "") -> str:
        """The resort the guest already picked, and the stay already known (the room check or the open payment link):
        the AI must never ask for any of it again (owner, 2026-11-22: 'use memory like a human')."""
        stay = self._enquiry_note(st, phone)
        prop = self._known_place(st)
        if prop is None:
            return stay
        if stay:
            return stay
        return (f"CHOSEN RESORT: the guest picked {self._place_label(prop)} and got the booking form. When they "
                "give dates and persons, call check_availability for it right away; never send the form again.\n\n")

    def _enquiry_note(self, st: dict[str, Any], phone: str) -> str:
        """CURRENT ENQUIRY: what the guest already gave and what was offered, from the room check or their open
        payment link. Empty when no stay is known yet."""
        bk = st.get("bk") or {}
        held = [] if bk.get("check_in") or not phone else self._open_hold(phone)
        if bk.get("check_in") and bk.get("check_out") and bk.get("adults"):
            prop = self.repo.get_property(bk["property_id"])
            chosen = bk.get("chosen") or {}
            ids = [p["room_type_id"] for p in chosen.get("mix") or bk.get("mix") or []] or \
                [x for x in (chosen.get("room_type_id"), bk.get("pref")) if x]
            if not ids and prop and (fits := self._fits(prop, bk)):
                ids = [fits[0][0].id]
            ci, co = bk["check_in"], bk["check_out"]
            persons, children, rooms = bk["adults"], bk.get("children", 0), chosen.get("rooms") or bk.get("rooms")
            link = "not sent yet (name and mobile still needed)"
        elif held:
            prop = self.repo.get_property(held[0].property_id)
            ids = [b.room_type_id for b in held]
            ci, co = held[0].check_in.isoformat(), held[0].check_out.isoformat()
            persons, children = sum(b.adults for b in held), sum(b.children for b in held)
            rooms = sum(b.rooms for b in held)
            link = f"SENT and open (guest {held[0].guest_name or '?'}); a change gives a new summary and link"
        else:
            return ""
        names = " + ".join((self.repo.get_room_type(i) or RoomType("", "?")).name for i in ids) or "?"
        place = self._place_label(prop) if prop else "?"
        return (f"CURRENT ENQUIRY (remembered; never ask for it again): {place}, check-in {ci}, check-out {co}, "
                f"{persons} person(s){f' + {children} child(ren)' if children else ''}, {rooms or '?'} room(s), "
                f"room type {names}. Payment link: {link}. Any change to it: call change_enquiry.\n\n")

    # ------------------------------------------------------------------ the conversation

    @staticmethod
    def _session_start(rows: list[ChatMessage], by_time: bool = False) -> int:
        """Index of the first message of the current conversation. 'restart' or a finished booking starts a new one.
        After a pause of SESSION_GAP it depends on what the guest says (owner, 2026-11-20): 'Hi' / 'Hello' starts afresh,
        a new booking request ('rooms for 20 Nov') starts afresh too; anything else ('ok book it', 'what about the
        price you said?') continues the same conversation. by_time=True: any long pause starts afresh (repeat check)."""
        for i in range(len(rows) - 1, -1, -1):
            if rows[i].text == RESTART_MARK:
                return i + 1
            if i and rows[i].at - rows[i - 1].at >= SESSION_GAP:
                first_in = next((m for m in rows[i:] if m.direction == "in"), None)
                if by_time or first_in is None or GREETING.match(first_in.text) or BOOKING_WISH.search(first_in.text):
                    return i
        return 0

    def _session_rows(self, phone: str) -> list[ChatMessage]:
        """Every stored message of the current conversation (up to 200), photos included."""
        rows = self.repo.list_chat(phone, limit=200)
        return rows[self._session_start(rows):]

    def _chatting_now(self, phone: str) -> bool:
        """True when we already talked in this conversation (no long gap): a 'hi' then continues, never restarts."""
        return any(m.direction == "out" for m in self._session_rows(phone))

    def _history(self, phone: str) -> list[ChatMessage]:
        """The last HISTORY real messages of this conversation. Each photo is stored as its own '[photo] ...' row; those
        used to fill the window (a glimpse with 12 photos pushed out the guest's dates and name, 2026-11-18), so a run of
        photos is now one short note."""
        merged: list[ChatMessage] = []
        rows: list[ChatMessage] = []
        for m in self._session_rows(phone):
            if m.direction == "in" and m.text.startswith("[image]") and not getattr(self, "READ_IMAGES", False):
                continue  # ignored images are not shown to the AI (it once replied "checking your payment screenshot")
            if m.direction == "out" and m.text.startswith(("[invoice]", "[template")):
                continue  # file / template log lines: the AI once pasted "[invoice] Invoice-...pdf" into a reply
            if m.direction == "out" and m.text.startswith("[photo] "):
                if rows and rows[-1].text.startswith("[photos sent"):
                    n = int(re.search(r"\d+", rows[-1].text).group(0)) + 1
                    rows[-1] = replace(rows[-1], text=f"[photos sent: {n}]")
                else:
                    rows.append(replace(m, text="[photos sent: 1]"))
                continue
            rows.append(m)
        prev = None
        for m in rows[-HISTORY:]:  # the current conversation only; older chat is fetched on demand
            role = "user" if m.direction == "in" else "assistant"
            text = LINK.sub("[link]", m.text[:700])  # an old payment link must never be copied into a new chat
            if prev is not None and m.at - prev.at >= SESSION_GAP and role == "user":
                text = f"[{(m.at - prev.at).days or 1} day(s) later] {text}"  # the AI knows time passed
            prev = m
            if merged and merged[-1].role == role:
                merged[-1] = ChatMessage(role, merged[-1].content + "\n" + text)
            else:
                merged.append(ChatMessage(role, text))
        while merged and merged[0].role != "user":
            merged.pop(0)
        return merged

    def _greet(self, phone: str, st: dict[str, Any], text: str = "") -> str:
        """Any plain greeting gets the client's own line: 'Hi, How may I assist you?' (no AI call)."""
        self._text(phone, t("greet", st.get("lang", "en")))
        return "greeting"

    @staticmethod
    def _reply_problem(reply: str, guest_text: str, st: dict[str, Any]) -> str:
        """What is wrong with the AI's reply, as an instruction for one rewrite ('' = nothing)."""
        own = st.get("gs", "")  # ml / ta / hi when the guest writes in that script, else English letters
        present = {k for k, rx in SCRIPTS.items() if rx.search(reply)}
        if present:
            want = {"ml": "Manglish", "ta": "Tanglish", "hi": "Hinglish"}.get(own, "Manglish, Tanglish, Hinglish or English, the guest's own language")
            return f"Rewrite that reply using only English letters ({want}). Do not use Malayalam, Tamil or Hindi script."
        if st.get("style") == "en" and (MANGLISH_WORDS.search(reply) or re.search(r"\b(?:milega|chahiye|nahi|kitne|theek|aap|mujhe)\b", reply, re.I)):
            return "Rewrite that reply in plain English only. The guest's latest message is in English."
        talk = "\n".join(line for line in reply.splitlines() if not _LIST_LINE.search(line))
        if st.get("style") == "en" and _classify_latin(_words(talk))[1]:  # Manglish words the short list misses
            return "Rewrite that reply in plain English only. The guest's latest message is in English."
        if HINGLISH_WORDS.search(guest_text) and not MANGLISH_WORDS.search(guest_text):
            st["hing"] = True
        if st.get("hing") and not MANGLISH_WORDS.search(guest_text) and MANGLISH_WORDS.search(reply):
            return "Rewrite that reply in Hinglish (Hindi in English letters), the guest's language. Do not use Malayalam."
        if st.get("style") == "tanglish" and MANGLISH_WORDS.search(reply) and not MANGLISH_WORDS.search(guest_text):
            return "Rewrite that reply in Tanglish (Tamil in English letters), the guest's language. Do not use Malayalam."
        mine = {"hinglish": HINGLISH_WORDS, "tanglish": TANGLISH_WORDS}.get(st.get("style", ""))
        if mine is not None and chatty_words(reply) >= 6 and not mine.search(reply) and latin_style(talk) == "en":
            name = "Hinglish (Hindi" if st["style"] == "hinglish" else "Tanglish (Tamil"
            return (f"Rewrite that reply in {name} in English letters), the guest's language. Do not answer in "
                    "English; words like room, booking and payment are fine.")
        if NO_OFFER.search(reply) and ASKS_REDUCE.search(guest_text):
            return ("Rewrite that reply without saying there is no discount or that you cannot reduce. Instead "
                    "convince the guest warmly that the price is fair, using the real good points of the place.")
        if NO_OFFER.search(reply):
            return ("Rewrite that reply without saying there is no offer or discount and without saying you cannot "
                    "reduce anything: the guest only asked. Just give the price for their dates, or ask for the dates.")
        if (st.get("style") == "manglish" and not st.get("hing") and chatty_words(reply) >= 6
                and latin_style(talk) == "en"):
            return ("Rewrite that reply in Manglish (Malayalam in English letters), the guest's language. Do not "
                    "answer in English; words like room, booking and payment are fine.")
        if ROOM_NUMBERS_SAID.search(reply) and not ROOM_NUMBER_ASKED.search(guest_text):
            return ("Rewrite that reply without any room numbers: room numbers are not the guest's concern. Say only "
                    "how many rooms are available, e.g. 'Yes, 2 rooms available.'")
        return ""  # no word limits of any kind on answers (owner, 2026-11-20)

    def _fix_reply(self, llm, system: str, messages: list[ChatMessage], reply: str, guest_text: str,
                   st: dict[str, Any]) -> str:
        """One rewrite when the reply mixes scripts or says 'no offer'; if the rewrite is no better it is sent as it is."""
        problem = self._reply_problem(reply, guest_text, st)
        if not problem:
            return reply
        ask = ChatMessage.user(problem + " Keep the meaning and the same short style.")
        try:
            fixed = llm.complete(system, [*messages, ChatMessage("assistant", reply), ask], None, MAX_REPLY_TOKENS).text.strip()
        except LLMError:
            fixed = ""
        out = fixed if fixed and not self._reply_problem(fixed, guest_text, st) else reply
        swap = _STRAY_ML.get(st.get("style", ""))
        if swap and not MANGLISH_WORDS.search(guest_text):  # the rewrite kept a Malayalam word: put the guest's own
            out = MANGLISH_WORDS.sub(lambda m: swap.get(m.group(0).lower(), m.group(0)), out)
        return self._latin_only(self._without_room_numbers(out, guest_text), fixed, st)

    @staticmethod
    def _without_room_numbers(reply: str, guest_text: str) -> str:
        """Room numbers are never told unless the guest named one (owner, 2026-11-22: 'is there 2 rooms in kerala
        cottage' got 'Room numbers 103 and 104.'; just 'Yes, 2 rooms available.'). The sentences that still say them
        after the rewrite are dropped; if nothing is left the reply stays as it is."""
        if not ROOM_NUMBERS_SAID.search(reply) or ROOM_NUMBER_ASKED.search(guest_text):
            return reply
        keep = [x for x in re.split(r"(?<=[.?!])\s+|\n+", reply) if x.strip() and not ROOM_NUMBERS_SAID.search(x)]
        return " ".join(keep).strip() or reply

    @staticmethod
    def _latin_only(reply: str, other: str, st: dict[str, Any]) -> str:
        """Native script is NEVER sent (client rule): when the reply still has Malayalam / Tamil / Hindi letters after
        the rewrite, the sentences in English letters are kept (from whichever version has them); if none is left, a
        short ask to say it again goes instead of a native-script line (2026-11-18)."""
        if not INDIC.search(reply):
            return reply
        if other and not INDIC.search(other):
            return other
        keep = [s for s in re.split(r"(?<=[.?!])\s+|\n+", reply) if s.strip() and not INDIC.search(s)]
        return " ".join(keep).strip() or t("say_again", st.get("lang", "en"))

    def _ease_sir(self, phone: str, reply: str) -> str:
        """'sir' only ONCE per conversation (client, 2026-11-17): once any of our messages in this conversation said
        sir or madam, every later one is sent without it ('Yes sir.' -> 'Yes.', 'Hi sir, how' -> 'Hi, how')."""
        rows = self.repo.list_chat(phone, limit=120)
        said = any(m.direction == "out" and SIR.search(m.text) for m in rows[self._session_start(rows):])
        # 'sir' only at the start of a conversation (owner, 2026-11-21: 'just initial only'): once it was said, or
        # after the bot's first 3 messages, it is never said again
        started = sum(1 for m in rows[self._session_start(rows):] if m.direction == "out" and m.source != "staff"
                      and not m.text.startswith("[")) > 3
        if not SIR.search(reply) or not (said or started):
            return reply
        eased = re.sub(rf"^\s*(?:{SIR.pattern})[,.!]?\s*", "", reply, flags=re.IGNORECASE)
        eased = re.sub(rf"\s*,?\s*(?:{SIR.pattern})", "", eased, flags=re.IGNORECASE).strip()
        return eased[:1].upper() + eased[1:] if eased else reply

    @staticmethod
    def _room_in_words(text: str, rooms: list[RoomType]) -> RoomType | None:
        """The room type the guest names by its OWN words ('cottage', 'tiny', 'basic', 'standard'), else None."""
        words = set(re.findall(r"[a-z]{3,}", (text or "").lower()))
        best, score = None, 0
        for rt in rooms:
            names = set(re.findall(r"[a-z]{3,}", rt.name.lower())) - CHOICE_FILLER
            hits = sum(1 for n in names if any(close_word(w, n) for w in words))
            if hits > score:
                best, score = rt, hits
        return best

    def _room_choice_now(self, phone: str, st: dict[str, Any], text: str, in_id: int = 0) -> str | None:
        """The guest picks a room type (by name, by room number, 'only X', 'not X', 'another room'). It is remembered
        and the stay already known (the room check, or the open payment link) is checked again with that room: a new
        'free' line, or a new summary and link. The dates are never asked again (owner, 2026-11-22: 'if you fetched it,
        use memory like a human'). None when the message is not a room choice."""
        if (PHOTO_ASK.search(text) or SEE_ASK.search(text)) and not STRONG_CHOICE.search(text):
            return None  # 'cottage photos venam' is a photo ask
        # a swipe-reply to a room's photo (owner, 2026-11-22): 'this one venam' / 'is this free?' / 'ok' is that room;
        # a price question about it is answered by _price_now
        photo_room = getattr(self, "_quoted_rooms", {}).get(phone)
        if photo_room and (PRICE_Q.search(text) or PRICE_SHORT.match(text) or NOT_WANT.search(text)):
            photo_room = None
        if "?" in text and not STRONG_CHOICE.search(text) and not photo_room:
            return None  # 'is the cottage free?' / 'any other rooms?' are questions (answered elsewhere)
        if (PRICE_Q.search(text) or PRICE_SHORT.match(text)) and not STRONG_CHOICE.search(text):
            return None  # 'whats the cost for standard room' only asks the price (_price_now), it is not a switch
        bk = st.get("bk") or {}
        held = [] if bk.get("check_in") else self._open_hold(phone)
        place_id = bk.get("property_id") or (held[0].property_id if held else "") or st.get("form_place")
        prop = self.repo.get_property(place_id) if place_id else None
        if prop is None:
            return None
        rooms = self.repo.list_room_types(prop.id)
        if len(rooms) < 2:
            return None
        chosen = bk.get("chosen") or {}
        current = {x for x in (chosen.get("room_type_id"), st.get("room_pref")) if x}
        current |= {p["room_type_id"] for p in chosen.get("mix") or []}
        current |= {b.room_type_id for b in held}
        dated = bk if bk.get("check_in") and bk.get("check_out") and bk.get("adults") else ({"check_in": held[0].check_in.isoformat(),
                                                "check_out": held[0].check_out.isoformat(),
                                                "adults": sum(b.adults for b in held),
                                                "children": sum(b.children for b in held)} if held else None)
        free = [rt for rt, _ in self._fits(prop, {**dated, "rooms": 0, "pref": None, "own": phone})] if dated else []
        if not current and free:
            current = {free[0].id}  # the room that was offered (the owner's priority)
        words = _words(text)
        target = None
        if photo_room and (CHOICE_ASK.search(text) or PHOTO_PICK.search(text)) \
                and not self._room_in_words(text, rooms) and not ROOM_NUMBER.search(text):
            target = next((rt for rt in rooms if rt.id == photo_room), None)  # the room in the photo
        if target:
            pass
        elif (m := ROOM_NUMBER.search(text)) and (CHOICE_ASK.search(text) or len(words) <= 4):
            number = m.group(1) or m.group(2)
            target = next((rt for rt in rooms if number in rt.room_list), None)
        named = None if target else self._room_in_words(text, rooms)
        if named and NOT_WANT.search(text):
            # 'I don't want standard rooms' / 'remove standard': the other room type of their enquiry, else the next
            # one by the owner's priority
            others = [rt for rt in rooms if rt.id != named.id]
            target = next((rt for rt in others if rt.id in current), None) or (others[0] if others else None)
        elif named and (CHOICE_ASK.search(text) or len(words) <= 4):
            target = named
        elif not target and OTHER_ROOM.search(text) and ROOM_WORD.search(text) and CHOICE_ASK.search(text):
            # 'I want other room' / 'can I change to another room': the next free room type after the one offered
            target = next((rt for rt in (free if dated else rooms) if rt.id not in current), None)
            if target is None and dated:
                self._text(phone, t("no_other_room", st.get("lang", "en")), repeat_ok=True)
                return "room_choice"
        if target is None:
            return None
        if current == {target.id}:
            if not held:
                return None  # the room already offered: the booking goes on as before
            # their open link is already for that room: said in one line, no new link
            self._text(phone, t("room_picked", st.get("lang", "en"), room=target.name), repeat_ok=True)
            return "room_choice"
        st["room_pref"] = target.id
        if not (bk.get("check_in") or held):
            self._save(phone, st)  # no dates yet: remembered, and the normal flow asks for them
            return None
        st["room_switch"] = True
        if bk.get("check_in") and (who := contact_in(text)):
            # 'cottage, Naseem, same number': the new summary and link straight away
            bk["guest_name"], bk["mobile"] = who[0], phone if who[1] == "same" else who[1]
            st["rebook"] = True
        self._save(phone, st)
        return self._converse(phone, st, text, in_id)

    def _stay_in_code(self, ctx: _Turn, text: str, force: bool = False) -> bool:
        """Dates or persons in this message, with the resort known: the code checks the rooms (or asks only for the
        missing lines) instead of the AI, which once reused old dates and once made up 'fully booked' (2026-11-19).
        True when something was queued for the guest."""
        st, phone = ctx.st, ctx.phone
        bk = st.get("bk") or {}
        mine0 = parse_stay(text, self._today())
        if (not bk.get("check_in") and mine0.get("check_in") and not mine0.get("persons") and not mine0.get("rooms")
                and not BOOKING_WISH.search(text) and any(
                    b.status in ("paid", "checked_in") and b.check_in == mine0["check_in"]
                    for b in self.repo.list_bookings(phone=phone, limit=10))):
            # 'Lets meet on 21 jan' with a confirmed booking on 21 Jan: talk about THAT booking, never a new enquiry
            # (live chat 2026-11-22: it got the form for a new booking)
            return False
        switch = ctx.room_switched = bool(st.pop("room_switch", None))  # the guest picked a room (_room_choice_now)
        changes = switch or bool(mine0.get("rooms") or mine0.get("more_rooms") or mine0.get("less_rooms")
                                 or mine0.get("persons") or mine0.get("children")
                                 or (mine0.get("check_in") and DATE_CHANGE.search(text)))
        if not bk.get("check_in") and changes and not ANOTHER_STAY.search(text) and (held := self._open_hold(phone)):
            # the payment link is out but not paid: a change ('add 1 room', '1 room venda', '6 per', new dates) starts
            # from that enquiry; the rooms are re-checked and a NEW summary and link replace the old ones (owner,
            # 2026-11-21: 'add that extra room and give a new link'). Dates, name and mobile are never asked again.
            first = held[0]
            mobile = re.search(r"Mobile: ([+\d][\d ]{6,})", first.notes or "")
            bk = st.setdefault("bk", {})
            bk.update(property_id=first.property_id, check_in=first.check_in.isoformat(),
                      check_out=first.check_out.isoformat(), adults=sum(b.adults for b in held),
                      children=sum(b.children for b in held), rooms=sum(b.rooms for b in held),
                      guest_name=first.guest_name or "",
                      mobile=try_normalize_phone(mobile.group(1)) if mobile else phone)
            bk.pop("chosen", None)
            st["form_place"] = first.property_id
            st["rebook"] = True
            st.pop("checked", None)
            force = True
        elif not bk.get("check_in") and (SAME_DATES.search(text) or _more_rooms_in(text)) \
                and (last := self._upcoming_booking(phone)) is not None:
            # 'add 1 room also' / 'same date' after a booking: the dates and resort of their latest booking
            # (live chat 2026-11-21: 'same date' got the form again and again)
            bk = st.setdefault("bk", {})
            bk.update(property_id=last.property_id, check_in=last.check_in.isoformat(),
                      check_out=last.check_out.isoformat())
            st["form_place"] = last.property_id
            force = True
        place_id = st.get("form_place") or bk.get("property_id")
        if switch and bk.get("check_in"):
            bk.pop("chosen", None)  # the figures were for the old room type
            force = True
        if not place_id or bk.get("chosen") or (CHANGE_ASK.search(text) and not st.get("rebook") and not switch):
            return False
        if st.get("suggest_rooms") and (is_ack(text) or YES_REPLY.match(text)):
            bk["rooms"] = st.pop("suggest_rooms")  # 'ok' to 'Shall I go ahead with 5 rooms?'
            force = True
        mine = parse_stay(text, self._today())
        if not force and not (mine.get("check_in") or _STAY_LENGTH.search(text) or _WORD_NIGHTS.search(text)
                              or _DAY_ONLY.search(text) or _UNTIL.search(text)
                              or (mine.get("persons") and not bk.get("check_in"))
                              or (mine.get("persons") and len(_words(text)) <= 5
                                  and mine["persons"] != int(bk.get("adults") or 0) + int(bk.get("children") or 0))
                              or mine.get("children") or mine.get("rooms") or mine.get("more_rooms")
                              or mine.get("less_rooms")
                              or SAME_DATES.search(text)):
            # a new number of persons after the check ('7 ppl', 'now we are 7') is re-checked HERE, so the rooms and
            # extra beds always come from the code (live chat 2026-11-20: the AI did that maths wrong)
            return False
        prop = self.repo.get_property(place_id)
        if prop is None:
            return False
        got = self._stay_known(phone, st)
        if got.get("check_in") and got.get("check_out") and got.get("persons"):
            self._tool_check(ctx, {"place": self._place_label(prop), "check_in": got["check_in"].isoformat(),
                                   "check_out": got["check_out"].isoformat(), "adults": got["persons"],
                                   "children": got.get("children", 0), "rooms": got.get("rooms", 0),
                                   "language": st.get("style")})
        elif got or force:
            self._ask_missing(ctx, got)
        if st.pop("rebook", None) and bk.get("check_in") and not ctx.none_found and st.get("checked"):
            self._relink(ctx, bk.get("guest_name") or "", bk.get("mobile") or "same", int(got.get("rooms") or 0))
        self._save(phone, st)
        return bool(ctx.after or ctx.summary_message)

    def _relink(self, ctx: _Turn, name: str, mobile: str, rooms_said: int = 0) -> dict[str, Any]:
        """The change fits: the new summary and payment link straight away (name and mobile are known); the old link
        is closed by create_booking (close_old_holds)."""
        st = ctx.st
        # the rooms of the NEW check ('4 persons' after a 1-room link: at least 2 rooms), never the old count
        rooms_now = int((st.get("bk") or {}).get("rooms") or 0) or rooms_said
        prep = self._tool_prepare(ctx, {"rooms": rooms_now} if rooms_now else {})
        if "error" in prep:
            return prep
        asks = {t(k, lg) for k in ("ask_contact", "contact_again") for lg in ("en", "mn", "tn", "hn")}
        ctx.after = [m for m in ctx.after if not (isinstance(m, str) and m in asks)]
        st.pop("last_summary", None)  # after a change the summary always goes again
        return self._tool_create(ctx, {"guest_name": name, "mobile": mobile})

    def _tool_change(self, ctx: _Turn, a: dict[str, Any]) -> dict[str, Any]:
        """change_enquiry: the AI understood a change in any words; the code applies it to the stay it remembers
        (the room check, or the open payment link) and re-checks: a new 'free' line, or a new summary and link."""
        st, phone = ctx.st, ctx.phone
        self._set_style(st, a)
        bk = st.get("bk") or {}
        held = [] if bk.get("check_in") else self._open_hold(phone)
        if held:
            first = held[0]
            note = re.search(r"Mobile: ([+\d][\d ]{6,})", first.notes or "")
            bk = {"property_id": first.property_id, "check_in": first.check_in.isoformat(),
                  "check_out": first.check_out.isoformat(), "adults": sum(b.adults for b in held),
                  "children": sum(b.children for b in held), "rooms": sum(b.rooms for b in held),
                  "guest_name": first.guest_name or "",
                  "mobile": try_normalize_phone(note.group(1)) if note else phone}
            current = {b.room_type_id for b in held}
        elif bk.get("check_in") and bk.get("check_out") and bk.get("adults"):
            chosen = bk.get("chosen") or {}
            current = {x for x in (chosen.get("room_type_id"), bk.get("pref")) if x}
            current |= {p["room_type_id"] for p in chosen.get("mix") or bk.get("mix") or []}
        else:
            return {"error": "no stay is known yet: if the guest gave dates and persons call check_availability, "
                             "else call ask_stay_details"}
        prop = self.repo.get_property(bk["property_id"])
        if prop is None:
            return {"error": "unknown resort"}
        rooms = self.repo.list_room_types(prop.id)
        if not current and (fits := self._fits(prop, {**bk, "pref": None, "rooms": 0, "own": phone})):
            current = {fits[0][0].id}
        target = None
        if number := str(a.get("room_number") or "").strip().lstrip("#"):
            target = next((rt for rt in rooms if number in rt.room_list), None)
            if target is None:
                return {"error": f"there is no room {number}; the room numbers are in the FACTS"}
        elif wanted := str(a.get("room_type") or "").strip():
            target = next((rt for rt in rooms if rt.name.lower() == wanted.lower()), None) or \
                self._room_in_words(wanted, rooms) or self._room_named(wanted, rooms)
        elif unwanted := str(a.get("remove_room_type") or "").strip():
            gone = next((rt for rt in rooms if rt.name.lower() == unwanted.lower()), None) or \
                self._room_in_words(unwanted, rooms) or self._room_named(unwanted, rooms)
            others = [rt for rt in rooms if gone is None or rt.id != gone.id]
            target = next((rt for rt in others if rt.id in current), None) or (others[0] if others else None)
        elif a.get("other_room"):
            free = [rt for rt, _ in self._fits(prop, {**bk, "pref": None, "rooms": 0, "own": phone})]
            target = next((rt for rt in free if rt.id not in current), None)
            if target is None:
                ctx.after.append(t("no_other_room", st.get("lang", "en")))
                ctx.silent = True
                return {"ok": True, "note": "no other room type is free; the guest is told. Reply exactly [no reply]."}
        if target is not None:
            st["room_pref"] = target.id
        persons = self._int(a, "adults") or int(bk["adults"])
        children = self._int(a, "children") if a.get("children") is not None else int(bk.get("children") or 0)
        rooms_now = int(bk.get("rooms") or 0) or int(((st.get("checked") or "").split("|") + [""] * 5)[4] or 0)
        if self._int(a, "rooms"):
            rooms_now = self._int(a, "rooms")
        elif self._int(a, "add_rooms"):
            rooms_now = (rooms_now or 1) + self._int(a, "add_rooms")
        elif self._int(a, "remove_rooms"):
            rooms_now = max(1, (rooms_now or 1) - self._int(a, "remove_rooms"))
        elif self._int(a, "adults") and self._int(a, "adults") != int(bk["adults"]):
            rooms_now = 0  # new persons: the fewest rooms that fit them
        ci, co = str(a.get("check_in") or bk["check_in"]), str(a.get("check_out") or bk["check_out"])
        if a.get("check_in") and not a.get("check_out"):  # a new check-in keeps the number of nights
            try:
                nights = (date.fromisoformat(bk["check_out"]) - date.fromisoformat(bk["check_in"])).days
                co = (date.fromisoformat(ci) + timedelta(days=nights)).isoformat()
            except ValueError:
                return {"error": "check_in must be YYYY-MM-DD"}
        bk.pop("chosen", None)
        st["bk"] = bk
        if held:
            st.pop("checked", None)  # every change after the link goes out as a fresh check
        out = self._tool_check(ctx, {"place": self._place_label(prop), "check_in": ci, "check_out": co,
                                     "adults": persons, "children": children, "rooms": rooms_now,
                                     "language": st.get("style")})
        if "error" in out:
            return out
        if (held or (bk.get("guest_name") and bk.get("mobile"))) and not ctx.none_found:
            made = self._relink(ctx, bk.get("guest_name") or "", bk.get("mobile") or "same", rooms_now)
            if "error" in made:
                return made
        self._save(phone, st)
        ctx.silent = True
        return {"ok": True, "room_type": (self.repo.get_room_type(st.get("room_pref") or "") or RoomType("", "")).name,
                "note": "the change was checked and the guest is told automatically (a new summary and link when the "
                        "link was already sent). Reply exactly [no reply]."}


    def _other_question(self, text: str) -> bool:
        """other_question() with our town, resort and room type names counted as booking details."""
        names: set[str] = set()
        for p in self.repo.list_properties(active_only=False):
            names |= self._place_words(p)
            names |= {w for rt in self.repo.list_room_types(p.id) for w in re.findall(r"[a-z]+", rt.name.lower())}
        return other_question(text, names)

    def answer_question(self, phone: str, st: dict[str, Any], text: str) -> str:
        """The AI answers only the guest's question; the fixed booking step goes separately (owner, 2026-11-20)."""
        return self._converse(phone, st, text, 0, answer_only=True)

    def _converse(self, phone: str, st: dict[str, Any], text: str, in_id: int = 0,
                  images: list[tuple[str, str]] | None = None, answer_only: bool = False) -> str:
        if self.debounce_s and in_id:  # a second message may follow: wait, and let the newest one answer
            time.sleep(self.patience_s if looks_unfinished(text) else self.debounce_s)
            if self.repo.last_chat_id(phone, "in") != in_id:
                return "superseded"
        st["turn"] = st.get("turn", 0) + 1
        ctx = _Turn(phone, st)
        out_before = self.repo.last_chat_id(phone, "out")  # to know at the end whether anything at all went out
        ctx.text = text
        messages = self._history(phone)
        if images and messages and messages[-1].role == "user":
            messages[-1].images = list(images)  # the AI sees the picture this turn only; the chat keeps just "[image]"
        before = st.get("style")
        # the language comes from everything the guest wrote since our last reply ('avde engane und' + 'Alleppey')
        # only when the newest message is too short to tell (1-2 words); otherwise the newest message alone decides
        latest = (messages[-1].content if messages and messages[-1].role == "user" and len(_words(text)) <= 2
                  else text)
        system = self._system_prompt(st, phone, latest, fresh=len(messages) <= 1)
        if before and st.get("style") != before and messages and messages[-1].role == "user":
            # the guest switched language: say it again right on their message, the latest one decides
            note = STYLE_LABELS.get(st["style"], "English")
            messages[-1].content += f"\n\n[Reply ONLY in {note}. Ignore the language of earlier messages.]"
        quote = getattr(self, "_quotes", {}).pop(phone, None)
        if quote and messages and messages[-1].role == "user":
            messages[-1].content += "\n\n" + quote
        if answer_only and messages and messages[-1].role == "user":
            messages[-1].content += ("\n\n[The booking steps for this message are sent automatically. Answer ONLY "
                                     "the guest's question in one or two short, friendly lines. Do not ask for "
                                     "dates, location, persons, name or mobile, and do not say whether rooms are "
                                     "available.]")
            if is_ack(text):  # 'ok' / 'thanks': a polite word back, never an answer to an older message (2026-11-21)
                messages[-1].content += ("\n[The guest only said this short word. Reply in a few polite words to it "
                                         "only. Do not answer older messages and do not add any new information.]")
        reply = ""
        coded = False if answer_only else self._stay_in_code(ctx, text)
        if coded and not ctx.room_switched and self._other_question(text):
            # '25-27 Dec, 3 people, is parking free?': the AI answers the parking question first, then the room
            # check's messages go (owner, 2026-11-20: answer like an agent, never drop a question)
            self.answer_question(phone, st, text)
        # answering only: tools that just READ (the guest's bookings, earlier chat), never ones that send steps
        tools = [x for x in TOOLS if x.name in ("my_bookings", "recall_old_chat")] if answer_only else TOOLS
        try:
            if coded:
                reply = NO_REPLY  # the stay details were handled in code: rooms checked or the missing lines asked
            elif self._llm is None:
                raise LLMError("none", "no LLM configured")
            if not coded:
                llm = self._llm()
                response = llm.complete(system, messages, tools, MAX_REPLY_TOKENS)
            for _ in range(0 if coded else MAX_TOOL_ROUNDS):
                if not response.tool_calls or ctx.handed_off:
                    break
                messages.append(response.to_message())
                for call in response.tool_calls:
                    content, is_error = self._run_tool(ctx, call)
                    messages.append(ChatMessage.tool_result(call.id, content, is_error=is_error))
                response = llm.complete(system, messages, tools, MAX_REPLY_TOKENS)
            if not coded and not response.tool_calls:
                reply = self._fix_reply(llm, system, messages, response.text.strip(), text, st)
                if not GREETING.match(text) and reply.strip().rstrip(".!?").lower() in {
                        t("greet", lg).rstrip(".!?").lower() for lg in ("en", "mn", "tn", "hn")}:
                    reply = NO_REPLY  # 'Any places nearby?' once got 'Hi, How may I assist you?' (Sarvam, 2026-11-20)
                if SOLD_OUT_CLAIM.search(reply) and not ctx.none_found and not answer_only:
                    # 'today is fully booked' with no room check behind it (live chat 2026-11-19, rooms were free)
                    reply = NO_REPLY
                    self._stay_in_code(ctx, text, force=True)
        except LLMError as exc:
            log.warning("AI answer failed: %s", exc)
        except Exception as exc:  # a missing key or a bad tool must never leave the guest unanswered
            log.exception("AI unavailable: %s", exc)
        self._save(phone, st)
        if ctx.handed_off:
            return "handoff"
        if answer_only:  # only the AI's answer: no fixed steps, no fallbacks (the caller sends those)
            if not reply or NO_REPLY in reply.lower() or SOLD_OUT_CLAIM.search(reply):
                return "quiet"
            clean = LOG_LINE.sub("", self._without_name(reply.replace("—", "-").replace("**", "*"), "")).strip()
            keep = [x for x in re.split(r"(?<=[.?!])\s+|\n+", clean) if x.strip() and not CLOSING.match(x.strip())
                    and not self._own_location_question(x) and not self._asks_location(x)
                    and not self._own_dates_ask(st, x)[0]
                    and not CONTACT_ASK.search(x) and not ROOM_FACT.search(x)]
            clean = self._negotiation_guard(phone, text, self._code_facts_only(
                st, text, no_unasked_prices(" ".join(keep), text)))
            if not clean:
                if any(self._own_dates_ask(st, x)[0] for x in re.split(r"(?<=[.?!])\s+|\n+", reply)):
                    self._strict_form(ctx, "")  # it only asked for the dates: the owner's form, never silence
                    self._save(phone, st)
                    return "answered"
                return "quiet"
            # the code's rooms sentence may go again when the guest asks about rooms ('how many rooms?' after it went
            # once got silence, 2026-11-20); everything else follows the client rule: nothing is said twice
            self._text(phone, self._ease_sir(phone, clean), repeat_ok=self._rooms_line_in(st, clean))
            return "answered"
        if not ctx.summary_message and (st.get("bk") or {}).get("check_in") and (contact := contact_in(text)):
            # the guest sent their number ('same number', '9876543210', 'Rahul 98470 12345') after rooms were found but
            # the AI did not book: the code does, so the summary and the payment link go (client, 2026-11-18)
            done = self._tool_create(ctx, {"guest_name": contact[0], "mobile": contact[1], "language": st.get("style")})
            if "error" not in done:
                reply = NO_REPLY
            elif "ONLY the guest's name" in done["error"]:
                reply = t("ask_name", st.get("lang", "en"))
            self._save(phone, st)
        if (not (ctx.after or ctx.summary_message or ctx.pay_message or ctx.photos) and (not reply or NO_REPLY in reply.lower())
                and st.get("form_place") and not (st.get("bk") or {}).get("check_in") and not (is_ack(text) and "?" not in text)):
            # The form is open and the AI did nothing (free models sometimes stay silent; a guest once got no reply to
            # 'dec12 nn verum pinne 2 disam ... 3 per und', 2026-11-19). The code carries on: complete details ->
            # rooms are checked; anything else -> the form with only the missing lines. Never silence.
            got = self._stay_known(phone, st)
            prop = self.repo.get_property(st["form_place"])
            if prop and got.get("check_in") and got.get("check_out") and got.get("persons"):
                self._tool_check(ctx, {"place": self._place_label(prop), "check_in": got["check_in"].isoformat(),
                                       "check_out": got["check_out"].isoformat(), "adults": got["persons"],
                                       "children": got.get("children", 0), "rooms": got.get("rooms", 0),
                                       "language": st.get("style")})
            else:
                self._ask_missing(ctx, got)
            reply = NO_REPLY
            self._save(phone, st)
        if (not (ctx.after or ctx.summary_message or ctx.pay_message or ctx.photos) and reply and NO_REPLY in reply.lower()
                and (outcome := self._never_silent(ctx, text))):  # the AI works but chose silence (an outage gets the apology)
            return outcome
        queued = bool(ctx.after or ctx.summary_message or ctx.pay_message or ctx.photos)
        if not queued and not ctx.handed_off and NO_REPLY in reply.lower() and (self.repo.last_outbound_text(phone) or "") \
                in {t("rooms_none", lg) for lg in ("en", "mn", "tn", "hn")}:
            # 'not available' was the last thing said and the guest writes on (often their name and number): ask for
            # other dates with the form, never silence and never the same 'not available' twice (stress test, 2026-11-18)
            ctx.after.append(t("form", st.get("lang", "en")))
            queued = True
        quiet = NO_REPLY in reply.lower()
        if quiet and not queued and GREETING.match(text):  # a mid-chat "hi" always gets an answer
            reply, quiet = t("greet", st.get("lang", "en")), False  # always the owner's greeting
        if (quiet and not queued and BOOKING_WISH.search(text) and not NOT_NOW.search(text)
                and not (st.get("bk") or {}).get("check_in")):
            # 'need a room' / 'resort venam' left unanswered by the AI: the next fixed step goes (client's flow)
            if st.get("form_place") or self._named_place(text):  # else the location question first (owner flow)
                self._tool_form(ctx, {"place": self._place_label(self.repo.get_property(st["form_place"]))
                                      if st.get("form_place") else text})
            else:
                self._tool_location(ctx, {})
            self._save(phone, st)
            queued = True
        if quiet and not queued:
            return "quiet"
        if not reply and not queued:
            self._text(phone, t("ai_down", st.get("lang", "en")))
            now = self.clock.now()
            last = getattr(self, "_down_alerted", None)
            if last is None or now - last >= timedelta(minutes=30):  # one staff alert per outage, not per message
                self._down_alerted = now
                self.alert_staff(f"Bot could not answer a message from {phone}: {text[:200]} (AI unavailable; further "
                                 "failures in the next 30 min are not alerted)", st.get("bk", {}).get("property_id"))
            return "ai_down"
        # the client's order: the AI's short line, then the fixed messages (form / glimpse / ask name), then the
        # enquiry summary, then the payment link
        if (ctx.silent or any(isinstance(m, str) and m.startswith(FORM_START) for m in ctx.after)) \
                and not (reply and not quiet and self._other_question(text)):
            quiet = True  # a fixed step goes: the AI's own line only when it answers a real question (2026-11-20)
        elif reply and not quiet and (ctx.silent or ctx.after):
            # it answers the guest's question; anything it says about rooms, dates or contact the fixed steps say
            reply = " ".join(x for x in re.split(r"(?<=[.?!])\s+|\n+", reply) if x.strip()
                             and not ROOM_FACT.search(x)
                             and not CONTACT_ASK.search(x) and not self._own_dates_ask(st, x)[0])
            quiet = not reply
        if reply and not quiet:
            guest = self.repo.get_guest(phone)
            clean = self._without_name(reply.replace("—", "-").replace("**", "*"), guest.name if guest else "")
            clean = LOG_LINE.sub("", clean).strip()  # never "[invoice] ...", "[photos sent: 3]" or "[image]" to a guest
            parts = [x for x in re.split(r"(?<=[.?!])\s+|\n+", clean) if x.strip()]
            if len(parts) > 1 and CLOSING.match(parts[-1].strip()):
                clean = " ".join(parts[:-1])  # no 'Anything else?' tails (owner: very few words)
            clean = self._negotiation_guard(phone, text, self._code_facts_only(st, text, clean))
            if self._known_place(st) is not None:  # the resort is picked: the AI never asks the location again
                clean = " ".join(x for x in re.split(r"(?<=[.?!])\s+|\n+", clean)
                                 if x.strip() and not self._asks_location(x))
            if not clean.strip():
                pass  # nothing left of the AI's reply: the safety net at the end of this turn sends the next step
            elif self._own_location_question(clean):  # the AI asked "which location" in its own words: the owner's
                clean = t("ask_location", st.get("lang", "en"), towns=self._towns_text())  # wording only
                if clean == (self.repo.last_outbound_text(phone) or ""):
                    clean = self._towns_text() + " ?"  # just asked: the short re-ask, never silence (2026-11-21)
                self._text(phone, clean, repeat_ok=True)
            elif (dates := self._own_dates_ask(st, clean))[0]:  # it asked for dates / persons itself: the owner's form
                self._strict_form(ctx, dates[1])
            elif clean := self._no_stale_payment(phone, text, self._no_repeat_ask(phone, self._tool_prices_only(ctx, no_unasked_prices(clean, text)))):
                # the code's rooms sentence may repeat when asked again (2026-11-20); nothing else is said twice
                self._text(phone, self._ease_sir(phone, clean), repeat_ok=self._rooms_line_in(st, clean))
                if (m := ROOMS_OFFER_ASK.search(clean)) and (st.get("bk") or {}).get("check_in"):
                    # 'Shall I book 3 rooms?' in the AI's own words: 'Yes' / 'Ok' then re-checks with that many rooms
                    # in code (live chat 2026-11-20: 'Yes' and 'Ok' got silence)
                    st["suggest_rooms"] = int(m.group(1))
                    self._save(phone, st)
        if ctx.photos:
            self._send_photos(phone, ctx.photos)
        asked = {t("ask_contact", lang) for lang in ("en", "mn")}
        form_sent = False
        for message in ctx.after:
            if isinstance(message, str) and message.startswith(FORM_START):
                if form_sent:
                    continue  # one form per turn: never the full form and then the short one (live chat, 2026-11-19)
                form_sent = True
            if isinstance(message, tuple):  # (caption, photos): one room's photos, its name on the first
                self._send_photos(phone, message[1], message[0])
                continue
            if isinstance(message, list):  # a batch of photos in its place in the order
                self._send_photos(phone, message)
                continue
            if ctx.summary_message and message in asked:  # they already gave name and mobile in this turn
                continue
            forms = {t("form", lang) for lang in ("en", "mn")}
            if message in forms:  # the form may go again for a missing part (client, 2026-11-18), never twice in a row
                if not self._form_was_last(phone):
                    self._text(phone, message, repeat_ok=True)
                continue
            # the code's rooms sentence after a change ('2 rooms mathi' back again) and the name ask after new photos
            # may go again; everything else follows 'nothing said twice' (owner, 2026-11-21)
            self._text(phone, message, repeat_ok=message == st.get("rooms_line") or message in {
                t(k, lg) for k in ("ask_contact", "rooms_ok") for lg in ("en", "mn", "tn", "hn")})
        if ctx.summary_message and ctx.summary_message != st.get("last_summary"):  # never the same summary twice
            st["last_summary"] = ctx.summary_message
            self._save(phone, st)
            self._text(phone, ctx.summary_message, repeat_ok=True)  # a changed enquiry may repeat an older summary
        if ctx.pay_message:  # the guest asked for the link again: the only message that may repeat
            self._text(phone, ctx.pay_message, repeat_ok=ctx.resend)
            return "payment_link"
        if not ctx.handed_off and self.repo.last_chat_id(phone, "out") == out_before \
                and (outcome := self._never_silent(ctx, text)):
            # the safety net (owner, 2026-11-21): NOTHING went out for this message (every AI sentence was filtered,
            # or a repeat was held back), so the open step / a short answer goes, chosen in code
            return outcome
        return "answered"

    # ------------------------------------------------------------------ tools

    def _run_tool(self, ctx: _Turn, call: ToolCall) -> tuple[str, bool]:
        handlers = {"ask_location": self._tool_location, "ask_stay_details": self._tool_form,
                    "send_photos": self._tool_photos,
                    "check_availability": self._tool_check, "prepare_booking": self._tool_prepare,
                    "create_booking": self._tool_create, "my_bookings": self._tool_bookings,
                    "send_payment_link": self._tool_resend, "recall_old_chat": self._tool_recall,
                    "talk_to_owner": self._tool_owner, "change_name": self._tool_change_name,
                    "change_enquiry": self._tool_change}
        handler = handlers.get(call.name)
        if handler is None or call.arguments_error:
            return json.dumps({"error": call.arguments_error or f"unknown tool {call.name}"}), True
        try:
            result = handler(ctx, call.arguments)
        except Exception as exc:
            log.exception("Tool %s failed", call.name)
            return json.dumps({"error": f"internal error: {exc}"}), True
        return json.dumps(result, ensure_ascii=False), "error" in result

    @staticmethod
    def _int(args: dict[str, Any], key: str, default: int = 0) -> int:
        try:
            return int(args.get(key) or default)
        except (TypeError, ValueError):
            return default

    def _find_property(self, place: str) -> Property | None:
        props = self._places()
        if len(props) == 1:
            return props[0]
        query = re.sub(r"[^a-z0-9 ]", " ", latin_places(place or "").lower())
        best, best_score = None, 0
        for p in props:
            said = set(re.findall(r"[a-z]{4,}", query))
            score = sum(1 for w in self._place_words(p) if w[:5] in query or any(close_word(x, w) for x in said))
            if score > best_score:
                best, best_score = p, score
        return best

    def _mix(self, prop: Property, bk: dict[str, Any], wanted: int = 0) -> list[tuple[RoomType, int]] | None:
        """Rooms over two or more room types by the owner's priority, only when the top room type cannot take the
        whole group itself; None otherwise (then one room type is offered as before)."""
        ci, co = date.fromisoformat(bk["check_in"]), date.fromisoformat(bk["check_out"])
        now = self.clock.now()
        free = [(rt, self.repo.free_rooms(rt, ci, co, now, bk.get("own", ""))) for rt in self._types(prop, bk)]
        mix = av.mixed_rooms(free, bk["adults"], bk["children"], wanted, bool(prop.extra_bed_rate))
        if not mix or len(mix) < 2:
            return None
        fits = self._fits(prop, bk)
        if fits and not wanted and fits[0][1][0] < sum(n for _, n in mix):
            return None  # one room type needs fewer rooms (10 guests: 3 Family Suites, not 2 Deluxe + 2 Family)
        return mix

    @staticmethod
    def _rooms_of(rt: RoomType, bk: dict[str, Any], options: list[dict[str, Any]]) -> int:
        """How many rooms of this type the guest is offered (a split group: its share; else the quoted count)."""
        for part in bk.get("mix") or []:
            if part["room_type_id"] == rt.id:
                return part["rooms"]
        return int(options[0].get("quoted_for_rooms") or 1) if options else 1

    @staticmethod
    def _mix_label(mix: list[tuple[RoomType, int]]) -> str:
        return " + ".join(f"{n} {rt.name}" for rt, n in mix)

    def _types(self, prop: Property, bk: dict[str, Any]) -> list[RoomType]:
        """The room types a check may use: the owner's priority order, or ONLY the one the guest picked (owner,
        2026-11-22: 'I only need cottage' must give the cottage, never the top-priority room again)."""
        rooms = self.repo.list_room_types(prop.id)
        picked = [rt for rt in rooms if rt.id == bk.get("pref")]
        return picked or rooms

    def _fits(self, prop: Property, bk: dict[str, Any]) -> list[tuple[RoomType, list[int]]]:
        ci, co = date.fromisoformat(bk["check_in"]), date.fromisoformat(bk["check_out"])
        now = self.clock.now()
        out = []
        beds_ok = bool(prop.extra_bed_rate)
        for rt in self._types(prop, bk):
            plan = av.plan_rooms(rt, bk["adults"], bk["children"],
                                 self.repo.free_rooms(rt, ci, co, now, bk.get("own", "")),
                                 int(bk.get("rooms") or 0), beds_ok)
            if plan:
                out.append((rt, [plan[0]]))
        return out

    @staticmethod
    def _beds_for(prop: Property, rt: RoomType, adults: int, children: int, rooms: int) -> int:
        """Extra beds when more persons than the normal 2 per room share the rooms (only when the guest asked)."""
        if not prop.extra_bed_rate:
            return 0
        return max(0, adults + children - av.base_per_room(rt) * rooms)

    @staticmethod
    def _without_name(text: str, name: str) -> str:
        """'Rahul sir' / 'sir, Rahul' / ', Rahul' -> 'sir' (any script): the guest is addressed as sir, never by name."""
        for part in (x for x in re.split(r"[\s,.]+", name or "") if len(x) >= 2):
            q = re.escape(part)
            text = re.sub(rf"(?<!\S){q}\s+(sir|madam)\b", r"\1", text, flags=re.IGNORECASE)
            text = re.sub(rf"\b(sir|madam),?\s+{q}(?=[\s,.!?]|$)", r"\1", text, flags=re.IGNORECASE)
            text = re.sub(rf",\s*{q}(?=[\s,.!?]|$)", "", text, flags=re.IGNORECASE)
        return text

    @staticmethod
    def _set_style(st: dict[str, Any], a: dict[str, Any]) -> None:
        """The AI reports the guest's language style; the fixed messages (payment link, confirmations) follow it."""
        style = a.get("language")
        if style not in STYLES:
            return
        if st.get("gs") in LANG_NAMES:  # the guest writes in this script: that decides, whatever the AI called it
            st["lang"], st["style"] = st["gs"], LATIN_STYLE[st["gs"]]
            return
        if st.get("style") in STYLE_LANG:  # the code already read the guest's latest message: English stays English
            st["lang"] = STYLE_LANG[st["style"]]
            return
        if style in LANG_NAMES:  # a script label for a guest who writes in English letters
            style = {"ml": "manglish", "ta": "tanglish", "hi": "hinglish"}[style]
        st["lang"], st["style"] = STYLE_LANG[style], style

    def _tool_form(self, ctx: _Turn, a: dict[str, Any]) -> dict[str, Any]:
        """The client's booking form: Check in / Check out / Number of persons, as a fixed message."""
        prop = self._find_property(a.get("place", ""))
        if prop is None:
            return {"error": "that resort has no online booking: give the owner's numbers from the FACTS",
                    "open_resorts": [self._place_label(p) for p in self._places()]}
        bk = ctx.st.get("bk") or {}
        known = bk if bk.get("check_in") and bk.get("adults") else self._held_stay(ctx.phone)
        if known and not ANOTHER_STAY.search(ctx.text or ""):
            # the dates and persons are already known (live chat 2026-11-22: after the payment link, 'Yes' to 'should
            # I look up the cost?' got the form, then 'same date' and 'same persons' got it again)
            return {"error": f"the stay is already known: {known['check_in']} to {known['check_out']}, "
                             f"{known['adults']} adults, {known.get('children') or 0} children, "
                             f"{known.get('rooms') or 0} rooms. Never ask for the dates or persons again. For a price "
                             "call check_availability with these values; for a change call change_enquiry."}
        ctx.st["form_place"] = prop.id
        if t("form", "en") not in ctx.after:
            ctx.after.append(t("form", ctx.st.get("lang", "en")))
        ctx.silent = True
        return {"ok": True, "resort": self._place_label(prop),
                "note": "the form is sent automatically; reply exactly [no reply]"}

    @staticmethod
    def _ordinal_date(d: date) -> str:
        """15th Nov 26."""
        suffix = "th" if 10 <= d.day % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(d.day % 10, "th")
        return f"{d.day}{suffix} {d.strftime('%b')} {d.strftime('%y')}"

    @staticmethod
    def _short_time(hhmm: str) -> str:
        """06:00 -> 6am, 18:30 -> 6:30pm."""
        try:
            hour, minute = (int(x) for x in hhmm.split(":")[:2])
        except ValueError:
            return hhmm
        return f"{hour % 12 or 12}{f':{minute:02d}' if minute else ''}{'am' if hour < 12 else 'pm'}"

    @staticmethod
    def _show_mobile(number: str) -> str:
        """+918088678167 -> 80886 78167."""
        digits = display_phone(number)
        return f"{digits[:5]} {digits[5:]}" if len(digits) == 10 and digits.isdigit() else digits

    def _price(self, prop: Property, rt: RoomType, ci: date, co: date, rooms: int, beds: int,
               early: bool) -> tuple[av.Quote, int, int, int]:
        """(quote, early check-in fee, total, advance). The advance is the property's share of the full total."""
        q = av.quote(prop, rt, self.repo.list_rate_rules(rt.id), ci, co, rooms, beds)
        fee = prop.early_checkin_rate * rooms if early else 0
        total = q.total + fee
        return q, fee, total, round(total * prop.advance_pct / 100) if fee else q.advance

    def _tool_check(self, ctx: _Turn, a: dict[str, Any]) -> dict[str, Any]:
        st = ctx.st
        ctx.priced = True
        self._set_style(st, a)
        prop = self._find_property(a.get("place", ""))
        if prop is None:
            return {"error": "unknown place", "places": [self._place_label(p) for p in self._places()]}
        try:
            ci = date.fromisoformat(str(a.get("check_in", "")))
            nights = self._int(a, "nights")
            co = date.fromisoformat(str(a["check_out"])) if a.get("check_out") else ci + timedelta(days=nights)
        except (ValueError, KeyError):
            return {"error": "check_in and check_out must be YYYY-MM-DD, or give nights"}
        today = self._today()
        year_note = ""
        if (today - ci).days > 14:  # "25 Dec" given with a past year: a booking date is never in the past, take the next one
            for years in (1, 2):
                try:
                    moved = ci.replace(year=ci.year + years)
                except ValueError:
                    continue
                if moved >= today:
                    co, ci = co + (moved - ci), moved
                    year_note = f" The guest gave no future year, so the dates were taken as {ci.isoformat()} to {co.isoformat()}."
                    break
        if problem := av.check_dates(ci, co, today):
            if ci < today:  # a date that just passed: told in code, the dates are asked again (owner, 2026-11-21)
                ctx.after.append(t("past_date", st.get("lang", "en"), day=f"{ci.day} {ci.strftime('%b')}"))
                ctx.silent = True
                return {"error": "the check-in date has passed; the guest is told automatically. Reply exactly "
                                 "[no reply]."}
            return {"error": problem}
        said = self._recent_guest_text(ctx.phone)
        got = self._stay_known(ctx.phone, st)
        if "persons" not in got and not _PERSONS_LOOSE.search(said):
            # the number of persons is never assumed (it once took 2 for 'one more room for my friends')
            self._ask_missing(ctx, got)
            return {"error": "the guest has not said how many persons; the form was sent to ask it. Reply exactly "
                             "[no reply]."}
        if got.get("check_in") == ci and "check_out" not in got and not _STAY_LENGTH.search(said):
            # 'dec 20 nu 2 per': the AI made up a one-night stay once. The check-out is never assumed: the form asks
            # only for what is missing, and something always goes out (a guest once got silence, 2026-11-19)
            self._ask_missing(ctx, got)
            return {"error": "the guest has not given the check-out date; the form was sent to ask it. Reply exactly "
                             "[no reply]."}
        adults, children = self._int(a, "adults"), self._int(a, "children")
        if adults < 1:  # no fixed maximum (owner, 2026-11-20): the room check says what the free rooms can hold
            return {"error": "at least 1 adult is needed"}
        # "own": this guest's unpaid holds count as free rooms (a new summary replaces their open link)
        bk = {"property_id": prop.id, "check_in": ci.isoformat(), "check_out": co.isoformat(),
              "adults": adults, "children": children, "rooms": self._int(a, "rooms"), "own": ctx.phone}
        st["bk"] = bk
        wanted = self._int(a, "rooms")
        if wanted and (least := av.fewest_rooms(self.repo.list_room_types(prop.id), adults, children)) \
                and wanted < least:
            # 8 persons in 2 rooms: at most 3 per room (2 + 1 extra bed), so the guest is told and the check goes on
            # with the fewest rooms that work, extra beds included. Never a dead end (live chat 2026-11-20).
            ctx.after.append(t("min_rooms", st.get("lang", "en"), persons=adults + children, rooms=least))
            wanted = least
            bk["rooms"] = least
        if (pref := st.get("room_pref")) and (prt := self.repo.get_room_type(pref)) and prt.property_id == prop.id:
            # the room type the guest picked is remembered (owner, 2026-11-22); if it cannot take them on these
            # dates they are told plainly and the owner's priority goes on as before
            bk["pref"] = pref
            if not self._fits(prop, bk):
                ctx.after.append(t("room_pick_no", st.get("lang", "en"), room=prt.name, persons=adults + children))
                bk.pop("pref", None)
                st.pop("room_pref", None)
        leads.save_lead(self.repo, ctx.phone, self.clock.now(), property_id=prop.id, check_in=ci.isoformat(),
                        check_out=co.isoformat(), adults=adults, children=children, rooms=wanted, room_type="",
                        stage="asked", booking_id="")
        options, offered = [], []
        mix = self._mix(prop, bk, wanted)
        if mix:
            # the top-priority room type cannot take the whole group: its free rooms first, then the next room type
            # (owner, 2026-11-19: 4 guests, 1 Basic left -> 1 Basic + 1 Standard)
            parts = []
            beds_left = max(0, adults + children - sum(av.base_per_room(rt) * n for rt, n in mix)) \
                if prop.extra_bed_rate else 0
            for rt, n in mix:
                beds = min(beds_left, n * max(0, rt.max_occupancy - av.base_per_room(rt)))
                beds_left -= beds
                q = av.quote(prop, rt, self.repo.list_rate_rules(rt.id), ci, co, n, beds)
                parts.append({"room_type_id": rt.id, "rooms": n, "beds": beds, "total": q.total,
                              "advance": q.advance})
            bk["mix"] = parts
            offered = [rt for rt, _ in mix]
            total, advance = sum(p["total"] for p in parts), sum(p["advance"] for p in parts)
            first = av.quote(prop, mix[0][0], self.repo.list_rate_rules(mix[0][0].id), ci, co, 1)
            options.append({"room_type": self._mix_label(mix), "rooms_possible": [sum(n for _, n in mix)],
                            "quoted_for_rooms": sum(n for _, n in mix), "total_for_stay": total,
                            "advance_now": advance, "balance_at_property": total - advance,
                            "price_per_night": round(first.total / max(1, first.nights))})
            if mix_beds := sum(p["beds"] for p in parts):
                options[-1]["extra_beds"] = mix_beds
        for rt, rooms_ok in ([] if mix else self._fits(prop, bk)):
            offered.append(rt)
            rooms = wanted if wanted in rooms_ok else rooms_ok[0]
            beds = self._beds_for(prop, rt, adults, children, rooms)
            q = av.quote(prop, rt, self.repo.list_rate_rules(rt.id), ci, co, rooms, beds)
            option = {"room_type": rt.name, "rooms_possible": rooms_ok, "quoted_for_rooms": rooms,
                      "total_for_stay": q.total, "advance_now": q.advance, "balance_at_property": q.total - q.advance}
            if beds:
                option["extra_beds"] = beds
            if len({rate for _, rate in q.per_night}) == 1:
                option["price_per_night"] = q.per_night[0][1]  # per room, only when every night costs the same
            options.append(option)
        out: dict[str, Any] = {"place": self._place_label(prop), "check_in": ci.isoformat(),
                               "check_out": co.isoformat(), "nights": (co - ci).days, "adults": adults,
                               "children": children, "options": options}
        out["note"] = ("Give the guest the total for their stay. Never mention that rates change, seasons, peak or high "
                       "season dates, or a price increase." + year_note)
        if not options:
            ctx.none_found = True
            out["available"] = False
            out["reason"] = "no room type is free for this many guests on these dates (sold out or too many guests)"
            key = f"none|{prop.id}|{ci.isoformat()}|{co.isoformat()}|{adults + children}"
            if st.get("checked") != key:  # the client's flow says plainly whether rooms are free
                st["checked"] = key
                ctx.after.append(self._no_room_answer(st, prop, bk, ci, co, adults, children, wanted))
                ctx.silent = True
                out["note"] += " The guest is told automatically. Reply exactly [no reply]."
            if alt := st.get("alt_dates"):
                out["free_dates"] = alt  # the AI may answer 'which dates are free?' from these
            return out
        if wanted and not any(wanted in o["rooms_possible"] for o in options):
            out["note"] += (f" {wanted} room(s) is not possible for {adults + children} guest(s); "
                            "rooms_possible lists the counts that work.")
        out["available"] = True
        # The owner's priority (2026-11-17): offer ONE room type, the top-priority one that is free and fits. The guest
        # is never asked to pick; one who names another room type still gets it through prepare_booking.
        options = out["options"] = options[:1]
        # The client's flow, once per set of dates: "rooms available" -> the glimpse -> the general photos -> the offered
        # room's photos -> ask name and mobile.
        # the rooms the guest asked for are part of the check: '2 rooms mathi' after 3 were quoted is a NEW check;
        # asking for the number already quoted ('1 room') changes nothing
        quoted_now = int((options[0] if options else {}).get("quoted_for_rooms") or 1)
        key = f"{prop.id}|{ci.isoformat()}|{co.isoformat()}|{adults + children}|{quoted_now}|{bk.get('pref') or ''}"
        lang = st.get("lang", "en")
        # a room type the guest just picked: said first, in code ('Sure, 2 Bedroom Tiny Kerala Cottage is free')
        picked = (t("room_picked", lang, room=offered[0].name)
                  if bk.get("pref") and offered and (st.get("checked") or "").split("|")[5:6] != [bk["pref"]] else "")
        same_dates = (st.get("checked") or "").split("|")[:3] == key.split("|")[:3]
        # whose photos go: ONLY the one room offered (top priority AND free). Both room types only when the group is
        # split over two (1 Basic + 1 Standard). A live chat got Basic + Standard photos together (owner, 2026-11-19).
        shown = offered if mix else offered[:1]
        # the ONE sentence about rooms and extra beds for these dates and persons, always worked out here in code;
        # the AI never does this maths (live chat 2026-11-20: '7 people ... 2 rooms with 1 extra bed in each')
        quoted = int(options[0].get("quoted_for_rooms") or 1)
        if beds := options[0].get("extra_beds"):
            # owner, 2026-11-21: always say plainly how many rooms AND extra beds ('2 rooms + 1 extra bed')
            rooms_line = t("rooms_beds", lang, persons=adults + children, rooms=quoted, beds=beds,
                           rate=f"{prop.extra_bed_rate:,}")
        elif (not wanted and prop.extra_bed_rate
              and (fewer := av.fewest_rooms(self.repo.list_room_types(prop.id), adults, children))
              and fewer < quoted):
            # owner, 2026-11-20: before the photos say how many rooms are needed, or that 3 can share one room
            # with an extra bed at the extra-bed rate; 'one room' from the guest then re-checks with extra beds
            rooms_line = t("rooms_note", lang, persons=adults + children, rooms=quoted, fewer=fewer,
                           rate=f"{prop.extra_bed_rate:,}")
        else:  # the number of rooms is always said after a room check (owner, 2026-11-21)
            rooms_line = t("rooms_plain", lang, persons=adults + children, rooms=quoted)
        st["rooms_line"] = rooms_line
        out["rooms_line"] = st["rooms_line"]
        if st.get("checked") != key and same_dates:
            # only the number of guests changed ('pinne oru kuttiyum'): nothing is said twice (live chat, 2026-11-19).
            # A different room now fits: its photos only. Then the name question, unless it is already the last ask.
            st["checked"] = key
            ctx.silent = True
            if picked:
                ctx.after.append(picked)
            if rooms_line:  # the persons changed: the rooms sentence for the NEW number, from the code
                ctx.after.append(rooms_line)
            sent = self._sent_photos(ctx.phone)
            fresh = False
            for rt in shown:  # each room's number and name as a text, then its photos (owner, 2026-11-20)
                if refs := [x for x in self._room_refs(rt, st, self._rooms_of(rt, bk, options)) if x not in sent]:
                    self._queue_room_photos(ctx, rt, refs[:MAX_ROOM_PHOTOS])
                    fresh = True
            asks = {t("ask_contact", lg) for lg in ("en", "mn", "tn", "hn")}
            # after a change the next step is asked again, never a bare 'noted' (owner, 2026-11-21); only when the
            # name ask is the message just above (nothing new was said) does 'noted' go instead
            if fresh or rooms_line or (self.repo.last_outbound_text(ctx.phone) or "") not in asks:
                ctx.after.append(t("ask_contact", lang))
            else:
                ctx.after.append(t("noted", lang))
            out["note"] += " Messages to the guest are sent automatically. Reply exactly [no reply]."
        elif st.get("checked") != key:
            st["checked"] = key
            ctx.after.append(picked or t("rooms_ok", lang))
            if rooms_line:
                ctx.after.append(rooms_line)
            ctx.silent = True
            if prop.intro and st.get("intro_sent") != prop.id:
                price = options[0].get("price_per_night") or round(
                    options[0]["total_for_stay"] / max(1, out["nights"] * options[0]["quoted_for_rooms"]))
                ctx.after.append(prop.intro.replace("{price}", _money(price)).replace("{room}", offered[0].name))
                st["intro_sent"] = prop.id
            # the owner's order (2026-11-19): rooms available -> glimpse -> general photos -> the offered room's photos
            general = self._general_photos(ctx.phone, prop)
            sent = self._sent_photos(ctx.phone) | set(general)
            if general:
                ctx.after.append(general)
            for rt in shown:  # each room's number and name as a text, then its photos (owner, 2026-11-20)
                if refs := [x for x in self._room_refs(rt, st, self._rooms_of(rt, bk, options)) if x not in sent]:
                    self._queue_room_photos(ctx, rt, refs[:MAX_ROOM_PHOTOS])
            ctx.after.append(t("ask_contact", lang))
            out["note"] += " Messages to the guest are sent automatically. Reply exactly [no reply]."
        else:
            out["note"] += " Rooms are still available. Answer only what the guest asked, in one short line."
        return out

    def _no_room_answer(self, st: dict[str, Any], prop: Property, bk: dict[str, Any], ci: date, co: date,
                        adults: int, children: int, wanted: int) -> str:
        """'Not available' is never a dead end (owner, 2026-11-19): a group too big for 2 per room is offered fewer
        rooms with extra beds ('Shall I go ahead with 5 rooms?'); otherwise the nearest dates that are free."""
        lang = st.get("lang", "en")
        st.pop("suggest_rooms", None)
        st.pop("alt_dates", None)
        if not wanted and prop.extra_bed_rate:
            now = self.clock.now()
            free = [(rt, self.repo.free_rooms(rt, ci, co, now, bk.get("own", "")))
                    for rt in self.repo.list_room_types(prop.id)]
            least = av.fewest_rooms([rt for rt, _ in free], adults, children)
            for n in range(max(least, 1), sum(f for _, f in free) + 1):
                if any(av.plan_rooms(rt, adults, children, f, n) for rt, f in free) or av.mixed_rooms(
                        free, adults, children, n):
                    st["suggest_rooms"] = n
                    return t("beds_offer", lang, persons=adults + children, rooms=n, rate=f"{prop.extra_bed_rate:,}")
        # a group bigger than every free room together (no fixed guest limit, owner 2026-11-20): say what IS free
        now = self.clock.now()
        free = [(rt, self.repo.free_rooms(rt, ci, co, now, bk.get("own", "")))
                for rt in self.repo.list_room_types(prop.id)]
        beds_ok = bool(prop.extra_bed_rate)
        cap = sum(f * (rt.max_occupancy if beds_ok else av.base_per_room(rt, False)) for rt, f in free if f > 0)
        if 0 < cap < adults + children:
            return t("rooms_most", lang, rooms=sum(f for _, f in free if f > 0), cap=cap, persons=adults + children)
        nights, today = (co - ci).days, self._today()
        found: list[str] = []
        for shift in sorted((d for d in range(-7, 22) if d), key=abs):
            start = ci + timedelta(days=shift)
            if start < today:
                continue
            probe = {**bk, "check_in": start.isoformat(), "check_out": (start + timedelta(days=nights)).isoformat()}
            if self._fits(prop, probe) or self._mix(prop, probe, wanted):
                found.append(f"{start.day} {start.strftime('%b')} - {(start + timedelta(days=nights)).day} "
                             f"{(start + timedelta(days=nights)).strftime('%b')}")
                if len(found) == 2:
                    break
        if found:
            st["alt_dates"] = found
            return t("rooms_none_alt", lang, dates=" or ".join(found))
        return t("rooms_none", lang)

    def _sold_out(self, ctx: _Turn) -> None:
        """The last rooms went to another guest while this one was giving their name (stress test, 2026-11-18: with
        many guests at once the AI left the losers unanswered). The fixed 'not available' line goes, once per turn."""
        msg = t("rooms_none", ctx.st.get("lang", "en"))
        if (self.repo.last_outbound_text(ctx.phone) or "") == msg:
            msg = t("form", ctx.st.get("lang", "en"))  # already told: ask for other dates instead of repeating it
        if msg not in ctx.after:
            ctx.after.append(msg)
        ctx.silent = True

    def _tool_prepare(self, ctx: _Turn, a: dict[str, Any]) -> dict[str, Any]:
        st = ctx.st
        ctx.priced = True
        self._set_style(st, a)
        bk = st.get("bk")
        if not bk:
            return {"error": "call check_availability first"}
        prop = self.repo.get_property(bk["property_id"])
        wanted = str(a.get("room_type", "")).lower().strip()
        fits = self._fits(prop, bk)
        chosen = next(((rt, o) for rt, o in fits if rt.name.lower() == wanted or rt.id.lower() == wanted), None) or next(
            ((rt, o) for rt, o in fits if wanted and (wanted in rt.name.lower() or rt.name.lower() in wanted)), None)
        if chosen is None and (mix := bk.get("mix")):
            # a group over two room types (1 Basic + 1 Standard), figures from check_availability
            if a.get("early_checkin") and str(a.get("early_checkin")).lower() not in ("false", "0", "no"):
                return {"error": "early check-in for a stay over two room types is arranged by the owner: "
                                 "call talk_to_owner"}
            total, advance = sum(p["total"] for p in mix), sum(p["advance"] for p in mix)
            rooms = sum(p["rooms"] for p in mix)
            bk["chosen"] = {"mix": mix, "rooms": rooms, "extra_beds": sum(p.get("beds", 0) for p in mix),
                            "early": False, "total": total,
                            "advance": advance, "turn": st["turn"]}
            label = " + ".join(f"{p['rooms']} {self.repo.get_room_type(p['room_type_id']).name}" for p in mix)
            leads.save_lead(self.repo, ctx.phone, self.clock.now(), room_type=label, rooms=rooms, stage="summary")
            return {"place": self._place_label(prop), "room_type": label, "rooms": rooms, "total": total,
                    "advance_now": advance, "balance_at_property": total - advance,
                    "next_step": ("Nothing is sent yet. If you do not have the guest's name and mobile number, ask for "
                                  "them in one short line; once you have both, call create_booking.")}
        if chosen is None and fits:
            # No room type named, or one that is not free: the owner's top-priority free one. The guest is never
            # told about other room types or asked to pick (client, 2026-11-17).
            chosen = fits[0]
        if chosen is None:
            self._sold_out(ctx)
            return {"error": "no room is free for these dates any more; the guest is told automatically. Reply exactly "
                             "[no reply]."}
        rt, options = chosen
        rooms = self._int(a, "rooms", options[0])
        if rooms not in options:  # the guest asks for another number of rooms: allowed if it works (owner's rule)
            ci_, co_ = date.fromisoformat(bk["check_in"]), date.fromisoformat(bk["check_out"])
            plan = av.plan_rooms(rt, bk["adults"], bk["children"], self.repo.free_rooms(rt, ci_, co_, self.clock.now(), bk.get("own", "")),
                                 rooms, bool(prop.extra_bed_rate))
            if plan:
                options = [plan[0]]
        beds = self._int(a, "extra_beds") or self._beds_for(prop, rt, bk["adults"], bk["children"], rooms)
        early = bool(a.get("early_checkin")) and str(a.get("early_checkin")).lower() not in ("false", "0", "no")
        if rooms not in options:
            return {"error": f"{rooms} room(s) is not possible; choose one of {options}"}
        if beds and (not prop.extra_bed_rate or beds > rooms):
            return {"error": "extra beds are not available" if not prop.extra_bed_rate
                    else f"at most one extra bed per room ({rooms})"}
        if early and not (prop.early_checkin_time and prop.early_checkin_rate):
            return {"error": "early check-in is not offered here; the owner can tell them (give the owner's numbers)"}
        ci, co = date.fromisoformat(bk["check_in"]), date.fromisoformat(bk["check_out"])
        q, fee, total, advance = self._price(prop, rt, ci, co, rooms, beds, early)
        bk["chosen"] = {"room_type_id": rt.id, "rooms": rooms, "extra_beds": beds, "early": early, "total": total,
                        "advance": advance, "turn": st["turn"]}
        leads.save_lead(self.repo, ctx.phone, self.clock.now(), room_type=rt.name, rooms=rooms, stage="summary")
        out = {"place": self._place_label(prop), "room_type": rt.name, "rooms": rooms, "extra_beds": beds,
               "nights": q.nights, "total": total, "advance_now": advance, "balance_at_property": total - advance,
               "next_step": ("Nothing is sent yet. If you do not have the guest's name and mobile number, ask for them "
                             "in one short line; once you have both, call create_booking.")}
        if beds:
            out["extra_bed_cost"] = beds * prop.extra_bed_rate * q.nights
        if fee:
            out["early_checkin"] = f"from {self._short_time(prop.early_checkin_time)}, {_money(fee)}"
        return out

    @staticmethod
    def _clock12(hhmm: str) -> str:
        try:
            hour, minute = (int(x) for x in hhmm.split(":")[:2])
        except ValueError:
            return hhmm
        return f"{hour % 12 or 12}:{minute:02d} {'AM' if hour < 12 else 'PM'}"

    def _summary_text(self, prop: Property, rt: RoomType, rooms: int, beds: int, ci: date, co: date, q: av.Quote,
                      guests: int, *, name: str = "", mobile: str = "", fee: int = 0, total: int | None = None,
                      advance: int | None = None, room_label: str = "") -> str:
        """The client's "glimpse of your enquiry" (2026-11-17), always English, one fact per line. The advance is
        shown as an amount, never as a percent."""
        total = q.total if total is None else total
        advance = q.advance if advance is None else advance
        bed_cost = beds * prop.extra_bed_rate * q.nights
        plural = lambda n, word: f"{n} {word}{'s' if n != 1 else ''}"  # noqa: E731
        lines = ["Following are the glimpse of your enquiry,", ""]
        if name:
            lines.append(f"Guest - {name}")
        if mobile:
            lines.append(self._show_mobile(mobile))
        lines.append(f"Check in - {self._ordinal_date(ci)}")
        if fee:
            lines.append(f"*Early Check-in @ {self._short_time(prop.early_checkin_time)}*")
        lines += [f"Check out - {self._ordinal_date(co)}", plural(q.nights, "Night"), f"{guests} PAX",
                  plural(rooms, "Room")]
        if room_label:
            lines.append(f"*{room_label}*")
        elif len(self.repo.list_room_types(prop.id)) > 1:
            lines.append(f"*{rt.name}*")
        if prop.meal_plan:
            lines.append(f"*{prop.meal_plan}*")
        lines.append(f"Room Cost - {total - fee - bed_cost:,}")
        if bed_cost:
            lines.append(f"Extra Bed - {bed_cost:,}")
        if fee:
            lines.append(f"Early Check-in Charges - {fee:,}")
        lines.append(f"Total Cost - {total:,}")
        if total > advance:
            lines += ["", "As a procedure you may need to pay an advance to block the room.", "",
                      f"Pay - {advance:,}", "", "Balance can be paid during the time of check in."]
        return "\n".join(lines)

    def _confirmed_text(self, b: Booking, prop: Property | None, rt: RoomType | None) -> str:
        """The client's booking confirmation (2026-11-17): the ONLY message the guest gets once the advance is paid.
        Always English, the same layout as the enquiry summary, with the given room number(s)."""
        notes = b.notes or ""
        mobile = re.search(r"Mobile: ([+\d][\d ]{6,})", notes)
        early = re.search(r"Early check-in ([^:;]+): ₹([\d,]+)", notes)
        beds = re.search(r"Extra beds: (\d+)", notes)
        fee = int(early.group(2).replace(",", "")) if early else 0
        bed_cost = int(beds.group(1)) * (prop.extra_bed_rate if prop else 0) * b.nights if beds else 0
        plural = lambda n, word: f"{n} {word}{'s' if n != 1 else ''}"  # noqa: E731
        parts = self.repo.list_group(b)  # a stay over two room types is confirmed as one booking
        rooms, guests = sum(x.rooms for x in parts), sum(x.adults + x.children for x in parts)
        total, paid = sum(x.total for x in parts), sum(x.paid_amount for x in parts)
        balance = sum(x.balance for x in parts)
        if len(parts) > 1:  # every part's extra beds (a split group with 2 + 1 per room)
            bed_cost = sum(int(m.group(1)) * (prop.extra_bed_rate if prop else 0) * x.nights for x in parts
                           if (m := re.search(r"Extra beds: (\d+)", x.notes or "")))
        numbers = [n for x in parts for n in av.split_rooms(x.room_numbers)]
        label = ""
        if len(parts) > 1:
            label = " + ".join(f"{x.rooms} {(self.repo.get_room_type(x.room_type_id) or rt).name}" for x in parts)
        lines = ["Following are the glimpse of your booking,", ""]
        if b.guest_name:
            lines.append(f"Guest - {b.guest_name}")
        lines.append(self._show_mobile(mobile.group(1).strip() if mobile else b.guest_phone))
        lines.append(f"Check in - {self._ordinal_date(b.check_in)}")
        if early:
            lines.append(f"*Early Check-in @ {early.group(1).strip()}*")
        lines += [f"Check out - {self._ordinal_date(b.check_out)}", plural(b.nights, "Night"),
                  f"{guests} PAX", plural(rooms, "Room")]
        if numbers:
            lines.append("*" + ", ".join(f"#{n}" for n in numbers) + "*")
        if label:
            lines.append(f"*{label}*")
        elif prop and rt and len(self.repo.list_room_types(prop.id)) > 1:
            lines.append(f"*{rt.name}*")
        if prop and prop.meal_plan:
            lines.append(f"*{prop.meal_plan}*")
        lines.append(f"Room Cost - {total - fee - bed_cost:,}")
        if bed_cost:
            lines.append(f"Extra Bed - {bed_cost:,}")
        if fee:
            lines.append(f"Early Check-in Charges - {fee:,}")
        lines += [f"Total Cost - {total:,}", "", f"Advance Paid - {paid:,}",
                  f"Balance Payable - {balance:,}", "*Booking Confirmed ✅*"]
        return "\n".join(lines)

    def _tool_create(self, ctx: _Turn, a: dict[str, Any]) -> dict[str, Any]:
        st, phone = ctx.st, ctx.phone
        ctx.priced = True
        self._set_style(st, a)
        bk = st.get("bk") or {}
        if not bk:
            return {"error": "call check_availability first"}
        # The room type is never taken from create_booking (the AI guessed one once and the guest was asked to pick):
        # it is the one locked by prepare_booking, else the owner's top-priority free one.
        if not bk.get("chosen") or any(a.get(k) for k in ("rooms", "extra_beds", "early_checkin")):
            picked = {k: a[k] for k in ("rooms", "extra_beds", "early_checkin") if a.get(k)}
            prior = bk.get("chosen") or {}
            if prior.get("room_type_id"):
                picked["room_type"] = prior["room_type_id"]
            if prior.get("mix"):
                picked.pop("rooms", None)  # the rooms of a mixed stay come from the owner's priority
            done = self._tool_prepare(ctx, {"room_type": "", **picked, "language": a.get("language")})
            if "error" in done:
                return done
            bk = st.get("bk") or {}
        chosen = bk["chosen"]
        now = self.clock.now()
        guest = self.repo.touch_guest(phone, now, language=st.get("lang"))
        # "same number" = the WhatsApp number; with no name given, the guest's WhatsApp profile name is used
        # the name the guest gave in THIS conversation wins over one saved from an earlier chat (a guest who restarted
        # and gave a new name once got the old one on the summary, 2026-11-18)
        name = (a.get("guest_name") or "").strip()[:60] or self._name_in_chat(phone) or guest.name
        raw_mobile = str(a.get("mobile") or "").strip()
        same = raw_mobile.lower() in ("same", "this", "this number", "same number", "whatsapp", "same no", "this no")
        mobile = phone if same else (try_normalize_phone(raw_mobile) or bk.get("mobile", ""))
        if mobile:
            bk["mobile"] = mobile
        if not mobile:
            return {"error": ("ask the guest's name and mobile number for the booking" if not name else
                              "ask the guest's mobile number for the booking") if not raw_mobile
                    else "that mobile number does not look right; ask the guest to send it again"}
        if not name:
            return {"error": "the mobile number is saved; ask ONLY the guest's name, in a few words"}
        if name != guest.name:
            guest.name = name
            self.repo.upsert_guest(guest)
        prop = self.repo.get_property(bk["property_id"])
        if chosen.get("mix"):
            return self._create_mix(ctx, a, bk, chosen, prop, name, mobile)
        rt = self.repo.get_room_type(chosen["room_type_id"])
        ci, co = date.fromisoformat(bk["check_in"]), date.fromisoformat(bk["check_out"])
        early = bool(chosen.get("early"))
        q, fee, total, advance = self._price(prop, rt, ci, co, chosen["rooms"], chosen["extra_beds"], early)
        if total != chosen["total"]:  # a rate changed since the figures were locked
            bk.pop("chosen", None)
            return {"error": "the price changed; call create_booking again"}
        lang = st.get("lang", "en")
        hold = timedelta(minutes=self.config.hold_minutes)
        notes = [f"Mobile: {display_phone(mobile)}"]
        if chosen["extra_beds"]:
            notes.append(f"Extra beds: {chosen['extra_beds']}")
        if fee:
            notes.append(f"Early check-in {self._short_time(prop.early_checkin_time)}: {_money(fee)}")
        booking = Booking(prop.id, rt.id, phone, ci, co, bk["adults"], bk["children"], chosen["rooms"], total,
                          advance, now, guest_name=name, language=lang, hold_expires_at=now + hold,
                          notes="; ".join(notes))
        summary = self._summary_text(prop, rt, chosen["rooms"], chosen["extra_beds"], ci, co, q,
                                     bk["adults"] + bk["children"], name=name, mobile=mobile, fee=fee,
                                     total=total, advance=advance)
        try:
            self.repo.create_hold(booking, now, replace_own=True)
        except SoldOut:
            bk.pop("chosen", None)
            self._sold_out(ctx)
            return {"error": "those rooms were just taken by someone else; the guest is told automatically. Reply "
                             "exactly [no reply]."}
        try:  # Razorpay wants a link that lives at least 15 minutes
            link = self.payments.create_payment_link(
                booking.advance, booking.id, name, phone,
                f"{prop.name}: {rt.name} x {booking.rooms}, {av.fmt_range(ci, co)}",
                int((now + max(hold, timedelta(minutes=16))).timestamp()))
        except PaymentError as exc:
            log.error("Payment link failed for %s: %s", booking.id, exc)
            booking.status, booking.notes = "failed", f"payment link failed: {exc}"
            self.repo.save_booking(booking)
            self._text(phone, t("pay_failed_link", lang))
            self.handoff(phone, st, f"payment link could not be created for {booking.id}", notify_guest=False)
            ctx.handed_off = True
            return {"error": "payment link failed; the team has been told and will contact the guest"}
        booking.payment_link_id, booking.payment_link_url = link.id, link.url
        self.repo.save_booking(booking)
        self.close_old_holds(phone, {booking.id})  # one open enquiry per guest: the new one replaces older ones
        st["booking_id"] = booking.id
        st.pop("bk", None)
        leads.save_lead(self.repo, phone, now, stage="held", booking_id=booking.id,
                        from_city=(a.get("from_city") or "").strip()[:60] or None)
        ctx.summary_message = summary
        ctx.pay_message = t("pay", lang, id=booking.id, minutes=self.config.hold_minutes,
                            advance=f"{booking.advance:,}", url=link.url)
        return {"ok": True, "booking_id": booking.id,
                "note": "the enquiry summary and the payment link are sent automatically. Reply exactly [no reply]."}

    def _create_mix(self, ctx: _Turn, a: dict[str, Any], bk: dict[str, Any], chosen: dict[str, Any], prop: Property,
                    name: str, mobile: str) -> dict[str, Any]:
        """One stay over two or more room types: one booking per room type (linked by group_id), held together, one
        enquiry summary, one payment link for the whole advance (owner, 2026-11-19)."""
        st, phone = ctx.st, ctx.phone
        now = self.clock.now()
        ci, co = date.fromisoformat(bk["check_in"]), date.fromisoformat(bk["check_out"])
        lang = st.get("lang", "en")
        hold = timedelta(minutes=self.config.hold_minutes)
        adults_left, children_left = bk["adults"], bk["children"]
        rooms_left = chosen["rooms"]
        parts, types = [], []
        for p in chosen["mix"]:
            rt = self.repo.get_room_type(p["room_type_id"])
            if rt is None or av.quote(prop, rt, self.repo.list_rate_rules(rt.id), ci, co, p["rooms"],
                                      p.get("beds", 0)).total != p["total"]:
                bk.pop("chosen", None)
                return {"error": "the price changed; call check_availability again"}
            rooms_left -= p["rooms"]
            last = rooms_left == 0
            adults = adults_left if last else min(rt.max_adults * p["rooms"], adults_left - rooms_left)
            children = children_left if last else min(rt.max_children * p["rooms"],
                                                      rt.max_occupancy * p["rooms"] - adults, children_left)
            adults_left, children_left = adults_left - adults, children_left - children
            parts.append(Booking(prop.id, rt.id, phone, ci, co, adults, children, p["rooms"], p["total"], p["advance"], now,
                                 guest_name=name, language=lang, hold_expires_at=now + hold,
                                 notes=f"Mobile: {display_phone(mobile)}" + (
                                     f"; Extra beds: {p['beds']}" if p.get("beds") else "")))
            types.append((rt, p["rooms"]))
        label = self._mix_label(types)
        try:
            self.repo.create_holds(parts, now, replace_own=True)
        except SoldOut:
            bk.pop("chosen", None)
            self._sold_out(ctx)
            return {"error": "those rooms were just taken by someone else; the guest is told automatically. Reply "
                             "exactly [no reply]."}
        total, advance = sum(b.total for b in parts), sum(b.advance for b in parts)
        try:
            link = self.payments.create_payment_link(
                advance, parts[0].id, name, phone, f"{prop.name}: {label}, {av.fmt_range(ci, co)}",
                int((now + max(hold, timedelta(minutes=16))).timestamp()))
        except PaymentError as exc:
            log.error("Payment link failed for %s: %s", parts[0].id, exc)
            for b in parts:
                b.status, b.notes = "failed", f"payment link failed: {exc}"
                self.repo.save_booking(b)
            self._text(phone, t("pay_failed_link", lang))
            self.handoff(phone, st, f"payment link could not be created for {parts[0].id}", notify_guest=False)
            ctx.handed_off = True
            return {"error": "payment link failed; the team has been told and will contact the guest"}
        ids = [b.id for b in parts]
        for b in parts:
            b.payment_link_id, b.payment_link_url = link.id, link.url
            b.notes += "; One stay with " + ", ".join(i for i in ids if i != b.id)
            self.repo.save_booking(b)
        self.close_old_holds(phone, {b.id for b in parts})
        st["booking_id"] = parts[0].id
        st.pop("bk", None)
        leads.save_lead(self.repo, phone, now, stage="held", booking_id=parts[0].id,
                        from_city=(a.get("from_city") or "").strip()[:60] or None)
        q0 = av.quote(prop, types[0][0], self.repo.list_rate_rules(types[0][0].id), ci, co, 1)
        ctx.summary_message = self._summary_text(prop, types[0][0], chosen["rooms"], chosen.get("extra_beds", 0),
                                                 ci, co, q0,
                                                 bk["adults"] + bk["children"], name=name, mobile=mobile,
                                                 total=total, advance=advance, room_label=label)
        ctx.pay_message = t("pay", lang, id=parts[0].id, minutes=self.config.hold_minutes, advance=f"{advance:,}",
                            url=link.url)
        return {"ok": True, "booking_id": parts[0].id,
                "note": "the enquiry summary and the payment link are sent automatically. Reply exactly [no reply]."}

    def close_old_holds(self, phone: str, keep: set[str]) -> int:
        """Close this guest's other unpaid enquiries and their payment links (owner, 2026-11-19: a guest who left one
        enquiry unpaid and booked other dates got 'your payment link is still open' after paying the new one)."""
        closed = 0
        for old in self.repo.list_bookings(phone=phone, status="hold", limit=50):
            if old.id in keep:
                continue
            if old.payment_link_id:
                try:
                    self.payments.cancel_link(old.payment_link_id)
                except Exception as exc:  # a link that cannot be closed must never stop the booking
                    log.warning("Could not close the old link of %s: %s", old.id, exc)
            old.status = "expired"
            old.notes = (old.notes + " [replaced by a newer booking]").strip()
            self.repo.save_booking(old)
            closed += 1
        return closed

    def _tool_recall(self, ctx: _Turn, a: dict[str, Any]) -> dict[str, Any]:
        rows = self.repo.list_chat(ctx.phone, limit=80)
        older = rows[:self._session_start(rows)][-30:]
        if not older:
            return {"messages": [], "note": "there is no earlier chat with this guest"}
        lines = [f"{m.at.strftime('%d %b')} {'Guest' if m.direction == 'in' else 'Bot'}: "
                 f"{LINK.sub('[link]', m.text)[:300]}" for m in older]
        return {"messages": lines,
                "note": "Earlier chat, oldest first. Links are hidden: if the guest needs a payment link, use "
                        "send_payment_link (only works while the booking is on hold). Answer only what they asked."}

    def _tool_bookings(self, ctx: _Turn, a: dict[str, Any]) -> dict[str, Any]:
        ctx.priced = True  # amounts in the guest's own booking records may be stated (balance, paid)
        paid_now = self.check_payments()  # a guest who says "I paid" gets their confirmation without waiting
        rows = []
        for b in self.repo.list_bookings(phone=ctx.phone, limit=5):
            prop = self.repo.get_property(b.property_id)
            row = {"id": b.id, "place": self._place_label(prop) if prop else "", "check_in": b.check_in.isoformat(),
                   "check_out": b.check_out.isoformat(), "rooms": b.rooms, "status": status_name(b.status, "en"),
                   "total": b.total, "paid": b.paid_amount, "balance_at_property": b.balance}
            if b.status == "hold" and b.payment_link_url:
                row["payment_link"] = b.payment_link_url
            if b.status == "paid" and b.check_in >= self._today():  # the owner's policy, worked out in code
                from app.resort.refunds import cancellation_terms
                if terms := cancellation_terms(prop, b, self._today()):
                    row["if_cancelled_today"] = terms
            rows.append(row)
        out: dict[str, Any] = {"bookings": rows} if rows else {"bookings": [], "note": "this guest has no booking yet"}
        if paid_now:
            out["note"] = "a payment was just found and the confirmation message was sent automatically; do not repeat its details"
        return out

    def _tool_change_name(self, ctx: _Turn, a: dict[str, Any]) -> dict[str, Any]:
        """A name correction after the summary (live chat 2026-11-21: 'my name is not Naseem, it is Shibil' started the
        booking again). The name changes on the guest's open and upcoming bookings; nothing else restarts."""
        name = re.sub(r"[^A-Za-z .']", "", str(a.get("name") or "")).strip()[:60]
        if not name:
            return {"error": "give the new name in English letters"}
        name = " ".join(w[:1].upper() + w[1:] for w in name.split())
        today = self._today()
        changed = 0
        for b in self.repo.list_bookings(phone=ctx.phone, limit=10):
            if b.status in ("hold", "paid", "checked_in") and b.check_out >= today:
                b.guest_name = name
                self.repo.save_booking(b)
                changed += 1
        guest = self.repo.get_guest(ctx.phone)
        if guest is not None:
            guest.name = name
            self.repo.upsert_guest(guest)
        bk = ctx.st.get("bk") or {}
        if bk:
            bk["guest_name"] = name
        ctx.after.append(t("name_changed", ctx.st.get("lang", "en"), name=name))
        ctx.silent = True
        return {"ok": True, "bookings_changed": changed,
                "note": "the guest is told automatically. Reply exactly [no reply]."}

    def _tool_owner(self, ctx: _Turn, a: dict[str, Any]) -> dict[str, Any]:
        """The guest wants the owner (owner, 2026-11-18): pause the bot, send the owner a short summary."""
        reason = (str(a.get("reason") or "").strip() or "the guest asked for the owner")[:200]
        self.handoff(ctx.phone, ctx.st, reason)
        ctx.handed_off = True
        return {"ok": True, "note": "the owner has the summary and the guest was told; reply exactly [no reply]"}

    def _tool_resend(self, ctx: _Turn, a: dict[str, Any]) -> dict[str, Any]:
        self._set_style(ctx.st, a)
        now = self.clock.now()
        live = [b for b in self.repo.list_bookings(phone=ctx.phone, status="hold", limit=5)
                if b.payment_link_url and b.hold_expires_at and b.hold_expires_at > now]
        if not live:
            return {"error": "no booking is waiting for payment (it may have expired or been paid); "
                             "use my_bookings to see its status, or start a new booking"}
        b = live[0]
        minutes = max(1, int((b.hold_expires_at - now).total_seconds() // 60))
        ctx.resend = True
        ctx.pay_message = t("pay", ctx.st.get("lang", "en"), id=b.id, minutes=minutes, advance=f"{b.advance:,}",
                            url=b.payment_link_url)
        return {"ok": True, "booking_id": b.id, "minutes_left": minutes,
                "note": "the link is sent automatically after your reply; do not write it"}
