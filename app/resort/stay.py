"""Check-in and check-out, ID proof handling and the privacy clean-up.

Statuses: paid -> checked_in -> checked_out. Both checked_* statuses keep blocking the rooms (db.BLOCKING).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from app.core.clock import IST
from app.resort.billing import IN_HOUSE_STATES, Folio
from app.resort.db import ResortRepo
from app.resort.models import Booking, Charge, IdProof

ID_TYPES = {"aadhaar": "Aadhaar", "passport": "Passport", "driving_licence": "Driving licence",
            "voter_id": "Voter ID", "other": "Other ID"}
IMAGE_TYPES = ("image/jpeg", "image/png", "image/webp")
KEEP_ID_DAYS = 30  # ID photos are deleted this many days after check-out


class StayError(ValueError):
    """The action can't be done; the text is shown to staff."""


def id_summary(proofs: list[IdProof]) -> str:
    """none | pending (promised for check-in) | received | verified, for the chips on lists."""
    real = [p for p in proofs if p.status != "rejected"]
    if not real:
        return "none"
    if all(p.status == "verified" for p in real):
        return "verified"
    if any(p.status == "received" for p in real):
        return "received"
    return "pending"


def check_in_warnings(b: Booking, f: Folio, proofs: list[IdProof]) -> list[str]:
    out = []
    if id_summary(proofs) in ("none", "pending"):
        out.append("No ID proof received yet. Ask the guest to show one, then use 'ID shown in person'.")
    elif id_summary(proofs) == "received":
        out.append("ID proof received but not verified yet.")
    if f.balance > 0:
        out.append(f"₹{f.balance:,} is still to be paid.")
    return out


def check_in(repo: ResortRepo, b: Booking, *, room_numbers: str, by: str, now: datetime) -> None:
    if b.status != "paid":
        raise StayError("Only a paid booking can be checked in" if b.status != "checked_in" else "Already checked in")
    b.status, b.checked_in_at = "checked_in", now
    b.room_numbers = ", ".join(p.strip() for p in room_numbers.replace("\n", ",").split(",") if p.strip())
    stamp = now.astimezone(IST).strftime("%d %b %H:%M")
    b.notes = (b.notes + f"\n[{stamp}] Checked in by {by}" + (f", rooms {b.room_numbers}" if b.room_numbers else "")).strip()
    repo.save_booking(b)


def check_out(repo: ResortRepo, b: Booking, f: Folio, *, waive: bool, note: str, by: str, now: datetime) -> None:
    """f is the folio AFTER any final payment was recorded. A balance blocks check-out unless waived."""
    if b.status != "checked_in":
        raise StayError("Only a checked-in guest can be checked out" if b.status != "checked_out" else "Already checked out")
    if f.balance > 0:
        if not waive:
            raise StayError(f"₹{f.balance:,} is still to be paid. Record the payment, or tick 'Waive the balance'.")
        if not note.strip():
            raise StayError("Write a note for the waived balance")
        repo.add_charge(Charge(b.id, f"Balance waived: {note.strip()}", -f.balance, now, "discount", by))
    b.status, b.checked_out_at = "checked_out", now
    stamp = now.astimezone(IST).strftime("%d %b %H:%M")
    b.notes = (b.notes + f"\n[{stamp}] Checked out by {by}").strip()
    repo.save_booking(b)
    # Photos of the IDs are deleted a month after the stay.
    due = (now.astimezone(IST) + timedelta(days=KEEP_ID_DAYS)).date()
    for p in repo.list_id_proofs(b.id, with_data=True):
        p.delete_after = due
        repo.save_id_proof(p)


def mark_shown_in_person(repo: ResortRepo, b: Booking, *, id_type: str, by: str, now: datetime) -> IdProof:
    if id_type not in ID_TYPES:
        raise StayError("Choose the type of ID that was shown")
    pending = [p for p in repo.list_id_proofs(b.id, with_data=True) if p.status in ("pending", "received") and not p.data]
    proof = pending[0] if pending else IdProof(b.id, len(repo.list_id_proofs(b.id)) + 1, id_type, now)
    proof.id_type, proof.status, proof.verified_by = id_type, "verified", by
    return repo.save_id_proof(proof)


def set_proof_status(repo: ResortRepo, proof_id: str, status: str, by: str) -> IdProof:
    if status not in ("verified", "rejected"):
        raise StayError("Unknown status")
    proof = repo.get_id_proof(proof_id)
    if proof is None:
        raise StayError("ID proof not found")
    proof.status, proof.verified_by = status, by
    return repo.save_id_proof(proof)


def purge_expired_id_proofs(repo: ResortRepo, today: date | None = None) -> int:
    """Delete ID photos past their delete_after date. The bot's sweeper calls this about once a day."""
    return repo.delete_expired_id_proofs(today or datetime.now(IST).date())


# --------------------------------------------------------------------------- lists for the stay page


def arrivals(bookings: list[Booking], day: date) -> list[Booking]:
    return sorted((b for b in bookings if b.status == "paid" and b.check_in == day), key=lambda b: b.guest_name)


def in_house(bookings: list[Booking]) -> list[Booking]:
    return sorted((b for b in bookings if b.status == "checked_in"), key=lambda b: b.check_out)


def departures(bookings: list[Booking], day: date) -> list[Booking]:
    """Checked-in guests leaving today, plus anyone who should have left already."""
    return sorted((b for b in bookings if b.status == "checked_in" and b.check_out <= day), key=lambda b: b.check_out)


def overdue_arrivals(bookings: list[Booking], day: date) -> list[Booking]:
    """Paid but never checked in, and the stay has already started (a no-show or a missed check-in)."""
    return sorted((b for b in bookings if b.status in IN_HOUSE_STATES and b.status == "paid" and b.check_in < day <= b.check_out),
                  key=lambda b: b.check_in)
