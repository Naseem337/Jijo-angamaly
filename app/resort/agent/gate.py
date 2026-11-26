"""Everything before the AI (docs/AI_AGENT_DESIGN.md section 1). Code only, no AI, and no reading of what the guest means:

    dedupe by WhatsApp message id -> media: reactions / stickers get nothing, location / contact / video get the
    'text or voice only' line, a voice note becomes text (one retry; failure -> voice_problem), an image becomes
    "[Guest sent an image]" (the AI may check the payment; an image never confirms anything) -> the message is saved
    -> exact STOP / START / restart commands -> a staff pause (24 h auto-resume) -> per-guest lock -> run_turn()

BookingBot.handle() sends a message here when AGENT_MODE=agent (the default stays 'old').
"""

from __future__ import annotations

import logging
from typing import Any

from app.core.phone import mask_phone, try_normalize_phone
from app.resort.agent import conversation
from app.resort.agent.outbox import Outbox, bot_outbox
from app.resort.agent.state import Enquiry
from app.resort.agent.tools import I18N_LANG
from app.resort.agent.turn import run_turn, swipe_note
from app.resort.chat import RESTART_MARK
from app.resort.flow import AUTO_RESUME, SILENT_TYPES, profile_name
from app.resort.i18n import t
from app.resort.models import ChatMessage as Chat

log = logging.getLogger(__name__)

IMAGE_NOTE = "[Guest sent an image]"
COMMANDS = ("stop", "start", "restart", "resetall")  # the ONLY words code reads; anything else goes to the AI
RESET_DONE = "Reset done."


def command(text: str) -> str:
    """'stop' / 'start' / 'restart' / 'resetall' ('reset all', testers only) when the whole message is exactly that
    word, else ''. Case, spaces and trailing
    marks (a dot, '!', an emoji) do not count: 'Restart.', ' RESTART 🙏', 're start'."""
    word = "".join(text.split()).lower()
    while word and not word[-1].isalnum():
        word = word[:-1]
    return word if word in COMMANDS else ""


def handle(bot: Any, msg: Any, *, dry_run: bool = False) -> str:
    """One WhatsApp message in agent mode. Returns what happened (for logs and tests)."""
    now, phone = bot.clock.now(), msg.phone
    if not bot.repo.mark_event_processed(msg.id, now):
        return "duplicate"
    if not is_tester(bot, phone):  # AI-6: the owner's test runs only for the listed phones
        log.info("Agent: %s is not a tester: no reply", mask_phone(phone))
        return "not_a_tester"
    bot.repo.touch_guest(phone, now, name=profile_name(msg.profile_name))
    box = bot_outbox(bot, phone, dry_run=dry_run)
    lang = I18N_LANG[Enquiry.load(bot.repo, phone).language]

    text, source = (msg.text or "").strip(), "text"
    if msg.type == "audio":
        _mark_read(bot, msg)
        text, why = _transcribe(bot, msg)
        if not text:
            log.error("Voice note not understood for %s: %s", mask_phone(phone), why)
            bot.repo.add_chat(Chat(phone, "in", f"[voice note: not understood ({why})]", now, "voice"))
            box.text("voice_problem", t("voice_problem", lang))
            box.flush()
            return "voice_problem"
        source = "voice"
    elif msg.type == "image":
        bot.repo.add_chat(Chat(phone, "in", f"[image] {text}".strip(), now, "text", msg.id))
        text = IMAGE_NOTE + (f": {text}" if text else "")
        source = "image"
    elif not msg.is_text or not text:
        bot.repo.add_chat(Chat(phone, "in", f"[{msg.type}]", now, "text"))
        if msg.type in SILENT_TYPES:  # reactions, stickers, edits, system events, empty texts: no reply
            return "silent"
        box.text("unsupported", t("unsupported", lang))  # location, contacts, video, documents ...
        box.flush()
        return "unsupported"
    if source != "image":
        bot.repo.add_chat(Chat(phone, "in", text, now, source, msg.id))

    with bot._guest_lock(phone):  # one guest's messages are answered one after another
        cmd = command(text) if source == "text" else ""
        if cmd == "resetall":
            if listed_tester(bot, phone):
                return _reset_all(bot, phone, box)
            cmd = ""  # not a tester: 'reset all' is a normal message for the AI
        if cmd in ("stop", "start") and (outcome := _opt(bot, phone, cmd, box, lang)):
            return outcome
        paused, since = bot.repo.is_paused(phone)
        if paused:
            if since is not None and now - since >= AUTO_RESUME:
                bot.repo.set_paused(phone, False)
            else:
                return "paused"  # staff are talking to this guest
        _mark_read(bot, msg)
        if cmd == "restart":
            return _restart(bot, phone, box)
        llm = bot._llm() if bot._llm else None
        result = run_turn(bot, llm, phone, text, box, swipe=swipe_note(bot, phone, msg.context_id))
        return result.outcome


def is_tester(bot: Any, phone: str) -> bool:
    """AGENT_TEST_PHONES lists the phones the agent answers during the owner's test; empty = everyone."""
    testers = list(getattr(bot.config, "agent_test_phones", []) or [])
    return not testers or (try_normalize_phone(phone) or phone) in testers


def listed_tester(bot: Any, phone: str) -> bool:
    """The phone is named in AGENT_TEST_PHONES (an empty list names nobody)."""
    testers = list(getattr(bot.config, "agent_test_phones", []) or [])
    return (try_normalize_phone(phone) or phone) in testers


def staff_override(bot: Any) -> None:
    """Test setup: STAFF_ALERT_PHONES set -> every staff alert of this bot goes only to those numbers (the old code and
    the agent both ask bot.staff_numbers(), so it is replaced on this bot object; nothing else changes)."""
    only = list(getattr(bot.config, "staff_alert_phones", []) or [])
    if only:
        bot.staff_numbers = lambda property_id=None: list(only)


def _mark_read(bot: Any, msg: Any) -> None:
    try:
        bot.wa.mark_read(msg.id)
    except Exception:  # best effort only
        pass


def _transcribe(bot: Any, msg: Any) -> tuple[str, str]:
    """(text, '') or ('', why). The existing voice-in service, with one retry for a download / speech-to-text hiccup."""
    voice = bot._voice_in()
    if voice is None:
        return "", "voice notes are switched off (no speech-to-text key)"
    why = ""
    for _ in range(2):
        try:
            result = voice.process(msg.media_id)
        except Exception as exc:  # download / ffmpeg / STT crashed: the guest still gets the polite text
            why = f"crash: {type(exc).__name__}: {str(exc)[:120]}"
            continue
        if (result.text or "").strip():
            return result.text.strip(), ""
        problem = getattr(result, "problem", None)
        why = f"{problem or 'empty'}: {(getattr(result, 'detail', '') or '')[:120]}".strip(": ")
        if problem == "too_long":
            break  # the same audio gives the same answer: no retry (an empty transcript is tried twice, 2026-11-26)
    return "", why


def _opt(bot: Any, phone: str, cmd: str, box: Outbox, lang: str) -> str:
    """STOP: no more offers or reminders. START after a STOP: they come back. '' = not a change (START when not opted
    out, STOP twice): the message goes on as a normal one."""
    st = bot.repo.get_state(phone)
    out = cmd == "stop"
    if bool(st.get("opted_out")) == out:
        return ""
    st["opted_out"] = out
    bot.repo.set_state(phone, st)
    guest = bot.repo.touch_guest(phone, bot.clock.now())
    guest.offers_consent = not out
    bot.repo.upsert_guest(guest)
    box.text("opt_out" if out else "opt_in", t("opt_out" if out else "opt_in", lang))
    box.flush()
    return "opt_out" if out else "opt_in"


def _restart(bot: Any, phone: str, box: Outbox) -> str:
    """'restart' (live bug 2026-11-25: the agent still talked about the previous chat): a clean new conversation.
    - the agent's Enquiry and the whole chat state go (place, dates, adults, rooms, picks, extra beds, contact, the
      checked result, the language and its lock); only STOP's opt-out stays
    - every open UNPAID hold of this guest is closed with its payment link; a PAID booking is never touched
    - `restarted_at` is stored: GUEST RECORDS then lists only confirmed bookings from before it, and recall_old_chat
      never reads past it
    - RESTART_MARK goes in the chat log BEFORE the greeting (fix round 3), so the AI's history, the photo memory and
      the 'already sent' checks all start after it, and the greeting is part of the new conversation"""
    st = bot.repo.get_state(phone)
    keep = {k: st[k] for k in ("opted_out",) if k in st}
    keep[conversation.RESTARTED_KEY] = bot.clock.now().isoformat()
    bot.repo.set_state(phone, keep)
    close_unpaid(bot, phone)
    bot._log(phone, RESTART_MARK, "system")
    box.text("greet", t("greet", "en"))
    box.flush()
    return "restart"


def _reset_all(bot: Any, phone: str, box: Outbox) -> str:
    """Tester-only 'reset all': what restart does (unpaid links closed) PLUS every booking of this phone deleted (the
    rooms are free again) and everything stored about it gone: chat, chat state (enquiry, language, pause, opt-out)
    and the guest record. Then "Reset done." Other phones are never touched."""
    close_unpaid(bot, phone)
    counts = bot.repo.delete_phone_data(phone)
    log.info("Reset all for %s: %s", mask_phone(phone), counts)
    box.text("reset_done", RESET_DONE)
    box.flush()
    return "reset_all"


def close_unpaid(bot: Any, phone: str) -> int:
    """Close this guest's unpaid holds and their payment links (status hold only: paid / checked-in bookings stay)."""
    closed = 0
    for b in bot.repo.list_bookings(phone=phone, status="hold", limit=50):
        if b.payment_link_id:
            try:
                bot.payments.cancel_link(b.payment_link_id)
            except Exception as exc:  # a link that cannot be closed must never stop the restart
                log.warning("Restart: could not close the link of %s: %s", b.id, exc)
        b.status = "expired"
        b.notes = (b.notes + " [closed by restart]").strip()
        bot.repo.save_booking(b)
        closed += 1
    return closed
