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

import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from app.core.clock import to_ist
from app.core.language import _classify_latin, _words, is_language_neutral
from app.core.phone import try_normalize_phone
from app.providers.llm import ChatMessage, LLMError, ToolCall, ToolSpec
from app.resort import availability as av
from app.resort import leads
from app.resort.db import SoldOut
from app.resort.i18n import status_name, t
from app.resort.models import Booking, Property, RoomType
from app.resort.payments import PaymentError

log = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 6
MAX_REPLY_TOKENS = 700
HISTORY = 16
SESSION_GAP = timedelta(hours=6)  # a pause this long starts a new conversation: older chat is not shown to the AI
LINK = re.compile(r"https?://\S+")
STYLE_LABELS = {"en": "English", "ml": "Malayalam script", "hi": "Hindi script", "ta": "Tamil script",
                "manglish": "Manglish (Malayalam in English letters)", "hinglish": "Hinglish (Hindi in English letters)",
                "tanglish": "Tanglish (Tamil in English letters)"}
GREETING = re.compile(r"^\W*((hi+|hello+|hey+|hai+|hlo|hola|namaste|namaskar\w*|vanakkam|good\s*(morning|afternoon|evening))"
                      r"|നമസ്കാരം|ഹായ്|ഹലോ|வணக்கம்|ஹாய்|நமஸ்தே|नमस्ते|हाय|हेलो|हैलो)\W*$", re.IGNORECASE)
# Styles the AI may report. Fixed messages (payment, ID) use the script language; Latin-script styles get English.
STYLES = ("en", "ml", "manglish", "hi", "hinglish", "ta", "tanglish")
# Replies are ALWAYS in English letters (user, 2026-11-11): a guest who writes or speaks Malayalam / Tamil / Hindi gets
# Manglish / Tanglish / Hinglish, never the native script.
LATIN_STYLE = {"ml": "manglish", "ta": "tanglish", "hi": "hinglish"}
STYLE_LANG = {"en": "en", "ml": "ml", "hi": "hi", "ta": "ta", "manglish": "mn", "hinglish": "en", "tanglish": "en"}
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
    if english + unknown <= 2:  # a short message: English only if it has a real English word ('any pool nearby?')
        if english and english >= unknown:
            return "en"
        return previous if previous in ("en", "manglish") else "en"  # '2 divasam', 'Asha, Kochi'
    return "manglish" if unknown > english else "en"


MAX_GENERAL_PHOTOS = 6  # general photos sent automatically when a guest picks a property

# The owner wants no essays (2026-11-17): more words than this, outside the room/price lines, gets one shorter rewrite.
MAX_CHAT_WORDS = 30  # "very simple" (client, 2026-11-17)
_LIST_LINE = re.compile(r"₹|\bRs\.?\s?\d|https?://|^\s*(?:[-*•]|\d+[.)])\s", re.IGNORECASE)


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
MANGLISH_WORDS = re.compile(r"\b(?:und|undo|aanu|ethra|venam|cheyyanam|cheyyam|ippol|aakum|evide|ningal\w*|parayamo|mathi)\b", re.IGNORECASE)
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


def is_ack(text: str) -> bool:
    """Only an acknowledgement: 'ok', 'okay thanks', 'sheri', '👍'. Nothing to answer."""
    s = text.strip().lower()
    if not s:
        return False
    if not re.search(r"\w", s):  # emoji or punctuation only
        return True
    words = [w.strip(".,!?;:()-'\"") for w in s.split() if re.search(r"\w", w)]  # not \w: it would cut Malayalam vowel signs
    return 0 < len(words) <= 5 and all(w in ACK_WORDS for w in words)


def display_phone(e164: str) -> str:
    """+919497861924 -> 9497861924 (how Indians write it); other countries keep the +."""
    return e164[3:] if e164.startswith("+91") and len(e164) == 13 else e164


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


LANGUAGE_FIELD = {"type": "string", "enum": list(STYLES),
                  "description": "the language style of the guest's latest message (Manglish = Malayalam in English letters)"}

EARLY_FIELD = {"type": "boolean", "description": "true only if the guest asked for early check-in"}

TOOLS = [
    ToolSpec(
        "ask_location",
        "When the guest wants a resort, a booking or rooms and has not said which location, call this: it sends the "
        "owner's question 'Sure, Kindly confirm the Location' with the towns. Your reply must then be exactly [no reply].",
        {"type": "object", "properties": {}}),
    ToolSpec(
        "send_photos",
        "Send the guest photos of a resort (or of one room type) that the owner uploaded. ONLY when the guest's "
        "message itself asks for photos, pictures, pics or images. Never for a town or resort name alone. The "
        "photos are sent automatically after your reply; reply exactly [no reply].",
        {"type": "object", "properties": {
            "place": {"type": "string", "description": "the resort or town"},
            "room_type": {"type": "string", "description": "only if they asked to see one room type"}},
         "required": ["place"]}),
    ToolSpec(
        "ask_stay_details",
        "Send the guest the booking form (Check in / Check out / Number of persons) for the resort they chose. Use it "
        "once the resort is known and the guest has NOT yet given their dates and number of persons. If they already "
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
        "Lock the room type and figures when the guest picks one of several room types, or asks for extra beds or "
        "early check-in. Does not book anything and sends nothing.",
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
            "room_type": {"type": "string", "description": "only when there are several room types: the one chosen"},
            "rooms": {"type": "integer", "description": "only if the guest asked for a number of rooms"},
            "extra_beds": {"type": "integer", "description": "only if the guest asked for extra beds"},
            "early_checkin": EARLY_FIELD,
            "from_city": {"type": "string", "description": "the city they are coming from, only if they said it"},
            "language": LANGUAGE_FIELD},
         "required": ["guest_name", "mobile"]}),
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
        "an existing booking or payment.",
        {"type": "object", "properties": {}}),
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
        """The property's general photos (outside, beach, common areas) not yet sent: they go to EVERY guest who
        picks the property (client, 2026-11-17), in the owner's order, at most MAX_GENERAL_PHOTOS."""
        sent = self._sent_photos(phone)
        return [r for r in prop.photo_urls if r not in sent][:MAX_GENERAL_PHOTOS]

    def _tool_photos(self, ctx: _Turn, a: dict[str, Any]) -> dict[str, Any]:
        """Up to 4 photos on request: the room type asked about, else every room type's photos, then any general
        photo not yet sent. Never one already sent."""
        prop = self._find_property(a.get("place", ""))
        if prop is None:
            return {"error": "unknown place", "places": [self._place_label(p) for p in self._places()]}
        rooms = self.repo.list_room_types(prop.id)
        wanted = str(a.get("room_type") or "").lower().strip()
        room = next((rt for rt in rooms if wanted and (wanted in rt.name.lower() or rt.name.lower() in wanted)), None)
        refs = list(room.photo_urls) if room else [r for rt in rooms for r in rt.photo_urls]
        refs += list(prop.photo_urls)
        sent = self._sent_photos(ctx.phone)
        fresh = [r for r in dict.fromkeys(refs) if r not in sent][:4]
        if not fresh:
            numbers = ", ".join(display_phone(n) for n in prop.contact_numbers) or "the owner"
            return {"error": ("all photos were already sent" if refs else "no photos uploaded yet") +
                    f"; say so in one short line and that the owner can share more on {numbers}"}
        ctx.photos = fresh
        return {"ok": True, "sending": len(fresh), "note": "the photos are sent automatically; reply exactly [no reply]"}

    def _send_photos(self, phone: str, refs: list[str], caption: str = "") -> int:
        from app.resort import photos as ph
        sent = 0
        for ref in refs:
            path = ph.local_path(ref)
            if path is None:
                continue
            try:
                media_id = self.wa.upload_media(path.read_bytes(), ph.mime_of(path), path.name)
                self.wa.send_image(phone, media_id, caption if not sent else "")
            except Exception as exc:  # one bad photo must not stop the chat
                log.warning("Photo send failed: %s", exc)
                continue
            self._log(phone, f"[photo] {ref}")
            sent += 1
        return sent

    def _named_place(self, text: str) -> Property | None:
        """An open resort the guest named by town or name ('Alleppey', 'pozhiyoram', 'Vagamon please'), else None."""
        words = {w for w in re.findall(r"[a-z]{4,}", text.lower())}
        hits = []
        for p in self._places():
            names = set(re.findall(r"[a-z]{4,}", f"{p.name} {p.city} {p.place}".lower())) - {"resort", "kerala", "vibes"}
            if words & names:
                hits.append(p)
        return hits[0] if len(hits) == 1 else None

    def _location_answer(self, phone: str, st: dict[str, Any], text: str) -> str | None:
        """The guest answered our location question with just a place: send the form at once (no AI guesswork)."""
        if "Kindly confirm the Location" not in (self.repo.last_outbound_text(phone) or ""):
            return None
        if len(text.split()) > 4 or (prop := self._named_place(text)) is None:
            return None
        st["form_place"] = prop.id
        self._save(phone, st)
        self._send_photos(phone, self._general_photos(phone, prop))  # every guest who picks it sees the place
        self._text(phone, t("form", st.get("lang", "en")))
        return "form"

    def _tool_location(self, ctx: _Turn, a: dict[str, Any]) -> dict[str, Any]:
        ctx.after.append(t("ask_location", ctx.st.get("lang", "en"), towns=" or ".join(self._towns())))
        ctx.silent = True
        return {"ok": True, "note": "the location question is sent automatically; reply exactly [no reply]"}

    def _closed_facts(self) -> list[str]:
        """Resorts the guest may pick that have no online booking yet: the owner takes those."""
        open_props = self._places()
        fallback = open_props[0].contact_numbers if open_props else []
        out = []
        for p in self.repo.list_properties(active_only=False):
            if p.active:
                continue
            numbers = ", ".join(display_phone(n) for n in (p.contact_numbers or fallback)) or "not set"
            out.append(f"RESORT '{p.name}': online booking is not open yet. If the guest picks it, say in one short "
                       f"line that the owner will help them with it and give the owner's numbers: {numbers}")
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
        if own in LANG_NAMES:
            style = LATIN_STYLE[own]  # Malayalam / Tamil / Hindi script in -> Manglish / Tanglish / Hinglish out
        elif is_language_neutral(text):
            style = st.get("style") or "en"  # 'ok' / 'hi' / a name: the guest's own language so far
        elif MANGLISH_WORDS.search(text) and not HINGLISH_WORDS.search(text):
            style = "manglish"
        elif HINGLISH_WORDS.search(text) and not MANGLISH_WORDS.search(text):
            style = "hinglish"
        else:
            style = latin_style(text, st.get("style") or "en")
        if style not in STYLE_LABELS:
            style = "en"
        if not is_language_neutral(text):
            st["style"] = style
            if own not in LANG_NAMES:  # English letters: the fixed messages (payment, confirmation) follow too
                st["lang"] = STYLE_LANG[style]
        extra = " Plain English words like room, booking and payment are fine inside it." if style != "en" else ""
        return (f"REPLY LANGUAGE (decided from the guest's latest message): {STYLE_LABELS[style]}. Write the whole "
                f"reply in it and no other language.{extra}")

    def _system_prompt(self, st: dict[str, Any], phone: str = "", text: str = "", fresh: bool = False) -> str:
        today = self._today()
        return (
            f"You are the WhatsApp booking assistant of {self._brand()}. You talk like a friendly, sensible person at "
            "the front desk, never like a form or a menu. " + self.config.speaking_style + "\n\n"
            f"Today is {today.isoformat()} ({today.strftime('%A')}), India time.\n\n"
            "LANGUAGE\n"
            "- Work out the guest's language from their message. Never ask which language they want.\n"
            "- Reply in the same language but ALWAYS in English letters, never in Malayalam, Tamil or Hindi script: "
            "English -> English; Malayalam (typed in any script, or spoken) -> Manglish; Tamil -> Tanglish; Hindi -> "
            "Hinglish. If they switch, you switch. Short words like 'ok' or 'hi' keep the language so far.\n"
            "- Use ONE language per reply, the guest's own. Malayalam typed in English letters ('avde engane und', "
            "'room undo', 'evide', 'ethra aaya') is Manglish: answer in Manglish, in English letters only, NEVER in "
            "Malayalam script. Malayalam script gets Manglish too. English gets English. Never mix languages or "
            "languages in one reply; plain English words like room, booking, check-in, payment and price are fine "
            "inside Manglish. Example: guest 'Alleppey il room undo' -> 'Und sir, Alleppey il rooms und. Ethra "
            "aalkkaar und, ethra divasam?'\n"
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
            "- Answer ONLY what the guest asked, in as few words as possible. Usually ONE short line; at most two or "
            "three short lines. No introductions, no explanations they did not ask for, no repeating, no closing "
            "lines like 'let me know if you need anything'. Simple, lively and friendly, like a quick WhatsApp reply "
            "from a real person.\n"
            "- Asked about rooms? Name the room types (one short line each, who it fits) and ask their dates in a "
            "few words. Asked the location? Send the Maps link with a few words. Asked a yes/no question? Start with "
            "yes or no.\n"
            "- Call the guest \"sir\", now and then and never in every message (\"madam\" only if they are clearly a woman). "
            "Never bro, dear, da, chetta, chechi, machane or their name.\n"
            "- Each room type on its own line when you list prices. Never one long paragraph.\n"
            "- Ask only ONE thing at a time. No lists of options, no emoji piles (one is fine).\n"
            "- Do not paste the property description. Do not mention offers, discounts or 'special' deals. Just "
            "state the price.\n"
            "- Never repeat details the guest already gave, except in the final summary.\n"
            "- A 'hi' or 'hello' in the middle of a chat is NOT a new start and always gets a reply (never [no reply]): "
            "use the chat so far and carry on from where you left off in one short line (for example the next thing "
            "you still need, or 'Hi sir, how can I help?'). Never ask again what they already told you.\n"
            "- Always polite, concise and to the point.\n"
            "- Talk ONLY about our resorts: rooms, prices, dates, bookings, payment, the stay, location and facilities. "
            "For anything else (general questions, other places, news, jokes, coding, personal chat) say politely in "
            "one short line that you can help only with resort bookings.\n"
            "- When the guest's message carries a note that they swiped to reply to an earlier message, answer about "
            "THAT message.\n"
            "- VERY SIMPLE: easy everyday words a school child understands, short sentences, no fancy or formal "
            "phrases ('kindly', 'we would be delighted', 'nestled', 'serene'), no sales talk. Example: guest 'rooms "
            "available?' -> 'Yes sir. Which dates?'. Room lines look like 'Deluxe - <price from the tool>/night'.\n"
            "- Reply only when it is necessary: a question, a request, a booking detail, a payment problem, a complaint, "
            "or an answer to something you asked. For anything else send no reply at all, answer with exactly "
            "[no reply]: 'ok', 'thanks', a thumbs up, compliments, jokes, 'will tell my family', 'let me check and "
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
            f"means that resort ({'; '.join(self._resort_names())}).\n"
            "2. As soon as the resort is known and the guest has not given their dates and persons, call "
            "ask_stay_details: it sends the form (Check in / Check out / Number of persons). Reply [no reply]. If they "
            "gave only part of it, ask just the missing part in a few words. If a date has no year, use the next time "
            "it comes. Never assume the number of persons or nights.\n"
            "3. When you have check-in, check-out (or nights) and persons, call check_availability. Rooms: use the "
            "fewest rooms the group needs unless the guest asked for more. If rooms are available, say ONLY that in "
            "one short line; the resort's details and the request for name and mobile number are sent automatically. "
            "If not available, say so and suggest other dates.\n"
            "4. With several room types, name each (no prices) and let the guest pick (prepare_booking).\n"
            "- Never state prices, totals or the advance while checking availability; the price reaches the guest "
            "in the resort's details and the enquiry summary. Give a price only if the guest asks for it.\n"
            "5. When the guest gives their name and mobile number, call create_booking right away (no 'shall I "
            "confirm?'). The enquiry summary and the payment link are sent automatically: reply [no reply]. Never "
            "write the summary, the link, the advance amount or a percent yourself. If they say 'this number', "
            "pass mobile 'same'.\n"
            "- Photos: when the guest asks for photos or to see the place or a room, call send_photos.\n"
            "- Early check-in: only if the guest asks. Tell them the time and charge from the FACTS and pass "
            "early_checkin true to create_booking if they want it.\n"
            "- Never ask the same thing twice.\n"
            "- Before the payment is received, NEVER say the room or booking is booked, confirmed, reserved or done. "
            "Say it will be confirmed once the payment is complete. The confirmation message arrives on its own.\n"
            "- If the party does not fit or the dates are sold out, say so and suggest another date, fewer rooms "
            "or the other place.\n\n"
            "MONEY AND FACTS\n"
            "- Never tell the guest that rates change, that their dates are peak or high season, or that a price went "
            "up. Give only the total for their stay, and a per-night price only when the tool provides one. Never "
            "quote a price from memory: only from check_availability or prepare_booking. If they ask the price before "
            "you know their dates, ask for the dates first.\n"
            "- Every price, date, room count and availability you state must come from the tool results or the "
            "FACTS below. Never calculate your own totals. Never invent amenities or policies. If you do not "
            "know something, say the owner will tell them and give the owner's number.\n"
            "- Extra bed: never add one on your own. When a room would hold a third person (for example 3 persons "
            "in 1 room), ask the guest whether they want an extra bed and tell them its price from the FACTS. "
            "Pass extra_beds to prepare_booking only if they say yes; the tool adds the cost.\n\n"
            "PAYMENT\n"
            "- The payment link opens a secure Razorpay page. The guest can pay by UPI (GPay, PhonePe, Paytm, any UPI "
            "app or ID), debit or credit card, net banking or a wallet. Only the advance is paid online; the balance "
            "is paid at the property. The rooms are held for a limited time while they pay.\n"
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
            "- You cannot cancel or change a booking or promise a refund. Politely ask them to call the owner and give "
            "the owner's number(s) of the place they booked.\n\n"
            "NEGOTIATION\n"
            "- Asking about offers, deals or the price is only a question, NOT a request to reduce. Just answer: the "
            "price for their dates (ask the dates if you do not have them yet). Never say there is no offer, never "
            "say you cannot reduce, never mention discounts, unless they ask for a lower price themselves.\n"
            "- Only when the guest asks for a lower price (reduce, discount, last price, 'kurachu tharamo', 'koodiyo'), "
            "be warm and confident and CONVINCE them: the price is fair for what they get "
            "(use the real good points of the place from the FACTS). NEVER say 'no discount', 'no offer' or 'I "
            "cannot reduce': just explain the value, as a friendly person would. Never offer a discount. If they "
            "push again, convince once more with a different good point.\n"
            "- Only if they still keep pushing after that, say the owner can talk to them directly and give the "
            "owner's number(s).\n\n"
            "GUEST MEMORY\n"
            "- You remember every guest. After the FACTS you get their records (name, earlier bookings with dates, place and status). The chat shown is today's conversation; older chat is kept and you can read it with the recall_old_chat tool.\n"
            "- Look things up only when needed, to save effort: a guest who asks about an earlier booking, a payment, a link, a stay date or something said before gets the real answer (use the records, my_bookings and/or recall_old_chat). A guest who only says hi, makes small talk or jokes gets a short, polite, friendly reply with no lookup.\n"
            "- Earlier bookings: if they ask about one, answer from the records or my_bookings in plain words: dates, place, rooms, whether it is confirmed, how much was paid and what is left to pay at the property. Paid and upcoming: say you can see it and be warm about their coming stay. Past: a warm thanks or welcome back. ON HOLD and still valid: tell them it is waiting for payment and offer the link (send_payment_link). Expired, cancelled or failed: say so kindly and offer to book again. Payment queries are answered politely and respectfully, never defensively.\n"
            "- When the guest asks for another room or a new stay while a paid booking is upcoming or in progress, say you can see that booking (place, dates) and ask in one friendly line whether the new room is for the same stay or a different one, then follow their answer. With only past stays, treat it as a new booking and add at most one warm welcome-back line.\n"
            "- A new booking always takes its dates, rooms and price from what the guest says now and from the tools, never from an earlier booking. Never paste an old payment link: only the send_payment_link tool sends one, and only for a booking still on hold.\n"
            "- Warm and easy: one or two short lines, no forms, no jargon.\n"
            "- Never address them by name.\n\n"
            "FACTS\n" + self._facts() + "\n\n" + self._guest_memory(phone, fresh) + "\n\n"
            + self._chosen_note(st) + self._language_note(st, text))

    def _chosen_note(self, st: dict[str, Any]) -> str:
        """The resort the guest already picked (the form was sent for it): the AI must not ask again."""
        prop = self.repo.get_property(st["form_place"]) if st.get("form_place") else None
        if prop is None:
            return ""
        return (f"CHOSEN RESORT: the guest picked {self._place_label(prop)} and got the booking form. When they "
                "give dates and persons, call check_availability for it right away; never send the form again.\n\n")

    # ------------------------------------------------------------------ the conversation

    @staticmethod
    def _session_start(rows: list[ChatMessage]) -> int:
        """Index of the first message of the current conversation: a pause of SESSION_GAP starts a new one."""
        for i in range(len(rows) - 1, 0, -1):
            if rows[i].at - rows[i - 1].at >= SESSION_GAP:
                return i
        return 0

    def _chatting_now(self, phone: str) -> bool:
        """True when we already talked in this conversation (no long gap): a 'hi' then continues, never restarts."""
        rows = self.repo.list_chat(phone, limit=HISTORY)
        rows = rows[self._session_start(rows):]
        return any(m.direction == "out" for m in rows)

    def _history(self, phone: str) -> list[ChatMessage]:
        merged: list[ChatMessage] = []
        rows = self.repo.list_chat(phone, limit=HISTORY)
        rows = rows[self._session_start(rows):]  # the current conversation only; older chat is fetched on demand
        for m in rows:
            role = "user" if m.direction == "in" else "assistant"
            text = LINK.sub("[link]", m.text[:700])  # an old payment link must never be copied into a new chat
            if merged and merged[-1].role == role:
                merged[-1] = ChatMessage(role, merged[-1].content + "\n" + text)
            else:
                merged.append(ChatMessage(role, text))
        while merged and merged[0].role != "user":
            merged.pop(0)
        return merged

    def _greet(self, phone: str, st: dict[str, Any], text: str = "") -> str:
        """Any plain greeting gets the client's own line: 'Hi, How can I assist you?' (no AI call)."""
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
        if st.get("style") == "en" and (MANGLISH_WORDS.search(reply) or re.search(r"(?:milega|chahiye|nahi|kitne|theek|aap|mujhe)", reply, re.I)):
            return "Rewrite that reply in plain English only. The guest's latest message is in English."
        talk = "\n".join(line for line in reply.splitlines() if not _LIST_LINE.search(line))
        if st.get("style") == "en" and _classify_latin(_words(talk))[1]:  # Manglish words the short list misses
            return "Rewrite that reply in plain English only. The guest's latest message is in English."
        if HINGLISH_WORDS.search(guest_text) and not MANGLISH_WORDS.search(guest_text):
            st["hing"] = True
        if st.get("hing") and not MANGLISH_WORDS.search(guest_text) and MANGLISH_WORDS.search(reply):
            return "Rewrite that reply in Hinglish (Hindi in English letters), the guest's language. Do not use Malayalam."
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
        if chatty_words(reply) > MAX_CHAT_WORDS:
            return ("Too long. Rewrite it much shorter and simpler: answer only what the guest asked, in one or two "
                    "short lines of easy words. "
                    "Keep any room lines with prices as they are.")
        return ""

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
            return reply
        return fixed if fixed and not self._reply_problem(fixed, guest_text, st) else reply

    def _ease_sir(self, phone: str, reply: str) -> str:
        """'sir' now and then: when our last message already had it, drop a leading or trailing one."""
        previous = self.repo.last_outbound_text(phone) or ""
        if not re.search(r"\b(sir|madam)\b", previous, re.IGNORECASE):
            return reply
        eased = re.sub(r"^\s*(sir|madam)[,.]?\s+", "", reply, flags=re.IGNORECASE)
        eased = re.sub(r",?\s*\b(sir|madam)\b(?=[.!?]?\s*$)", "", eased, flags=re.IGNORECASE)
        return eased[:1].upper() + eased[1:] if eased != reply and eased else reply

    def _converse(self, phone: str, st: dict[str, Any], text: str, in_id: int = 0,
                  images: list[tuple[str, str]] | None = None) -> str:
        if self.debounce_s and in_id:  # a second message may follow: wait, and let the newest one answer
            time.sleep(self.patience_s if looks_unfinished(text) else self.debounce_s)
            if self.repo.last_chat_id(phone, "in") != in_id:
                return "superseded"
        st["turn"] = st.get("turn", 0) + 1
        ctx = _Turn(phone, st)
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
        reply = ""
        try:
            if self._llm is None:
                raise LLMError("none", "no LLM configured")
            llm = self._llm()
            response = llm.complete(system, messages, TOOLS, MAX_REPLY_TOKENS)
            for _ in range(MAX_TOOL_ROUNDS):
                if not response.tool_calls or ctx.handed_off:
                    break
                messages.append(response.to_message())
                for call in response.tool_calls:
                    content, is_error = self._run_tool(ctx, call)
                    messages.append(ChatMessage.tool_result(call.id, content, is_error=is_error))
                response = llm.complete(system, messages, TOOLS, MAX_REPLY_TOKENS)
            if not response.tool_calls:
                reply = self._fix_reply(llm, system, messages, response.text.strip(), text, st)
        except LLMError as exc:
            log.warning("AI answer failed: %s", exc)
        except Exception as exc:  # a missing key or a bad tool must never leave the guest unanswered
            log.exception("AI unavailable: %s", exc)
        self._save(phone, st)
        if ctx.handed_off:
            return "handoff"
        queued = bool(ctx.after or ctx.summary_message or ctx.pay_message or ctx.photos)
        quiet = NO_REPLY in reply.lower()
        if quiet and not queued and GREETING.match(text):  # a mid-chat "hi" always gets an answer
            reply, quiet = t("greet_again", st.get("lang", "en")), False
        if quiet and not queued:
            return "quiet"
        if not reply and not queued:
            self._text(phone, t("ai_down", st.get("lang", "en")))
            self.alert_staff(f"Bot could not answer a message from {phone}: {text[:200]}", st.get("bk", {}).get("property_id"))
            return "ai_down"
        # the client's order: the AI's short line, then the fixed messages (form / glimpse / ask name), then the
        # enquiry summary, then the payment link
        if ctx.silent:  # a fixed step (location, form, rooms available) was sent: the AI's own line would repeat
            quiet = True  # it or add prices (client, 2026-11-17)
        if reply and not quiet:
            guest = self.repo.get_guest(phone)
            clean = self._without_name(reply.replace("—", "-").replace("**", "*"), guest.name if guest else "")
            self._text(phone, self._ease_sir(phone, clean))
        if ctx.photos:
            self._send_photos(phone, ctx.photos)
        asked = {t("ask_contact", lang) for lang in ("en", "mn")}
        for message in ctx.after:
            if isinstance(message, list):  # a batch of photos in its place in the order
                self._send_photos(phone, message)
                continue
            if ctx.summary_message and message in asked:  # they already gave name and mobile in this turn
                continue
            self._text(phone, message)
        if ctx.summary_message and ctx.summary_message != st.get("last_summary"):  # never the same summary twice
            st["last_summary"] = ctx.summary_message
            self._save(phone, st)
            self._text(phone, ctx.summary_message)
        if ctx.pay_message:  # the guest asked for the link again: the only message that may repeat
            self._text(phone, ctx.pay_message, repeat_ok=ctx.resend)
            return "payment_link"
        return "answered"

    # ------------------------------------------------------------------ tools

    def _run_tool(self, ctx: _Turn, call: ToolCall) -> tuple[str, bool]:
        handlers = {"ask_location": self._tool_location, "ask_stay_details": self._tool_form,
                    "send_photos": self._tool_photos,
                    "check_availability": self._tool_check, "prepare_booking": self._tool_prepare,
                    "create_booking": self._tool_create, "my_bookings": self._tool_bookings,
                    "send_payment_link": self._tool_resend, "recall_old_chat": self._tool_recall}
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
        query = re.sub(r"[^a-z0-9 ]", " ", (place or "").lower())
        best, best_score = None, 0
        for p in props:
            words = set(re.findall(r"[a-z0-9]{4,}", f"{p.name} {p.city} {p.place}".lower()))
            score = sum(1 for w in words if w[:5] in query)
            if score > best_score:
                best, best_score = p, score
        return best

    def _fits(self, prop: Property, bk: dict[str, Any]) -> list[tuple[RoomType, list[int]]]:
        ci, co = date.fromisoformat(bk["check_in"]), date.fromisoformat(bk["check_out"])
        now = self.clock.now()
        out = []
        for rt in self.repo.list_room_types(prop.id):
            options = av.room_options(rt, bk["adults"], bk["children"], self.repo.free_rooms(rt, ci, co, now))
            if options:
                out.append((rt, options))
        return out

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
        ctx.st["form_place"] = prop.id
        if t("form", "en") not in ctx.after:
            if general := self._general_photos(ctx.phone, prop):
                ctx.after.append(general)  # the general photos first, then the form
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
            return {"error": problem}
        adults, children = self._int(a, "adults"), self._int(a, "children")
        if adults < 1 or adults + children > av.MAX_GUESTS:
            return {"error": f"guests must be between 1 and {av.MAX_GUESTS}"}
        bk = {"property_id": prop.id, "check_in": ci.isoformat(), "check_out": co.isoformat(),
              "adults": adults, "children": children}
        st["bk"] = bk
        wanted = self._int(a, "rooms")
        leads.save_lead(self.repo, ctx.phone, self.clock.now(), property_id=prop.id, check_in=ci.isoformat(),
                        check_out=co.isoformat(), adults=adults, children=children, rooms=wanted, room_type="",
                        stage="asked", booking_id="")
        options = []
        for rt, rooms_ok in self._fits(prop, bk):
            rooms = wanted if wanted in rooms_ok else rooms_ok[0]
            q = av.quote(prop, rt, self.repo.list_rate_rules(rt.id), ci, co, rooms)
            option = {"room_type": rt.name, "rooms_possible": rooms_ok, "quoted_for_rooms": rooms,
                      "total_for_stay": q.total, "advance_now": q.advance, "balance_at_property": q.total - q.advance}
            if len({rate for _, rate in q.per_night}) == 1:
                option["price_per_night"] = q.per_night[0][1]  # per room, only when every night costs the same
            options.append(option)
        out: dict[str, Any] = {"place": self._place_label(prop), "check_in": ci.isoformat(),
                               "check_out": co.isoformat(), "nights": (co - ci).days, "adults": adults,
                               "children": children, "options": options}
        out["note"] = ("Give the guest the total for their stay. Never mention that rates change, seasons, peak or high "
                       "season dates, or a price increase." + year_note)
        if not options:
            out["available"] = False
            out["reason"] = "no room type is free for this many guests on these dates (sold out or too many guests)"
            return out
        if wanted and not any(wanted in o["rooms_possible"] for o in options):
            out["note"] += (f" {wanted} room(s) is not possible for {adults + children} guest(s); "
                            "rooms_possible lists the counts that work.")
        out["available"] = True
        # the client's flow: "rooms available" -> the resort's glimpse -> ask name and mobile, once per set of dates
        key = f"{prop.id}|{ci.isoformat()}|{co.isoformat()}|{adults + children}"
        lang = st.get("lang", "en")
        if len(options) == 1 and st.get("checked") != key:
            st["checked"] = key
            ctx.after.append(t("rooms_ok", lang))
            ctx.silent = True
            if general := self._general_photos(ctx.phone, prop):  # a guest who skipped the form still sees them
                ctx.after.append(general)
            if prop.intro and st.get("intro_sent") != prop.id:
                price = options[0].get("price_per_night") or round(
                    options[0]["total_for_stay"] / max(1, out["nights"] * options[0]["quoted_for_rooms"]))
                ctx.after.append(prop.intro.replace("{price}", _money(price)))
                st["intro_sent"] = prop.id
            ctx.after.append(t("ask_contact", lang))
            out["note"] += " Messages to the guest are sent automatically. Reply exactly [no reply]."
        elif len(options) == 1:
            out["note"] += " Rooms are still available. Answer only what the guest asked, in one short line."
        else:
            out["note"] += (" Rooms ARE available. Name each room type in one short line WITHOUT prices and ask "
                            "which one, and ask their name and mobile number.")
        return out

    def _tool_prepare(self, ctx: _Turn, a: dict[str, Any]) -> dict[str, Any]:
        st = ctx.st
        self._set_style(st, a)
        bk = st.get("bk")
        if not bk:
            return {"error": "call check_availability first"}
        prop = self.repo.get_property(bk["property_id"])
        wanted = str(a.get("room_type", "")).lower().strip()
        fits = self._fits(prop, bk)
        chosen = next(((rt, o) for rt, o in fits if rt.name.lower() == wanted or rt.id.lower() == wanted), None) or next(
            ((rt, o) for rt, o in fits if wanted and (wanted in rt.name.lower() or rt.name.lower() in wanted)), None)
        if chosen is None and len(fits) == 1:
            chosen = fits[0]
        if chosen is None:
            return {"error": "that room type is not free any more or not offered",
                    "free_room_types": [rt.name for rt, _ in fits]}
        rt, options = chosen
        rooms, beds = self._int(a, "rooms", options[0]), self._int(a, "extra_beds")
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
                      advance: int | None = None) -> str:
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
        if len(self.repo.list_room_types(prop.id)) > 1:
            lines.append(f"*{rt.name}*")
        if prop.meal_plan:
            lines.append(f"*{prop.meal_plan}*")
        lines.append(f"Room Cost - {q.total - bed_cost:,}")
        if bed_cost:
            lines.append(f"Extra Bed - {bed_cost:,}")
        if fee:
            lines.append(f"Early Check-in Charges - {fee:,}")
        lines.append(f"Total Cost - {total:,}")
        if total > advance:
            lines += ["", "As a procedure you may need to pay an advance to block the room.", "",
                      f"Ie - {advance:,}", "", "Balance can be paid during the time of check in."]
        return "\n".join(lines)

    def _tool_create(self, ctx: _Turn, a: dict[str, Any]) -> dict[str, Any]:
        st, phone = ctx.st, ctx.phone
        self._set_style(st, a)
        bk = st.get("bk") or {}
        if not bk:
            return {"error": "call check_availability first"}
        if not bk.get("chosen") or any(a.get(k) for k in ("room_type", "rooms", "extra_beds", "early_checkin")):
            picked = {k: a[k] for k in ("room_type", "rooms", "extra_beds", "early_checkin") if a.get(k)}
            prior = bk.get("chosen") or {}
            if prior and "room_type" not in picked:
                picked["room_type"] = prior["room_type_id"]
            done = self._tool_prepare(ctx, {"room_type": "", **picked, "language": a.get("language")})
            if "error" in done:
                return done
            bk = st.get("bk") or {}
        chosen = bk["chosen"]
        now = self.clock.now()
        guest = self.repo.touch_guest(phone, now, language=st.get("lang"))
        name = (a.get("guest_name") or "").strip()[:60] or guest.name
        if not name:
            return {"error": "ask the guest's name and mobile number for the booking"}
        raw_mobile = str(a.get("mobile") or "").strip()
        same = raw_mobile.lower() in ("same", "this", "this number", "same number", "whatsapp")
        mobile = phone if same else (try_normalize_phone(raw_mobile) or "")
        if not mobile:
            return {"error": "ask the guest's mobile number for the booking" if not raw_mobile
                    else "that mobile number does not look right; ask the guest to send it again"}
        if name != guest.name:
            guest.name = name
            self.repo.upsert_guest(guest)
        prop = self.repo.get_property(bk["property_id"])
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
            self.repo.create_hold(booking, now)
        except SoldOut:
            bk.pop("chosen", None)
            return {"error": "those rooms were just taken by someone else; call check_availability again"}
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
        st["booking_id"] = booking.id
        st.pop("bk", None)
        leads.save_lead(self.repo, phone, now, stage="held", booking_id=booking.id,
                        from_city=(a.get("from_city") or "").strip()[:60] or None)
        ctx.summary_message = summary
        ctx.pay_message = t("pay", lang, id=booking.id, minutes=self.config.hold_minutes,
                            advance=f"{booking.advance:,}", url=link.url)
        return {"ok": True, "booking_id": booking.id,
                "note": "the enquiry summary and the payment link are sent automatically. Reply exactly [no reply]."}

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
        paid_now = self.check_payments()  # a guest who says "I paid" gets their confirmation without waiting
        rows = []
        for b in self.repo.list_bookings(phone=ctx.phone, limit=5):
            prop = self.repo.get_property(b.property_id)
            row = {"id": b.id, "place": self._place_label(prop) if prop else "", "check_in": b.check_in.isoformat(),
                   "check_out": b.check_out.isoformat(), "rooms": b.rooms, "status": status_name(b.status, "en"),
                   "total": b.total, "paid": b.paid_amount, "balance_at_property": b.balance}
            if b.status == "hold" and b.payment_link_url:
                row["payment_link"] = b.payment_link_url
            rows.append(row)
        out: dict[str, Any] = {"bookings": rows} if rows else {"bookings": [], "note": "this guest has no booking yet"}
        if paid_now:
            out["note"] = "a payment was just found and the confirmation message was sent automatically; do not repeat its details"
        return out

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
