"""Demo bookings and chats for a presentation (owner-only dashboard action). Writes straight to the database: no
WhatsApp message, no staff alert, no payment link. Remove it again with the 'reset test data' action."""

from __future__ import annotations

from datetime import datetime, timedelta

from app.core.clock import IST
from app.resort import availability as av
from app.resort.db import ResortRepo, SoldOut
from app.resort.models import Booking, Charge, ChatMessage
from app.resort.paying import apply_payment

# (guest, phone, room type position by priority, rooms, days from today in, nights, adults, children, state, method)
PLAN = [
    ("Arjun Nair", "+919846110021", 0, 1, -17, 2, 2, 0, "checked_out", "upi"),
    ("Priya Menon", "+919895220034", 2, 1, -11, 2, 3, 0, "checked_out", "card"),
    ("Rahul Sharma", "+919810330045", 1, 2, -7, 2, 4, 0, "checked_out", "online"),
    ("Fathima Rasheed", "+919747440056", 0, 1, -1, 2, 2, 0, "checked_in", "online"),
    ("David Thomas", "+919447550067", 2, 1, 0, 2, 3, 0, "checked_in", "cash"),
    ("Anjali Krishnan", "+919633660078", 1, 1, 3, 2, 2, 0, "paid", "online"),
    ("Mohammed Ashiq", "+919562770089", 0, 2, 9, 2, 4, 0, "paid", "online"),
    ("Sneha Pillai", "+919746880090", 2, 2, 16, 2, 5, 1, "paid", "upi"),
    ("Karthik Raj", "+919840990012", 1, 1, 23, 3, 3, 0, "paid", "online"),
    ("Neha Gupta", "+919811000123", 0, 1, 35, 2, 2, 0, "paid", "online"),
    ("Joseph Mathew", "+919447111234", 2, 1, 41, 2, 3, 0, "paid", "online"),
    ("Vishnu Prasad", "+919605222345", 1, 1, 7, 1, 2, 0, "hold", ""),
    ("Aisha Khan", "+919895333456", 0, 1, 26, 1, 2, 0, "cancelled", "online"),
    ("Arun Kumar", "+919544444567", 1, 1, 12, 2, 2, 0, "expired", ""),
]

CHATS = {
    "Anjali Krishnan": [
        ("in", "Hi"), ("out", "Hi, How may I assist you?"), ("in", "Need a room for this weekend"),
        ("out", "Sure, Kindly confirm the Location 📍\nAlleppey or Kanthalloor ?"), ("in", "Alleppey"),
        ("out", "Please share these details:\n\nCheck in :\nCheck out :\nNumber of persons :"),
        ("in", "22 nov to 24 nov, 2 people"), ("out", "Yes, rooms are available for your dates."),
        ("out", "Please share your name and mobile number to proceed."), ("in", "Anjali 9633660078"),
        ("out", "Please pay the advance using this link:\nhttps://rzp.io/demo"),
        ("out", "*Booking Confirmed ✅*"), ("in", "Thank you!"),
    ],
    "Mohammed Ashiq": [
        ("in", "Hai"), ("out", "Hi, How may I assist you?"), ("in", "rooms undo? njangal 4 per und"),
        ("out", "Sure, Kindly confirm the Location 📍\nAlleppey or Kanthalloor ?"), ("in", "allepy"),
        ("out", "Please share these details:\n\nCheck in :\nCheck out :"), ("in", "28 nu varum 30 nu pokum"),
        ("out", "Und, ningalude dates-il rooms available aanu."), ("in", "photos undo?"),
        ("out", "Interested aanenkil ningalude name-um mobile number-um ayakkamo?"), ("in", "Ashiq same number"),
        ("out", "Please pay the advance using this link:\nhttps://rzp.io/demo"), ("out", "*Booking Confirmed ✅*"),
    ],
    "Karthik Raj": [
        ("in", "Vanakkam, room venum"), ("out", "Hi, How may I assist you?"), ("in", "Alleppey, dec 12 to 15, 3 per"),
        ("out", "Aama, unga dates-la rooms available."), ("in", "Karthik 9840990012"),
        ("out", "Please pay the advance using this link:\nhttps://rzp.io/demo"), ("out", "*Booking Confirmed ✅*"),
    ],
    "Vishnu Prasad": [
        ("in", "Hello, is a room free on 26 Nov?"), ("out", "Please share these details:\n\nCheck out :\nNumber of persons :"),
        ("in", "1 night, 2 people"), ("out", "Yes, rooms are available for your dates."), ("in", "Vishnu 9605222345"),
        ("out", "Please pay the advance using this link:\nhttps://rzp.io/demo"),
    ],
}


def seed_demo(repo: ResortRepo, now: datetime) -> dict[str, int]:
    props = repo.list_properties()
    if not props:
        return {"bookings": 0}
    prop = props[0]
    types = repo.list_room_types(prop.id)
    today = now.astimezone(IST).date()
    made, chats = 0, 0
    for i, (name, phone, pos, rooms, start, nights, adults, children, state, method) in enumerate(PLAN):
        rt = types[min(pos, len(types) - 1)]
        rooms = min(rooms, rt.units)
        ci = today + timedelta(days=start)
        co = ci + timedelta(days=nights)
        q = av.quote(prop, rt, repo.list_rate_rules(rt.id), ci, co, rooms)
        created = now - timedelta(days=max(3, 20 - start // 2) if start < 0 else 2 + i % 5, hours=i * 3)
        b = Booking(prop.id, rt.id, phone, ci, co, adults, children, rooms, q.total, q.advance, created,
                    status="hold", guest_name=name, hold_expires_at=now + timedelta(hours=6), notes="Demo booking")
        if method and state not in ("hold", "expired"):
            full = state == "checked_out"
            apply_payment(b, method=method, amount=q.total if full else q.advance,
                          ref=f"DEMO{i:03d}" if method != "cash" else "", now=created + timedelta(minutes=8))
        try:
            repo.create_hold(b, now)
        except SoldOut:
            continue
        if state in ("checked_in", "checked_out"):
            b.status = state
            b.checked_in_at = datetime.combine(ci, datetime.min.time(), IST) + timedelta(hours=13)
            if state == "checked_out":
                b.checked_out_at = datetime.combine(co, datetime.min.time(), IST) + timedelta(hours=11, minutes=30)
        elif state == "cancelled":
            b.status, b.cancel_reason = "cancelled", "Change of plans"
        elif state == "expired":
            b.status = "expired"
        repo.save_booking(b)
        if state == "checked_in":
            repo.add_charge(Charge(b.id, "Breakfast (2)", 600, now - timedelta(hours=3), category="food"))
        repo.touch_guest(phone, created, name=name)
        made += 1
        for k, (direction, text) in enumerate(CHATS.get(name, [])):
            repo.add_chat(ChatMessage(phone, direction, text, created - timedelta(minutes=20) + timedelta(minutes=k)))
            chats += 1
    return {"bookings": made, "chat_messages": chats}

