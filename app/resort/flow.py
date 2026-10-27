"""The guest conversation: a tap-option booking flow plus an AI answer for free questions.

Every booking choice is a WhatsApp list or buttons (language, property, check-in date, nights, adults,
children, room type, rooms, confirm). Typing is only for a custom date or number, the guest's name and
questions. The code (not the AI) decides capacity, availability, price and the hold; the AI only
answers questions from the data it is given and can never create a booking.

State per guest lives in repo.get_state(phone): {lang, step, property_id, check_in, nights, adults,
children, room_type_id, rooms, ...}. All replies are text or interactive: this bot never sends voice.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from datetime import date, datetime, timedelta
from typing import Any

from app.core.clock import ist_today
from app.core.language import is_opt_in, is_opt_out
from app.core.phone import mask_phone
from app.providers.llm import ChatMessage, LLMError, LLMProvider
from app.resort import availability as av
from app.resort.config import ResortConfig
from app.resort.db import ResortRepo, SoldOut
from app.resort.i18n import LANG_NAMES, LANGS, detect_script, status_name, t
from app.resort.models import Booking, ChatMessage as Chat, Property, RoomType
from app.resort.payments import Payments, PaymentError
from app.whatsapp.client import WhatsAppClient, WhatsAppError
from app.whatsapp.parse import InboundMessage

log = logging.getLogger(__name__)

AUTO_RESUME = timedelta(hours=24)
FREE_STEPS = {"menu", "ask", "done", "lang"}  # typed text here is a question, not a wrong answer
GREETING = re.compile(r"^\W*(hi+|hello+|hey+|hai|hlo|menu|start|namaste|namaskar\w*|vanakkam|"
                      r"നമസ്കാരം|ഹായ്|ഹലോ|வணக்கம்|ஹாய்|நமஸ்தே|नमस्ते|हाय|हेलो|हैलो)\W*$", re.IGNORECASE)
MONTHS = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}


# --------------------------------------------------------------------------- date parsing


def parse_date(text: str, today: date) -> date | None:
    """'25 Dec', 'Dec 25', '25/12', '25-12-2026', '2026-12-25', 'today', 'tomorrow' -> date, else None.
    Without a year, a date already past this year means next year."""
    s = text.strip().lower()
    if s in ("today", "aaj", "indru"):
        return today
    if s in ("tomorrow", "kal", "naalai"):
        return today + timedelta(days=1)
    found: tuple[int, int, int | None] | None = None
    if m := re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", s):
        found = (int(m[3]), int(m[2]), int(m[1]))
    elif m := re.fullmatch(r"(\d{1,2})[/\-.](\d{1,2})(?:[/\-.](\d{2,4}))?", s):
        year = int(m[3]) if m[3] else None
        found = (int(m[1]), int(m[2]), year + 2000 if year is not None and year < 100 else year)
    elif m := re.fullmatch(r"(\d{1,2})(?:st|nd|rd|th)?\s*(?:of\s+)?([a-z]{3,9})\.?(?:,?\s*(\d{4}))?", s):
        if m[2][:3] in MONTHS:
            found = (int(m[1]), MONTHS[m[2][:3]], int(m[3]) if m[3] else None)
    elif m := re.fullmatch(r"([a-z]{3,9})\.?\s*(\d{1,2})(?:st|nd|rd|th)?(?:,?\s*(\d{4}))?", s):
        if m[1][:3] in MONTHS:
            found = (int(m[2]), MONTHS[m[1][:3]], int(m[3]) if m[3] else None)
    if not found:
        return None
    day, month, year = found
    try:
        result = date(year or today.year, month, day)
        if year is None and result < today:
            result = date(today.year + 1, month, day)
    except ValueError:
        return None
    return result


def fmt_date(d: date) -> str:
    return f"{d.day} {d.strftime('%b')} {d.year}"


def _short_date(d: date) -> str:
    return f"{d.day} {d.strftime('%b')} ({d.strftime('%a')})"


def _int_in(text: str, lo: int, hi: int) -> int | None:
    m = re.fullmatch(r"\D*(\d{1,3})\D*", text.strip())
    if not m:
        return None
    n = int(m[1])
    return n if lo <= n <= hi else None


# --------------------------------------------------------------------------- the bot


class BookingBot:
    def __init__(self, config: ResortConfig, repo: ResortRepo, whatsapp: WhatsAppClient, payments: Payments,
                 clock: Any, *, llm: Callable[[], LLMProvider] | None = None,
                 voice_in: Callable[[], Any] | None = None) -> None:
        self.config = config
        self.repo = repo
        self.wa = whatsapp
        self.payments = payments
        self.clock = clock
        self._llm = llm
        self._voice_in = voice_in or (lambda: None)

    # ------------------------------------------------------------------ sending

    def _log(self, phone: str, text: str, source: str = "text") -> None:
        self.repo.add_chat(Chat(phone, "out", text, self.clock.now(), source))

    def _text(self, phone: str, text: str) -> bool:
        try:
            self.wa.send_text(phone, text)
        except WhatsAppError as exc:
            log.error("Could not send to %s: %s", mask_phone(phone), exc)
            return False
        self._log(phone, text)
        return True

    def _list(self, phone: str, body: str, button: str, rows: list[tuple[str, str, str]]) -> bool:
        try:
            self.wa.send_interactive_list(phone, body, button, rows[:10])
        except WhatsAppError as exc:
            log.error("Could not send list to %s: %s", mask_phone(phone), exc)
            return False
        self._log(phone, body + "\n" + "\n".join(f"- {r[1]}" for r in rows[:10]), "list")
        return True

    def _buttons(self, phone: str, body: str, buttons: list[tuple[str, str]]) -> bool:
        try:
            self.wa.send_reply_buttons(phone, body, buttons[:3])
        except WhatsAppError as exc:
            log.error("Could not send buttons to %s: %s", mask_phone(phone), exc)
            return False
        self._log(phone, body + "\n" + " | ".join(b[1] for b in buttons[:3]), "buttons")
        return True

    # ------------------------------------------------------------------ entry point

    def handle(self, msg: InboundMessage) -> str:
        now, phone = self.clock.now(), msg.phone
        if not self.repo.mark_event_processed(msg.id, now):
            return "duplicate"
        st = self.repo.get_state(phone)
        lang = st.get("lang", "en")
        guest = self.repo.touch_guest(phone, now, language=st.get("lang"))

        text, source = msg.text.strip(), ("button" if msg.reply_id else "text")
        if msg.type == "audio":
            try:
                self.wa.mark_read(msg.id)
            except Exception:  # best effort only
                pass
            voice = self._voice_in()
            result = voice.process(msg.media_id) if voice else None
            if result is None or result.text is None:
                self.repo.add_chat(Chat(phone, "in", "[voice note: not understood]", now, "voice"))
                self._text(phone, t("voice_problem", lang))
                return "voice_problem"
            text, source = result.text, "voice"
        elif not msg.is_text:
            self.repo.add_chat(Chat(phone, "in", f"[{msg.type}]", now, "text"))
            if msg.type not in ("reaction", "sticker"):
                self._text(phone, t("unsupported", lang))
            return "unsupported"
        self.repo.add_chat(Chat(phone, "in", text, now, source))

        paused, since = self.repo.is_paused(phone)
        if paused:
            if since is not None and now - since >= AUTO_RESUME:
                self.repo.set_paused(phone, False)
            else:
                return "paused"
        try:
            self.wa.mark_read(msg.id)
        except Exception:
            pass
        return self._dispatch(phone, st, text, None if source != "button" else msg.reply_id, source, guest.name)

    # ------------------------------------------------------------------ routing

    def _save(self, phone: str, st: dict[str, Any]) -> None:
        self.repo.set_state(phone, st)

    def _dispatch(self, phone: str, st: dict[str, Any], text: str, rid: str | None, source: str,
                  guest_name: str) -> str:
        typed = rid is None
        lang = st.get("lang")

        if typed:
            if st.get("opted_out") and is_opt_in(text):
                st["opted_out"] = False
                self._save(phone, st)
                guest = self.repo.touch_guest(phone, self.clock.now())
                guest.offers_consent = True
                self.repo.upsert_guest(guest)
                self._text(phone, t("opt_in", lang or "en"))
                return "opt_in"
            if is_opt_out(text) and not st.get("opted_out"):
                st["opted_out"] = True
                self._save(phone, st)
                guest = self.repo.touch_guest(phone, self.clock.now())
                guest.offers_consent = False
                self.repo.upsert_guest(guest)
                self._text(phone, t("opt_out", lang or "en"))
                return "opt_out"
            script = detect_script(text, lang or "en")
            if lang and script != lang and re.search(r"[ऀ-ॿ஀-௿ഀ-ൿ]", text):
                lang = st["lang"] = script  # they switched language by typing in another script

        # First contact: a typed script picks the language; otherwise show the language list.
        if not lang:
            if rid and rid.startswith("lang:") and rid[5:] in LANGS:
                st["lang"] = rid[5:]
                return self._show_menu(phone, st)
            script = detect_script(text, "en") if typed else "en"
            if script != "en":
                st["lang"] = script
                return self._show_menu(phone, st)
            st["step"] = "lang"
            self._save(phone, st)
            self._send_step(phone, st)
            return "language_picker"
        lang = st["lang"]

        if rid and rid.startswith("lang:") and rid[5:] in LANGS:  # changed language from an old list
            st["lang"] = rid[5:]
            return self._show_menu(phone, st)
        if typed and GREETING.match(text):
            return self._show_menu(phone, st)

        step = st.get("step", "menu")
        handler = getattr(self, f"_step_{step}", None)
        if handler is None:
            return self._show_menu(phone, st)
        outcome = handler(phone, st, text, rid, guest_name)
        if outcome is not None:
            return outcome
        # Not an answer to this step: stale button, or a typed question.
        if typed and text:
            self._answer(phone, st, text)
            if step not in FREE_STEPS:
                self._send_step(phone, st)
            return "answered"
        self._send_step(phone, st)
        return "reprompt"

    # ------------------------------------------------------------------ prompts (one per step)

    def _show_menu(self, phone: str, st: dict[str, Any]) -> str:
        for key in ("property_id", "check_in", "nights", "adults", "children", "room_type_id", "rooms", "booking_id"):
            st.pop(key, None)
        st["step"] = "menu"
        self._save(phone, st)
        self._send_step(phone, st)
        return "menu"

    def _brand(self) -> str:
        return self.config.display_name

    def _send_step(self, phone: str, st: dict[str, Any]) -> None:
        lang, step = st.get("lang", "en"), st.get("step", "menu")
        today = self._today()
        if step == "lang":
            self._list(phone, t("lang_body", "en"), t("lang_button", "en"),
                       [(f"lang:{c}", LANG_NAMES[c], "") for c in LANGS])
        elif step == "menu":
            self._list(phone, t("menu_body", lang, brand=self._brand()), t("menu_button", lang), [
                ("menu:book", t("menu_book", lang), ""), ("menu:ask", t("menu_ask", lang), ""),
                ("menu:status", t("menu_status", lang), ""), ("menu:staff", t("menu_staff", lang), "")])
        elif step == "property":
            props = self.repo.list_properties()
            self._list(phone, t("prop_body", lang), t("prop_button", lang),
                       [(f"prop:{p.id}", p.name, p.city) for p in props])
        elif step == "date":
            rows = [("d:" + (today + timedelta(days=i)).isoformat(),
                     t("date_today", lang) if i == 0 else t("date_tomorrow", lang) if i == 1
                     else _short_date(today + timedelta(days=i)),
                     _short_date(today + timedelta(days=i)) if i < 2 else "") for i in range(0, 9)]
            rows.append(("d:other", t("date_other", lang), t("date_other_desc", lang)))
            self._list(phone, t("date_body", lang), t("date_button", lang), rows)
        elif step == "date_typed":
            self._text(phone, t("date_type", lang))
        elif step == "nights":
            rows = [(f"n:{n}", t("night_1" if n == 1 else "night_n", lang, n=n), "") for n in range(1, 8)]
            rows.append(("n:more", t("more", lang), t("more_desc", lang)))
            ci = date.fromisoformat(st["check_in"])
            self._list(phone, t("nights_body", lang, date=fmt_date(ci)), t("nights_button", lang), rows)
        elif step == "nights_typed":
            self._text(phone, t("nights_type", lang, max=av.MAX_NIGHTS))
        elif step == "adults":
            rows = [(f"a:{n}", t("adult_1" if n == 1 else "adult_n", lang, n=n), "") for n in range(1, 10)]
            rows.append(("a:more", t("more", lang), t("more_desc", lang)))
            self._list(phone, t("adults_body", lang), t("adults_button", lang), rows)
        elif step == "adults_typed":
            self._text(phone, t("adults_type", lang, max=av.MAX_GUESTS))
        elif step == "children":
            rows = [(f"c:{n}", t("child_0" if n == 0 else "child_1" if n == 1 else "child_n", lang, n=n), "")
                    for n in range(0, 7)]
            rows.append(("c:more", t("more", lang), t("more_desc", lang)))
            self._list(phone, t("children_body", lang), t("children_button", lang), rows)
        elif step == "children_typed":
            self._text(phone, t("children_type", lang, max=av.MAX_GUESTS))
        elif step == "type":
            self._send_types(phone, st)
        elif step == "nofit":
            guests = st.get("adults", 0) + st.get("children", 0)
            self._buttons(phone, t("no_fit", lang, guests=guests), [
                ("nf_guests", t("no_fit_change", lang)), ("nf_dates", t("no_fit_dates", lang)),
                ("nf_staff", t("no_fit_staff", lang))])
        elif step == "rooms":
            self._send_rooms(phone, st)
        elif step == "confirm":
            self._send_summary(phone, st)
        elif step == "name":
            self._text(phone, t("ask_name", lang))
        elif step == "ask":
            self._text(phone, t("ask_prompt", lang))

    # ------------------------------------------------------------------ helpers over the stay in `st`

    def _stay(self, st: dict[str, Any]) -> tuple[Property, date, date]:
        prop = self.repo.get_property(st["property_id"])
        ci = date.fromisoformat(st["check_in"])
        return prop, ci, ci + timedelta(days=st["nights"])  # type: ignore[return-value]

    def _fits(self, st: dict[str, Any]) -> list[tuple[RoomType, list[int]]]:
        """Room types with free rooms for this party, each with the room counts that may be chosen."""
        prop, ci, co = self._stay(st)
        now = self.clock.now()
        out = []
        for rt in self.repo.list_room_types(prop.id):
            options = av.room_options(rt, st["adults"], st["children"], self.repo.free_rooms(rt, ci, co, now))
            if options:
                out.append((rt, options))
        return out

    def _send_types(self, phone: str, st: dict[str, Any]) -> None:
        lang = st.get("lang", "en")
        prop, ci, co = self._stay(st)
        fits = self._fits(st)
        if not fits:
            st["step"] = "nofit"
            self._save(phone, st)
            self._send_step(phone, st)
            return
        rows = []
        for rt, options in fits[:10]:
            q = av.quote(prop, rt, self.repo.list_rate_rules(rt.id), ci, co, options[0])
            rows.append((f"rt:{rt.id}", rt.name, t("type_desc", lang, total=f"{q.total:,}", rooms=options[0])))
        guests = st["adults"] + st["children"]
        self._list(phone, t("type_body", lang, nights=st["nights"], guests=guests), t("type_button", lang), rows)

    def _send_rooms(self, phone: str, st: dict[str, Any]) -> None:
        lang = st.get("lang", "en")
        rt = self.repo.get_room_type(st["room_type_id"])
        _, options = next(((r, o) for r, o in self._fits(st) if r.id == rt.id), (rt, []))
        rows = [(f"r:{n}", t("room_1" if n == 1 else "room_n", lang, n=n),
                 t("rooms_min_desc", lang) if i == 0 else "") for i, n in enumerate(options[:10])]
        self._list(phone, t("rooms_body", lang, guests=st["adults"] + st["children"], type=rt.name,
                            cap=rt.max_occupancy), t("rooms_button", lang), rows)

    def _quote(self, st: dict[str, Any]) -> tuple[Property, RoomType, date, date, av.Quote]:
        prop, ci, co = self._stay(st)
        rt = self.repo.get_room_type(st["room_type_id"])
        return prop, rt, ci, co, av.quote(prop, rt, self.repo.list_rate_rules(rt.id), ci, co, st["rooms"])

    def _send_summary(self, phone: str, st: dict[str, Any]) -> None:
        lang = st.get("lang", "en")
        prop, rt, ci, co, q = self._quote(st)
        balance = t("balance_line", lang, balance=f"{q.total - q.advance:,}") if q.advance < q.total else ""
        self._buttons(phone, t("summary", lang, property=prop.name, room_type=rt.name, rooms=q.rooms,
                               check_in=fmt_date(ci), check_out=fmt_date(co), nights=q.nights,
                               adults=st["adults"], children=st["children"], total=f"{q.total:,}",
                               advance=f"{q.advance:,}", balance_line=balance),
                      [("ok", t("confirm", lang)), ("chg", t("change", lang))])

    # ------------------------------------------------------------------ step handlers (None = not an answer)

    def _goto(self, phone: str, st: dict[str, Any], step: str) -> str:
        st["step"] = step
        self._save(phone, st)
        self._send_step(phone, st)
        return step

    def _step_lang(self, phone, st, text, rid, name):
        return None

    def _step_menu(self, phone, st, text, rid, name):
        lang = st["lang"]
        if rid == "menu:book":
            props = self.repo.list_properties()
            if len(props) == 1:
                st["property_id"] = props[0].id
                return self._goto(phone, st, "date")
            return self._goto(phone, st, "property")
        if rid == "menu:ask":
            return self._goto(phone, st, "ask")
        if rid == "menu:staff":
            return self.handoff(phone, st, "guest asked for the team")
        if rid == "menu:status":
            latest = self.repo.list_bookings(phone=phone, limit=1)
            if not latest:
                self._text(phone, t("no_booking", lang))
            else:
                b = latest[0]
                prop = self.repo.get_property(b.property_id)
                msg = t("status", lang, id=b.id, property=prop.name if prop else "", check_in=fmt_date(b.check_in),
                        check_out=fmt_date(b.check_out), rooms=b.rooms, status=status_name(b.status, lang))
                if b.status == "hold" and b.payment_link_url:
                    msg += "\n" + t("retry_link", lang, url=b.payment_link_url)
                self._text(phone, msg)
            return "status"
        return None

    def _step_ask(self, phone, st, text, rid, name):
        return None  # typed text is answered by the AI in _dispatch

    def _step_done(self, phone, st, text, rid, name):
        return None

    def _step_property(self, phone, st, text, rid, name):
        if rid and rid.startswith("prop:"):
            prop = self.repo.get_property(rid[5:])
            if prop and prop.active:
                st["property_id"] = prop.id
                return self._goto(phone, st, "date")
        return None

    def _set_date(self, phone: str, st: dict[str, Any], d: date) -> str:
        if d < self._today():
            self._text(phone, t("date_past", st["lang"]))
            return self._goto(phone, st, "date")
        st["check_in"] = d.isoformat()
        return self._goto(phone, st, "nights")

    def _today(self) -> date:
        return ist_today(self.clock)

    def _step_date(self, phone, st, text, rid, name):
        if rid == "d:other":
            return self._goto(phone, st, "date_typed")
        if rid and rid.startswith("d:"):
            try:
                return self._set_date(phone, st, date.fromisoformat(rid[2:]))
            except ValueError:
                return None
        if not rid and (d := parse_date(text, self._today())):
            return self._set_date(phone, st, d)
        return None

    def _step_date_typed(self, phone, st, text, rid, name):
        if rid:
            return None
        d = parse_date(text, self._today())
        if d is None:
            self._text(phone, t("date_bad", st["lang"]))
            return "date_bad"
        return self._set_date(phone, st, d)

    def _number_step(self, phone, st, text, rid, prefix, lo, hi, key, typed_step, next_step):
        if rid == f"{prefix}:more":
            return self._goto(phone, st, typed_step)
        n = None
        if rid and rid.startswith(f"{prefix}:"):
            n = _int_in(rid[len(prefix) + 1:], lo, hi)
        elif not rid:
            n = _int_in(text, lo, hi)
        if n is None:
            return None
        st[key] = n
        return self._goto(phone, st, next_step)

    def _step_nights(self, phone, st, text, rid, name):
        return self._number_step(phone, st, text, rid, "n", 1, av.MAX_NIGHTS, "nights", "nights_typed", "adults")

    def _step_nights_typed(self, phone, st, text, rid, name):
        if rid:
            return None
        if self._number_step(phone, st, text, None, "n", 1, av.MAX_NIGHTS, "nights", "nights_typed", "adults") is None:
            self._text(phone, t("nights_type", st["lang"], max=av.MAX_NIGHTS))
            return "nights_bad"
        return "adults"

    def _step_adults(self, phone, st, text, rid, name):
        return self._number_step(phone, st, text, rid, "a", 1, av.MAX_GUESTS, "adults", "adults_typed", "children")

    def _step_adults_typed(self, phone, st, text, rid, name):
        if rid:
            return None
        if self._number_step(phone, st, text, None, "a", 1, av.MAX_GUESTS, "adults", "adults_typed", "children") is None:
            self._text(phone, t("adults_type", st["lang"], max=av.MAX_GUESTS))
            return "adults_bad"
        return "children"

    def _after_children(self, phone, st) -> str:
        return self._goto(phone, st, "type")

    def _step_children(self, phone, st, text, rid, name):
        if rid == "c:more":
            return self._goto(phone, st, "children_typed")
        n = _int_in(rid[2:], 0, av.MAX_GUESTS) if rid and rid.startswith("c:") else (
            _int_in(text, 0, av.MAX_GUESTS) if not rid else None)
        if n is None:
            return None
        st["children"] = n
        return self._after_children(phone, st)

    def _step_children_typed(self, phone, st, text, rid, name):
        if rid:
            return None
        n = _int_in(text, 0, av.MAX_GUESTS)
        if n is None:
            self._text(phone, t("children_type", st["lang"], max=av.MAX_GUESTS))
            return "children_bad"
        st["children"] = n
        return self._after_children(phone, st)

    def _step_nofit(self, phone, st, text, rid, name):
        if rid == "nf_guests":
            st.pop("adults", None)
            st.pop("children", None)
            return self._goto(phone, st, "adults")
        if rid == "nf_dates":
            return self._goto(phone, st, "date")
        if rid == "nf_staff":
            return self.handoff(phone, st, "no room fits the party")
        return None

    def _step_type(self, phone, st, text, rid, name):
        if not (rid and rid.startswith("rt:")):
            return None
        chosen = next(((rt, o) for rt, o in self._fits(st) if rt.id == rid[3:]), None)
        if chosen is None:
            return self._goto(phone, st, "type")  # no longer free: show the fresh list
        rt, options = chosen
        st["room_type_id"] = rt.id
        if len(options) == 1:
            st["rooms"] = options[0]
            return self._goto(phone, st, "confirm")
        return self._goto(phone, st, "rooms")

    def _step_rooms(self, phone, st, text, rid, name):
        options = next((o for rt, o in self._fits(st) if rt.id == st["room_type_id"]), [])
        n = _int_in(rid[2:], 1, 99) if rid and rid.startswith("r:") else (_int_in(text, 1, 99) if not rid else None)
        if n is None:
            return None
        if n not in options:  # never accept a count we did not offer (e.g. 1 room for 10 guests)
            return self._goto(phone, st, "rooms")
        st["rooms"] = n
        return self._goto(phone, st, "confirm")

    def _step_confirm(self, phone, st, text, rid, name):
        if rid == "chg":
            for key in ("property_id", "check_in", "nights", "adults", "children", "room_type_id", "rooms"):
                st.pop(key, None)
            return self._show_menu(phone, st)
        if rid == "ok":
            guest = self.repo.get_guest(phone)
            if guest is None or not guest.name:
                return self._goto(phone, st, "name")
            return self._create_booking(phone, st)
        return None

    def _step_name(self, phone, st, text, rid, name):
        if rid or not text or len(text) > 60 or "?" in text:
            return None
        guest = self.repo.touch_guest(phone, self.clock.now())
        guest.name = text.strip()
        self.repo.upsert_guest(guest)
        return self._create_booking(phone, st)

    # ------------------------------------------------------------------ booking creation

    def _create_booking(self, phone: str, st: dict[str, Any]) -> str:
        lang, now = st["lang"], self.clock.now()
        prop, rt, ci, co, q = self._quote(st)
        guest = self.repo.touch_guest(phone, now, language=lang)
        hold = timedelta(minutes=self.config.hold_minutes)
        booking = Booking(prop.id, rt.id, phone, ci, co, st["adults"], st["children"], st["rooms"], q.total, q.advance,
                          now, guest_name=guest.name, language=lang, hold_expires_at=now + hold)
        try:
            self.repo.create_hold(booking, now)
        except SoldOut:
            self._text(phone, t("soldout", lang))
            return self._goto(phone, st, "type")
        try:  # Razorpay wants a link that lives at least 15 minutes
            link = self.payments.create_payment_link(
                booking.advance, booking.id, guest.name, phone,
                f"{prop.name}: {rt.name} x {booking.rooms}, {fmt_date(ci)} to {fmt_date(co)}",
                int((now + max(hold, timedelta(minutes=16))).timestamp()))
        except PaymentError as exc:
            log.error("Payment link failed for %s: %s", booking.id, exc)
            booking.status, booking.notes = "failed", f"payment link failed: {exc}"
            self.repo.save_booking(booking)
            self._text(phone, t("pay_failed_link", lang))
            st["step"] = "done"
            return self.handoff(phone, st, f"payment link could not be created for {booking.id}")
        booking.payment_link_id, booking.payment_link_url = link.id, link.url
        self.repo.save_booking(booking)
        st["booking_id"], st["step"] = booking.id, "done"
        self._save(phone, st)
        self._text(phone, t("pay", lang, id=booking.id, minutes=self.config.hold_minutes,
                            advance=f"{booking.advance:,}", url=link.url))
        return "payment_link"

    # ------------------------------------------------------------------ staff handoff

    def staff_numbers(self, property_id: str | None = None) -> list[str]:
        numbers = list(self.config.staff_numbers)
        props = [self.repo.get_property(property_id)] if property_id else self.repo.list_properties()
        for p in props:
            if p:
                numbers += p.staff_numbers
        return list(dict.fromkeys(numbers))

    def alert_staff(self, text: str, property_id: str | None = None) -> None:
        for number in self.staff_numbers(property_id):
            try:
                self.wa.send_text(number, text)
            except WhatsAppError as exc:  # outside their 24 h window: needs a template at go-live
                log.warning("Staff alert to %s failed: %s", mask_phone(number), exc)

    def handoff(self, phone: str, st: dict[str, Any], reason: str) -> str:
        now = self.clock.now()
        guest = self.repo.get_guest(phone)
        recent = self.repo.list_chat(phone, limit=6)
        lines = [f"{'Guest' if m.direction == 'in' else 'Bot'}: {m.text[:120]}" for m in recent]
        summary = (f"Guest needs the team: {reason}\n{guest.name if guest and guest.name else 'Unknown'} {phone}\n"
                   f"Language: {st.get('lang', 'en')}\n" + "\n".join(lines))
        self.repo.set_paused(phone, True, now)
        self._text(phone, t("handoff", st.get("lang", "en")))
        self.alert_staff(summary, st.get("property_id"))
        return "handoff"

    # ------------------------------------------------------------------ free questions (AI)

    def _knowledge(self, st: dict[str, Any]) -> str:
        lines = []
        props = [self.repo.get_property(st["property_id"])] if st.get("property_id") else self.repo.list_properties()
        for p in props:
            if p is None:
                continue
            lines.append(f"PROPERTY {p.name} ({p.city}). {p.description} Address: {p.address} {p.map_url}. "
                         f"Check-in {p.checkin_time}, check-out {p.checkout_time}. Advance to confirm: "
                         f"{p.advance_pct}%. Policies: {p.policies or 'the team will confirm'}")
            for rt in self.repo.list_room_types(p.id):
                lines.append(f"  ROOM {rt.name}: up to {rt.max_adults} adults and {rt.max_children} children per "
                             f"room, max {rt.max_occupancy} guests; from Rs {rt.base_rate} per night "
                             f"(weekend Rs {rt.weekend_rate or rt.base_rate}). {rt.description} {rt.amenities}")
        return "\n".join(lines)

    def _answer(self, phone: str, st: dict[str, Any], text: str) -> None:
        lang = st.get("lang", "en")
        name = {"en": "English", "ml": "Malayalam", "ta": "Tamil", "hi": "Hindi"}[lang]
        system = (
            f"You are the WhatsApp assistant of {self._brand()}, a resort booking service. {self.config.speaking_style}\n"
            f"Reply in {name} (use its own script), in at most 3 short sentences, plain text.\n"
            "Use ONLY the facts below. Never invent prices, dates, availability, offers or policies; if a fact is "
            "missing say the team will confirm. You cannot create or change bookings: for booking, tell the guest to "
            "type MENU and choose 'Book a stay'. Never promise anything.\n\nFACTS\n" + self._knowledge(st))
        history = [ChatMessage("user" if m.direction == "in" else "assistant", m.text[:300])
                   for m in self.repo.list_chat(phone, limit=6)[:-1] if m.source in ("text", "voice")]
        while history and history[0].role != "user":
            history.pop(0)
        try:
            if self._llm is None:
                raise LLMError("none", "no LLM configured")
            reply = self._llm().complete(system, [*history, ChatMessage.user(text)], None, 400).text.strip()
        except LLMError as exc:
            log.warning("AI answer failed: %s", exc)
            reply = ""
        except Exception as exc:  # e.g. LLMNotConfigured: a missing key must not break the booking flow
            log.warning("AI unavailable: %s", exc)
            reply = ""
        if not reply:
            self._text(phone, t("ai_down", lang))
            self.alert_staff(f"Bot could not answer a question from {phone}: {text[:200]}", st.get("property_id"))
            return
        self._text(phone, reply)

    # ------------------------------------------------------------------ payment events and sweeps

    def _notify_guest(self, booking: Booking, text: str, template_key: str, params: list[str]) -> None:
        phone = booking.guest_phone
        try:
            self.wa.send_text(phone, text)
            self._log(phone, text, "system")
            return
        except WhatsAppError as exc:
            if exc.code != 131047:  # 131047 = outside the 24 h window
                log.error("Could not notify %s: %s", mask_phone(phone), exc)
                return
        name = getattr(self.config.templates, template_key, "")
        if not name:
            log.warning("Guest %s is outside the 24 h window and no %s template is set", mask_phone(phone), template_key)
            return
        try:
            self.wa.send_template(phone, name, booking.language if booking.language in LANGS else "en", params)
            self._log(phone, f"[template {name}] " + text, "system")
        except WhatsAppError as exc:
            log.error("Template %s to %s failed: %s", name, mask_phone(phone), exc)

    def _params(self, b: Booking) -> list[str]:
        prop = self.repo.get_property(b.property_id)
        return [b.id, prop.name if prop else "", fmt_date(b.check_in), fmt_date(b.check_out), str(b.rooms)]

    def mark_paid(self, *, booking_id: str = "", link_id: str = "", payment_id: str = "") -> str:
        """Razorpay says the link is paid. Idempotent. Returns paid | already_paid | refund_needed | unknown."""
        b = (self.repo.get_booking(booking_id) if booking_id else None) or (
            self.repo.find_booking_by_link(link_id) if link_id else None)
        if b is None:
            return "unknown"
        if b.status == "paid":
            return "already_paid"
        now = self.clock.now()
        rt = self.repo.get_room_type(b.room_type_id)
        prop = self.repo.get_property(b.property_id)
        if b.status in ("expired", "cancelled") and rt is not None \
                and self.repo.free_rooms(rt, b.check_in, b.check_out, now) < b.rooms:
            b.notes = (b.notes + " PAID AFTER EXPIRY, ROOMS GONE: REFUND NEEDED").strip()
            b.razorpay_payment_id = payment_id or b.razorpay_payment_id
            self.repo.save_booking(b)
            self.alert_staff(f"REFUND NEEDED: {b.id} was paid after the hold expired and the rooms are gone "
                             f"({b.guest_name} {b.guest_phone}, Rs {b.advance}).", b.property_id)
            return "refund_needed"
        b.status, b.paid_at = "paid", now
        b.razorpay_payment_id = payment_id or b.razorpay_payment_id
        b.hold_expires_at = None
        self.repo.save_booking(b)
        text = t("paid", b.language, id=b.id, property=prop.name if prop else "", check_in=fmt_date(b.check_in),
                 check_out=fmt_date(b.check_out), rooms=b.rooms, checkin_time=prop.checkin_time if prop else "")
        self._notify_guest(b, text, "paid", self._params(b))
        self.alert_staff(f"New booking {b.id} PAID: {prop.name if prop else ''}, {rt.name if rt else ''} x {b.rooms}, "
                         f"{fmt_date(b.check_in)} to {fmt_date(b.check_out)}, {b.adults}A {b.children}C. "
                         f"{b.guest_name} {b.guest_phone}. Advance Rs {b.advance} of Rs {b.total}.", b.property_id)
        return "paid"

    def payment_failed(self, *, booking_id: str = "", link_id: str = "") -> str:
        b = (self.repo.get_booking(booking_id) if booking_id else None) or (
            self.repo.find_booking_by_link(link_id) if link_id else None)
        if b is None or b.status == "paid":
            return "ignored"
        text = t("failed", b.language, id=b.id)
        if b.status == "hold" and b.payment_link_url:
            text += "\n" + t("retry_link", b.language, url=b.payment_link_url)
        self._notify_guest(b, text, "failed", self._params(b))
        return "notified"

    def payment_expired(self, *, booking_id: str = "", link_id: str = "") -> str:
        b = (self.repo.get_booking(booking_id) if booking_id else None) or (
            self.repo.find_booking_by_link(link_id) if link_id else None)
        if b is None or b.status != "hold":
            return "ignored"
        b.status = "expired"
        self.repo.save_booking(b)
        self._notify_guest(b, t("expired", b.language, id=b.id), "failed", self._params(b))
        return "expired"

    def sweep(self, now: datetime | None = None) -> dict[str, int]:
        """Run every minute or so: expire unpaid holds (and tell the guests), nudge holds about to expire."""
        now = now or self.clock.now()
        expired = self.repo.expire_holds(now)
        for b in expired:
            self._notify_guest(b, t("expired", b.language, id=b.id), "failed", self._params(b))
        nudged = 0
        for b in self.repo.list_bookings(status="hold", limit=200):
            if (b.hold_expires_at and b.payment_link_url and "[reminded]" not in b.notes
                    and timedelta(0) < b.hold_expires_at - now <= timedelta(minutes=5)):
                b.notes = (b.notes + " [reminded]").strip()
                self.repo.save_booking(b)
                mins = max(1, int((b.hold_expires_at - now).total_seconds() // 60))
                self._notify_guest(b, t("pending", b.language, id=b.id, minutes=mins, url=b.payment_link_url),
                                   "pending", self._params(b))
                nudged += 1
        return {"expired": len(expired), "reminded": nudged}
