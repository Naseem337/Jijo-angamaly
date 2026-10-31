"""Play full guest conversations against the REAL AI (keys from .env) and check the resort bot's behaviour.

    .venv\\Scripts\\python.exe scripts\\live_eval_resort.py            # every language
    .venv\\Scripts\\python.exe scripts\\live_eval_resort.py ml hi      # only these

Each scenario goes from "hi" to a held booking, a payment, casual replies and a cancellation. Money is a mock and
WhatsApp is recorded, so nothing is sent. Transcripts go to data/eval/resort_<language>.txt and the checks print
at the end: language and script, "sir" (never bro), the English summary, no price-hike talk, nothing called booked
before payment, silence on "ok", no "can't reduce" for a plain offers question, the owner's number on a cancellation.
"""

from __future__ import annotations

import itertools
import re
import sys
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import os  # noqa: E402

os.environ.setdefault("RESORT_STAFF_NUMBERS", "")
from app.core.clock import IST, FixedClock  # noqa: E402
from app.providers.llm import build_llm  # noqa: E402
from app.resort import seed  # noqa: E402
from app.resort.config import load_resort_config  # noqa: E402
from app.resort.db import ResortRepo  # noqa: E402
from app.resort.flow import BookingBot  # noqa: E402
from app.resort.payments import MockPayments  # noqa: E402
from app.settings import EnvSecretResolver  # noqa: E402
from app.whatsapp.parse import InboundMessage  # noqa: E402

OWNER_NUMBER = "9497861924"
INDIC = {"ml": re.compile(r"[ഀ-ൿ]"), "ta": re.compile(r"[஀-௿]"), "hi": re.compile(r"[ऀ-ॿ]")}
ANY_INDIC = re.compile(r"[ऀ-ॿ஀-௿ഀ-ൿ]")
BAD_ADDRESS = re.compile(r"\b(bro|dear|machane|machan|chetta|chechi|bhai|anna)\b|ബ്രോ|ചേട്ടാ|ചേച്ചി", re.IGNORECASE)
HIKE = re.compile(r"season|peak|price hike|hike|rates? (change|differ|increase|are higher)|price (went up|increase)|"
                  r"സീസൺ|சீசன்|सीजन|सीज़न", re.IGNORECASE)
CLAIMS_BOOKED = re.compile(
    r"\b(is|are|been|has been|have been) (booked|confirmed|reserved)\b|booking (is )?confirmed|booked aayi|confirmed aayi|"
    r"confirm aayi|booking aayi|book ho gaya|confirm ho gaya|book aayiduchu|confirm aagiduchu|"
    r"ബുക്ക് ചെയ്തു|ബുക്കായി|ബുക്ക് ആയി|ഉറപ്പായി|ഉറപ്പിച്ചു|"
    r"பதிவு செய்யப்பட்டது|புக் ஆகிவிட்டது|உறுதியாகிவிட்டது|உறுதி செய்யப்பட்டது|"
    r"बुक हो गया|बुक हो गई|कन्फर्म हो गया|कन्फर्म हो गई|बुक कर दिया|बुक कर दी", re.IGNORECASE)
CANT_REDUCE = re.compile(r"cannot reduce|can't reduce|cant reduce|no discount|no offer|not able to reduce|kurakkan pattilla|"
                         r"kurakkaan pattilla|kuraikka mudiyaadhu|kam nahi|kam nahin|discount nahi|കുറയ്ക്കാൻ പറ്റില്ല|"
                         r"ഓഫർ ഇല്ല|குறைக்க முடியாது|ஆஃபர் இல்லை|कम नहीं|ऑफर नहीं", re.IGNORECASE)

PAY = "<<payment received>>"
SCENARIOS = {
    "en": ("English", "en", ["Hi", "Do you have rooms available in Alleppey?", "25 December, 2 nights", "2 adults",
                             "1 room is fine", "any offers?", "yes please book it, I am Rahul from Pune", ("ok", "quiet"),
                             "can I pay by GPay?", PAY, ("thanks", "quiet"), ("I want to cancel my booking", "cancel")]),
    "ml": ("Malayalam", "ml", ["ഹായ്", "ആലപ്പുഴയിൽ റൂം ഉണ്ടോ?", "ഡിസംബർ 25 മുതൽ 2 രാത്രി", "2 പേർ", "ഒരു റൂം മതി",
                               ("ഓഫർ വല്ലതും ഉണ്ടോ?", "offer"), "ശരി ബുക്ക് ചെയ്തോളൂ, പേര് അനു, കൊച്ചിയിൽ നിന്നാണ്",
                               ("ശരി", "quiet"), "GPay വഴി അടയ്ക്കാമോ?", PAY, ("നന്ദി", "quiet"),
                               ("ബുക്കിംഗ് ക്യാൻസൽ ചെയ്യണം", "cancel")]),
    "manglish": ("Manglish", "latin", ["hi", "avde engane und", "alleppey", "25 dec 2 divasam, 2 per", "1 room mathi",
                                       ("offer undo?", "offer"), "ok book cheyyoo, ente per Anu, kochi", ("ok", "quiet"),
                                       "gpay vazhi pay cheyyaamo?", PAY, ("thanks", "quiet"),
                                       ("booking cancel cheyyanam", "cancel")]),
    "ta": ("Tamil", "ta", ["வணக்கம்", "ஆலப்புழாவில் ரூம் இருக்கா?", "டிசம்பர் 25 முதல் 2 இரவு", "2 பேர்", "ஒரு ரூம் போதும்",
                           ("ஏதாவது ஆஃபர் இருக்கா?", "offer"), "சரி புக் பண்ணுங்க, என் பெயர் கார்த்திக், சென்னையிலிருந்து",
                           ("சரி", "quiet"), "GPay மூலம் பணம் செலுத்தலாமா?", PAY, ("நன்றி", "quiet"),
                           ("புக்கிங்கை ரத்து செய்யணும்", "cancel")]),
    "tanglish": ("Tanglish", "latin", ["hi", "alleppey la room irukka?", "december 25 ku 2 nights", "2 peru", "oru room podhum",
                                       ("offer irukka?", "offer"), "sari book pannunga, en peru Karthik, Chennai",
                                       ("ok", "quiet"), "gpay la pay panlama?", PAY, ("thanks", "quiet"),
                                       ("booking cancel pannanum", "cancel")]),
    "hi": ("Hindi", "hi", ["नमस्ते", "अलेप्पी में कमरा मिलेगा क्या?", "25 दिसंबर से 2 रात", "2 लोग", "1 कमरा ठीक है",
                           ("कोई ऑफर है?", "offer"), "ठीक है बुक कर दीजिए, मेरा नाम राहुल है, पुणे से", ("ठीक है", "quiet"),
                           "क्या मैं GPay से पेमेंट कर सकता हूँ?", PAY, ("धन्यवाद", "quiet"),
                           ("मुझे बुकिंग कैंसल करनी है", "cancel")]),
    "hinglish": ("Hinglish", "latin", ["hi", "alleppey me room milega kya?", "25 december se 2 raat", "2 log", "1 room theek hai",
                                       ("koi offer hai?", "offer"), "theek hai book kar do, naam Rahul, Pune se",
                                       ("ok", "quiet"), "gpay se pay kar sakta hu?", PAY, ("thanks", "quiet"),
                                       ("booking cancel karni hai", "cancel")]),
}


class RecordingWA:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def send_text(self, to, text):
        self.sent.append((to, text))
        return "wamid.x"

    def mark_read(self, *a, **k):
        return True

    def send_template(self, *a, **k):
        return "wamid.x"

    def download_media(self, *a, **k):
        raise RuntimeError("no media in this test")


def run(key: str, llm) -> list[str]:
    label, script_kind, steps = SCENARIOS[key]
    repo = ResortRepo(Path(tempfile.mkdtemp()) / "r.sqlite3")
    seed.seed(repo)
    clock = FixedClock(datetime(2026, 11, 2, 18, 0, tzinfo=IST))
    wa, pay = RecordingWA(), MockPayments()
    bot = BookingBot(load_resort_config(), repo, wa, pay, clock, llm=lambda: llm)
    phone, ids = f"+9198470{100 + list(SCENARIOS).index(key):05d}"[:13], itertools.count(1)
    lines: list[str] = []
    issues: list[str] = []
    kinds: list[tuple[str, str, str]] = []  # (tag, step tag, text) for the checks
    for step in steps:
        text, tag = (step, "") if isinstance(step, str) else step
        start = len(wa.sent)
        last_out = wa.sent[-1][1] if wa.sent else ""
        waiting_for_answer = "?" in last_out or bool((repo.get_state(phone).get("bk") or {}).get("chosen"))
        if text == PAY:
            booking = next(iter(repo.list_bookings(phone=phone, status="hold", limit=1)), None)
            lines.append("-- Razorpay: payment received --" if booking else "-- (no booking was held, so no payment) --")
            if booking:
                bot.mark_paid(booking_id=booking.id, payment_id="pay_test")
        else:
            lines.append(f"GUEST: {text}")
            outcome = bot.handle(InboundMessage(f"wamid.{key}{next(ids)}", phone, "text", datetime(2026, 11, 2, 12, 30), text=text))
            if outcome == "ai_down":
                issues.append(f"AI did not answer '{text}' (outcome ai_down)")
        replies = [t for _, t in wa.sent[start:]]
        for reply in replies:
            lines.append("BOT:\n" + "\n".join("    " + ln for ln in reply.split("\n")))
            kind = ("fixed" if text == PAY else "summary" if reply.startswith("*Booking summary*")
                    else "pay" if "rzp.io/mock" in reply else "ai")
            kinds.append((kind, tag, reply))
        if tag == "quiet" and replies and not waiting_for_answer:
            issues.append(f"replied to the casual '{text}': {replies[0][:70]!r}")
        if tag == "offer" and any(CANT_REDUCE.search(r) for r in replies):
            issues.append(f"answered an offers question like a price negotiation: {replies[0][:90]!r}")
        if tag == "cancel" and not any(OWNER_NUMBER in r for r in replies):
            issues.append("cancellation: the owner's number was not given")
        if tag == "cancel" and not replies:
            issues.append("cancellation: no reply at all")
    # ---- checks over everything the AI said
    summaries = [t for k, _, t in kinds if k == "summary"]
    ai = [(tg, t) for k, tg, t in kinds if k == "ai"]
    if len(summaries) != 1:
        issues.append(f"expected exactly one booking summary, saw {len(summaries)}")
    for s in summaries:
        if ANY_INDIC.search(s) or "Total:" not in s or "25 Dec" not in s:
            issues.append("the summary is not clean English with the 25 Dec dates")
    before_payment = True
    for kind, tag, reply in kinds:
        if kind == "fixed":
            before_payment = False
        if BAD_ADDRESS.search(reply):
            issues.append(f"bad form of address: {reply[:80]!r}")
        if kind == "ai" and HIKE.search(reply):
            issues.append(f"talks about rates changing: {reply[:90]!r}")
        if kind == "ai" and before_payment and CLAIMS_BOOKED.search(reply):
            issues.append(f"calls it booked before payment: {reply[:90]!r}")
        if kind == "ai" and script_kind == "latin" and ANY_INDIC.search(reply):
            issues.append(f"native script for a guest who writes in English letters: {reply[:70]!r}")
        if kind == "ai" and script_kind in INDIC:
            if any(INDIC[o].search(reply) for o in INDIC if o != script_kind):
                issues.append(f"another language's script mixed in: {reply[:70]!r}")
            if len(reply) > 40 and not INDIC[script_kind].search(reply):
                issues.append(f"a long reply without any {label} script: {reply[:70]!r}")
    pay_msgs = [t for k, _, t in kinds if k == "pay"]
    if not pay_msgs:
        issues.append("no payment link message was sent")
    else:
        text = pay_msgs[0]
        if script_kind in INDIC and not INDIC[script_kind].search(text):
            issues.append(f"payment message is not in {label} script: {text[:70]!r}")
        if key == "manglish" and "cheyyuka" not in text:
            issues.append(f"payment message is not Manglish: {text[:70]!r}")
        if re.search(r"\bheld\b|booked|reserved", text, re.IGNORECASE):
            issues.append("payment message calls it held/booked")
    out = ROOT / "data" / "eval"
    out.mkdir(parents=True, exist_ok=True)
    (out / f"resort_{key}.txt").write_text("\n".join(lines) + "\n\nISSUES:\n" + "\n".join(issues or ["none"]), encoding="utf-8")
    return issues


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    keys = [k for k in sys.argv[1:] if k in SCENARIOS] or list(SCENARIOS)
    llm = build_llm(load_resort_config(), EnvSecretResolver())
    total = 0
    for key in keys:
        issues = run(key, llm)
        total += len(issues)
        print(f"\n== {SCENARIOS[key][0]}: {'OK' if not issues else str(len(issues)) + ' issue(s)'}")
        for issue in issues:
            print("   -", issue)
    print(f"\nTranscripts: {ROOT / 'data' / 'eval'}")
    return 1 if total else 0


if __name__ == "__main__":
    raise SystemExit(main())
