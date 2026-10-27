"""All dashboard routes. Pages are server-rendered; the only JSON endpoint is the booking price preview."""

from __future__ import annotations

import csv
import io
from calendar import monthrange
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response

from app.core.clock import IST
from app.core.phone import try_normalize_phone
from app.resort import availability as av
from app.resort.dashboard import integrations
from app.resort.dashboard.deps import (BASE, COOKIE, HERE, Auth, form_of, get_repo, make_session, now_utc, page,
                                       require_owner, require_user, toast_url)
from app.resort.db import SoldOut, check_password, hash_password
from app.resort.models import (Booking, ChatMessage, DashboardUser, Property, RateRule, RoomType)

router = APIRouter(prefix=BASE)

STATUSES = ["hold", "paid", "failed", "expired", "cancelled"]


def today_ist() -> date:
    return datetime.now(IST).date()


def _date(value: str | None, default: date | None = None) -> date | None:
    try:
        return date.fromisoformat(value) if value else default
    except ValueError:
        return default


def _int(form: dict[str, str], key: str, default: int = 0, lo: int = 0, hi: int = 10_000_000) -> int:
    try:
        return max(lo, min(hi, int(float(form.get(key, "").strip() or default))))
    except ValueError:
        return default


def _lines(text: str) -> list[str]:
    return [p.strip() for chunk in text.replace(",", "\n").splitlines() if (p := chunk.strip())]


def _prop(repo, auth: Auth, property_id: str) -> Property:
    prop = repo.get_property(property_id)
    if prop is None or not auth.can_see(prop.id):
        raise HTTPException(404, "Property not found")
    return prop


def _booking(repo, auth: Auth, booking_id: str) -> Booking:
    b = repo.get_booking(booking_id)
    if b is None or not auth.can_see(b.property_id):
        raise HTTPException(404, "Booking not found")
    return b


def _names(repo) -> tuple[dict[str, Property], dict[str, RoomType]]:
    props = {p.id: p for p in repo.list_properties(False)}
    rooms = {rt.id: rt for p in props.values() for rt in repo.list_room_types(p.id, False)}
    return props, rooms


def _visible_bookings(repo, auth: Auth, **kw) -> list[Booking]:
    return [b for b in repo.list_bookings(**kw) if auth.can_see(b.property_id)]


# --------------------------------------------------------------------------- static + login


@router.get("/static/{path:path}")
def static_file(path: str):
    root = (HERE / "static").resolve()
    target = (root / path).resolve()
    if root not in target.parents or not target.is_file():
        raise HTTPException(404)
    return FileResponse(target)


@router.get("/login")
def login_form(request: Request):
    return page(request, "login.html", None, error="")


@router.post("/login")
async def login(request: Request):
    form = await request.form()
    repo = get_repo()
    user = repo.get_user(str(form.get("email", "")).strip())
    if not user or not user.active or not check_password(str(form.get("password", "")), user.password_hash):
        return _login_error(request)
    resp = RedirectResponse(f"{BASE}/", status_code=303)
    resp.set_cookie(COOKIE, make_session(user.email), httponly=True, samesite="lax", max_age=60 * 60 * 24 * 7)
    return resp


def _login_error(request: Request):
    resp = page(request, "login.html", None, error="Wrong email or password")
    resp.status_code = 401
    return resp


@router.post("/logout")
async def logout(request: Request, auth: Auth = Depends(require_user)):
    await form_of(request, auth)
    resp = RedirectResponse(f"{BASE}/login", status_code=303)
    resp.delete_cookie(COOKIE)
    return resp


# --------------------------------------------------------------------------- home


def occupancy(repo, prop: Property, start: date, days: int, now: datetime) -> list[dict]:
    types = repo.list_room_types(prop.id)
    total = sum(rt.units for rt in types)
    bookings = repo.list_bookings(property_id=prop.id, start=start, end=start + timedelta(days=days), limit=5000)
    out = []
    for i in range(days):
        day = start + timedelta(days=i)
        used = sum(b.rooms for b in bookings if av.booking_blocks(b, now) and b.check_in <= day < b.check_out)
        out.append({"day": day, "used": used, "total": total, "pct": round(100 * used / total) if total else 0})
    return out


@router.get("/")
def home(request: Request, auth: Auth = Depends(require_user)):
    repo, today, now = get_repo(), today_ist(), now_utc()
    props = auth.properties(repo)
    props_by_id, rooms = _names(repo)
    mine = _visible_bookings(repo, auth, limit=5000)
    arrivals = [b for b in mine if b.status == "paid" and b.check_in == today]
    departures = [b for b in mine if b.status == "paid" and b.check_out == today]
    holds = [b for b in mine if b.status == "hold" and av.booking_blocks(b, now)]
    month_start = today.replace(day=1)
    paid = [b for b in mine if b.status == "paid"]
    revenue_month = sum(b.advance for b in paid if b.paid_at and b.paid_at.astimezone(IST).date() >= month_start)
    revenue_all = sum(b.advance for b in paid)
    convos = repo.list_conversations()
    paused = [c for c in convos if c["paused"]]
    waiting = [c for c in convos if c["direction"] == "in" and not c["paused"]]
    occ = [(p, occupancy(repo, p, today, 14, now)) for p in props if p.active]
    return page(request, "home.html", auth, nav="home", today=today, arrivals=arrivals, departures=departures,
                holds=holds, revenue_month=revenue_month, revenue_all=revenue_all, paused=len(paused),
                waiting=len(waiting), occ=occ, props=props_by_id, rooms=rooms, paid_count=len(paid))


# --------------------------------------------------------------------------- bookings


def _filtered_bookings(repo, auth: Auth, params) -> list[Booking]:
    day = _date(params.get("date"))
    start, end = _date(params.get("from")), _date(params.get("to"))
    if day:
        start, end = day, day + timedelta(days=1)
    items = _visible_bookings(repo, auth, property_id=params.get("property_id") or None,
                              status=params.get("status") or None, start=start, end=end, limit=5000)
    if rt := params.get("room_type_id"):
        items = [b for b in items if b.room_type_id == rt]
    if q := (params.get("q") or "").strip().lower():
        items = [b for b in items if q in b.guest_phone.lower() or q in b.guest_name.lower() or q in b.id.lower()]
    return items


@router.get("/bookings")
def bookings_list(request: Request, auth: Auth = Depends(require_user)):
    repo = get_repo()
    items = _filtered_bookings(repo, auth, request.query_params)
    props, rooms = _names(repo)
    return page(request, "bookings.html", auth, nav="bookings", items=items, props=props, rooms=rooms,
                my_props=auth.properties(repo), statuses=STATUSES, f=dict(request.query_params),
                query=str(request.query_params))


def _csv_cell(value: object) -> str:
    text = str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@") and not text.startswith("+91") else text


@router.get("/bookings/export.csv")
def bookings_csv(request: Request, auth: Auth = Depends(require_user)):
    repo = get_repo()
    props, rooms = _names(repo)
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["Booking", "Status", "Property", "Room type", "Guest", "Phone", "Check-in", "Check-out", "Nights",
                     "Rooms", "Adults", "Children", "Total", "Advance", "Paid at", "Created"])
    for b in _filtered_bookings(repo, auth, request.query_params):
        writer.writerow([b.id, b.status, _csv_cell(props[b.property_id].name if b.property_id in props else ""),
                         _csv_cell(rooms[b.room_type_id].name if b.room_type_id in rooms else ""),
                         _csv_cell(b.guest_name), b.guest_phone, b.check_in, b.check_out, b.nights, b.rooms,
                         b.adults, b.children, b.total, b.advance, b.paid_at or "", b.created_at])
    return Response(out.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": 'attachment; filename="bookings.csv"'})


def _booking_form_data(repo, auth: Auth) -> list[dict]:
    return [{"prop": p, "rooms": repo.list_room_types(p.id)} for p in auth.properties(repo, active_only=True)]


@router.get("/bookings/new")
def booking_new(request: Request, auth: Auth = Depends(require_user)):
    repo = get_repo()
    today = today_ist()
    return page(request, "booking_new.html", auth, nav="bookings", data=_booking_form_data(repo, auth),
                today=today, tomorrow=today + timedelta(days=1))


@router.get("/api/quote")
def api_quote(request: Request, auth: Auth = Depends(require_user)):
    """Live availability + price for the new-booking form."""
    repo, q = get_repo(), request.query_params
    rt = repo.get_room_type(q.get("room_type_id", ""))
    if rt is None or not auth.can_see(rt.property_id):
        return JSONResponse({"ok": False, "error": "Choose a room type"})
    prop = repo.get_property(rt.property_id)
    check_in, check_out = _date(q.get("check_in")), _date(q.get("check_out"))
    if not (check_in and check_out):
        return JSONResponse({"ok": False, "error": "Choose the dates"})
    if problem := av.check_dates(check_in, check_out, today_ist()):
        return JSONResponse({"ok": False, "error": problem})
    try:
        adults, children = int(q.get("adults", 2)), int(q.get("children", 0))
    except ValueError:
        return JSONResponse({"ok": False, "error": "Guests must be numbers"})
    free = repo.free_rooms(rt, check_in, check_out, now_utc())
    options = av.room_options(rt, adults, children, free)
    need = av.min_rooms(rt, adults, children)
    rooms = int(q.get("rooms") or (options[0] if options else 1))
    quote = av.quote(prop, rt, repo.list_rate_rules(rt.id), check_in, check_out, rooms)
    error = ""
    if need is None:
        error = "This room type cannot take this party"
    elif not options:
        error = f"Needs at least {need} room(s); only {free} free"
    elif rooms not in options:
        error = f"Choose between {options[0]} and {options[-1]} rooms"
    return JSONResponse({"ok": not error, "error": error, "free": free, "options": options, "rooms": rooms,
                         "nights": quote.nights, "total": quote.total, "advance": quote.advance, "gst": quote.gst})


@router.post("/bookings/new")
async def booking_create(request: Request, auth: Auth = Depends(require_user)):
    f = await form_of(request, auth)
    repo, back = get_repo(), f"{BASE}/bookings/new"
    rt = repo.get_room_type(f.get("room_type_id", ""))
    if rt is None or not auth.can_see(rt.property_id):
        raise redirect(toast_url(back, "!Choose a room type"))
    prop = repo.get_property(rt.property_id)
    check_in, check_out = _date(f.get("check_in")), _date(f.get("check_out"))
    phone = try_normalize_phone(f.get("phone", ""))
    adults, children, rooms = _int(f, "adults", 2, 1, 40), _int(f, "children", 0, 0, 40), _int(f, "rooms", 1, 1, 40)
    if not (check_in and check_out) or (problem := av.check_dates(check_in, check_out, today_ist())):
        raise redirect(toast_url(back, "!" + (problem if check_in and check_out else "Choose the dates")))
    if not phone:
        raise redirect(toast_url(back, "!Enter a valid phone number"))
    if rooms not in av.room_options(rt, adults, children, rt.units):
        raise redirect(toast_url(back, f"!{rooms} room(s) cannot hold {adults} adults and {children} children"))
    quote = av.quote(prop, rt, repo.list_rate_rules(rt.id), check_in, check_out, rooms)
    now = now_utc()
    paid = f.get("payment") == "paid"
    booking = Booking(prop.id, rt.id, phone, check_in, check_out, adults, children, rooms, quote.total, quote.advance,
                      now, status="paid" if paid else "hold", guest_name=f.get("guest_name", "").strip(),
                      hold_expires_at=None if paid else now + timedelta(hours=1), paid_at=now if paid else None,
                      notes=f"Added by {auth.user.email}")
    try:
        repo.add_manual_booking(booking, now)
    except SoldOut:
        raise redirect(toast_url(back, "!Not enough free rooms for those dates")) from None
    repo.touch_guest(phone, now, name=booking.guest_name)
    raise redirect(toast_url(f"{BASE}/bookings/{booking.id}", "Booking created"))


def redirect(url: str) -> HTTPException:
    return HTTPException(303, headers={"Location": url})


@router.get("/bookings/{booking_id}")
def booking_detail(booking_id: str, request: Request, auth: Auth = Depends(require_user)):
    repo = get_repo()
    b = _booking(repo, auth, booking_id)
    props, rooms = _names(repo)
    timeline = [(b.created_at, "Booking created (hold)")]
    if b.payment_link_url:
        timeline.append((None, "Payment link created"))
    if b.paid_at:
        timeline.append((b.paid_at, "Advance paid"))
    if b.status in ("expired", "cancelled", "failed"):
        timeline.append((None, f"Marked {b.status}"))
    return page(request, "booking_detail.html", auth, nav="bookings", b=b, prop=props.get(b.property_id),
                rt=rooms.get(b.room_type_id), timeline=timeline, guest=repo.get_guest(b.guest_phone))


@router.post("/bookings/{booking_id}/action")
async def booking_action(booking_id: str, request: Request, auth: Auth = Depends(require_user)):
    f = await form_of(request, auth)
    repo = get_repo()
    b = _booking(repo, auth, booking_id)
    url, action, now = f"{BASE}/bookings/{b.id}", f.get("action", ""), now_utc()
    if action == "mark_paid":
        if b.status != "paid":
            rt = repo.get_room_type(b.room_type_id)
            if b.status != "hold" and rt and repo.free_rooms(rt, b.check_in, b.check_out, now) < b.rooms:
                raise redirect(toast_url(url, "!Those rooms are taken now; cannot mark paid"))
            b.status, b.paid_at, b.hold_expires_at = "paid", now, None
            b.notes = (b.notes + f"\nMarked paid by {auth.user.email}").strip()
            repo.save_booking(b)
        raise redirect(toast_url(url, "Marked as paid"))
    if action == "cancel":
        b.status = "cancelled"
        b.notes = (b.notes + f"\nCancelled by {auth.user.email}").strip()
        repo.save_booking(b)
        raise redirect(toast_url(url, "Booking cancelled"))
    if action == "resend_link":
        error = integrations.resend_payment_link(b)
        raise redirect(toast_url(url, f"!{error}" if error else "Payment link sent"))
    if action == "note":
        if text := f.get("note", "").strip():
            stamp = datetime.now(IST).strftime("%d %b %H:%M")
            b.notes = (b.notes + f"\n[{stamp} {auth.user.email}] {text}").strip()
            repo.save_booking(b)
        raise redirect(toast_url(url, "Note added"))
    raise redirect(toast_url(url, "!Unknown action"))


# --------------------------------------------------------------------------- calendar


@router.get("/calendar")
def calendar(request: Request, auth: Auth = Depends(require_user)):
    repo, q, now = get_repo(), request.query_params, now_utc()
    props = auth.properties(repo, active_only=True)
    if not props:
        return page(request, "calendar.html", auth, nav="calendar", props=[], prop=None, rows=[], days=[], month="")
    prop = next((p for p in props if p.id == q.get("property_id")), props[0])
    first = _date(q.get("month", "") + "-01") or today_ist().replace(day=1)
    first = first.replace(day=1)
    days = [first + timedelta(days=i) for i in range(monthrange(first.year, first.month)[1])]
    nxt = (first + timedelta(days=32)).replace(day=1)
    prev = (first - timedelta(days=1)).replace(day=1)
    bookings = repo.list_bookings(property_id=prop.id, start=first, end=nxt, limit=5000)
    rows = []
    for rt in repo.list_room_types(prop.id):
        cells = []
        for day in days:
            on = [b for b in bookings if b.room_type_id == rt.id and av.booking_blocks(b, now)
                  and b.check_in <= day < b.check_out]
            booked = sum(b.rooms for b in on if b.status == "paid")
            held = sum(b.rooms for b in on if b.status == "hold")
            cells.append({"day": day, "booked": booked, "held": held, "free": max(rt.units - booked - held, 0)})
        rows.append({"rt": rt, "cells": cells})
    return page(request, "calendar.html", auth, nav="calendar", props=props, prop=prop, rows=rows, days=days,
                month=first.strftime("%B %Y"), prev=prev.strftime("%Y-%m"), nxt=nxt.strftime("%Y-%m"),
                today=today_ist())


# --------------------------------------------------------------------------- properties, room types, rates


@router.get("/properties")
def properties_list(request: Request, auth: Auth = Depends(require_user)):
    repo = get_repo()
    rows = [(p, repo.list_room_types(p.id, False)) for p in auth.properties(repo)]
    return page(request, "properties.html", auth, nav="properties", rows=rows)


@router.get("/properties/new")
def property_new(request: Request, auth: Auth = Depends(require_owner)):
    return page(request, "property_form.html", auth, nav="properties", p=Property(""), rooms=[], is_new=True)


@router.get("/properties/{property_id}")
def property_edit(property_id: str, request: Request, auth: Auth = Depends(require_user)):
    repo = get_repo()
    return page(request, "property_form.html", auth, nav="properties", p=_prop(repo, auth, property_id),
                rooms=repo.list_room_types(property_id, False), is_new=False)


@router.post("/properties/{property_id}")
async def property_save(property_id: str, request: Request, auth: Auth = Depends(require_user)):
    f = await form_of(request, auth)
    repo = get_repo()
    if property_id == "new":
        if not auth.is_owner:
            raise HTTPException(403, "Owner only")
        p = Property("")
    else:
        p = _prop(repo, auth, property_id)
    name = f.get("name", "").strip()
    if not name:
        raise redirect(toast_url(f"{BASE}/properties/{property_id}", "!Name is required"))
    staff = [n for raw in _lines(f.get("staff_numbers", "")) if (n := try_normalize_phone(raw))]
    p.name, p.city, p.address = name, f.get("city", "").strip(), f.get("address", "").strip()
    p.map_url, p.description, p.policies = f.get("map_url", "").strip(), f.get("description", "").strip(), \
        f.get("policies", "").strip()
    p.checkin_time, p.checkout_time = f.get("checkin_time", "14:00") or "14:00", f.get("checkout_time", "11:00") or "11:00"
    p.advance_pct, p.gst_pct = _int(f, "advance_pct", 50, 1, 100), _int(f, "gst_pct", 0, 0, 40)
    p.staff_numbers, p.photo_urls = staff, [u for u in _lines(f.get("photo_urls", "").replace(",", "\n")) if u]
    p.active = f.get("active") == "on"
    repo.save_property(p)
    raise redirect(toast_url(f"{BASE}/properties/{p.id}", "Property saved"))


@router.post("/properties/{property_id}/delete")
async def property_delete(property_id: str, request: Request, auth: Auth = Depends(require_owner)):
    await form_of(request, auth)
    repo = get_repo()
    _prop(repo, auth, property_id)
    if repo.list_bookings(property_id=property_id, limit=1):
        raise redirect(toast_url(f"{BASE}/properties/{property_id}", "!It has bookings; mark it inactive instead"))
    repo.delete_property(property_id)
    raise redirect(toast_url(f"{BASE}/properties", "Property deleted"))


def _room(repo, auth: Auth, property_id: str, room_id: str) -> tuple[Property, RoomType | None]:
    prop = _prop(repo, auth, property_id)
    if room_id == "new":
        return prop, None
    rt = repo.get_room_type(room_id)
    if rt is None or rt.property_id != prop.id:
        raise HTTPException(404, "Room type not found")
    return prop, rt


@router.get("/properties/{property_id}/rooms/{room_id}")
def room_edit(property_id: str, room_id: str, request: Request, auth: Auth = Depends(require_user)):
    repo = get_repo()
    prop, rt = _room(repo, auth, property_id, room_id)
    return page(request, "room_form.html", auth, nav="properties", prop=prop,
                rt=rt or RoomType(prop.id, ""), is_new=rt is None,
                rules=repo.list_rate_rules(rt.id) if rt else [])


@router.post("/properties/{property_id}/rooms/{room_id}")
async def room_save(property_id: str, room_id: str, request: Request, auth: Auth = Depends(require_user)):
    f = await form_of(request, auth)
    repo = get_repo()
    prop, rt = _room(repo, auth, property_id, room_id)
    rt = rt or RoomType(prop.id, "")
    name = f.get("name", "").strip()
    if not name:
        raise redirect(toast_url(f"{BASE}/properties/{property_id}/rooms/{room_id}", "!Name is required"))
    rt.name = name
    rt.units, rt.max_adults = _int(f, "units", 1, 1, 500), _int(f, "max_adults", 2, 1, 20)
    rt.max_children, rt.max_occupancy = _int(f, "max_children", 1, 0, 20), _int(f, "max_occupancy", 3, 1, 40)
    rt.base_rate = _int(f, "base_rate", 0)
    rt.weekend_rate = _int(f, "weekend_rate") if f.get("weekend_rate", "").strip() else None
    rt.description, rt.amenities = f.get("description", "").strip(), f.get("amenities", "").strip()
    rt.photo_urls = _lines(f.get("photo_urls", "").replace(",", "\n"))
    rt.active = f.get("active") == "on"
    repo.save_room_type(rt)
    raise redirect(toast_url(f"{BASE}/properties/{prop.id}/rooms/{rt.id}", "Room type saved"))


@router.post("/properties/{property_id}/rooms/{room_id}/delete")
async def room_delete(property_id: str, room_id: str, request: Request, auth: Auth = Depends(require_owner)):
    await form_of(request, auth)
    repo = get_repo()
    prop, rt = _room(repo, auth, property_id, room_id)
    if rt and any(b.room_type_id == rt.id for b in repo.list_bookings(property_id=prop.id, limit=5000)):
        raise redirect(toast_url(f"{BASE}/properties/{prop.id}/rooms/{rt.id}", "!It has bookings; mark it inactive"))
    if rt:
        repo.delete_room_type(rt.id)
    raise redirect(toast_url(f"{BASE}/properties/{prop.id}", "Room type deleted"))


@router.post("/properties/{property_id}/rooms/{room_id}/rates")
async def rate_save(property_id: str, room_id: str, request: Request, auth: Auth = Depends(require_user)):
    f = await form_of(request, auth)
    repo = get_repo()
    prop, rt = _room(repo, auth, property_id, room_id)
    url = f"{BASE}/properties/{prop.id}/rooms/{room_id}"
    if rt is None:
        raise redirect(toast_url(url, "!Save the room type first"))
    if f.get("delete"):
        if any(r.id == f["delete"] for r in repo.list_rate_rules(rt.id)):
            repo.delete_rate_rule(f["delete"])
        raise redirect(toast_url(url, "Season price removed"))
    start, end, rate = _date(f.get("start")), _date(f.get("end")), _int(f, "rate")
    if not (start and end) or end < start or rate <= 0:
        raise redirect(toast_url(url, "!Enter valid dates and a price"))
    repo.save_rate_rule(RateRule(rt.id, start, end, rate, f.get("label", "").strip()))
    raise redirect(toast_url(url, "Season price added"))


# --------------------------------------------------------------------------- guests


@router.get("/guests")
def guests_list(request: Request, auth: Auth = Depends(require_user)):
    repo, q = get_repo(), (request.query_params.get("q") or "").strip().lower()
    guests = repo.list_guests(2000)
    if not auth.user.property_ids:
        pass
    else:  # managers see guests who booked or chatted; keep it to guests with a booking at their properties
        mine = {b.guest_phone for b in _visible_bookings(repo, auth, limit=5000)}
        guests = [g for g in guests if g.phone in mine]
    if q:
        guests = [g for g in guests if q in g.phone.lower() or q in g.name.lower()]
    stays = {}
    for b in _visible_bookings(repo, auth, status="paid", limit=5000):
        stays[b.guest_phone] = stays.get(b.guest_phone, 0) + 1
    return page(request, "guests.html", auth, nav="guests", guests=guests, stays=stays, q=request.query_params.get("q", ""))


@router.get("/guests/{phone}")
def guest_detail(phone: str, request: Request, auth: Auth = Depends(require_user)):
    repo = get_repo()
    guest = repo.get_guest(phone)
    if guest is None:
        raise HTTPException(404, "Guest not found")
    props, rooms = _names(repo)
    stays = _visible_bookings(repo, auth, phone=phone, limit=500)
    if auth.user.property_ids and not stays:
        raise HTTPException(404, "Guest not found")
    return page(request, "guest_detail.html", auth, nav="guests", g=guest, stays=stays, props=props, rooms=rooms,
                chat=repo.list_chat(phone, 60))


@router.post("/guests/{phone}")
async def guest_save(phone: str, request: Request, auth: Auth = Depends(require_user)):
    f = await form_of(request, auth)
    repo = get_repo()
    guest = repo.get_guest(phone)
    if guest is None:
        raise HTTPException(404, "Guest not found")
    guest.name, guest.notes = f.get("name", "").strip(), f.get("notes", "").strip()
    if f.get("language") in ("en", "ml", "ta", "hi"):
        guest.language = f["language"]
    guest.offers_consent = f.get("offers_consent") == "on"
    repo.upsert_guest(guest)
    raise redirect(toast_url(f"{BASE}/guests/{phone}", "Guest saved"))


# --------------------------------------------------------------------------- conversations


@router.get("/conversations")
def conversations(request: Request, auth: Auth = Depends(require_user)):
    return page(request, "conversations.html", auth, nav="chats", convos=get_repo().list_conversations(),
                current=None, chat=[], paused=False)


@router.get("/conversations/{phone}")
def conversation(phone: str, request: Request, auth: Auth = Depends(require_user)):
    repo = get_repo()
    paused, _ = repo.is_paused(phone)
    return page(request, "conversations.html", auth, nav="chats", convos=repo.list_conversations(), current=phone,
                chat=repo.list_chat(phone, 200), paused=paused, guest=repo.get_guest(phone))


@router.post("/conversations/{phone}/pause")
async def conversation_pause(phone: str, request: Request, auth: Auth = Depends(require_user)):
    f = await form_of(request, auth)
    pause = f.get("pause") == "1"
    get_repo().set_paused(phone, pause, now_utc())
    raise redirect(toast_url(f"{BASE}/conversations/{phone}", "Bot paused for this guest" if pause else "Bot resumed"))


@router.post("/conversations/{phone}/send")
async def conversation_send(phone: str, request: Request, auth: Auth = Depends(require_user)):
    f = await form_of(request, auth)
    repo, text = get_repo(), f.get("text", "").strip()
    url = f"{BASE}/conversations/{phone}"
    if not text:
        raise redirect(url)
    error = integrations.send_staff_text(phone, text)
    if error:
        raise redirect(toast_url(url, f"!{error}"))
    repo.add_chat(ChatMessage(phone, "out", text, now_utc(), source="staff"))
    raise redirect(toast_url(url, "Sent"))


# --------------------------------------------------------------------------- users, settings, search


@router.get("/users")
def users_list(request: Request, auth: Auth = Depends(require_owner)):
    repo = get_repo()
    return page(request, "users.html", auth, nav="users", users=repo.list_users(), props=repo.list_properties(False))


@router.post("/users")
async def user_save(request: Request, auth: Auth = Depends(require_owner)):
    f = await form_of(request, auth)
    form, repo = await request.form(), get_repo()
    email = f.get("email", "").strip().lower()
    if "@" not in email:
        raise redirect(toast_url(f"{BASE}/users", "!Enter a valid email"))
    existing = repo.get_user(email)
    password = f.get("password", "")
    if not existing and len(password) < 8:
        raise redirect(toast_url(f"{BASE}/users", "!Password needs at least 8 characters"))
    role = "owner" if f.get("role") == "owner" else "manager"
    ids = [i for i in form.getlist("property_ids") if repo.get_property(str(i))]
    if existing and existing.email == auth.user.email and (role != "owner" or f.get("active") != "on"):
        raise redirect(toast_url(f"{BASE}/users", "!You cannot demote or disable yourself"))
    repo.save_user(DashboardUser(
        email, hash_password(password) if password else existing.password_hash, f.get("name", "").strip() or email,
        role, [] if role == "owner" else [str(i) for i in ids], f.get("active") == "on" or not existing))
    raise redirect(toast_url(f"{BASE}/users", "User saved"))


@router.post("/users/delete")
async def user_delete(request: Request, auth: Auth = Depends(require_owner)):
    f = await form_of(request, auth)
    email = f.get("email", "").lower()
    if email == auth.user.email:
        raise redirect(toast_url(f"{BASE}/users", "!You cannot delete yourself"))
    get_repo().delete_user(email)
    raise redirect(toast_url(f"{BASE}/users", "User deleted"))


INTEGRATIONS = [
    ("WhatsApp", ["RESORT_WHATSAPP_PHONE_NUMBER_ID", "RESORT_WHATSAPP_ACCESS_TOKEN", "RESORT_WHATSAPP_APP_SECRET",
                  "RESORT_WHATSAPP_VERIFY_TOKEN"]),
    ("Razorpay", ["RAZORPAY_KEY_ID", "RAZORPAY_KEY_SECRET", "RAZORPAY_WEBHOOK_SECRET"]),
    ("AI model", ["GEMINI_API_KEY", "GROQ_API_KEY", "SARVAM_API_KEY", "OPENROUTER_API_KEY", "DEEPSEEK_API_KEY"]),
    ("Dashboard", ["SESSION_SECRET", "RESORT_DB_PATH"]),
]


@router.get("/settings")
def settings_view(request: Request, auth: Auth = Depends(require_owner)):
    import os
    rows = [(name, [(v, bool(os.environ.get(v))) for v in names]) for name, names in INTEGRATIONS]
    return page(request, "settings.html", auth, nav="settings", rows=rows)


@router.get("/search")
def search(request: Request, auth: Auth = Depends(require_user)):
    repo, q = get_repo(), (request.query_params.get("q") or "").strip()
    bookings, guests = [], []
    if q:
        bookings = _filtered_bookings(repo, auth, {"q": q})[:30]
        low = q.lower()
        visible = {b.guest_phone for b in _visible_bookings(repo, auth, limit=5000)} if auth.user.property_ids else None
        guests = [g for g in repo.list_guests(2000) if (low in g.phone.lower() or low in g.name.lower())
                  and (visible is None or g.phone in visible)][:30]
    props, rooms = _names(repo)
    return page(request, "search.html", auth, nav="", q=q, bookings=bookings, guests=guests, props=props, rooms=rooms)
