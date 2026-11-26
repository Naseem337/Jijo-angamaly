"""FACTS for the agent's cached prompt: ALL descriptive dashboard text for every resort (2026-11-27).

Live bug: "is breakfast included?" got a handoff although the property glimpse (Property.intro) said there is no
restaurant and food can be ordered via Swiggy / Zomato to the gate; the old bot's FACTS left the glimpse out.
Every free-text field is here now: description, glimpse, highlights, address / area, stay info (house rules,
caretaker, Wi-Fi), meal plan, policies, cancellation refund table, check-in / out, early check-in, extra bed, and each
room type's description and amenities. Sentences that state a rupee amount (or the glimpse's {price} slot) are dropped:
amounts come only from the tools. Changes only when the dashboard changes, so it stays in the cached part.
"""

from __future__ import annotations

import re
from typing import Any

from app.resort import availability as av
from app.resort.chat import display_phone

_SLOT = re.compile(r"\{[a-z_]+\}")


def _clean(bot: Any, text: str) -> str:
    """Dashboard text without placeholders, {price}-slot sentences or sentences that state a rupee amount."""
    if not text or "PLACEHOLDER" in text:
        return ""
    lines = []
    for line in text.split("\n"):
        parts = [s for s in re.split(r"(?<=[.!?])\s+", line) if s.strip() and not _SLOT.search(s)]
        lines.append(" ".join(parts))
    return " ".join(x.strip() for x in bot._no_amounts("\n".join(lines)).split("\n") if x.strip())


def _cancel_text(policy: list[dict]) -> str:
    rows = sorted(policy or [], key=lambda r: -int(r.get("days_before", 0)))
    parts = []
    for r in rows:
        refund = 100 - int(r.get("retention_pct", 0))
        days = int(r.get("days_before", 0))
        when = f"{days}+ days before check-in" if days else "less than that / no-show"
        parts.append(f"{when}: {refund}% of the amount paid refunded")
    return "; ".join(parts)


def _rooms(bot: Any, p: Any) -> list[str]:
    out = []
    for rt in bot.repo.list_room_types(p.id):
        line = (f"  ROOM TYPE '{rt.name}': {rt.units} room(s), max {av.room_cap(rt)} adults and {rt.max_children} "
                f"child(ren) per room")
        if rt.room_list:
            line += f", room numbers {', '.join(rt.room_list)} (only to understand a guest who names one; never tell them)"
        line += "."
        if desc := _clean(bot, rt.description):
            line += f" About: {desc}"
        if amen := _clean(bot, rt.amenities):
            line += f" Amenities: {amen}"
        out.append(line)
    return out


def build_facts(bot: Any) -> str:
    out = bot._closed_facts()
    for p in bot._places():
        numbers = ", ".join(display_phone(n) for n in p.contact_numbers) or "not set"
        out.append(f"PLACE '{bot._place_label(p)}' (property name {p.name})")
        if area := ", ".join(x for x in (p.address, p.city) if x):
            out.append(f"  Address / area: {area}")
        if p.map_url:
            out.append(f"  Location (Google Maps link, send it when the guest asks where it is): {p.map_url}")
        for label, text in (("About", p.description), ("Property description (glimpse)", p.intro)):
            if t := _clean(bot, text):
                out.append(f"  {label}: {t}")
        if good := [h for h in (_clean(bot, h) for h in p.highlights) if h]:
            out.append(f"  Good points: {'; '.join(good)}")
        if meal := _clean(bot, p.meal_plan):
            out.append(f"  Food (owner's code word, never say it to the guest; tell them in plain words): {meal}")
        if info := _clean(bot, p.stay_info):
            out.append(f"  Stay info / house rules: {info}")
        if pol := _clean(bot, p.policies):
            out.append(f"  Policies: {pol}")
        if cancel := _cancel_text(p.cancel_policy):
            out.append(f"  Cancellation refund: {cancel}")
        out.append(f"  Check-in {p.checkin_time}, check-out {p.checkout_time}. To confirm a booking the guest pays "
                   "an advance online now; the rest is paid at the property. (The advance amount comes only from the tools.)")
        if p.extra_bed_rate:
            out.append("  Extra bed: available (its cost comes only from the tools).")
        if p.early_checkin_time and p.early_checkin_rate:
            out.append(f"  Early check-in (only if asked): from {bot._short_time(p.early_checkin_time)}; the "
                       "charge comes only from prepare_booking / create_booking.")
        out.append(f"  Team's phone/WhatsApp numbers: {numbers}")
        if topics := [a for a in (p.owner_answers or []) if a.get("topic") and a.get("text")]:
            out.append("  OWNER ANSWER topics (question on one of these, any language → owner_answer(topic)): "
                       + "; ".join(f"'{a['topic']}' (e.g. {a.get('examples') or a['topic']})" for a in topics))
        out += _rooms(bot, p)
    return "\n".join(out)
