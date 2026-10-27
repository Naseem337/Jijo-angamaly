"""Folio, invoices, cancellation and refunds, collection report, check-in / check-out and ID proofs."""

import csv
import io
import re
from datetime import timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.resort import billing, refunds, stay
from app.resort.dashboard import integrations, router
from app.resort.dashboard import routes
from app.resort.dashboard.deps import BASE, get_repo, now_utc
from app.resort.dashboard.routes import today_ist
from app.resort.db import hash_password
from app.resort.models import Booking, Charge, DashboardUser, IdProof, Property, Refund, RoomType
from app.resort.paying import apply_payment

PHONE = "+919847012345"
SENT: list[tuple[str, str]] = []


@pytest.fixture(autouse=True)
def no_whatsapp(monkeypatch):
    SENT.clear()
    monkeypatch.setattr(integrations, "announce_paid", lambda b: None)
    monkeypatch.setattr(integrations, "announce_cancelled", lambda b, amount: SENT.append(("cancelled", b.id)))
    monkeypatch.setattr(integrations, "announce_checked_out", lambda b: SENT.append(("checked_out", b.id)))
    monkeypatch.setattr(integrations, "send_staff_text", lambda phone, text: SENT.append((phone, text)))


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("RESORT_DB_PATH", str(tmp_path / "resort.sqlite3"))
    repo = get_repo()
    repo.save_user(DashboardUser("owner@x.com", hash_password("owner-pass-1"), "Owner", "owner"))
    p1 = repo.save_property(Property("Pozhiyoram Resort", gst_pct=12, gstin="32ABCDE1234F1Z5", invoice_prefix="PZR"))
    p2 = repo.save_property(Property("Vibes Kanthalloor"))
    rt1 = repo.save_room_type(RoomType(p1.id, "Deluxe", units=2, base_rate=4000))
    rt2 = repo.save_room_type(RoomType(p2.id, "Cottage", units=2, base_rate=6000))
    repo.save_user(DashboardUser("mgr@x.com", hash_password("manager-pass-1"), "Mgr", "manager", [p2.id]))
    app = FastAPI()
    app.include_router(router)
    return repo, app, (p1, p2, rt1, rt2)


def login(app, email="owner@x.com", password="owner-pass-1"):
    client = TestClient(app)
    resp = client.post(f"{BASE}/login", data={"email": email, "password": password})
    assert resp.status_code == 200
    return client, re.search(r'name="csrf" value="([^"]+)"', resp.text).group(1)


def make(repo, prop, rt, *, status="paid", paid=4000, day=10, total=8000, rooms=1):
    now = now_utc()
    d = today_ist() + timedelta(days=day)
    b = Booking(prop.id, rt.id, PHONE, d, d + timedelta(days=2), 2, 0, rooms, total, total // 2, now, status=status,
                guest_name="Asha", amount_paid=paid, payment_method="cash" if paid else "", payment_ref="RCPT-1" if paid else "",
                paid_at=now if paid else None)
    repo.create_hold(b, now)
    return b


def post(client, token, url, **data):
    return client.post(f"{BASE}{url}", data={"csrf": token, **data}, follow_redirects=False)


# --------------------------------------------------------------------------- folio math


def test_folio_gst_charges_refund_balance(env):
    repo, _, (p1, _, rt1, _) = env
    now = now_utc()
    b = Booking(p1.id, rt1.id, PHONE, today_ist(), today_ist() + timedelta(days=2), 2, 0, 1, 11200, 5600, now,
                status="paid", amount_paid=5600)
    f = billing.folio(b, [Charge(b.id, "Dinner", 1120, now, "food")], [Refund(b.id, 500, now)], p1, "Deluxe")
    assert (f.room_total, f.charges_total, f.bill_total) == (11200, 1120, 12320)
    assert (f.subtotal, f.gst, f.cgst, f.sgst) == (11000, 1320, 660, 660)
    assert (f.paid, f.refunded, f.net_paid, f.balance, f.credit) == (5600, 500, 5100, 7220, 0)
    assert [l.kind for l in f.lines] == ["room", "charge"]


def test_folio_credit_cancelled_and_waiver(env):
    repo, _, (p1, _, rt1, _) = env
    now = now_utc()
    b = Booking(p1.id, rt1.id, PHONE, today_ist(), today_ist() + timedelta(days=1), 2, 0, 1, 5000, 2500, now,
                status="paid", amount_paid=6000)
    assert billing.folio(b, [], [], p1).credit == 1000
    waived = billing.folio(b, [Charge(b.id, "Balance waived", -500, now, "discount")], [], p1)
    assert waived.bill_total == 4500 and waived.lines[1].kind == "discount"
    b.status = "cancelled"
    gone = billing.folio(b, [], [Refund(b.id, 4000, now)], p1)
    assert gone.balance == 0 and gone.retained == 2000


def test_payments_parse_from_history(env):
    repo, _, (p1, _, rt1, _) = env
    b = make(repo, p1, rt1, status="hold", paid=0)
    apply_payment(b, method="cash", amount=3000, received_by="staff1@x.com", now=now_utc())
    apply_payment(b, method="upi", amount=2000, ref="UTR99", note="Gpay", received_by="owner@x.com", now=now_utc())
    pays = billing.payments_of(b)
    assert [(p.method, p.amount, p.ref, p.by) for p in pays] == [
        ("cash", 3000, f"RCPT-{b.id}-1", "staff1@x.com"), ("upi", 2000, "UTR99", "owner@x.com")]
    assert billing.invoice_number(b, p1).startswith("PZR-") and b.id.split("-")[-1] in billing.invoice_number(b, p1)


# --------------------------------------------------------------------------- charges and balance payments


def test_add_charge_and_pay_balance_including_extras(env):
    repo, app, (p1, _, rt1, _) = env
    b = make(repo, p1, rt1)  # total 8000, paid 4000
    client, token = login(app)
    post(client, token, f"/bookings/{b.id}/charges", category="food", description="Dinner for 4", amount="1500")
    assert [c.amount for c in repo.list_charges(b.id)] == [1500]
    page = client.get(f"{BASE}/bookings/{b.id}").text
    assert "Dinner for 4" in page and "₹9,500" in page  # bill total
    # the balance is 5500: more than room rent alone would allow
    post(client, token, f"/bookings/{b.id}/action", action="record_payment", method="cash", amount="5500")
    assert repo.get_booking(b.id).paid_amount == 9500
    cid = repo.list_charges(b.id)[0].id
    post(client, token, f"/bookings/{b.id}/charges/{cid}/delete")
    assert repo.list_charges(b.id) == []


def test_balance_payment_keeps_checked_in_status(env):
    repo, _, (p1, _, rt1, _) = env
    b = make(repo, p1, rt1, status="checked_in")
    apply_payment(b, method="cash", amount=1000, now=now_utc())
    assert b.status == "checked_in"


# --------------------------------------------------------------------------- invoice, receipt, public link


def test_invoice_and_receipt_render(env):
    repo, app, (p1, _, rt1, _) = env
    b = make(repo, p1, rt1, total=11200)
    repo.add_charge(Charge(b.id, "Campfire", 1120, now_utc(), "activity"))
    client, _ = login(app)
    html = client.get(f"{BASE}/bookings/{b.id}/invoice").text
    no = billing.invoice_number(repo.get_booking(b.id), p1)
    assert no in html and "TAX INVOICE" in html and "GSTIN:" in html and "32ABCDE1234F1Z5" in html
    assert "₹12,320" in html and "CGST" in html and "SGST" in html and "₹660" in html
    receipt = client.get(f"{BASE}/bookings/{b.id}/receipt/1")
    assert receipt.status_code == 200 and "₹4,000" in receipt.text and "RCPT-1" in receipt.text
    assert client.get(f"{BASE}/bookings/{b.id}/receipt/9").status_code == 404
    assert TestClient(app).get(f"{BASE}/bookings/{b.id}/invoice", follow_redirects=False).status_code == 303


def test_public_invoice_token_valid_expired_tampered(env, monkeypatch):
    repo, app, (p1, _, rt1, _) = env
    b = make(repo, p1, rt1)
    token = routes.invoice_token(b)
    anon = TestClient(app)  # no login at all
    ok = anon.get(f"/resort/invoice/{token}")
    assert ok.status_code == 200 and billing.invoice_number(b, p1) in ok.text
    assert ok.headers["cache-control"] == "no-store"
    assert anon.get(f"/resort/invoice/{token[:-3]}abc").status_code == 404
    monkeypatch.setattr(routes, "INVOICE_LINK_SECONDS", -1)
    assert anon.get(f"/resort/invoice/{token}").status_code == 404


def test_send_invoice_on_whatsapp(env):
    repo, app, (p1, _, rt1, _) = env
    b = make(repo, p1, rt1)
    client, token = login(app)
    resp = post(client, token, f"/bookings/{b.id}/send-invoice")
    assert resp.status_code == 303 and "toast=Invoice" in resp.headers["location"]
    phone, text = SENT[-1]
    assert phone == PHONE and "/resort/invoice/" in text and billing.invoice_number(b, p1) in text


# --------------------------------------------------------------------------- cancellation and refunds


def test_policy_gives_refund_by_days_left(env):
    repo, _, (p1, _, rt1, _) = env
    far, mid, near = (make(repo, p1, rt1, day=d, rooms=1) for d in (20, 5, 1))
    today = today_ist()
    assert refunds.suggested_refund(p1, far, today) == (100, 4000)
    assert refunds.suggested_refund(p1, mid, today) == (50, 2000)
    assert refunds.suggested_refund(p1, near, today) == (0, 0)


def test_cancel_records_refund_and_frees_rooms(env):
    repo, app, (p1, _, rt1, _) = env
    b = make(repo, p1, rt1, day=10, rooms=2)  # takes both rooms
    d1, d2 = b.check_in, b.check_out
    assert repo.free_rooms(rt1, d1, d2, now_utc()) == 0
    client, token = login(app)
    assert "Policy: 100%" in client.get(f"{BASE}/bookings/{b.id}").text
    resp = post(client, token, f"/bookings/{b.id}/cancel", reason="Guest ill", refund_amount="4000", method="upi", ref="")
    assert "toast=%21" in resp.headers["location"] and repo.get_booking(b.id).status == "paid"  # UPI needs a reference
    post(client, token, f"/bookings/{b.id}/cancel", reason="Guest ill", refund_amount="4000", method="upi", ref="UTRX1")
    done = repo.get_booking(b.id)
    assert done.status == "cancelled" and done.cancel_reason == "Guest ill"
    assert [(r.amount, r.method, r.ref) for r in repo.list_refunds(b.id)] == [(4000, "upi", "UTRX1")]
    assert repo.free_rooms(rt1, d1, d2, now_utc()) == 2
    assert ("cancelled", b.id) in SENT


def test_refund_cannot_exceed_paid_and_can_be_added_later(env):
    repo, app, (p1, _, rt1, _) = env
    b = make(repo, p1, rt1)
    client, token = login(app)
    post(client, token, f"/bookings/{b.id}/cancel", reason="No show", refund_amount="0")
    assert repo.get_booking(b.id).status == "cancelled" and repo.list_refunds(b.id) == []
    assert "toast=%21" in post(client, token, f"/bookings/{b.id}/refund", refund_amount="9000", method="cash").headers["location"]
    post(client, token, f"/bookings/{b.id}/refund", refund_amount="1500", method="cash")
    r = repo.list_refunds(b.id)[0]
    assert r.amount == 1500 and r.ref.startswith("RFND-") and r.by == "owner@x.com"
    assert refunds.refundable(repo.get_booking(b.id), repo.list_refunds(b.id)) == 2500


# --------------------------------------------------------------------------- collection report


def test_collection_report_by_method_staff_and_refunds(env):
    repo, app, (p1, _, rt1, _) = env
    now = now_utc()
    a = make(repo, p1, rt1, status="hold", paid=0)
    apply_payment(a, method="cash", amount=3000, received_by="staff1@x.com", now=now)
    apply_payment(a, method="upi", amount=2000, ref="UTR1", received_by="owner@x.com", now=now)
    repo.save_booking(a)
    b = make(repo, p1, rt1, status="hold", paid=0, day=20)
    apply_payment(b, method="cash", amount=1000, received_by="staff1@x.com", now=now)
    repo.save_booking(b)
    repo.add_refund(Refund(b.id, 400, now, "cash", "RFND-1", by="owner@x.com"))
    today = today_ist()
    rep = billing.collections([repo.get_booking(a.id), repo.get_booking(b.id)], repo.list_refunds(), today, today)
    assert rep.by_method == {"cash": 3600, "upi": 2000}  # 3000 + 1000 - 400, and 2000
    assert rep.by_staff == {"staff1@x.com": 4000, "owner@x.com": 1600}
    assert (rep.collected, rep.refunded, rep.net) == (6000, 400, 5600)
    client, _ = login(app)
    page = client.get(f"{BASE}/reports/collections")
    assert page.status_code == 200 and "₹6,000" in page.text and "staff1@x.com" in page.text
    rows = list(csv.reader(io.StringIO(client.get(f"{BASE}/reports/collections?format=csv").text)))
    assert rows[0][:3] == ["Date (IST)", "Type", "Booking"]
    assert any(r[:1] == ["Net"] and r[-1] == "5600" for r in rows)
    assert any(len(r) > 1 and r[1] == "refund" and r[5] == "-400" for r in rows)


def test_report_lists_pending_balances_and_respects_property(env):
    repo, app, (p1, p2, rt1, rt2) = env
    owing = make(repo, p1, rt1, day=0)  # arrives today, 4000 still due
    make(repo, p2, rt2, day=0, paid=6000, total=6000)  # fully paid
    client, _ = login(app)
    html = client.get(f"{BASE}/reports/collections").text
    assert owing.id in html and "Balances still to collect" in html
    mgr, _ = login(app, "mgr@x.com", "manager-pass-1")  # manager of the other property
    assert owing.id not in mgr.get(f"{BASE}/reports/collections").text


# --------------------------------------------------------------------------- check-in / check-out


def test_checkin_warns_then_checks_in_with_rooms(env):
    repo, app, (p1, _, rt1, _) = env
    b = make(repo, p1, rt1, day=0)
    client, token = login(app)
    page = client.get(f"{BASE}/bookings/{b.id}").text
    assert "No ID proof received yet" in page and "is still to be paid" in page
    post(client, token, f"/bookings/{b.id}/id/in-person", id_type="aadhaar")
    assert stay.id_summary(repo.list_id_proofs(b.id)) == "verified"
    assert "No ID proof received yet" not in client.get(f"{BASE}/bookings/{b.id}").text
    post(client, token, f"/bookings/{b.id}/checkin", room_numbers="101\n102")
    done = repo.get_booking(b.id)
    assert done.status == "checked_in" and done.room_numbers == "101, 102" and done.checked_in_at
    assert "toast=%21" in post(client, token, f"/bookings/{b.id}/checkin", room_numbers="103").headers["location"]
    assert repo.free_rooms(repo.get_room_type(rt1.id), b.check_in, b.check_out, now_utc()) == 1  # still blocks the room


def test_checkout_blocked_by_balance_unless_paid_or_waived(env):
    repo, app, (p1, _, rt1, _) = env
    b = make(repo, p1, rt1, status="checked_in")  # 4000 due
    client, token = login(app)
    post(client, token, f"/bookings/{b.id}/checkout")
    assert repo.get_booking(b.id).status == "checked_in"
    post(client, token, f"/bookings/{b.id}/checkout", waive="on")  # a waiver needs a note
    assert repo.get_booking(b.id).status == "checked_in"
    post(client, token, f"/bookings/{b.id}/checkout", waive="on", note="Guest unhappy with the pool")
    done = repo.get_booking(b.id)
    assert done.status == "checked_out" and done.checked_out_at
    assert [(c.amount, c.category) for c in repo.list_charges(b.id)] == [(-4000, "discount")]
    assert billing.folio(done, repo.list_charges(b.id), [], p1).balance == 0
    assert ("checked_out", b.id) in SENT


def test_checkout_with_final_payment_inline(env):
    repo, app, (p1, _, rt1, _) = env
    b = make(repo, p1, rt1, status="checked_in")
    repo.add_charge(Charge(b.id, "Laundry", 500, now_utc(), "laundry"))
    client, token = login(app)
    post(client, token, f"/bookings/{b.id}/checkout", pay_method="upi", pay_amount="4500", pay_ref="")
    assert repo.get_booking(b.id).status == "checked_in"  # UPI needs a reference
    post(client, token, f"/bookings/{b.id}/checkout", pay_method="upi", pay_amount="4500", pay_ref="UTR777")
    done = repo.get_booking(b.id)
    assert done.status == "checked_out" and done.paid_amount == 8500 and done.payment_method == "mixed"


def test_checkout_sets_id_deletion_date_and_purge(env):
    repo, app, (p1, _, rt1, _) = env
    b = make(repo, p1, rt1, status="checked_in", paid=8000)
    repo.save_id_proof(IdProof(b.id, 1, "aadhaar", now_utc(), last4="1234", mime="image/jpeg", data=b"\xff\xd8x"))
    client, token = login(app)
    post(client, token, f"/bookings/{b.id}/checkout")
    proof = repo.list_id_proofs(b.id)[0]
    assert proof.delete_after == today_ist() + timedelta(days=stay.KEEP_ID_DAYS)
    assert stay.purge_expired_id_proofs(repo, today_ist()) == 0
    assert stay.purge_expired_id_proofs(repo, today_ist() + timedelta(days=stay.KEEP_ID_DAYS + 1)) == 1
    assert repo.list_id_proofs(b.id) == []


# --------------------------------------------------------------------------- ID photos


def test_id_image_route_needs_login_role_and_a_real_image(env):
    repo, app, (p1, p2, rt1, rt2) = env
    b = make(repo, p1, rt1)
    good = repo.save_id_proof(IdProof(b.id, 1, "aadhaar", now_utc(), last4="4321", mime="image/png", data=b"\x89PNG\r\n"))
    evil = repo.save_id_proof(IdProof(b.id, 2, "other", now_utc(), mime="text/html", data=b"<script>alert(1)</script>"))
    url = f"{BASE}/bookings/{b.id}/id/{good.id}/view"
    assert TestClient(app).get(url, follow_redirects=False).status_code == 303  # not logged in
    client, token = login(app)
    resp = client.get(url)
    assert resp.status_code == 200 and resp.content == b"\x89PNG\r\n" and resp.headers["content-type"] == "image/png"
    assert resp.headers["cache-control"] == "no-store" and resp.headers["x-content-type-options"] == "nosniff"
    assert client.get(f"{BASE}/bookings/{b.id}/id/{evil.id}/view").status_code == 415  # never served as a page
    assert client.get(f"{BASE}/bookings/{b.id}/id/ID-NOPE00/view").status_code == 404
    mgr, _ = login(app, "mgr@x.com", "manager-pass-1")  # manager of the other property
    assert mgr.get(url).status_code == 404
    page = client.get(f"{BASE}/bookings/{b.id}").text
    assert "ending 4321" in page and f"/id/{good.id}/view" in page and f"/id/{evil.id}/view" in page


def test_verify_and_reject_id(env):
    repo, app, (p1, _, rt1, _) = env
    b = make(repo, p1, rt1)
    proof = repo.save_id_proof(IdProof(b.id, 1, "passport", now_utc(), mime="image/jpeg", data=b"\xff\xd8x"))
    client, token = login(app)
    post(client, token, f"/bookings/{b.id}/id/{proof.id}/status", status="verified")
    assert repo.get_id_proof(proof.id).status == "verified" and repo.get_id_proof(proof.id).verified_by == "owner@x.com"
    post(client, token, f"/bookings/{b.id}/id/{proof.id}/status", status="rejected")
    assert repo.get_id_proof(proof.id).status == "rejected"
    assert client.post(f"{BASE}/bookings/{b.id}/id/{proof.id}/status", data={"status": "verified"}).status_code == 403  # CSRF


# --------------------------------------------------------------------------- pages


def test_stay_page_and_nav(env):
    repo, app, (p1, _, rt1, _) = env
    arriving = make(repo, p1, rt1, day=0)
    inhouse = make(repo, p1, rt1, status="checked_in", day=-1, paid=8000)
    client, _ = login(app)
    html = client.get(f"{BASE}/stay").text
    assert arriving.id in html and inhouse.id in html and "?open=checkin" in html and "?open=checkout" in html
    assert "Reports" in client.get(f"{BASE}/").text and "Arrivals" in client.get(f"{BASE}/").text


def test_property_form_saves_gst_prefix_and_policy(env):
    repo, app, (p1, _, _, _) = env
    client, token = login(app)
    post(client, token, f"/properties/{p1.id}", name="Pozhiyoram Resort", gst_pct="12", advance_pct="50", gstin="32aaaaa0000a1z5",
         invoice_prefix="pz-r!", cp_days_1="14", cp_pct_1="100", cp_days_2="2", cp_pct_2="25", cp_days_3="", cp_pct_3="", active="on")
    saved = repo.get_property(p1.id)
    assert saved.gstin == "32AAAAA0000A1Z5" and saved.invoice_prefix == "PZR"
    assert saved.cancel_policy == [{"days_before": 14, "refund_pct": 100}, {"days_before": 2, "refund_pct": 25}]
