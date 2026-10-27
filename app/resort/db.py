"""ResortRepo: SQLite storage for the resort bot and its dashboard.

One class, plain SQL. A hosted Postgres (Supabase free tier) can replace it later by implementing
the same public methods. `create_hold` is the only place a booking is created: it checks the free
rooms and inserts inside one BEGIN IMMEDIATE transaction, so two guests can't get the same room.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from app.resort.availability import free_units
from app.resort.models import (BLOCKING, Booking, Charge, ChatMessage, DashboardUser, Guest, IdProof, Property,
                               RateRule, Refund, RoomType)


class SoldOut(Exception):
    """Not enough free rooms any more (someone else took them)."""


SCHEMA = """
CREATE TABLE IF NOT EXISTS properties (id TEXT PRIMARY KEY, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS room_types (id TEXT PRIMARY KEY, property_id TEXT NOT NULL, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS rate_rules (id TEXT PRIMARY KEY, room_type_id TEXT NOT NULL,
    start TEXT NOT NULL, end TEXT NOT NULL, rate INTEGER NOT NULL, label TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS bookings (
    id TEXT PRIMARY KEY, property_id TEXT NOT NULL, room_type_id TEXT NOT NULL, guest_phone TEXT NOT NULL,
    guest_name TEXT NOT NULL DEFAULT '', language TEXT NOT NULL DEFAULT 'en',
    check_in TEXT NOT NULL, check_out TEXT NOT NULL, adults INTEGER NOT NULL, children INTEGER NOT NULL,
    rooms INTEGER NOT NULL, total INTEGER NOT NULL, advance INTEGER NOT NULL, status TEXT NOT NULL,
    created_at TEXT NOT NULL, hold_expires_at TEXT, paid_at TEXT,
    payment_link_id TEXT NOT NULL DEFAULT '', payment_link_url TEXT NOT NULL DEFAULT '',
    razorpay_payment_id TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT '',
    payment_method TEXT NOT NULL DEFAULT '', payment_ref TEXT NOT NULL DEFAULT '', amount_paid INTEGER NOT NULL DEFAULT 0,
    payment_note TEXT NOT NULL DEFAULT '', received_by TEXT NOT NULL DEFAULT '',
    checked_in_at TEXT, checked_out_at TEXT, room_numbers TEXT NOT NULL DEFAULT '', cancel_reason TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS bookings_room ON bookings(room_type_id, status);
CREATE INDEX IF NOT EXISTS bookings_phone ON bookings(guest_phone);
CREATE TABLE IF NOT EXISTS guests (phone TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '',
    language TEXT NOT NULL DEFAULT 'en', offers_consent INTEGER NOT NULL DEFAULT 0,
    first_seen TEXT, last_seen TEXT, notes TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS chat (id INTEGER PRIMARY KEY AUTOINCREMENT, phone TEXT NOT NULL,
    direction TEXT NOT NULL, text TEXT NOT NULL, at TEXT NOT NULL, source TEXT NOT NULL DEFAULT 'text');
CREATE INDEX IF NOT EXISTS chat_phone ON chat(phone, id);
CREATE TABLE IF NOT EXISTS chat_state (phone TEXT PRIMARY KEY, data TEXT NOT NULL, paused INTEGER NOT NULL DEFAULT 0,
    paused_at TEXT);
CREATE TABLE IF NOT EXISTS events (id TEXT PRIMARY KEY, at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS users (email TEXT PRIMARY KEY, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS charges (id TEXT PRIMARY KEY, booking_id TEXT NOT NULL, description TEXT NOT NULL,
    amount INTEGER NOT NULL, at TEXT NOT NULL, category TEXT NOT NULL DEFAULT 'other', added_by TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS refunds (id TEXT PRIMARY KEY, booking_id TEXT NOT NULL, amount INTEGER NOT NULL, at TEXT NOT NULL,
    method TEXT NOT NULL DEFAULT 'cash', ref TEXT NOT NULL DEFAULT '', note TEXT NOT NULL DEFAULT '', by TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS id_proofs (id TEXT PRIMARY KEY, booking_id TEXT NOT NULL, guest_no INTEGER NOT NULL,
    id_type TEXT NOT NULL, received_at TEXT NOT NULL, last4 TEXT NOT NULL DEFAULT '', mime TEXT NOT NULL DEFAULT '',
    data BLOB, status TEXT NOT NULL DEFAULT 'received', verified_by TEXT NOT NULL DEFAULT '', delete_after TEXT);
CREATE INDEX IF NOT EXISTS id_proofs_booking ON id_proofs(booking_id);
"""


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(timezone.utc).isoformat() if value else None


def _booking(row: sqlite3.Row) -> Booking:
    return Booking(
        id=row["id"], property_id=row["property_id"], room_type_id=row["room_type_id"],
        guest_phone=row["guest_phone"], guest_name=row["guest_name"], language=row["language"],
        check_in=date.fromisoformat(row["check_in"]), check_out=date.fromisoformat(row["check_out"]),
        adults=row["adults"], children=row["children"], rooms=row["rooms"], total=row["total"],
        advance=row["advance"], status=row["status"], created_at=_dt(row["created_at"]),  # type: ignore[arg-type]
        hold_expires_at=_dt(row["hold_expires_at"]), paid_at=_dt(row["paid_at"]),
        payment_link_id=row["payment_link_id"], payment_link_url=row["payment_link_url"],
        razorpay_payment_id=row["razorpay_payment_id"], payment_method=row["payment_method"],
        payment_ref=row["payment_ref"], amount_paid=row["amount_paid"], payment_note=row["payment_note"],
        received_by=row["received_by"], checked_in_at=_dt(row["checked_in_at"]),
        checked_out_at=_dt(row["checked_out_at"]), room_numbers=row["room_numbers"],
        cancel_reason=row["cancel_reason"], notes=row["notes"])


def hash_password(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(8)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 120_000).hex()
    return f"{salt}${digest}"


def check_password(password: str, stored: str) -> bool:
    salt, _, _ = stored.partition("$")
    return hmac.compare_digest(hash_password(password, salt), stored)


class ResortRepo:
    def __init__(self, path: Path | str) -> None:
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(SCHEMA)
        have = {r["name"] for r in self._db.execute("PRAGMA table_info(bookings)")}
        for col, ddl in (("payment_method", "TEXT NOT NULL DEFAULT ''"), ("payment_ref", "TEXT NOT NULL DEFAULT ''"),
                         ("amount_paid", "INTEGER NOT NULL DEFAULT 0"), ("payment_note", "TEXT NOT NULL DEFAULT ''"),
                         ("received_by", "TEXT NOT NULL DEFAULT ''"), ("checked_in_at", "TEXT"), ("checked_out_at", "TEXT"),
                         ("room_numbers", "TEXT NOT NULL DEFAULT ''"), ("cancel_reason", "TEXT NOT NULL DEFAULT ''")):
            if col not in have:  # databases created before payment details were stored
                self._db.execute(f"ALTER TABLE bookings ADD COLUMN {col} {ddl}")

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield self._db
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            else:
                self._db.execute("COMMIT")

    def _all(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, args).fetchall()

    def _one(self, sql: str, args: tuple = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._db.execute(sql, args).fetchone()

    def _run(self, sql: str, args: tuple = ()) -> None:
        with self._lock:
            self._db.execute(sql, args)

    # ------------------------------------------------------------------ properties

    def save_property(self, p: Property) -> Property:
        self._run("INSERT INTO properties(id, data) VALUES(?, ?) ON CONFLICT(id) DO UPDATE SET data=excluded.data",
                  (p.id, json.dumps(p.__dict__, ensure_ascii=False)))
        return p

    def get_property(self, property_id: str) -> Property | None:
        row = self._one("SELECT data FROM properties WHERE id=?", (property_id,))
        return Property(**json.loads(row["data"])) if row else None

    def list_properties(self, active_only: bool = True) -> list[Property]:
        props = [Property(**json.loads(r["data"])) for r in self._all("SELECT data FROM properties ORDER BY rowid")]
        return [p for p in props if p.active or not active_only]

    def delete_property(self, property_id: str) -> None:
        for rt in self.list_room_types(property_id, active_only=False):
            self.delete_room_type(rt.id)
        self._run("DELETE FROM properties WHERE id=?", (property_id,))

    # ------------------------------------------------------------------ room types and rates

    def save_room_type(self, rt: RoomType) -> RoomType:
        self._run("INSERT INTO room_types(id, property_id, data) VALUES(?, ?, ?) "
                  "ON CONFLICT(id) DO UPDATE SET property_id=excluded.property_id, data=excluded.data",
                  (rt.id, rt.property_id, json.dumps(rt.__dict__, ensure_ascii=False)))
        return rt

    def get_room_type(self, room_type_id: str) -> RoomType | None:
        row = self._one("SELECT data FROM room_types WHERE id=?", (room_type_id,))
        return RoomType(**json.loads(row["data"])) if row else None

    def list_room_types(self, property_id: str, active_only: bool = True) -> list[RoomType]:
        rows = self._all("SELECT data FROM room_types WHERE property_id=? ORDER BY rowid", (property_id,))
        types = [RoomType(**json.loads(r["data"])) for r in rows]
        return [t for t in types if t.active or not active_only]

    def delete_room_type(self, room_type_id: str) -> None:
        self._run("DELETE FROM rate_rules WHERE room_type_id=?", (room_type_id,))
        self._run("DELETE FROM room_types WHERE id=?", (room_type_id,))

    def save_rate_rule(self, r: RateRule) -> RateRule:
        self._run("INSERT INTO rate_rules(id, room_type_id, start, end, rate, label) VALUES(?,?,?,?,?,?) "
                  "ON CONFLICT(id) DO UPDATE SET start=excluded.start, end=excluded.end, rate=excluded.rate, "
                  "label=excluded.label",
                  (r.id, r.room_type_id, r.start.isoformat(), r.end.isoformat(), r.rate, r.label))
        return r

    def list_rate_rules(self, room_type_id: str) -> list[RateRule]:
        return [RateRule(room_type_id=r["room_type_id"], start=date.fromisoformat(r["start"]),
                         end=date.fromisoformat(r["end"]), rate=r["rate"], label=r["label"], id=r["id"])
                for r in self._all("SELECT * FROM rate_rules WHERE room_type_id=? ORDER BY start", (room_type_id,))]

    def delete_rate_rule(self, rule_id: str) -> None:
        self._run("DELETE FROM rate_rules WHERE id=?", (rule_id,))

    # ------------------------------------------------------------------ bookings

    def blocking_bookings(self, room_type_id: str, check_in: date, check_out: date) -> list[Booking]:
        marks = ",".join("?" for _ in BLOCKING)
        rows = self._all(f"SELECT * FROM bookings WHERE room_type_id=? AND status IN ({marks}) "
                         "AND check_in < ? AND check_out > ?",
                         (room_type_id, *BLOCKING, check_out.isoformat(), check_in.isoformat()))
        return [_booking(r) for r in rows]

    def free_rooms(self, rt: RoomType, check_in: date, check_out: date, now: datetime) -> int:
        return free_units(rt, self.blocking_bookings(rt.id, check_in, check_out), check_in, check_out, now)

    def create_hold(self, b: Booking, now: datetime) -> Booking:
        """Insert the booking as a hold if enough rooms are free; raise SoldOut otherwise."""
        rt = self.get_room_type(b.room_type_id)
        if rt is None:
            raise SoldOut("unknown room type")
        with self._tx() as db:
            marks = ",".join("?" for _ in BLOCKING)
            rows = db.execute(f"SELECT * FROM bookings WHERE room_type_id=? AND status IN ({marks}) "
                              "AND check_in < ? AND check_out > ?",
                              (b.room_type_id, *BLOCKING, b.check_out.isoformat(), b.check_in.isoformat())).fetchall()
            if free_units(rt, [_booking(r) for r in rows], b.check_in, b.check_out, now) < b.rooms:
                raise SoldOut(f"only fewer than {b.rooms} room(s) free")
            self._insert_booking(db, b)
        return b

    def add_manual_booking(self, b: Booking, now: datetime) -> Booking:
        """Dashboard walk-in / phone booking: same room check as the bot."""
        return self.create_hold(b, now)

    def _insert_booking(self, db: sqlite3.Connection, b: Booking) -> None:
        db.execute(
            "INSERT INTO bookings(id, property_id, room_type_id, guest_phone, guest_name, language, check_in, "
            "check_out, adults, children, rooms, total, advance, status, created_at, hold_expires_at, paid_at, "
            "payment_link_id, payment_link_url, razorpay_payment_id, notes, payment_method, payment_ref, amount_paid, "
            "payment_note, received_by, checked_in_at, checked_out_at, room_numbers, cancel_reason) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (b.id, b.property_id, b.room_type_id, b.guest_phone, b.guest_name, b.language, b.check_in.isoformat(),
             b.check_out.isoformat(), b.adults, b.children, b.rooms, b.total, b.advance, b.status,
             _iso(b.created_at), _iso(b.hold_expires_at), _iso(b.paid_at), b.payment_link_id, b.payment_link_url,
             b.razorpay_payment_id, b.notes, b.payment_method, b.payment_ref, b.amount_paid, b.payment_note,
             b.received_by, _iso(b.checked_in_at), _iso(b.checked_out_at), b.room_numbers, b.cancel_reason))

    def save_booking(self, b: Booking) -> Booking:
        """Update an existing booking (status, payment ids, notes...)."""
        self._run(
            "UPDATE bookings SET guest_name=?, language=?, status=?, hold_expires_at=?, paid_at=?, "
            "payment_link_id=?, payment_link_url=?, razorpay_payment_id=?, notes=?, total=?, advance=?, payment_method=?, "
            "payment_ref=?, amount_paid=?, payment_note=?, received_by=?, checked_in_at=?, checked_out_at=?, "
            "room_numbers=?, cancel_reason=? WHERE id=?",
            (b.guest_name, b.language, b.status, _iso(b.hold_expires_at), _iso(b.paid_at), b.payment_link_id,
             b.payment_link_url, b.razorpay_payment_id, b.notes, b.total, b.advance, b.payment_method, b.payment_ref,
             b.amount_paid, b.payment_note, b.received_by, _iso(b.checked_in_at), _iso(b.checked_out_at),
            b.room_numbers, b.cancel_reason, b.id))
        return b

    def get_booking(self, booking_id: str) -> Booking | None:
        row = self._one("SELECT * FROM bookings WHERE id=?", (booking_id,))
        return _booking(row) if row else None

    def find_booking_by_link(self, payment_link_id: str) -> Booking | None:
        row = self._one("SELECT * FROM bookings WHERE payment_link_id=? AND payment_link_id<>''", (payment_link_id,))
        return _booking(row) if row else None

    def list_bookings(self, *, property_id: str | None = None, status: str | None = None,
                      phone: str | None = None, start: date | None = None, end: date | None = None,
                      limit: int = 500) -> list[Booking]:
        """Newest first. start/end keep bookings whose stay overlaps [start, end)."""
        where, args = ["1=1"], []
        for col, val in (("property_id", property_id), ("status", status), ("guest_phone", phone)):
            if val:
                where.append(f"{col}=?")
                args.append(val)
        if start:
            where.append("check_out > ?")
            args.append(start.isoformat())
        if end:
            where.append("check_in < ?")
            args.append(end.isoformat())
        rows = self._all(f"SELECT * FROM bookings WHERE {' AND '.join(where)} ORDER BY created_at DESC LIMIT ?",
                         (*args, limit))
        return [_booking(r) for r in rows]

    def expire_holds(self, now: datetime) -> list[Booking]:
        """Mark unpaid holds past their time as expired; returns them so the bot can tell the guest."""
        rows = self._all("SELECT * FROM bookings WHERE status='hold' AND hold_expires_at IS NOT NULL "
                         "AND hold_expires_at <= ?", (_iso(now),))
        expired = [_booking(r) for r in rows]
        for b in expired:
            self._run("UPDATE bookings SET status='expired' WHERE id=? AND status='hold'", (b.id,))
            b.status = "expired"
        return expired

    # ------------------------------------------------------------------ guests

    def upsert_guest(self, g: Guest) -> Guest:
        self._run("INSERT INTO guests(phone, name, language, offers_consent, first_seen, last_seen, notes) "
                  "VALUES(?,?,?,?,?,?,?) ON CONFLICT(phone) DO UPDATE SET name=excluded.name, "
                  "language=excluded.language, offers_consent=excluded.offers_consent, "
                  "last_seen=excluded.last_seen, notes=excluded.notes",
                  (g.phone, g.name, g.language, int(g.offers_consent), _iso(g.first_seen), _iso(g.last_seen), g.notes))
        return g

    @staticmethod
    def _guest(r: sqlite3.Row) -> Guest:
        return Guest(r["phone"], r["name"], r["language"], bool(r["offers_consent"]), _dt(r["first_seen"]),
                     _dt(r["last_seen"]), r["notes"])

    def get_guest(self, phone: str) -> Guest | None:
        row = self._one("SELECT * FROM guests WHERE phone=?", (phone,))
        return self._guest(row) if row else None

    def touch_guest(self, phone: str, now: datetime, *, name: str = "", language: str | None = None) -> Guest:
        g = self.get_guest(phone) or Guest(phone, first_seen=now)
        g.last_seen = now
        if name and not g.name:
            g.name = name
        if language:
            g.language = language
        return self.upsert_guest(g)

    def list_guests(self, limit: int = 500) -> list[Guest]:
        return [self._guest(r) for r in self._all("SELECT * FROM guests ORDER BY last_seen DESC LIMIT ?", (limit,))]

    # ------------------------------------------------------------------ chat

    def add_chat(self, m: ChatMessage) -> None:
        self._run("INSERT INTO chat(phone, direction, text, at, source) VALUES(?,?,?,?,?)",
                  (m.phone, m.direction, m.text, _iso(m.at), m.source))

    def list_chat(self, phone: str, limit: int = 100) -> list[ChatMessage]:
        rows = self._all("SELECT * FROM (SELECT * FROM chat WHERE phone=? ORDER BY id DESC LIMIT ?) ORDER BY id",
                         (phone, limit))
        return [ChatMessage(r["phone"], r["direction"], r["text"], _dt(r["at"]), r["source"]) for r in rows]  # type: ignore[arg-type]

    def list_conversations(self, limit: int = 200) -> list[dict[str, Any]]:
        """One row per guest: last message, newest first (for the dashboard inbox)."""
        rows = self._all(
            "SELECT c.phone, c.text, c.at, c.direction, COALESCE(s.paused, 0) AS paused, g.name AS name "
            "FROM chat c JOIN (SELECT phone, MAX(id) AS mid FROM chat GROUP BY phone) m ON c.id = m.mid "
            "LEFT JOIN chat_state s ON s.phone = c.phone LEFT JOIN guests g ON g.phone = c.phone "
            "ORDER BY c.id DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]

    def get_state(self, phone: str) -> dict[str, Any]:
        row = self._one("SELECT data FROM chat_state WHERE phone=?", (phone,))
        return json.loads(row["data"]) if row else {}

    def set_state(self, phone: str, data: dict[str, Any]) -> None:
        self._run("INSERT INTO chat_state(phone, data) VALUES(?, ?) ON CONFLICT(phone) DO UPDATE SET data=excluded.data",
                  (phone, json.dumps(data, ensure_ascii=False)))

    def set_paused(self, phone: str, paused: bool, now: datetime | None = None) -> None:
        self._run("INSERT INTO chat_state(phone, data, paused, paused_at) VALUES(?, '{}', ?, ?) "
                  "ON CONFLICT(phone) DO UPDATE SET paused=excluded.paused, paused_at=excluded.paused_at",
                  (phone, int(paused), _iso(now) if paused else None))

    def is_paused(self, phone: str) -> tuple[bool, datetime | None]:
        row = self._one("SELECT paused, paused_at FROM chat_state WHERE phone=?", (phone,))
        return (bool(row["paused"]), _dt(row["paused_at"])) if row else (False, None)

    def mark_event_processed(self, event_id: str, now: datetime) -> bool:
        """True the first time an id is seen (webhook dedupe)."""
        with self._lock:
            cur = self._db.execute("INSERT OR IGNORE INTO events(id, at) VALUES(?, ?)", (event_id, _iso(now)))
            return cur.rowcount == 1

    # ------------------------------------------------------------------ dashboard users

    def save_user(self, u: DashboardUser) -> None:
        self._run("INSERT INTO users(email, data) VALUES(?, ?) ON CONFLICT(email) DO UPDATE SET data=excluded.data",
                  (u.email.lower(), json.dumps(u.__dict__, ensure_ascii=False)))

    def get_user(self, email: str) -> DashboardUser | None:
        row = self._one("SELECT data FROM users WHERE email=?", (email.lower(),))
        return DashboardUser(**json.loads(row["data"])) if row else None

    def list_users(self) -> list[DashboardUser]:
        return [DashboardUser(**json.loads(r["data"])) for r in self._all("SELECT data FROM users ORDER BY email")]

    def delete_user(self, email: str) -> None:
        self._run("DELETE FROM users WHERE email=?", (email.lower(),))

    # ------------------------------------------------------------------ folio, refunds, ID proofs

    def add_charge(self, c: Charge) -> Charge:
        self._run("INSERT INTO charges(id, booking_id, description, amount, at, category, added_by) VALUES(?,?,?,?,?,?,?)",
                  (c.id, c.booking_id, c.description, c.amount, _iso(c.at), c.category, c.added_by))
        return c

    def list_charges(self, booking_id: str) -> list[Charge]:
        return [Charge(r["booking_id"], r["description"], r["amount"], _dt(r["at"]), r["category"],  # type: ignore[arg-type]
                       r["added_by"], r["id"])
                for r in self._all("SELECT * FROM charges WHERE booking_id=? ORDER BY at", (booking_id,))]

    def delete_charge(self, charge_id: str) -> None:
        self._run("DELETE FROM charges WHERE id=?", (charge_id,))

    def add_refund(self, r: Refund) -> Refund:
        self._run("INSERT INTO refunds(id, booking_id, amount, at, method, ref, note, by) VALUES(?,?,?,?,?,?,?,?)",
                  (r.id, r.booking_id, r.amount, _iso(r.at), r.method, r.ref, r.note, r.by))
        return r

    def list_refunds(self, booking_id: str | None = None) -> list[Refund]:
        rows = (self._all("SELECT * FROM refunds WHERE booking_id=? ORDER BY at", (booking_id,)) if booking_id
                else self._all("SELECT * FROM refunds ORDER BY at"))
        return [Refund(r["booking_id"], r["amount"], _dt(r["at"]), r["method"], r["ref"], r["note"],  # type: ignore[arg-type]
                       r["by"], r["id"]) for r in rows]

    def save_id_proof(self, p: IdProof) -> IdProof:
        self._run("INSERT INTO id_proofs(id, booking_id, guest_no, id_type, received_at, last4, mime, data, status, "
                  "verified_by, delete_after) VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                  "id_type=excluded.id_type, last4=excluded.last4, mime=excluded.mime, data=excluded.data, "
                  "status=excluded.status, verified_by=excluded.verified_by, delete_after=excluded.delete_after",
                  (p.id, p.booking_id, p.guest_no, p.id_type, _iso(p.received_at), p.last4, p.mime, p.data, p.status,
                   p.verified_by, p.delete_after.isoformat() if p.delete_after else None))
        return p

    @staticmethod
    def _id_proof(r: sqlite3.Row, with_data: bool) -> IdProof:
        return IdProof(r["booking_id"], r["guest_no"], r["id_type"], _dt(r["received_at"]),  # type: ignore[arg-type]
                       r["last4"], r["mime"], (r["data"] or b"") if with_data else b"", r["status"], r["verified_by"],
                       date.fromisoformat(r["delete_after"]) if r["delete_after"] else None, r["id"])

    def list_id_proofs(self, booking_id: str, with_data: bool = False) -> list[IdProof]:
        return [self._id_proof(r, with_data)
                for r in self._all("SELECT * FROM id_proofs WHERE booking_id=? ORDER BY guest_no, received_at",
                                   (booking_id,))]

    def get_id_proof(self, proof_id: str) -> IdProof | None:
        row = self._one("SELECT * FROM id_proofs WHERE id=?", (proof_id,))
        return self._id_proof(row, True) if row else None

    def delete_expired_id_proofs(self, today: date) -> int:
        """Privacy clean-up: drop ID photos whose delete_after date has passed. Returns how many."""
        with self._lock:
            cur = self._db.execute("DELETE FROM id_proofs WHERE delete_after IS NOT NULL AND delete_after < ?",
                                   (today.isoformat(),))
            return cur.rowcount
