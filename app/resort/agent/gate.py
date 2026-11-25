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

from app.core.phone import mask_phone
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
COMMANDS = ("stop", "start", "restart")  # the ONLY words code reads; anything else goes to the AI


def command(text: str) -> str:
    """'stop' / 'start' / 'restart' when the whole message is exactly that word (any case, a trailing . or !), else ''."""
    word = text.strip().rstrip(".! ").strip().lower()
    return word if word in COMMANDS else ""


def handle(bot: Any, msg: Any, *, dry_run: bool = False) -> str:
    """One WhatsApp message in agent mode. Returns what happened (for logs and tests)."""
    now, phone = bot.clock.now(), msg.phone
    if not bot.repo.mark_event_processed(msg.id, now):
        return "duplicate"
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
        if problem in ("too_long", "unclear"):
            break  # the same audio gives the same answer: no retry
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
    """'restart': a fresh conversation, the enquiry and the language start again. Bookings and records stay."""
    st = bot.repo.get_state(phone)
    keep = {k: st[k] for k in ("opted_out",) if k in st}
    bot.repo.set_state(phone, keep)  # the agent's Enquiry lives in the state too: it is cleared
    # the mark goes BEFORE the greeting (fix round 3): the greeting is then part of the new conversation, so the AI
    # sees it was already said and answers the next message ('I need a room') instead of greeting again
    bot._log(phone, RESTART_MARK, "system")
    box.text("greet", t("greet", "en"))
    box.flush()
    return "restart"
