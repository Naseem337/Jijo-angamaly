"""Run the resort bot through tests/eval/resort_scenarios.yaml and check what it does (Resort Step 2).

    .venv\\Scripts\\python.exe scripts\\resort_eval.py --mock                 # code-level checks, no AI, fast
    .venv\\Scripts\\python.exe scripts\\resort_eval.py --live --only A,G      # real AI (.env LLM_CHAIN)
    .venv\\Scripts\\python.exe scripts\\resort_eval.py --live --id B01,G05

Every scenario gets a fresh temp SQLite DB (seed data), the real BookingBot, a recording fake WhatsApp (nothing is
sent) and mock payments. --mock uses an AI that always stays silent, so only what the CODE does is judged (fixed
steps, language detection, dedupe, concurrency, STOP/START, AI-down fallback, no audio); expectations that need a
real AI are marked "skipped (needs --live)". --live uses the real AI chain; voice turns are spoken with Sarvam TTS
and go through the real voice-in path (Sarvam STT).

Results: data/eval/resort_<mode>_<timestamp>.json and .md (git-ignored).
"""

from __future__ import annotations

import argparse
import itertools
import json
import re
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import os  # noqa: E402

os.environ.setdefault("RESORT_STAFF_NUMBERS", "")
import yaml  # noqa: E402

from app.conversation.voice_in import VoiceResult  # noqa: E402
from app.core.clock import IST, FixedClock  # noqa: E402
from app.providers.llm.base import LLMError, LLMResponse  # noqa: E402
from app.resort import chat as rchat  # noqa: E402
from app.core.language import is_language_neutral  # noqa: E402
from app.resort import seed  # noqa: E402
from app.resort.config import load_resort_config  # noqa: E402
from app.resort.db import ResortRepo, SoldOut  # noqa: E402
from app.resort.flow import BookingBot  # noqa: E402
from app.resort.i18n import HN, MN, TN, S  # noqa: E402
from app.resort.models import Booking, RoomType  # noqa: E402
from app.resort.payments import MockPayments  # noqa: E402
from app.whatsapp.parse import InboundMessage  # noqa: E402

SCENARIOS = ROOT / "tests" / "eval" / "resort_scenarios.yaml"
OUT = ROOT / "data" / "eval"
GUEST, OTHER, STAFF = "+919847000555", "+919847000666", "+919847000999"
START = datetime(2026, 11, 18, 11, 0, tzinfo=IST)  # a Wednesday morning
NATIVE = re.compile(r"[ऀ-ॿ஀-௿ഀ-ൿ]")
MONEY = re.compile(r"(?:₹|\bRs\.?\s?)\s?(\d[\d,]*)|(\d[\d,]{2,})\s*(?:rupees|rs\b)", re.IGNORECASE)
VOICE_LANG = {"ml": "ml-IN", "ta": "ta-IN", "en": "en-IN", "hi": "hi-IN"}
# Owner decisions (docs/resort_gap_report.md): these are judged by the owner's rules, not the scenario text.
OWNER_RULES = {"A01": "one room type by priority, price only in the glimpse",
               "E02": "images are not read or answered",
               "J07": "photos only after availability is checked",
               "M04": "day-before message is only 'Please bring your ID proof while coming.'"}


# --------------------------------------------------------------------------- fakes


class RecordingWA:
    """Nothing is sent: every outgoing message is recorded with its kind."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []  # (to, kind, text / media)
        self.media: dict[str, tuple[bytes, str]] = {}
        self._lock = threading.Lock()

    def _add(self, to: str, kind: str, body: str) -> str:
        with self._lock:
            self.sent.append((to, kind, body))
            return f"wamid.out{len(self.sent)}"

    def send_text(self, to, text):
        return self._add(to, "text", text)

    def send_template(self, to, name, language, params, **kw):
        return self._add(to, "template", name)

    def send_image(self, to, media_id, caption=""):
        return self._add(to, "image", media_id)

    def send_document(self, to, media_id, filename, caption=""):
        return self._add(to, "document", filename)

    def send_audio(self, to, media_id, *, voice=True):  # must never happen (owner rule: no voice replies)
        return self._add(to, "audio", media_id)

    def upload_media(self, data, mime_type="image/jpeg", filename="x"):
        return f"media-{len(data)}"

    def mark_read(self, *a, **k):
        return True

    def download_media(self, media_id, **kw):
        return self.media[media_id]


class SilentLLM:
    """--mock: the AI always stays silent, so only the code's own steps run."""

    name = "silent"

    def complete(self, system, messages, tools=None, max_tokens=1024):
        return LLMResponse("[no reply]", provider="silent", model="silent")


class DownLLM:
    name = "down"

    def complete(self, *a, **k):
        raise LLMError("down", "AI provider unreachable (test)")


class Spy:
    """Wraps the real AI: keeps the tool results it is shown, so a ₹ figure can be traced to a tool."""

    def __init__(self, inner) -> None:
        self.inner, self.tool_text = inner, []

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def complete(self, system, messages, tools=None, max_tokens=1024):
        self.tool_text.extend(m.content for m in messages if getattr(m, "role", "") == "tool")
        return self.inner.complete(system, messages, tools, max_tokens)


class FakeVoiceIn:
    """--mock: a 'voice note' is just its words (the real STT is used with --live)."""

    def __init__(self) -> None:
        self.next_text = ""

    def process(self, media_id):
        return VoiceResult(text=self.next_text) if self.next_text else VoiceResult(problem="unclear")


# --------------------------------------------------------------------------- one scenario


@dataclass
class Result:
    id: str
    cat: str
    status: str = "pass"  # pass | fail | skipped
    checks: list[dict] = field(default_factory=list)
    manual: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    transcript: list[str] = field(default_factory=list)

    def check(self, name: str, ok: bool | None, detail: str = "") -> None:
        self.checks.append({"check": name, "result": "skipped" if ok is None else ("pass" if ok else "fail"),
                            "detail": detail})


def _day(text: str, today: date) -> date:
    """'Dec 10' / '+10 days' / 'today' / 'tomorrow' / 'past' -> a date."""
    text = str(text).strip().lower()
    if text in ("today", ""):
        return today
    if text == "tomorrow":
        return today + timedelta(days=1)
    if text == "past":
        return today - timedelta(days=10)
    if m := re.match(r"([+-]\d+)\s*days?", text):
        return today + timedelta(days=int(m.group(1)))
    got = rchat.parse_stay(text, today).get("check_in")
    return got or today + timedelta(days=7)


class Runner:
    def __init__(self, live: bool, pace: float) -> None:
        self.live, self.pace = live, pace
        self.config = load_resort_config()
        self._llm = None
        if live:
            from app.providers.llm import build_llm
            from app.settings import EnvSecretResolver
            self.secrets = EnvSecretResolver()
            self._llm = build_llm(self.config, self.secrets)

    # ---------------------------------------------------------------- setup

    def _world(self, sc: dict, res: Result):
        repo = ResortRepo(Path(tempfile.mkdtemp()) / "r.sqlite3")
        seed.seed(repo)
        setup = sc.get("setup") or {}
        clock = FixedClock(START)
        if "today" in setup:
            clock = FixedClock(datetime.fromisoformat(str(setup["today"])).replace(hour=11, tzinfo=IST))
        if "time" in setup:
            hh, mm = (int(x) for x in re.findall(r"\d+", str(setup["time"]))[:2])
            clock = FixedClock(clock.now().replace(hour=hh, minute=mm))
        wa = RecordingWA()
        llm: Any = DownLLM() if setup.get("llm_down") else (Spy(self._llm) if self.live else SilentLLM())
        voice = self._live_voice(wa) if self.live else FakeVoiceIn()
        bot = BookingBot(self.config, repo, wa, MockPayments(), clock, llm=lambda: llm, voice_in=lambda: voice)
        prop = repo.list_properties()[0]
        prop.staff_numbers = [STAFF]
        repo.save_property(prop)
        types = repo.list_room_types(prop.id)
        today = clock.now().astimezone(IST).date()
        for key, value in setup.items():
            if key in ("today", "time", "llm_down"):
                continue
            if key in ("booking", "bookings"):
                items = value if isinstance(value, list) else [value]
                for i, item in enumerate(items):
                    item = item if isinstance(item, dict) else {"id": item, "checkin": f"+{10 + 5 * i} days"}
                    self._make_booking(repo, prop, types, item, today, clock, res)
            elif key == "availability":
                for when, free in (value or {}).items():
                    if when == "extra_rooms":
                        mine = repo.list_bookings(phone=GUEST, limit=1)
                        if mine:
                            self._fill(repo, prop, types, mine[0].check_in, mine[0].check_out, clock, keep=int(free))
                    else:
                        d = _day(str(when), today)
                        self._fill(repo, prop, types, d, d + timedelta(days=1), clock, keep=int(free))
            elif key == "last_room":
                self._fill(repo, prop, types, today + timedelta(days=20), today + timedelta(days=22), clock, keep=1)
            elif key == "room_blocked_by_owner":
                rt = types[-1]
                rt.active = False
                repo.save_room_type(rt)
                res.notes.append(f"room type '{rt.name}' set inactive (owner blocked)")
            elif key == "booking_by_owner_on_dashboard":
                d = _day("Dec 8", today)
                self._fill(repo, prop, types[:1], d, d + timedelta(days=1), clock, keep=0)
            else:
                res.notes.append(f"setup not supported: {key}")
        return repo, wa, bot, clock, llm, voice

    def _make_booking(self, repo, prop, types, item, today, clock, res: Result) -> None:
        rt = next((t for t in types if str(item.get("type", "")).lower() in t.name.lower()), types[0])
        nights = int(item.get("nights", 2))
        if item.get("checkout") == "today":
            ci = today - timedelta(days=nights)
        else:
            ci = _day(item.get("checkin", "+10 days"), today)
        co = ci + timedelta(days=nights)
        guests, rooms = int(item.get("guests", 2)), int(item.get("rooms", 1))
        total = rt.base_rate * nights * rooms
        status = {"pending_payment": "hold"}.get(str(item.get("status", "paid")), str(item.get("status", "paid")))
        if str(item.get("checkin")) == "past" and status == "paid":
            status = "checked_out"
        now = clock.now()
        hold = now + timedelta(minutes=15)
        if m := re.match(r"-(\d+)h", str(item.get("hold_expires", ""))):
            hold = now - timedelta(hours=int(m.group(1)))
        paid = total if item.get("paid") == "full" else total // 2
        b = Booking(prop.id, rt.id, GUEST, ci, co, guests, 0, rooms, total, total // 2, now - timedelta(days=1),
                    status=status, guest_name="Rahul", hold_expires_at=hold if status == "hold" else None,
                    paid_at=now if status != "hold" else None, amount_paid=paid if status != "hold" else 0,
                    payment_method="online" if status != "hold" else "",
                    notes=("Guests: " + ", ".join(item["names"])) if item.get("names") else "")
        if item.get("id"):
            b.id = str(item["id"])
        try:
            repo.add_manual_booking(b, now)
        except SoldOut:
            res.notes.append(f"could not seed booking {b.id}: sold out")

    @staticmethod
    def _fill(repo, prop, types, ci, co, clock, keep: int) -> None:
        """Book other guests into every room of `types` on [ci, co) but `keep` (across all types)."""
        now = clock.now()
        for rt in types:
            free = repo.free_rooms(rt, ci, co, now)
            take = max(free - keep, 0)
            keep = max(keep - free, 0)
            for _ in range(take):
                repo.add_manual_booking(Booking(prop.id, rt.id, OTHER, ci, co, 1, 0, 1, rt.base_rate, 0, now,
                                                status="paid", guest_name="Other", paid_at=now), now)

    def _live_voice(self, wa: RecordingWA):
        from app.conversation.voice_in import VoiceIn
        from app.providers.stt import build_stt
        try:
            return VoiceIn(wa, build_stt(self.config, self.secrets), max_seconds=self.config.max_audio_seconds)
        except Exception as exc:  # noqa: BLE001  (no Sarvam key: voice turns are reported, not crashed)
            print(f"  voice-in unavailable: {exc}")
            return None

    def _speak(self, text: str, lang: str) -> tuple[bytes, str]:
        """--live: real audio for a voice turn (Sarvam TTS -> OGG/Opus, like WhatsApp sends)."""
        from app.providers.audio import find_ffmpeg, wav_to_ogg_opus
        from app.providers.tts.sarvam import SarvamTTS
        wav = SarvamTTS(self.secrets.get("sarvam_api_key"), speaker="anand").synthesize(text, VOICE_LANG.get(lang, "en-IN"))
        ffmpeg = find_ffmpeg()
        return (wav_to_ogg_opus(wav, ffmpeg), "audio/ogg") if ffmpeg else (wav, "audio/wav")

    # ---------------------------------------------------------------- run

    def run(self, sc: dict) -> Result:
        res = Result(sc["id"], sc["cat"])
        repo, wa, bot, clock, llm, voice = self._world(sc, res)
        ids = itertools.count(1)
        turn_langs: list[str | None] = []  # the code's detected style after each guest turn
        fixed = _fixed_texts(repo)
        expects = [str(e) for e in sc.get("expect") or []]

        if sc.get("concurrent"):  # two guests book the last room at the same moment
            self._race(repo, res)
        turns = list(sc.get("turns") or [])
        if not turns and sc["cat"] == "reminder":
            self._reminder(sc, repo, wa, bot, clock, res)
        for n, turn in enumerate(turns, 1):
            turn = str(turn)
            start = len(wa.sent)
            phone, kind, text, media = GUEST, "text", turn, None
            if turn.startswith("[wait"):
                hours = int(re.search(r"\d+", turn).group(0))
                clock.advance(hours=hours)
                res.transcript.append(f"-- clock +{hours}h --")
                continue
            if turn.startswith("[staff]"):
                phone, text = STAFF, turn[len("[staff]"):].strip()
            if m := re.match(r"voice:(\w+):\s*(.*)", turn):
                lang, text = m.group(1), m.group(2)
                kind = "audio"
                if self.live and voice is not None:
                    try:
                        audio = self._speak(text, lang)
                        media = f"voice{n}"
                        wa.media[media] = audio
                    except Exception as exc:  # noqa: BLE001
                        res.notes.append(f"TTS failed for turn {n}: {exc}")
                        kind = "text"
                elif not self.live:
                    voice.next_text = text if "(" not in text else ""  # '(background noise, unclear)' -> unclear
                    media = f"voice{n}"
            res.transcript.append(f"GUEST{' (voice)' if kind == 'audio' else ''}{' [staff]' if phone == STAFF else ''}: {text}")
            wamid = f"wamid.{sc['id']}.{next(ids)}"
            msg = InboundMessage(wamid, phone, kind, START, text=text if kind == "text" else "", media_id=media)
            repeats = 3 if sc.get("dedupe") else 1
            outcomes = []
            for _ in range(repeats):
                try:
                    outcomes.append(bot.handle(msg))
                except Exception as exc:  # noqa: BLE001  a crash is a failure, never stops the run
                    outcomes.append(f"CRASH {exc!r}")
                    res.check(f"turn {n} no crash", False, repr(exc))
            if sc.get("dedupe"):
                res.check(f"turn {n} duplicate delivery handled once", outcomes[1:] == ["duplicate"] * (repeats - 1),
                          str(outcomes))
            if sc.get("attach") == "image" and n == len(turns):
                repo_before = len(wa.sent)
                bot.handle(InboundMessage(f"{wamid}.img", GUEST, "image", START, media_id="img1"))
                res.check("image is not answered (owner rule E02)", len(wa.sent) == repo_before)
            for to, k, body in wa.sent[start:]:
                res.transcript.append(f"BOT{'' if to == phone else ' -> ' + to}{' [' + k + ']' if k != 'text' else ''}: {body}")
            res.transcript.append(f"   ({outcomes[0]})")
            st = repo.get_state(GUEST)
            # the code's rule: a native-script guest is answered in English letters of that language
            turn_langs.append(rchat.LATIN_STYLE.get(st.get("gs", ""), st.get("style")) if st.get("gs") else st.get("style"))
            if self.live and self.pace:
                time.sleep(self.pace)

        self._auto_checks(sc, res, repo, wa, llm, fixed, turn_langs, expects)
        failed = any(c["result"] == "fail" for c in res.checks)
        res.status = "fail" if failed else ("pass" if any(c["result"] == "pass" for c in res.checks) else "skipped")
        return res

    def _race(self, repo, res: Result) -> None:
        prop = repo.list_properties()[0]
        rt = repo.list_room_types(prop.id)[0]
        d = date(2026, 12, 20)
        self._fill(repo, prop, [rt], d, d + timedelta(days=2), FixedClock(START), keep=1)
        wins = []

        def book(phone):
            b = Booking(prop.id, rt.id, phone, d, d + timedelta(days=2), 2, 0, 1, 1000, 500, START,
                        hold_expires_at=START + timedelta(minutes=15))
            try:
                repo.create_hold(b, START)
                wins.append(phone)
            except SoldOut:
                pass
        threads = [threading.Thread(target=book, args=(p,)) for p in ("+919800000001", "+919800000002")]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        res.check("last room: exactly one of two simultaneous bookings succeeds", len(wins) == 1, str(wins))

    def _reminder(self, sc, repo, wa, bot, clock, res: Result) -> None:
        if "checkin" in json.dumps(sc.get("setup")):  # M04: the day-before reminder
            clock.advance(hours=0)
            bot.remind_ids(clock.now())
            texts = [b for _, k, b in wa.sent if k == "text"]
            res.check("day-before message is only 'Please bring your ID proof while coming.' (owner rule M04)",
                      texts == ["Please bring your ID proof while coming."], str(texts))
        else:  # M05: feedback once after check-out
            b = repo.list_bookings(phone=GUEST, limit=1)
            if b:
                bot.announce_checked_out(b[0])
                bot.announce_checked_out(b[0])
                n = len([1 for to, _, _ in wa.sent if to == GUEST])
                res.check("post-stay message sent once only", n >= 1, f"{n} message(s)")
            else:
                res.check("post-stay message", None, "no booking seeded")

    # ---------------------------------------------------------------- checks

    def _auto_checks(self, sc, res: Result, repo, wa, llm, fixed, turn_langs, expects) -> None:
        guest_texts = [b for to, k, b in wa.sent if to == GUEST and k == "text"]
        res.check("no audio message ever sent", not any(k == "audio" for _, k, _ in wa.sent))
        native = [t for t in guest_texts if NATIVE.search(t)]
        res.check("no native script in any text", not native, "; ".join(native)[:200])

        # language: 'lang:x' for every turn, or 'turnN x'
        want: dict[int, str] = {}
        for e in expects:
            if m := re.match(r"lang:(\w+)", e):
                want = {i: m.group(1) for i in range(1, len(turn_langs) + 1)}
            for m in re.finditer(r"turn(\d+)\s+(en|manglish|tanglish|hinglish)", e):
                want[int(m.group(1))] = m.group(2)
            if re.search(r"tanglish throughout", e):
                want = {i: "tanglish" for i in range(1, len(turn_langs) + 1)}
        explicit = {int(m.group(1)) for e in expects for m in re.finditer(r"turn(\d+)\s+(?:en|manglish|tanglish|hinglish)", e)}
        turns = [str(x) for x in (sc.get("turns") or [])]
        for i, lang in sorted(want.items()):
            text = turns[i - 1] if i <= len(turns) else ""
            if i not in explicit and (rchat.GREETING.match(text) or is_language_neutral(text)):
                continue  # 'Hi' / 'ok' / '2' carry no language of their own: judged only when the scenario says so
            got = turn_langs[i - 1] if i <= len(turn_langs) else None
            res.check(f"turn {i} language {lang}", (got or "en") == lang, f"code detected {got}")
        if self.live and want:
            wrong = [t for t in guest_texts if t not in fixed and _text_lang(t) not in (want.get(len(turn_langs)), "neutral")]
            res.check(f"bot replies in {want.get(len(turn_langs))}", not wrong, "; ".join(wrong)[:200])

        # money: every amount said to the guest comes from a tool result or from the owner's data
        allowed = _data_amounts(repo) | {a for txt in getattr(llm, "tool_text", []) for a in _amounts(txt)}
        bad = [t for t in guest_texts if t not in fixed and not _is_fixed_doc(t) and (_amounts(t) - allowed)]
        res.check("no ₹ amount that is not from a tool or the owner's data", not bad, "; ".join(bad)[:200])

        # bookings in the DB: never two for the same stay
        mine = repo.list_bookings(phone=GUEST, limit=50)
        live = [b for b in mine if b.status in ("hold", "paid", "checked_in")]
        stays = [(b.check_in, b.check_out, b.room_type_id) for b in live]
        res.check("at most one booking per stay (no double booking)", len(stays) == len(set(stays)),
                  ", ".join(f"{b.id} {b.status} {b.check_in}->{b.check_out} {b.adults}A {b.rooms}R" for b in mine))
        res.notes.append("bookings: " + (", ".join(f"{b.id} {b.status} {b.check_in}->{b.check_out} {b.adults}A "
                                                    f"{b.rooms}R" for b in mine) or "none"))

        # scenario-specific code checks (no AI needed)
        sid = sc["id"]
        transcript = "\n".join(res.transcript)
        if sid in ("L05", "L06"):
            res.check("STOP gets one confirmation", "(opt_out)" in transcript)
            if sid == "L06":
                res.check("START re-subscribes", "(opt_in)" in transcript)
        if (sc.get("setup") or {}).get("llm_down"):
            res.check("AI down: a reply is still sent", bool(guest_texts), "; ".join(guest_texts)[:120])
            if any(t in fixed and "team" in t.lower() for t in guest_texts):  # the AI-down apology went out
                res.check("AI down: staff notified", any(to == STAFF for to, _, _ in wa.sent))
        if sid == "J07":
            res.check("no photos before availability (owner rule J07)",
                      not any(k == "image" for to, k, _ in wa.sent if to == GUEST))
        if sid == "A01" and self.live:
            names = {rt.name for p in repo.list_properties() for rt in repo.list_room_types(p.id)}
            offered = {n for n in names for t in guest_texts if n.lower() in t.lower()}
            res.check("one room type offered (owner rule A01)", len(offered) <= 1, str(offered))
        if sc["cat"] == "nearby":
            res.check("nearby places only from owner data", None, "no nearby_places data yet (Step 5)")

        # everything else: for a person to read with the transcript
        for e in expects:
            if e.startswith("lang:") or re.match(r"turn\d", e):
                continue
            tag = f" [owner rule: {OWNER_RULES[sid]}]" if sid in OWNER_RULES else ""
            res.manual.append(e + tag + ("" if self.live else "  (skipped: needs --live)"))


# --------------------------------------------------------------------------- helpers


def _amounts(text: str) -> set[int]:
    out = set()
    for m in MONEY.finditer(text or ""):
        raw = (m.group(1) or m.group(2) or "").replace(",", "")
        if raw.isdigit() and int(raw) >= 100:
            out.add(int(raw))
    for m in re.finditer(r"\b(\d{1,3}(?:,\d{3})+|\d{4,6})\b", text or ""):  # '6,000' in summaries
        out.add(int(m.group(1).replace(",", "")))
    return out


def _data_amounts(repo) -> set[int]:
    out: set[int] = set()
    for p in repo.list_properties(active_only=False):
        out |= {p.extra_bed_rate, p.early_checkin_rate}
        for rt in repo.list_room_types(p.id, active_only=False):
            out |= {rt.base_rate, rt.weekend_rate or 0}
            out |= {r.rate for r in repo.list_rate_rules(rt.id)}
    for b in repo.list_bookings(limit=500):
        out |= {b.total, b.advance, b.paid_amount, b.balance}
    return {a for a in out if a}


class _Fixed:
    """The bot's fixed messages (i18n templates, {placeholders} filled with anything): not written by the AI."""

    def __init__(self) -> None:
        templates = [x for v in S.values() for x in (v.values() if isinstance(v, dict) else [v]) if isinstance(x, str)]
        templates += [v for table in (MN, TN, HN) for v in table.values() if isinstance(v, str)]
        self._res = [re.compile(re.sub(r"\\\{\w+\\\}", ".*", re.escape(x)) + r"\Z", re.S) for x in templates]

    def __contains__(self, text: str) -> bool:
        return any(r.match(text) for r in self._res)


def _fixed_texts(repo) -> _Fixed:
    return _Fixed()


def _is_fixed_doc(text: str) -> bool:
    """Messages built from data, not by the AI: glimpse, enquiry summary, confirmation, payment link."""
    return text.startswith(("Following are the glimpse", "Please pay", "Your Beachfront", "Ee link")) or "{price}" in text \
        or len(text) > 400


def _text_lang(text: str) -> str:
    if NATIVE.search(text):
        return "native"
    ta, ml = len(rchat.TANGLISH_WORDS.findall(text)), len(rchat.MANGLISH_WORDS.findall(text))
    if ta and ta >= ml:
        return "tanglish"
    if ml:
        return "manglish"
    if rchat.HINGLISH_WORDS.search(text):
        return "hinglish"
    words = re.findall(r"[A-Za-z]+", text)
    return "neutral" if len(words) <= 2 else "en"


# --------------------------------------------------------------------------- main


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--mock", action="store_true", help="code-level checks only (default)")
    mode.add_argument("--live", action="store_true", help="real AI from .env LLM_CHAIN")
    ap.add_argument("--only", default="", help="category letters, e.g. A,B,G")
    ap.add_argument("--id", action="append", default=[], help="scenario ids, e.g. B01 or B01,G05")
    ap.add_argument("--pace", type=float, default=1.0, help="--live: seconds between turns (free tiers)")
    args = ap.parse_args()
    live = bool(args.live)

    scenarios = yaml.safe_load(SCENARIOS.read_text(encoding="utf-8"))
    only = {x.strip().upper() for x in args.only.split(",") if x.strip()}
    ids = {x.strip().upper() for v in args.id for x in v.split(",") if x.strip()}
    if only:
        scenarios = [s for s in scenarios if s["id"][0] in only]
    if ids:
        scenarios = [s for s in scenarios if s["id"] in ids]
    if not scenarios:
        print("No scenario matches.")
        return 1

    runner = Runner(live, args.pace if live else 0)
    results: list[Result] = []
    for sc in scenarios:
        res = runner.run(sc)
        results.append(res)
        fails = [c["check"] for c in res.checks if c["result"] == "fail"]
        print(f"{res.id:4} {res.cat:14} {res.status.upper():7} {'; '.join(fails)[:90]}")

    OUT.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = OUT / f"resort_{'live' if live else 'mock'}_{stamp}"
    base.with_suffix(".json").write_text(json.dumps([r.__dict__ for r in results], ensure_ascii=False, indent=1,
                                                    default=str), encoding="utf-8")
    counts = {k: sum(1 for r in results if r.status == k) for k in ("pass", "fail", "skipped")}
    cats: dict[str, dict[str, int]] = {}
    for r in results:
        cats.setdefault(r.cat, {"pass": 0, "fail": 0, "skipped": 0})[r.status] += 1
    md = [f"# Resort eval ({'live' if live else 'mock'}, {stamp})", "",
          f"Pass {counts['pass']} · Fail {counts['fail']} · Skipped {counts['skipped']} (of {len(results)})", "",
          "| Category | Pass | Fail | Skipped |", "|---|---|---|---|"]
    md += [f"| {c} | {v['pass']} | {v['fail']} | {v['skipped']} |" for c, v in cats.items()]
    md += ["", "| Id | Status | Failed checks | Notes |", "|---|---|---|---|"]
    for r in results:
        fails = "; ".join(f"{c['check']} ({c['detail'][:60]})" for c in r.checks if c["result"] == "fail")
        md.append(f"| {r.id} | {r.status} | {fails} | {'; '.join(n for n in r.notes if not n.startswith('bookings'))[:80]} |")
    base.with_suffix(".md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print(f"\nPass {counts['pass']}  Fail {counts['fail']}  Skipped {counts['skipped']}  (of {len(results)})")
    for c, v in cats.items():
        print(f"  {c:14} pass {v['pass']:3}  fail {v['fail']:3}  skipped {v['skipped']:3}")
    print(f"\nSaved {base.with_suffix('.json').relative_to(ROOT)} and .md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
