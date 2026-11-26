"""All dashboard routes. Pages are server-rendered; the only JSON endpoint is the booking price preview."""

from __future__ import annotations

import csv
import io
import json
from calendar import monthrange
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response

from app.core.clock import IST
from app.core.phone import try_normalize_phone
from app.resort import availability as av
from app.resort import billing, refunds, stay
from app.resort.dashboard import integrations
from app.resort.dashboard.deps import (BASE, COOKIE, HERE, Auth, current_property, form_of, get_repo,
                                       make_session, now_utc, page, require_owner, require_user, templates, toast_url)
from app.resort.db import SoldOut, check_password, hash_password
from app.resort.paying import DESK_METHODS, METHODS, PaymentError, apply_payment, history, method_label
from app.resort.models import (Booking, Charge, ChatMessage, DashboardUser, Property, RateRule, RoomType)

router = APIRouter(prefix=BASE)
public_router = APIRouter(prefix="/resort")  # pages opened without logging in (the uploaded photos)

STATUSES = ["hold", "paid", "checked_in", "checked_out", "failed", "expired", "cancelled"]
PAID = billing.PAID_STATES


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
    return FileResponse(target, headers={"Cache-Control": "no-cache"})  # always ask whether it changed


@router.get("/manifest.webmanifest")
def manifest():
    """Makes the dashboard installable on a phone's home screen (PWA)."""
    data = {
        "name": "Resort Desk", "short_name": "Resort Desk", "description": "Bookings, guests and payments for your resort",
        "id": f"{BASE}/", "start_url": f"{BASE}/", "scope": f"{BASE}/", "display": "standalone", "orientation": "portrait",
        "background_color": "#ffffff", "theme_color": "#7c5cff",
        "icons": [{"src": f"{BASE}/static/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any maskable"},
                  {"src": f"{BASE}/static/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"}],
        "shortcuts": [{"name": "New booking", "url": f"{BASE}/bookings/new"}, {"name": "Calendar", "url": f"{BASE}/calendar"}],
    }
    return Response(json.dumps(data), media_type="application/manifest+json", headers={"Cache-Control": "no-cache"})


@router.get("/sw.js")
def service_worker():
    """Served from the dashboard root so it controls every dashboard page."""
    return FileResponse(HERE / "static" / "sw.js", media_type="text/javascript", headers={"Cache-Control": "no-cache"})


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
    """The front-desk view: today's arrivals, departures and house count, tonight's occupancy, money in today, and
    the bookings waiting for payment. Everything else lives in its own page."""
    repo, today, now = get_repo(), today_ist(), now_utc()
    props = auth.properties(repo)
    props_by_id, rooms = _names(repo)
    mine = _visible_bookings(repo, auth, limit=5000)
    arrivals = stay.arrivals(mine, today)
    departures = stay.departures(mine, today) or [b for b in mine if b.status == "paid" and b.check_out == today]
    in_house = stay.in_house(mine)
    holds = sorted((b for b in mine if b.status == "hold" and av.booking_blocks(b, now)), key=lambda b: b.check_in)
    occ = [(p, occupancy(repo, p, today, 14, now)) for p in props if p.active]
    used, total = sum(days[0]["used"] for _, days in occ), sum(days[0]["total"] for _, days in occ)
    ids = {b.id for b in mine}
    collected = billing.collections(mine, [r for r in repo.list_refunds() if r.booking_id in ids], today, today).net
    hour = now.astimezone(IST).hour
    greeting = "Good morning" if hour < 12 else "Good afternoon" if hour < 17 else "Good evening"
    active = [p for p in props if p.active] or props
    upcoming = sorted((b for b in mine if b.status == "paid" and b.check_in > today),
                      key=lambda b: b.check_in)
    return page(request, "home.html", auth, nav="home", today=today, greeting=greeting, arrivals=arrivals, departures=departures,
                holds=holds, in_house=in_house, occ=occ, props=props_by_id, rooms=rooms, collected=collected,
                tonight_used=used, tonight_total=total, tonight_pct=round(100 * used / total) if total else 0,
                welcome=active[0].name if len(active) == 1 else "Resort Desk", upcoming=upcoming,
                place=", ".join(dict.fromkeys(p.city.split(",")[0].strip() for p in active if p.city)))


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
    if method := params.get("method"):
        items = [b for b in items if (b.payment_method or ("online" if b.status == "paid" else ""))
                 == ("" if method == "unpaid" else method)]
    if q := (params.get("q") or "").strip().lower():
        items = [b for b in items if q in b.guest_phone.lower() or q in b.guest_name.lower() or q in b.id.lower()]
    return items


@router.get("/bookings")
def bookings_list(request: Request, auth: Auth = Depends(require_user)):
    repo = get_repo()
    params = dict(request.query_params)
    if "property_id" not in params and (scope := current_property(request, auth)):
        params["property_id"] = scope  # the sidebar switcher is the default filter
    items = _filtered_bookings(repo, auth, params)
    props, rooms = _names(repo)
    # the tabs count everything the other filters let through, whatever the status (owner's reference, 2026-11-18)
    every = items if not params.get("status") else _filtered_bookings(repo, auth, {k: v for k, v in params.items() if k != "status"})
    counts = {s: sum(1 for b in every if b.status == s) for s in STATUSES}
    today, now = today_ist(), now_utc()
    month = [b for b in _visible_bookings(repo, auth, property_id=params.get("property_id") or None, limit=5000)
             if b.paid_at and b.paid_at.astimezone(IST).date().replace(day=1) == today.replace(day=1)]
    waiting = [b for b in every if b.status == "hold" and av.booking_blocks(b, now)]
    upcoming = [b for b in every if b.status == "paid" and today <= b.check_in <= today + timedelta(days=30)]
    sort = params.get("sort", "")
    if sort == "stay":
        items = sorted(items, key=lambda b: b.check_in)
    elif sort == "amount":
        items = sorted(items, key=lambda b: -b.total)
    # 100 rows a page (load test 2026-11-19: 3000 bookings made a 1.7 MB page); totals still cover every match
    per_page, pages = BOOKINGS_PER_PAGE, max(1, -(-len(items) // BOOKINGS_PER_PAGE))
    page_no = min(max(1, _int(params, "page", 1, 1, 100000)), pages)
    shown = items[(page_no - 1) * per_page: page_no * per_page]
    keep = urlencode({k: v for k, v in params.items() if k != "page" and v})
    return page(request, "bookings.html", auth, nav="bookings", items=shown, props=props, rooms=rooms,
                my_props=auth.properties(repo), statuses=STATUSES, f=params, methods=METHODS, match_count=len(items),
                page_no=page_no, pages=pages, page_query=keep,
                query=str(request.query_params), collected=sum(b.paid_amount for b in items), counts=counts,
                total_count=len(every), month_collected=sum(b.paid_amount for b in month),
                month_count=len(month), waiting=waiting, waiting_sum=sum(b.advance for b in waiting),
                upcoming_count=len(upcoming))


BOOKINGS_PER_PAGE = 100
CSV_BOM = chr(0xFEFF)  # so Excel reads Malayalam / Tamil names as UTF-8


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
                     "Rooms", "Adults", "Children", "Total", "Advance", "Payment method", "Payment reference",
                     "Razorpay payment id", "Amount paid", "Balance", "Received by", "Paid at (IST)", "Created"])
    for b in _filtered_bookings(repo, auth, request.query_params):
        writer.writerow([b.id, b.status, _csv_cell(props[b.property_id].name if b.property_id in props else ""),
                         _csv_cell(rooms[b.room_type_id].name if b.room_type_id in rooms else ""),
                         _csv_cell(b.guest_name), b.guest_phone, b.check_in, b.check_out, b.nights, b.rooms,
                         b.adults, b.children, b.total, b.advance, method_label(b.payment_method) if b.payment_method else "",
                         _csv_cell(b.payment_ref), b.razorpay_payment_id, b.paid_amount, b.balance,
                         _csv_cell(b.received_by),
                         b.paid_at.astimezone(IST).strftime("%Y-%m-%d %H:%M") if b.paid_at else "", b.created_at])
    return Response(CSV_BOM + out.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="bookings.csv"'})


def _booking_form_data(repo, auth: Auth) -> list[dict]:
    return [{"prop": p, "rooms": repo.list_room_types(p.id)} for p in auth.properties(repo, active_only=True)]


@router.get("/bookings/new")
def booking_new(request: Request, auth: Auth = Depends(require_user)):
    repo = get_repo()
    today, q = today_ist(), request.query_params
    check_in = _date(q.get("check_in"), today)
    check_out = _date(q.get("check_out"), check_in + timedelta(days=1))
    if check_out <= check_in:
        check_out = check_in + timedelta(days=1)
    return page(request, "booking_new.html", auth, nav="bookings", data=_booking_form_data(repo, auth),
                today=check_in, tomorrow=check_out, desk_methods=DESK_METHODS,
                pre_property=q.get("property_id") or current_property(request, auth),
                pre_room=q.get("room_type_id", ""))


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
    method = f.get("pay_method") or ("cash" if f.get("payment") == "paid" else "")  # "payment=paid": the old form
    booking = Booking(prop.id, rt.id, phone, check_in, check_out, adults, children, rooms, quote.total, quote.advance,
                      now, status="hold", guest_name=f.get("guest_name", "").strip(),
                      hold_expires_at=now + timedelta(hours=1), notes=f"Added by {auth.user.email}")
    if method:
        amount = _int(f, "pay_amount", quote.advance, 0, quote.total) or quote.advance
        try:
            apply_payment(booking, method=method, amount=amount, ref=f.get("pay_ref", ""), note=f.get("pay_note", ""),
                          received_by=auth.user.email, now=now)
        except PaymentError as exc:
            raise redirect(toast_url(back, f"!{exc}")) from None
    try:
        repo.add_manual_booking(booking, now)
    except SoldOut:
        raise redirect(toast_url(back, "!Not enough free rooms for those dates")) from None
    repo.touch_guest(phone, now, name=booking.guest_name)
    note = integrations.announce_paid(booking) if method else None
    raise redirect(toast_url(f"{BASE}/bookings/{booking.id}", f"Booking created. {note}" if note else "Booking created"))


def redirect(url: str) -> HTTPException:
    return HTTPException(303, headers={"Location": url})


def _folio(repo, b: Booking, prop: Property | None = None, rt: RoomType | None = None) -> billing.Folio:
    prop = prop or repo.get_property(b.property_id)
    rt = rt or repo.get_room_type(b.room_type_id)
    return billing.folio(b, repo.list_charges(b.id), repo.list_refunds(b.id), prop, rt.name if rt else "")


def _glance(repo, b: Booking) -> dict:
    """Balance and ID state of one booking, for the chips on the arrivals page and the reports."""
    return {"balance": _folio(repo, b).balance, "id": stay.id_summary(repo.list_id_proofs(b.id))}


def _url(b: Booking) -> str:
    return f"{BASE}/bookings/{b.id}"


@router.get("/bookings/{booking_id}")
def booking_detail(booking_id: str, request: Request, auth: Auth = Depends(require_user)):
    repo = get_repo()
    b = _booking(repo, auth, booking_id)
    props, rooms = _names(repo)
    prop, rt = props.get(b.property_id), rooms.get(b.room_type_id)
    f = _folio(repo, b, prop, rt)
    proofs = repo.list_id_proofs(b.id)
    refund_list = repo.list_refunds(b.id)
    timeline = [(b.created_at, "Booking created (hold)")]
    if b.payment_link_url:
        timeline.append((None, "Payment link created"))
    if b.paid_at:
        timeline.append((b.paid_at, f"First payment received ({method_label(b.payment_method)})"))
    if b.checked_in_at:
        timeline.append((b.checked_in_at, "Checked in" + (f" · rooms {b.room_numbers}" if b.room_numbers else "")))
    if b.checked_out_at:
        timeline.append((b.checked_out_at, "Checked out"))
    for r in refund_list:
        timeline.append((r.at, f"Refund of ₹{r.amount:,} ({refunds.REFUND_METHODS.get(r.method, r.method)})"))
    if b.status in ("expired", "cancelled", "failed"):
        timeline.append((None, f"Marked {b.status}" + (f": {b.cancel_reason}" if b.cancel_reason else "")))
    pct, amount = refunds.suggested_refund(prop, b, today_ist(), refund_list)
    return page(request, "booking_detail.html", auth, nav="bookings", b=b, prop=prop, rt=rt, timeline=timeline,
                guest=repo.get_guest(b.guest_phone), pay_history=history(b), desk_methods=DESK_METHODS,
                source=_source(b), f=f, charges=repo.list_charges(b.id), refund_list=refund_list, proofs=proofs,
                id_state=stay.id_summary(proofs), id_types=stay.ID_TYPES, warnings=stay.check_in_warnings(b, f, proofs),
                sugg_pct=pct, sugg_amount=amount, left_to_refund=refunds.refundable(b, refund_list),
                refund_methods=refunds.REFUND_METHODS, categories=billing.CATEGORY_LABELS,
                payments=billing.payments_of(b),
                can_charge=b.status in ("paid", "checked_in"))


def _source(b: Booking) -> str:
    """Where the money came from, in plain words."""
    if not b.paid_amount:
        return "Not paid yet"
    if b.payment_method == "online":
        return "Online: Razorpay payment link (the guest paid on their phone)"
    if b.payment_method == "mixed":
        return "Several payments (see the history)"
    return f"Recorded by staff at the desk ({method_label(b.payment_method)})"


@router.post("/bookings/{booking_id}/action")
async def booking_action(booking_id: str, request: Request, auth: Auth = Depends(require_user)):
    f = await form_of(request, auth)
    repo = get_repo()
    b = _booking(repo, auth, booking_id)
    url, action, now = _url(b), f.get("action", ""), now_utc()
    if action in ("mark_paid", "record_payment"):
        if action == "mark_paid":  # the old one-click button: the advance, in cash
            if b.paid_amount:
                raise redirect(toast_url(url, "Already paid"))
            f = {**f, "method": "cash", "amount": str(b.advance)}
        if b.status == "cancelled":
            raise redirect(toast_url(url, "!This booking is cancelled"))
        first = b.paid_amount == 0
        active_hold = b.status == "hold" and b.hold_expires_at is not None and b.hold_expires_at > now
        rt = repo.get_room_type(b.room_type_id)
        if first and not active_hold and rt and repo.free_rooms(rt, b.check_in, b.check_out, now) < b.rooms:
            raise redirect(toast_url(url, "!Those rooms are taken now; cannot record the payment"))
        extra = sum(c.amount for c in repo.list_charges(b.id))
        try:
            ref = apply_payment(b, method=f.get("method", ""),
                                amount=_int(f, "amount", b.advance if first else _folio(repo, b).balance),
                                ref=f.get("ref", ""), note=f.get("note", ""), received_by=auth.user.email, now=now,
                                extra=extra)
        except PaymentError as exc:
            raise redirect(toast_url(url, f"!{exc}")) from None
        repo.save_booking(b)
        warning = integrations.announce_paid(b) if first else None
        raise redirect(toast_url(url, f"Payment recorded ({ref}). {warning}" if warning else f"Payment recorded ({ref})"))
    if action == "cancel":  # the old one-click cancel; the dialog below is the normal way
        try:
            refunds.cancel_booking(repo, b, reason="Cancelled by staff", by=auth.user.email, now=now)
        except refunds.RefundError as exc:
            raise redirect(toast_url(url, f"!{exc}")) from None
        raise redirect(toast_url(url, "Booking cancelled"))
    if action == "resend_link":
        error = integrations.resend_payment_link(b, by=auth.user.email)
        raise redirect(toast_url(url, f"!{error}" if error else "Payment link sent"))
    if action == "discount":  # before the guest pays: new total, new advance, new link sent on WhatsApp
        error = integrations.resend_payment_link(b, discount=_int(f, "discount", 0), by=auth.user.email)
        raise redirect(toast_url(url, f"!{error}" if error else "Discount given. The guest has the new payment link"))
    if action == "set_advance":  # before the guest pays: the owner's own advance, a new link sent on WhatsApp
        error = integrations.resend_payment_link(b, advance=_int(f, "advance", 0), by=auth.user.email)
        raise redirect(toast_url(url, f"!{error}" if error else "Advance changed. The guest has the new payment link"))
    if action == "note":
        if text := f.get("note", "").strip():
            stamp = datetime.now(IST).strftime("%d %b %H:%M")
            b.notes = (b.notes + f"\n[{stamp} {auth.user.email}] {text}").strip()
            repo.save_booking(b)
        raise redirect(toast_url(url, "Note added"))
    raise redirect(toast_url(url, "!Unknown action"))


# --------------------------------------------------------------------------- folio: extra charges


@router.post("/bookings/{booking_id}/charges")
async def charge_add(booking_id: str, request: Request, auth: Auth = Depends(require_user)):
    f = await form_of(request, auth)
    repo = get_repo()
    b = _booking(repo, auth, booking_id)
    url = _url(b)
    category = f.get("category", "other")
    description, amount = f.get("description", "").strip(), _int(f, "amount", 0, 0, 1_000_000)
    if b.status not in ("paid", "checked_in"):
        raise redirect(toast_url(url, "!Extras can be added once the booking is paid"))
    if category not in billing.CATEGORY_LABELS or category == "discount":
        category = "other"
    if not description or amount < 1:
        raise redirect(toast_url(url, "!Enter what it is and the amount"))
    repo.add_charge(Charge(b.id, description, amount, now_utc(), category, auth.user.email))
    raise redirect(toast_url(url, "Extra added to the bill"))


@router.post("/bookings/{booking_id}/charges/{charge_id}/delete")
async def charge_delete(booking_id: str, charge_id: str, request: Request, auth: Auth = Depends(require_user)):
    await form_of(request, auth)
    repo = get_repo()
    b = _booking(repo, auth, booking_id)
    if b.status in ("checked_out", "cancelled"):
        raise redirect(toast_url(_url(b), "!The bill is closed"))
    if any(c.id == charge_id for c in repo.list_charges(b.id)):
        repo.delete_charge(charge_id)
    raise redirect(toast_url(_url(b), "Extra removed"))


# --------------------------------------------------------------------------- cancellation and refunds


@router.post("/bookings/{booking_id}/cancel")
async def booking_cancel(booking_id: str, request: Request, auth: Auth = Depends(require_user)):
    f = await form_of(request, auth)
    repo = get_repo()
    b = _booking(repo, auth, booking_id)
    try:
        refund = refunds.cancel_booking(repo, b, reason=f.get("reason", ""), refund_amount=_int(f, "refund_amount", 0),
                                        method=f.get("method", "cash"), ref=f.get("ref", ""), note=f.get("note", ""),
                                        by=auth.user.email, now=now_utc())
    except refunds.RefundError as exc:
        raise redirect(toast_url(_url(b), f"!{exc}")) from None
    warning = integrations.announce_cancelled(b, refund.amount if refund else 0)
    msg = "Booking cancelled" + (f" and ₹{refund.amount:,} refund recorded" if refund else "")
    raise redirect(toast_url(_url(b), f"{msg}. {warning}" if warning else msg))


@router.post("/bookings/{booking_id}/refund")
async def booking_refund(booking_id: str, request: Request, auth: Auth = Depends(require_user)):
    f = await form_of(request, auth)
    repo = get_repo()
    b = _booking(repo, auth, booking_id)
    try:
        refund = refunds.record_refund(repo, b, amount=_int(f, "refund_amount", 0), method=f.get("method", ""),
                                       ref=f.get("ref", ""), note=f.get("note", ""), by=auth.user.email, now=now_utc())
    except refunds.RefundError as exc:
        raise redirect(toast_url(_url(b), f"!{exc}")) from None
    raise redirect(toast_url(_url(b), f"Refund of ₹{refund.amount:,} recorded ({refund.ref})"))


# --------------------------------------------------------------------------- check-in / check-out, ID proofs


@router.post("/bookings/{booking_id}/checkin")
async def booking_checkin(booking_id: str, request: Request, auth: Auth = Depends(require_user)):
    f = await form_of(request, auth)
    repo = get_repo()
    b = _booking(repo, auth, booking_id)
    try:
        stay.check_in(repo, b, room_numbers=f.get("room_numbers", ""), by=auth.user.email, now=now_utc())
    except stay.StayError as exc:
        raise redirect(toast_url(_url(b), f"!{exc}")) from None
    raise redirect(toast_url(_url(b), "Guest checked in"))


@router.post("/bookings/{booking_id}/checkout")
async def booking_checkout(booking_id: str, request: Request, auth: Auth = Depends(require_user)):
    f = await form_of(request, auth)
    repo = get_repo()
    b = _booking(repo, auth, booking_id)
    url, now = _url(b), now_utc()
    try:
        if f.get("pay_amount", "").strip() and _int(f, "pay_amount") > 0:  # the final payment, taken at the desk
            apply_payment(b, method=f.get("pay_method", ""), amount=_int(f, "pay_amount"), ref=f.get("pay_ref", ""),
                          note="At check-out", received_by=auth.user.email, now=now,
                          extra=sum(c.amount for c in repo.list_charges(b.id)))
            repo.save_booking(b)
        folio = _folio(repo, b)
        stay.check_out(repo, b, folio, waive=f.get("waive") == "on", note=f.get("note", ""), by=auth.user.email, now=now)
    except (PaymentError, stay.StayError) as exc:
        raise redirect(toast_url(url, f"!{exc}")) from None
    integrations.announce_checked_out(b)
    raise redirect(toast_url(url, "Guest checked out"))


@router.post("/bookings/{booking_id}/id/in-person")
async def id_in_person(booking_id: str, request: Request, auth: Auth = Depends(require_user)):
    f = await form_of(request, auth)
    repo = get_repo()
    b = _booking(repo, auth, booking_id)
    try:
        stay.mark_shown_in_person(repo, b, id_type=f.get("id_type", ""), by=auth.user.email, now=now_utc())
    except stay.StayError as exc:
        raise redirect(toast_url(_url(b), f"!{exc}")) from None
    raise redirect(toast_url(_url(b), "ID noted as shown in person"))


def _proof(repo, b: Booking, proof_id: str):
    proof = repo.get_id_proof(proof_id)
    if proof is None or proof.booking_id != b.id:
        raise HTTPException(404, "ID proof not found")
    return proof


@router.post("/bookings/{booking_id}/id/{proof_id}/status")
async def id_status(booking_id: str, proof_id: str, request: Request, auth: Auth = Depends(require_user)):
    f = await form_of(request, auth)
    repo = get_repo()
    b = _booking(repo, auth, booking_id)
    _proof(repo, b, proof_id)
    try:
        stay.set_proof_status(repo, proof_id, f.get("status", ""), auth.user.email)
    except stay.StayError as exc:
        raise redirect(toast_url(_url(b), f"!{exc}")) from None
    raise redirect(toast_url(_url(b), "ID " + f["status"]))


@router.get("/bookings/{booking_id}/id/{proof_id}/view")
def id_view(booking_id: str, proof_id: str, request: Request, auth: Auth = Depends(require_user)):
    """The ID photo. Only logged-in staff with access to this property; never cached, never run as a page."""
    repo = get_repo()
    b = _booking(repo, auth, booking_id)
    proof = _proof(repo, b, proof_id)
    if not proof.data or proof.mime not in stay.IMAGE_TYPES:
        raise HTTPException(415, "No image to show")
    return Response(proof.data, media_type=proof.mime, headers={
        "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", "Content-Security-Policy": "sandbox",
        "Content-Disposition": "inline"})


# --------------------------------------------------------------------------- receipt


def _receipt_ctx(repo, b: Booking) -> dict:
    prop, rt = repo.get_property(b.property_id), repo.get_room_type(b.room_type_id)
    return {"b": b, "prop": prop, "rt": rt, "f": _folio(repo, b, prop, rt), "payments": billing.payments_of(b),
            "refund_list": repo.list_refunds(b.id), "invoice_no": billing.invoice_number(b, prop),
            "guest": repo.get_guest(b.guest_phone), "issued": now_utc()}


@router.get("/bookings/{booking_id}/receipt/{n}")
def receipt_page(booking_id: str, n: int, request: Request, auth: Auth = Depends(require_user)):
    repo = get_repo()
    b = _booking(repo, auth, booking_id)
    ctx = _receipt_ctx(repo, b)
    payment = next((p for p in ctx["payments"] if p.n == n), None)
    if payment is None:
        raise HTTPException(404, "Receipt not found")
    return templates.TemplateResponse(request, "receipt.html", {**ctx, "payment": payment, "BASE": BASE})


# --------------------------------------------------------------------------- arrivals and departures


@router.get("/stay")
def stay_page(request: Request, auth: Auth = Depends(require_user)):
    repo, today = get_repo(), today_ist()
    scope = current_property(request, auth)
    mine = _visible_bookings(repo, auth, property_id=scope or None, limit=5000)
    sections = [("Arriving today", stay.arrivals(mine, today), "checkin"),
                ("Arriving tomorrow", stay.arrivals(mine, today + timedelta(days=1)), ""),
                ("Should have checked in", stay.overdue_arrivals(mine, today), "checkin"),
                ("In the house", stay.in_house(mine), "checkout"),
                ("Leaving today or overdue", stay.departures(mine, today), "checkout")]
    shown = {b.id: b for _, items, _ in sections for b in items}
    props, rooms = _names(repo)
    return page(request, "stay.html", auth, nav="stay", today=today, sections=sections, props=props, rooms=rooms,
                glance={bid: _glance(repo, b) for bid, b in shown.items()})


# --------------------------------------------------------------------------- collection report


def _report_range(q, today: date) -> tuple[date, date]:
    start = _date(q.get("from"), today)
    end = _date(q.get("to"), start)
    return (start, end) if end >= start else (end, start)


@router.get("/reports/collections")
def report_collections(request: Request, auth: Auth = Depends(require_user)):
    repo, q, today = get_repo(), request.query_params, today_ist()
    start, end = _report_range(q, today)
    prop_id = q.get("property_id") or current_property(request, auth)
    mine = _visible_bookings(repo, auth, property_id=prop_id or None, limit=20000)
    ids = {b.id for b in mine}
    report = billing.collections(mine, [r for r in repo.list_refunds() if r.booking_id in ids], start, end)
    owing = []
    due = [b for b in mine if b.status in ("paid", "checked_in") and b.check_in <= end]
    if due:  # one query for all charges and one for all refunds (load test 2026-11-19: it was 4 queries per booking)
        charges_by, refunds_by = repo.charges_by_booking(), repo.refunds_by_booking()
        props_all = {p.id: p for p in repo.list_properties(active_only=False)}
        _, rooms = _names(repo)
        for b in due:
            rt = rooms.get(b.room_type_id)
            f = billing.folio(b, charges_by.get(b.id, []), refunds_by.get(b.id, []), props_all.get(b.property_id),
                              rt.name if rt else "")
            if f.balance > 0:
                owing.append((b, f.balance))
    owing.sort(key=lambda row: row[0].check_in)
    if q.get("format") == "csv":
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(["Date (IST)", "Type", "Booking", "Guest", "Method", "Amount", "Reference", "By"])
        for row in report.rows:
            writer.writerow([row["at"].astimezone(IST).strftime("%Y-%m-%d %H:%M"), row["kind"], row["booking"],
                             _csv_cell(row["guest"]), method_label(row["method"]), row["amount"], _csv_cell(row["ref"]),
                             _csv_cell(row["by"])])
        writer.writerow([])
        writer.writerow(["Collected", "", "", "", "", report.collected])
        writer.writerow(["Refunded", "", "", "", "", -report.refunded])
        writer.writerow(["Net", "", "", "", "", report.net])
        return Response(CSV_BOM + out.getvalue(), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="collections-{start}-{end}.csv"'})
    props, _ = _names(repo)
    return page(request, "report_collections.html", auth, nav="reports", r=report, start=start, end=end, f=q, today=today,
                my_props=auth.properties(repo), prop_id=prop_id, owing=owing, props=props,
                days=sorted(report.by_day, reverse=True), methods=METHODS, query=str(q))


# --------------------------------------------------------------------------- calendar


def _month_start(value: str | None) -> date:
    return (_date((value or "") + "-01") or today_ist()).replace(day=1)


def _calendar_scope(request: Request, auth: Auth, repo):
    """(visible properties, chosen property, its room types, chosen room type id)."""
    q = request.query_params
    props = auth.properties(repo, active_only=True)
    wanted = q.get("property_id") or current_property(request, auth)
    prop = next((p for p in props if p.id == wanted), props[0] if props else None)
    types = repo.list_room_types(prop.id) if prop else []
    rt_id = q.get("room_type_id", "")
    return props, prop, types, rt_id if any(rt.id == rt_id for rt in types) else ""


def _month_weeks(repo, prop: Property, types: list[RoomType], rt_id: str, first: date, now: datetime) -> list[list[dict]]:
    """Monday-first weeks covering the month; every cell knows its rooms paid / held / free."""
    chosen = [rt for rt in types if rt.id == rt_id] or types
    ids, total = {rt.id for rt in chosen}, sum(rt.units for rt in chosen)
    last = first.replace(day=monthrange(first.year, first.month)[1])
    grid_start = first - timedelta(days=first.weekday())
    grid_end = last + timedelta(days=6 - last.weekday())
    bookings = [b for b in repo.list_bookings(property_id=prop.id, start=grid_start,
                                              end=grid_end + timedelta(days=1), limit=5000)
                if b.room_type_id in ids and av.booking_blocks(b, now)]
    today, weeks, day = today_ist(), [], grid_start
    while day <= grid_end:
        week = []
        for _ in range(7):
            on = [b for b in bookings if b.check_in <= day < b.check_out]
            paid, held = sum(b.rooms for b in on if b.status in PAID), sum(b.rooms for b in on if b.status == "hold")
            in_h = sum(b.rooms for b in on if b.status == "checked_in")
            free = max(total - paid - held, 0)
            week.append({"day": day, "in_month": day.month == first.month, "today": day == today,
                         "weekend": day.weekday() >= 5, "paid": paid, "held": held, "in_house": in_h, "free": free, "total": total,
                         "pct": round(100 * (paid + held) / total) if total else 0,
                         "state": "none" if not total else "full" if free == 0 else "busy" if paid + held else "free",
                         "past": day < today,
                         # guest chips like the owner's reference calendar (2026-11-18): who arrives that day
                         "chips": [{"name": (b.guest_name or b.guest_phone).split()[0][:12], "status": b.status,
                                    "nights": b.nights} for b in sorted(on, key=lambda x: x.created_at)
                                   if b.check_in == day or (day == grid_start and b.check_in < day)]})
            day += timedelta(days=1)
        weeks.append(week)
    return weeks


@router.get("/calendar")
def calendar(request: Request, auth: Auth = Depends(require_user)):
    repo, q, now = get_repo(), request.query_params, now_utc()
    props, prop, types, rt_id = _calendar_scope(request, auth, repo)
    if not prop:
        return page(request, "calendar.html", auth, nav="calendar", props=[], prop=None)
    first = _month_start(q.get("month"))
    nxt = (first + timedelta(days=32)).replace(day=1)
    prev = (first - timedelta(days=1)).replace(day=1)
    view = "timeline" if q.get("view") == "timeline" else "month"
    ctx = dict(props=props, prop=prop, types=types, rt_id=rt_id, month=first.strftime("%B %Y"), month_key=first.strftime("%Y-%m"),
               prev=prev.strftime("%Y-%m"), nxt=nxt.strftime("%Y-%m"), today=today_ist(), view=view, weeks=[], rows=[], days=[])
    if view == "month":
        ctx["weeks"] = _month_weeks(repo, prop, types, rt_id, first, now)
    else:
        days = [first + timedelta(days=i) for i in range(monthrange(first.year, first.month)[1])]
        bookings = repo.list_bookings(property_id=prop.id, start=first, end=nxt, limit=5000)
        rows = []
        for rt in types:
            cells = []
            for day in days:
                on = [b for b in bookings if b.room_type_id == rt.id and av.booking_blocks(b, now)
                      and b.check_in <= day < b.check_out]
                booked, held = sum(b.rooms for b in on if b.status in PAID), sum(b.rooms for b in on if b.status == "hold")
                cells.append({"day": day, "booked": booked, "held": held, "free": max(rt.units - booked - held, 0)})
            rows.append({"rt": rt, "cells": cells})
        ctx.update(days=days, rows=rows)
    return page(request, "calendar.html", auth, nav="calendar", **ctx)


@router.get("/calendar/day")
def calendar_day(request: Request, auth: Auth = Depends(require_user)):
    """The side panel for one day: HTML fragment, loaded by the calendar page."""
    repo, q, now = get_repo(), request.query_params, now_utc()
    _, prop, types, rt_id = _calendar_scope(request, auth, repo)
    day = _date(q.get("date"))
    if not prop or not day:
        raise HTTPException(404, "Pick a day")
    names = {rt.id: rt for rt in types}
    chosen = [rt for rt in types if rt.id == rt_id] or types
    items = [b for b in _visible_bookings(repo, auth, property_id=prop.id, start=day, end=day + timedelta(days=1),
                                          limit=500)
             if b.room_type_id in {rt.id for rt in chosen} and b.status in (*PAID, "hold")
             and (b.status in PAID or av.booking_blocks(b, now))]
    items.sort(key=lambda b: (b.status not in PAID, b.check_in))
    total = sum(rt.units for rt in chosen)
    used = sum(b.rooms for b in items)
    return templates.TemplateResponse(request, "calendar_day.html", {
        "BASE": BASE, "day": day, "prop": prop, "rt_id": rt_id, "items": items, "names": names, "total": total,
        "used": used, "free": max(total - used, 0)})


# --------------------------------------------------------------------------- properties, room types, rates


@router.get("/properties")
def properties_list(request: Request, auth: Auth = Depends(require_user)):
    repo = get_repo()
    rows = []
    for p in auth.properties(repo):
        types = repo.list_room_types(p.id, False)
        cover = next(iter(p.photo_urls), "") or next((rt.photo_urls[0] for rt in types if rt.photo_urls), "")
        rows.append((p, types, cover))
    return page(request, "properties.html", auth, nav="properties", rows=rows)


@router.get("/ai-status")
def ai_status(request: Request, auth: Auth = Depends(require_owner)):
    """Owner only: which AI the bot uses right now, in order (names and models, never keys)."""
    from app.resort.services import get_services
    try:
        llm = get_services().llm
        chain = [{"name": x.name, "model": getattr(x, "model", "")} for x in getattr(llm, "providers", [llm])]
        return JSONResponse({"llm_chain": chain})
    except Exception as exc:  # no usable provider: say why, still without keys
        return JSONResponse({"llm_chain": [], "error": str(exc)[:300]})


@router.get("/hold-cleanup")
def hold_cleanup(request: Request, auth: Auth = Depends(require_owner)):
    """Owner only: runs the one-hold-per-guest cleanup now, then lists every booking it ever closed (masked phones)."""
    from app.core.phone import mask_phone
    from app.resort.agent import holds
    from app.resort.services import get_services
    now_closed = holds.cleanup_all(get_services().bot)
    repo = get_repo()
    props, rooms = _names(repo)
    closed = [b for b in repo.list_bookings(status="cancelled", limit=5000) if holds.CLEANUP in (b.notes or "")]
    by_guest: dict[str, list[str]] = {}
    for b in closed:
        by_guest.setdefault(mask_phone(b.guest_phone), []).append(
            f"{b.id} {rooms[b.room_type_id].name if b.room_type_id in rooms else b.room_type_id} {b.check_in}..{b.check_out}")
    return JSONResponse({"closed_just_now": now_closed, "total_closed_by_cleanup": len(closed),
                         "guests": len(by_guest), "by_guest": by_guest})


@router.get("/photo-check")
def photo_check(request: Request, auth: Auth = Depends(require_owner)):
    """Owner only: does every photo map to one room? Lists what to fix, plus each list's photo count."""
    from app.resort.agent import photo_map
    repo = get_repo()
    counts = []
    for p in repo.list_properties(active_only=False):
        counts.append({"where": f"{p.name}: general resort photos", "photos": len(p.photo_urls or [])})
        for rt in repo.list_room_types(p.id, False):
            counts.append({"where": f"{p.name} / {rt.name}", "photos_without_room_number": len(rt.photo_urls or []),
                           "photos_by_room_number": {n: len(r) for n, r in (rt.number_photos or {}).items() if r}})
    return JSONResponse({"to_fix": photo_map.check(repo), "counts": counts})


@router.post("/delete-all-bookings")
async def delete_all_bookings(request: Request, auth: Auth = Depends(require_owner)):
    """Owner only: delete every booking (chats, guests, rooms and settings stay). Needs confirm=DELETE."""
    f = await request.form()
    if str(f.get("confirm", "")) != "DELETE":
        raise HTTPException(400, "Type DELETE to confirm")
    return JSONResponse({"deleted": get_repo().clear_bookings()})


@router.post("/reset-test-data")
async def reset_test_data(request: Request, auth: Auth = Depends(require_owner)):
    """Owner only: delete all bookings and chats (test data) before a client starts. Needs confirm=DELETE."""
    f = await request.form()
    if str(f.get("confirm", "")) != "DELETE":
        raise HTTPException(400, "Type DELETE to confirm")
    return JSONResponse({"deleted": get_repo().clear_bookings_and_chats()})


@router.post("/seed-demo")
async def seed_demo_data(request: Request, auth: Auth = Depends(require_owner)):
    """Owner only: demo bookings and chats for a presentation (no messages are sent). Needs confirm=DEMO."""
    from app.resort.demo import seed_demo
    f = await request.form()
    if str(f.get("confirm", "")) != "DEMO":
        raise HTTPException(400, "Type DEMO to confirm")
    return JSONResponse(seed_demo(get_repo(), now_utc()))


@router.post("/clear-demo")
async def clear_demo_data(request: Request, auth: Auth = Depends(require_owner)):
    """Owner only: remove the demo bookings and chats again (real data stays). Needs confirm=DEMO."""
    from app.resort.demo import clear_demo
    f = await request.form()
    if str(f.get("confirm", "")) != "DEMO":
        raise HTTPException(400, "Type DEMO to confirm")
    return JSONResponse(clear_demo(get_repo()))


@router.get("/properties/new")
def property_new(request: Request, auth: Auth = Depends(require_owner)):
    return page(request, "property_form.html", auth, nav="properties", p=Property(""), rooms=[], is_new=True)


@router.get("/properties/{property_id}")
def property_edit(property_id: str, request: Request, auth: Auth = Depends(require_user)):
    repo = get_repo()
    p = _prop(repo, auth, property_id)
    return page(request, "property_form.html", auth, nav="properties", p=p,
                rooms=repo.list_room_types(property_id, False), is_new=False)


async def _photo_form(request: Request, auth: Auth):
    form = await request.form()
    import secrets as _secrets
    if not _secrets.compare_digest(str(form.get("csrf", "")), auth.csrf):
        raise HTTPException(403, "Bad CSRF token")
    return form


async def _apply_photos(request: Request, auth: Auth, photos: list[str]) -> tuple[list[str], str]:
    """Upload / remove / make-cover on a photo list. Returns the new list and a toast message."""
    from app.resort import photos as ph
    form = await _photo_form(request, auth)
    action, ref = str(form.get("action", "upload")), str(form.get("ref", ""))
    if action == "remove" and ref in photos:
        ph.delete_photo(ref)
        return [x for x in photos if x != ref], "Photo removed"
    if action == "cover" and ref in photos:
        return [ref] + [x for x in photos if x != ref], "Cover photo set"
    files = [f for f in form.getlist("photos") if hasattr(f, "read")]
    added, problem = 0, ""
    for upload in files:  # no limit (client, 2026-11-19): every photo the owner uploads is kept and sent
        try:
            photos = photos + [ph.save_photo(await upload.read())]
            added += 1
        except ph.PhotoError as exc:
            problem = str(exc)
    if problem and not added:
        return photos, "!" + problem
    if not added:
        return photos, "!Choose one or more photos first"
    return photos, f"{added} photo{'s' if added != 1 else ''} added" + (f" ({problem})" if problem else "")


@router.post("/properties/{property_id}/photos")
async def property_photos(property_id: str, request: Request, auth: Auth = Depends(require_user)):
    repo = get_repo()
    p = _prop(repo, auth, property_id)
    p.photo_urls, message = await _apply_photos(request, auth, list(p.photo_urls))
    repo.save_property(p)
    raise redirect(toast_url(f"{BASE}/properties/{p.id}#photos", message))


@router.post("/properties/{property_id}/rooms/{room_id}/photos")
async def room_photos(property_id: str, room_id: str, request: Request, auth: Auth = Depends(require_user)):
    repo = get_repo()
    prop, rt = _room(repo, auth, property_id, room_id)
    if rt is None:
        raise HTTPException(404, "Save the room type first")
    rt.photo_urls, message = await _apply_photos(request, auth, list(rt.photo_urls))
    repo.save_room_type(rt)
    raise redirect(toast_url(f"{BASE}/properties/{prop.id}/rooms/{rt.id}#photos", message))


@router.post("/properties/{property_id}/rooms/{room_id}/numbers/{number}/photos")
async def room_number_photos(property_id: str, room_id: str, number: str, request: Request,
                             auth: Auth = Depends(require_user)):
    """One room number's own photos (owner, 2026-11-19): the bot sends these when it gives this room."""
    repo = get_repo()
    prop, rt = _room(repo, auth, property_id, room_id)
    if rt is None or number not in rt.room_list:
        raise HTTPException(404, "Add this room number first")
    photos, message = await _apply_photos(request, auth, list(rt.number_photos.get(number, [])))
    rt.number_photos = {**rt.number_photos, number: photos}
    repo.save_room_type(rt)
    raise redirect(toast_url(f"{BASE}/properties/{prop.id}/rooms/{rt.id}#room-{number}", message))


@public_router.get("/media/{name}")
def media(name: str):
    """Uploaded property/room photos (public: they are the resort's marketing pictures)."""
    from app.resort import photos as ph
    path = ph.local_path(ph.PREFIX + name)
    if path is None:
        raise HTTPException(404, "Not found")
    return FileResponse(path, media_type=ph.mime_of(path), headers={"Cache-Control": "public, max-age=31536000, immutable"})


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
    p.place = f.get("place", "").strip()[:60]
    p.contact_numbers = [n for raw in _lines(f.get("contact_numbers", "")) if (n := try_normalize_phone(raw))]
    p.extra_bed_rate = _int(f, "extra_bed_rate", 0, 0, 100000)
    p.map_url, p.description, p.policies = f.get("map_url", "").strip(), f.get("description", "").strip(), \
        f.get("policies", "").strip()
    p.checkin_time, p.checkout_time = f.get("checkin_time", "14:00") or "14:00", f.get("checkout_time", "11:00") or "11:00"
    p.advance_pct, p.gst_pct = _int(f, "advance_pct", 50, 1, 100), _int(f, "gst_pct", 0, 0, 40)
    p.staff_numbers = staff  # photos are managed by the photo gallery (property_photos), not this form
    p.highlights = _lines(f.get("highlights", ""))[:12]
    p.stay_info = f.get("stay_info", "").strip()[:3000]
    p.intro = f.get("intro", "").strip()[:3000]
    p.meal_plan = f.get("meal_plan", "").strip()[:40]
    p.early_checkin_time = f.get("early_checkin_time", "").strip()[:5]
    p.early_checkin_rate = _int(f, "early_checkin_rate", 0, 0, 100000)
    p.gstin = f.get("gstin", "").strip().upper()[:20]
    p.invoice_prefix = "".join(ch for ch in f.get("invoice_prefix", "INV").upper() if ch.isalnum())[:8] or "INV"
    rows = [{"days_before": _int(f, f"cp_days_{i}", 0, 0, 365), "retention_pct": _int(f, f"cp_pct_{i}", 0, 0, 100)}
            for i in (1, 2, 3, 4, 5) if f.get(f"cp_days_{i}", "").strip() and f.get(f"cp_pct_{i}", "").strip()]
    if rows:
        p.cancel_policy = sorted(rows, key=lambda r: -r["days_before"])
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
    rt.priority = _int(f, "priority", 1, 1, 99)
    rt.room_list = av.split_rooms(f.get("room_list", ""))[:200]
    if rt.room_list:  # the listed rooms are the rooms: the count always matches them
        rt.units = len(rt.room_list)
    rt.active = f.get("active") == "on"  # photos are managed by the photo gallery (room_photos)
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


LEAD_STAGES = {"booked": ("Booked", "paid"), "hold": ("Payment pending", "hold"), "summary": ("Checking the price", "hold"),
               "asked": ("Asked for dates", ""), "new": ("Just chatting", "")}
STAGE_FILTERS = {"interested": ("asked", "summary"), "pending": ("hold",), "booked": ("booked",), "new": ("new",)}


def _lead_rows(repo, auth, q: str = "", stage: str = "") -> list[dict]:
    """One row per WhatsApp guest: who they are, what they asked for, and how far they got."""
    from app.resort import leads as leads_mod
    lead_map = leads_mod.list_leads(repo)
    mine = _visible_bookings(repo, auth, limit=5000)
    by_id = {b.id: b for b in mine}
    latest: dict = {}
    for b in mine:
        if b.guest_phone not in latest or b.created_at > latest[b.guest_phone].created_at:
            latest[b.guest_phone] = b
    guests = repo.list_guests(2000)
    if auth.user.property_ids:  # a manager sees guests who booked or asked about their properties
        visible = set(latest) | {p for p, ld in lead_map.items() if ld["property_id"] and auth.can_see(ld["property_id"])}
        guests = [g for g in guests if g.phone in visible]
    q = q.strip().lower()
    if q:
        guests = [g for g in guests if q in g.phone.lower() or q in g.name.lower()
                  or q in (lead_map.get(g.phone, {}).get("from_city") or "").lower()]
    props, _ = _names(repo)
    rows = []
    for g in guests:
        lead = lead_map.get(g.phone)
        booking = by_id.get(lead["booking_id"]) if lead and lead.get("booking_id") else (None if lead else latest.get(g.phone))
        if booking is not None and booking.status in PAID:
            key = "booked"
        elif booking is not None and booking.status == "hold":
            key = "hold"
        elif lead:
            key = lead["stage"] if lead["stage"] in ("asked", "summary") else "asked"
        else:
            key = "new"
        if stage and key not in STAGE_FILTERS.get(stage, ()):
            continue
        label, cls = LEAD_STAGES[key]
        rows.append({"g": g, "lead": lead, "key": key, "label": label, "cls": cls,
                     "place": props[lead["property_id"]].name if lead and lead["property_id"] in props else ""})
    rows.sort(key=lambda r: r["g"].last_seen or r["g"].first_seen or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return rows


@router.get("/guests")
def guests_list(request: Request, auth: Auth = Depends(require_user)):
    repo, params = get_repo(), request.query_params
    q, stage = (params.get("q") or "").strip(), params.get("stage") or ""
    rows = _lead_rows(repo, auth, q, stage)
    if params.get("format") == "csv":
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(["Name", "Phone", "Language", "Place", "Check-in", "Check-out", "Adults", "Children", "Rooms",
                         "Room type", "Coming from", "Stage", "Last message (IST)"])
        for r in rows:
            g, ld = r["g"], r["lead"] or {}
            writer.writerow([_csv_cell(g.name), g.phone, g.language, _csv_cell(r["place"]), ld.get("check_in") or "",
                             ld.get("check_out") or "", ld.get("adults") or "", ld.get("children") or "", ld.get("rooms") or "",
                             _csv_cell(ld.get("room_type") or ""), _csv_cell(ld.get("from_city") or ""), r["label"],
                             g.last_seen.astimezone(IST).strftime("%Y-%m-%d %H:%M") if g.last_seen else ""])
        return Response(CSV_BOM + out.getvalue(), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": 'attachment; filename="guests-and-leads.csv"'})
    return page(request, "guests.html", auth, nav="guests", rows=rows, q=q, stage=stage, query=str(params),
                counts=_stage_counts(rows if not (q or stage) else _lead_rows(repo, auth)))


def _stage_counts(rows: list[dict]) -> dict[str, int]:
    """Guests per stage tab, from ONE list (load test 2026-11-19: it was rebuilt once per tab)."""
    return {k: sum(1 for r in rows if r["key"] in v) for k, v in STAGE_FILTERS.items()}


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
    from app.resort import leads as leads_mod
    return page(request, "guest_detail.html", auth, nav="guests", g=guest, stays=stays, props=props, rooms=rooms,
                chat=repo.list_chat(phone, 60), lead=leads_mod.get_lead(repo, phone))


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


def _convos(repo) -> list[dict]:
    """The inbox rows, with the last message time as a datetime (shown in IST)."""
    rows = repo.list_conversations()
    for r in rows:
        try:
            at = datetime.fromisoformat(str(r.get("at") or ""))
            r["at"] = at if at.tzinfo else at.replace(tzinfo=timezone.utc)
        except ValueError:
            r["at"] = None
    return rows


@router.get("/conversations")
def conversations(request: Request, auth: Auth = Depends(require_user)):
    return page(request, "conversations.html", auth, nav="chats", convos=_convos(get_repo()),
                current=None, chat=[], paused=False)


@router.get("/conversations/{phone}")
def conversation(phone: str, request: Request, auth: Auth = Depends(require_user)):
    repo = get_repo()
    paused, _ = repo.is_paused(phone)
    return page(request, "conversations.html", auth, nav="chats", convos=_convos(repo), current=phone,
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
    # a staff reply takes the chat over, like a reply from the WhatsApp app (2026-11-07): the bot steps back and
    # comes back by itself after 24 h of staff silence, or with Resume
    repo.set_paused(phone, True, now_utc())
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
