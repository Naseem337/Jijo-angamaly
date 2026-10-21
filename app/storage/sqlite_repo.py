"""SQLite implementation of the storage Repository protocol."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import RLock

from app.storage.models import (
    AlertLog,
    AlertStatus,
    ConsentSource,
    Customer,
    DeletionReport,
    Enquiry,
    EnquiryStatus,
    Lead,
    LeadRequirement,
    LeadStatus,
    Message,
    PendingAction,
    Requirement,
    RequirementStatus,
)
from app.storage.repository import NotFound


_SCHEMA = """
CREATE TABLE IF NOT EXISTS customers (
    client_id TEXT NOT NULL,
    phone TEXT NOT NULL,
    created_at TEXT NOT NULL,
    name TEXT,
    language TEXT NOT NULL,
    consent INTEGER NOT NULL,
    consent_at TEXT,
    consent_source TEXT,
    consent_recorded_by TEXT,
    opted_out_at TEXT,
    bot_paused INTEGER NOT NULL,
    paused_at TEXT,
    paused_reason TEXT,
    last_inbound_at TEXT,
    last_outbound_at TEXT,
    PRIMARY KEY (client_id, phone)
);
CREATE TABLE IF NOT EXISTS messages (
    client_id TEXT NOT NULL,
    id TEXT NOT NULL,
    phone TEXT NOT NULL,
    direction TEXT NOT NULL,
    text TEXT NOT NULL,
    ts TEXT NOT NULL,
    source TEXT NOT NULL,
    alert_id TEXT,
    meta TEXT NOT NULL,
    PRIMARY KEY (client_id, id)
);
CREATE INDEX IF NOT EXISTS messages_by_customer
    ON messages (client_id, phone, ts DESC);
CREATE TABLE IF NOT EXISTS pending_actions (
    client_id TEXT NOT NULL,
    phone TEXT NOT NULL,
    tool TEXT NOT NULL,
    args TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (client_id, phone)
);
CREATE TABLE IF NOT EXISTS enquiries (
    client_id TEXT NOT NULL,
    id TEXT NOT NULL,
    phone TEXT NOT NULL,
    created_at TEXT NOT NULL,
    name TEXT,
    interest TEXT,
    budget INTEGER,
    notes TEXT,
    status TEXT NOT NULL,
    source TEXT NOT NULL,
    updated_at TEXT,
    PRIMARY KEY (client_id, id)
);
CREATE INDEX IF NOT EXISTS enquiries_by_client_date ON enquiries (client_id, created_at DESC);
CREATE TABLE IF NOT EXISTS leads (
    client_id TEXT NOT NULL,
    id TEXT NOT NULL,
    phone TEXT NOT NULL,
    name TEXT,
    source TEXT NOT NULL,
    created_by TEXT NOT NULL,
    consent_at TEXT,
    status TEXT NOT NULL,
    stage TEXT NOT NULL,
    requirement TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT,
    PRIMARY KEY (client_id, id)
);
CREATE INDEX IF NOT EXISTS leads_by_phone ON leads (client_id, phone, created_at DESC);
CREATE TABLE IF NOT EXISTS requirements (
    client_id TEXT NOT NULL,
    id TEXT NOT NULL,
    phone TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    budget_max INTEGER,
    min_year INTEGER,
    make TEXT,
    model TEXT,
    fuel TEXT,
    max_km INTEGER,
    notes TEXT,
    status TEXT NOT NULL,
    source TEXT NOT NULL,
    unanswered_alerts INTEGER NOT NULL,
    still_looking_sent_at TEXT,
    last_near_match_at TEXT,
    updated_at TEXT,
    PRIMARY KEY (client_id, id)
);
CREATE INDEX IF NOT EXISTS requirements_by_client_date
    ON requirements (client_id, created_at DESC);
CREATE TABLE IF NOT EXISTS seen_cars (
    client_id TEXT NOT NULL,
    car_id TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    PRIMARY KEY (client_id, car_id)
);
CREATE TABLE IF NOT EXISTS alerts (
    client_id TEXT NOT NULL,
    phone TEXT NOT NULL,
    car_id TEXT NOT NULL,
    requirement_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL,
    batch_id TEXT,
    wa_message_id TEXT,
    reason TEXT,
    updated_at TEXT,
    PRIMARY KEY (client_id, phone, car_id)
);
CREATE INDEX IF NOT EXISTS alerts_by_client_date ON alerts (client_id, created_at DESC);
CREATE TABLE IF NOT EXISTS counters (
    client_id TEXT NOT NULL,
    name TEXT NOT NULL,
    day TEXT NOT NULL,
    value INTEGER NOT NULL,
    PRIMARY KEY (client_id, name, day)
);
CREATE TABLE IF NOT EXISTS cached_media (
    client_id TEXT NOT NULL,
    key TEXT NOT NULL,
    media_id TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    PRIMARY KEY (client_id, key)
);
CREATE TABLE IF NOT EXISTS processed_events (
    client_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    processed_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    PRIMARY KEY (client_id, event_id)
);
"""


def _dt(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Repository datetimes must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat()


def _read_dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value is not None else None


class SQLiteRepository:
    """Tenant-scoped SQLite storage with atomic alert, counter and dedupe writes."""

    def __init__(self, database: str | Path) -> None:
        self._database = str(database)
        self._lock = RLock()
        self._connection = sqlite3.connect(self._database, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._migrate_old_leads_table()
        self._connection.executescript(_SCHEMA)
        self._connection.commit()

    def _migrate_old_leads_table(self) -> None:
        """Before 2026-10-20 the chat visit/test-drive requests lived in a `leads` table. They are
        enquiries now, and `leads` holds yard-visitor alert leads (staff LEAD command)."""
        columns = {row[1] for row in self._connection.execute("PRAGMA table_info(leads)")}
        exists = self._connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='enquiries'").fetchone()
        if "interest" in columns and not exists:
            self._connection.execute("ALTER TABLE leads RENAME TO enquiries")
            self._connection.execute("DROP INDEX IF EXISTS leads_by_client_date")
            self._connection.commit()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def _execute(self, sql: str, values: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cursor = self._connection.execute(sql, values)
            self._connection.commit()
            return cursor

    def _one(self, sql: str, values: tuple = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._connection.execute(sql, values).fetchone()

    @staticmethod
    def _customer(row: sqlite3.Row) -> Customer:
        return Customer(
            client_id=row["client_id"], phone=row["phone"], created_at=_read_dt(row["created_at"]),
            name=row["name"], language=row["language"], consent=bool(row["consent"]),
            consent_at=_read_dt(row["consent_at"]), consent_source=row["consent_source"],
            consent_recorded_by=row["consent_recorded_by"], opted_out_at=_read_dt(row["opted_out_at"]),
            bot_paused=bool(row["bot_paused"]), paused_at=_read_dt(row["paused_at"]),
            paused_reason=row["paused_reason"], last_inbound_at=_read_dt(row["last_inbound_at"]),
            last_outbound_at=_read_dt(row["last_outbound_at"]),
        )

    @staticmethod
    def _message(row: sqlite3.Row) -> Message:
        return Message(
            client_id=row["client_id"], phone=row["phone"], direction=row["direction"],
            text=row["text"], ts=_read_dt(row["ts"]), source=row["source"], id=row["id"],
            alert_id=row["alert_id"], meta=json.loads(row["meta"]),
        )

    @staticmethod
    def _enquiry(row: sqlite3.Row) -> Enquiry:
        return Enquiry(
            client_id=row["client_id"], phone=row["phone"], created_at=_read_dt(row["created_at"]),
            id=row["id"], name=row["name"], interest=row["interest"], budget=row["budget"],
            notes=row["notes"], status=row["status"], source=row["source"],
            updated_at=_read_dt(row["updated_at"]),
        )

    @staticmethod
    def _requirement(row: sqlite3.Row) -> Requirement:
        return Requirement(
            client_id=row["client_id"], phone=row["phone"], created_at=_read_dt(row["created_at"]),
            expires_at=_read_dt(row["expires_at"]), id=row["id"], budget_max=row["budget_max"],
            min_year=row["min_year"], make=row["make"], model=row["model"], fuel=row["fuel"],
            max_km=row["max_km"], notes=row["notes"], status=row["status"], source=row["source"],
            unanswered_alerts=row["unanswered_alerts"],
            still_looking_sent_at=_read_dt(row["still_looking_sent_at"]),
            last_near_match_at=_read_dt(row["last_near_match_at"]),
            updated_at=_read_dt(row["updated_at"]),
        )

    @staticmethod
    def _alert(row: sqlite3.Row) -> AlertLog:
        return AlertLog(
            client_id=row["client_id"], phone=row["phone"], car_id=row["car_id"],
            requirement_id=row["requirement_id"], kind=row["kind"],
            created_at=_read_dt(row["created_at"]), status=row["status"], batch_id=row["batch_id"],
            wa_message_id=row["wa_message_id"], reason=row["reason"],
            updated_at=_read_dt(row["updated_at"]),
        )

    def get_customer(self, client_id: str, phone: str) -> Customer | None:
        row = self._one("SELECT * FROM customers WHERE client_id=? AND phone=?", (client_id, phone))
        return self._customer(row) if row else None

    def get_or_create_customer(self, client_id: str, phone: str, now: datetime) -> Customer:
        self._execute(
            "INSERT INTO customers (client_id, phone, created_at, language, consent, bot_paused) "
            "VALUES (?, ?, ?, 'en', 0, 0) ON CONFLICT (client_id, phone) DO NOTHING",
            (client_id, phone, _dt(now)),
        )
        return self.get_customer(client_id, phone)

    def save_customer(self, customer: Customer) -> None:
        self._execute(
            """INSERT INTO customers VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (client_id, phone) DO UPDATE SET
              created_at=excluded.created_at, name=excluded.name, language=excluded.language,
              consent=excluded.consent, consent_at=excluded.consent_at,
              consent_source=excluded.consent_source, consent_recorded_by=excluded.consent_recorded_by,
              opted_out_at=excluded.opted_out_at, bot_paused=excluded.bot_paused,
              paused_at=excluded.paused_at, paused_reason=excluded.paused_reason,
              last_inbound_at=excluded.last_inbound_at, last_outbound_at=excluded.last_outbound_at""",
            (customer.client_id, customer.phone, _dt(customer.created_at), customer.name,
             customer.language, int(customer.consent), _dt(customer.consent_at), customer.consent_source,
             customer.consent_recorded_by, _dt(customer.opted_out_at), int(customer.bot_paused),
             _dt(customer.paused_at), customer.paused_reason, _dt(customer.last_inbound_at),
             _dt(customer.last_outbound_at)),
        )

    def list_customers(self, client_id: str, limit: int = 100) -> list[Customer]:
        with self._lock:
            rows = self._connection.execute(
                """SELECT * FROM customers WHERE client_id=?
                ORDER BY MAX(COALESCE(last_inbound_at, created_at),
                             COALESCE(last_outbound_at, created_at)) DESC LIMIT ?""",
                (client_id, limit),
            ).fetchall()
        return [self._customer(row) for row in rows]

    def list_paused_customers(self, client_id: str) -> list[Customer]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM customers WHERE client_id=? AND bot_paused=1 ORDER BY paused_at DESC",
                (client_id,),
            ).fetchall()
        return [self._customer(row) for row in rows]

    def set_consent(self, client_id: str, phone: str, *, source: ConsentSource,
                    recorded_by: str, at: datetime) -> None:
        self.get_or_create_customer(client_id, phone, at)
        self._execute(
            """UPDATE customers SET consent=1, consent_at=?, consent_source=?,
            consent_recorded_by=?, opted_out_at=NULL WHERE client_id=? AND phone=?""",
            (_dt(at), source, recorded_by, client_id, phone),
        )

    def set_opted_out(self, client_id: str, phone: str, at: datetime | None) -> None:
        self._execute(
            "UPDATE customers SET opted_out_at=? WHERE client_id=? AND phone=?",
            (_dt(at), client_id, phone),
        )

    def set_bot_paused(self, client_id: str, phone: str, paused: bool, *,
                       at: datetime, reason: str | None = None) -> None:
        self.get_or_create_customer(client_id, phone, at)
        self._execute(
            "UPDATE customers SET bot_paused=?, paused_at=?, paused_reason=? "
            "WHERE client_id=? AND phone=?",
            (int(paused), _dt(at) if paused else None, reason if paused else None, client_id, phone),
        )

    def add_message(self, message: Message) -> None:
        with self._lock:
            self._connection.execute(
                "INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (message.client_id, message.id, message.phone, message.direction, message.text,
                 _dt(message.ts), message.source, message.alert_id,
                 json.dumps(message.meta, separators=(",", ":"))),
            )
            self._connection.execute(
                """INSERT INTO customers (client_id, phone, created_at, language, consent, bot_paused)
                VALUES (?, ?, ?, 'en', 0, 0) ON CONFLICT (client_id, phone) DO NOTHING""",
                (message.client_id, message.phone, _dt(message.ts)),
            )
            column = "last_inbound_at" if message.direction == "in" else "last_outbound_at"
            self._connection.execute(
                f"UPDATE customers SET {column}=CASE WHEN {column} IS NULL OR {column}<? "
                f"THEN ? ELSE {column} END WHERE client_id=? AND phone=?",
                (_dt(message.ts), _dt(message.ts), message.client_id, message.phone),
            )
            self._connection.commit()

    def list_messages(self, client_id: str, phone: str, limit: int = 50) -> list[Message]:
        with self._lock:
            rows = self._connection.execute(
                """SELECT * FROM (SELECT rowid AS seq, * FROM messages WHERE client_id=? AND phone=?
                ORDER BY ts DESC, seq DESC LIMIT ?) ORDER BY ts ASC, seq ASC""",
                (client_id, phone, limit),
            ).fetchall()
        return [self._message(row) for row in rows]

    def set_pending_action(self, action: PendingAction) -> None:
        self._execute(
            """INSERT INTO pending_actions VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (client_id, phone) DO UPDATE SET
              tool=excluded.tool, args=excluded.args, created_at=excluded.created_at""",
            (action.client_id, action.phone, action.tool,
             json.dumps(action.args, separators=(",", ":")), _dt(action.created_at)),
        )

    def get_pending_action(self, client_id: str, phone: str) -> PendingAction | None:
        row = self._one(
            "SELECT * FROM pending_actions WHERE client_id=? AND phone=?", (client_id, phone)
        )
        return PendingAction(client_id=row["client_id"], phone=row["phone"], tool=row["tool"],
                             args=json.loads(row["args"]), created_at=_read_dt(row["created_at"])) if row else None

    def clear_pending_action(self, client_id: str, phone: str) -> None:
        self._execute("DELETE FROM pending_actions WHERE client_id=? AND phone=?", (client_id, phone))

    def create_enquiry(self, enquiry: Enquiry) -> None:
        self._execute(
            "INSERT INTO enquiries VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (enquiry.client_id, enquiry.id, enquiry.phone, _dt(enquiry.created_at), enquiry.name, enquiry.interest,
             enquiry.budget, enquiry.notes, enquiry.status, enquiry.source, _dt(enquiry.updated_at)),
        )

    def get_enquiry(self, client_id: str, enquiry_id: str) -> Enquiry | None:
        row = self._one("SELECT * FROM enquiries WHERE client_id=? AND id=?", (client_id, enquiry_id))
        return self._enquiry(row) if row else None

    def update_enquiry_status(self, client_id: str, enquiry_id: str, status: EnquiryStatus,
                           at: datetime) -> None:
        cursor = self._execute(
            "UPDATE enquiries SET status=?, updated_at=? WHERE client_id=? AND id=?",
            (status, _dt(at), client_id, enquiry_id),
        )
        if cursor.rowcount == 0:
            raise NotFound(enquiry_id)

    def list_enquiries(self, client_id: str, status: EnquiryStatus | None = None,
                   limit: int = 200) -> list[Enquiry]:
        sql = "SELECT * FROM enquiries WHERE client_id=?"
        values: tuple = (client_id,)
        if status is not None:
            sql += " AND status=?"
            values += (status,)
        sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
        with self._lock:
            rows = self._connection.execute(sql, values + (limit,)).fetchall()
        return [self._enquiry(row) for row in rows]

    # ------------------------------------------------------------------ yard-visitor leads

    @staticmethod
    def _yard_lead(row: sqlite3.Row) -> Lead:
        return Lead(
            client_id=row["client_id"], phone=row["phone"], created_at=_read_dt(row["created_at"]),
            expires_at=_read_dt(row["expires_at"]), created_by=row["created_by"],
            requirement=LeadRequirement.from_dict(json.loads(row["requirement"])), id=row["id"],
            name=row["name"], source=row["source"], consent_at=_read_dt(row["consent_at"]),
            status=row["status"], stage=row["stage"], updated_at=_read_dt(row["updated_at"]),
        )

    @staticmethod
    def _lead_values(lead: Lead) -> tuple:
        return (lead.client_id, lead.id, lead.phone, lead.name, lead.source, lead.created_by,
                _dt(lead.consent_at), lead.status, lead.stage,
                json.dumps(lead.requirement.to_dict(), ensure_ascii=False),
                _dt(lead.expires_at), _dt(lead.created_at), _dt(lead.updated_at))

    def create_lead(self, lead: Lead) -> None:
        self._execute("INSERT INTO leads VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", self._lead_values(lead))

    def get_lead(self, client_id: str, lead_id: str) -> Lead | None:
        row = self._one("SELECT * FROM leads WHERE client_id=? AND id=?", (client_id, lead_id))
        return self._yard_lead(row) if row else None

    def update_lead(self, lead: Lead) -> None:
        values = self._lead_values(lead)
        cursor = self._execute(
            """UPDATE leads SET phone=?, name=?, source=?, created_by=?, consent_at=?, status=?, stage=?,
            requirement=?, expires_at=?, created_at=?, updated_at=? WHERE client_id=? AND id=?""",
            values[2:] + values[:2],
        )
        if cursor.rowcount == 0:
            raise NotFound(lead.id)

    def list_leads(self, client_id: str, status: LeadStatus | None = None, phone: str | None = None,
                   limit: int = 200) -> list[Lead]:
        sql, values = "SELECT * FROM leads WHERE client_id=?", [client_id]
        if status is not None:
            sql += " AND status=?"
            values.append(status)
        if phone is not None:
            sql += " AND phone=?"
            values.append(phone)
        sql += " ORDER BY created_at DESC, rowid DESC LIMIT ?"
        with self._lock:
            rows = self._connection.execute(sql, (*values, limit)).fetchall()
        return [self._yard_lead(row) for row in rows]

    def find_open_lead(self, client_id: str, phone: str) -> Lead | None:
        row = self._one(
            """SELECT * FROM leads WHERE client_id=? AND phone=? AND status IN ('pending_consent', 'active')
            ORDER BY created_at DESC, rowid DESC LIMIT 1""", (client_id, phone))
        return self._yard_lead(row) if row else None

    def set_leads_status(self, client_id: str, phone: str, from_statuses: tuple[LeadStatus, ...],
                         to_status: LeadStatus, at: datetime, consent_at: datetime | None = None) -> int:
        if not from_statuses:
            return 0
        marks = ", ".join("?" for _ in from_statuses)
        sql = f"UPDATE leads SET status=?, updated_at=?{', consent_at=?' if consent_at else ''} " \
              f"WHERE client_id=? AND phone=? AND status IN ({marks})"
        values = (to_status, _dt(at), *((_dt(consent_at),) if consent_at else ()), client_id, phone, *from_statuses)
        return self._execute(sql, values).rowcount

    def expire_leads(self, client_id: str, now: datetime) -> int:
        return self._execute(
            """UPDATE leads SET status='expired', updated_at=? WHERE client_id=? AND expires_at<=?
            AND status IN ('pending_consent', 'active')""", (_dt(now), client_id, _dt(now))).rowcount

    def create_requirement(self, req: Requirement) -> None:
        self._execute(
            "INSERT INTO requirements VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (req.client_id, req.id, req.phone, _dt(req.created_at), _dt(req.expires_at),
             req.budget_max, req.min_year, req.make, req.model, req.fuel, req.max_km, req.notes,
             req.status, req.source, req.unanswered_alerts, _dt(req.still_looking_sent_at),
             _dt(req.last_near_match_at), _dt(req.updated_at)),
        )

    def get_requirement(self, client_id: str, req_id: str) -> Requirement | None:
        row = self._one("SELECT * FROM requirements WHERE client_id=? AND id=?", (client_id, req_id))
        return self._requirement(row) if row else None

    def save_requirement(self, req: Requirement) -> None:
        cursor = self._execute(
            """UPDATE requirements SET phone=?, created_at=?, expires_at=?, budget_max=?, min_year=?,
            make=?, model=?, fuel=?, max_km=?, notes=?, status=?, source=?, unanswered_alerts=?,
            still_looking_sent_at=?, last_near_match_at=?, updated_at=? WHERE client_id=? AND id=?""",
            (req.phone, _dt(req.created_at), _dt(req.expires_at), req.budget_max, req.min_year,
             req.make, req.model, req.fuel, req.max_km, req.notes, req.status, req.source,
             req.unanswered_alerts, _dt(req.still_looking_sent_at), _dt(req.last_near_match_at),
             _dt(req.updated_at), req.client_id, req.id),
        )
        if cursor.rowcount == 0:
            raise NotFound(req.id)

    def list_requirements(self, client_id: str, status: RequirementStatus | None = None,
                          phone: str | None = None) -> list[Requirement]:
        sql = "SELECT * FROM requirements WHERE client_id=?"
        values: tuple = (client_id,)
        if status is not None:
            sql += " AND status=?"
            values += (status,)
        if phone is not None:
            sql += " AND phone=?"
            values += (phone,)
        sql += " ORDER BY created_at DESC, id DESC"
        with self._lock:
            rows = self._connection.execute(sql, values).fetchall()
        return [self._requirement(row) for row in rows]

    def reset_unanswered(self, client_id: str, phone: str, at: datetime) -> int:
        return self._execute(
            "UPDATE requirements SET unanswered_alerts=0, updated_at=? "
            "WHERE client_id=? AND phone=? AND status='active' AND unanswered_alerts<>0",
            (_dt(at), client_id, phone),
        ).rowcount

    def get_seen_car_ids(self, client_id: str) -> set[str]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT car_id FROM seen_cars WHERE client_id=?", (client_id,)
            ).fetchall()
        return {row["car_id"] for row in rows}

    def get_seen_cars(self, client_id: str) -> dict[str, datetime]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT car_id, first_seen_at FROM seen_cars WHERE client_id=?", (client_id,)
            ).fetchall()
        return {row["car_id"]: _read_dt(row["first_seen_at"]) for row in rows}

    def mark_cars_seen(self, client_id: str, car_ids: list[str], at: datetime) -> None:
        with self._lock:
            self._connection.executemany(
                "INSERT INTO seen_cars VALUES (?, ?, ?) ON CONFLICT (client_id, car_id) DO NOTHING",
                [(client_id, car_id, _dt(at)) for car_id in car_ids],
            )
            self._connection.commit()

    def claim_alert(self, alert: AlertLog) -> bool:
        cursor = self._execute(
            """INSERT INTO alerts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (client_id, phone, car_id) DO UPDATE SET
              requirement_id=excluded.requirement_id, kind=excluded.kind,
              created_at=excluded.created_at, status='pending', batch_id=excluded.batch_id,
              wa_message_id=NULL, reason=NULL, updated_at=excluded.updated_at
            WHERE alerts.status IN ('failed', 'dry_run')""",
            (alert.client_id, alert.phone, alert.car_id, alert.requirement_id, alert.kind,
             _dt(alert.created_at), "pending", alert.batch_id, alert.wa_message_id,
             alert.reason, _dt(alert.updated_at)),
        )
        return cursor.rowcount == 1

    def mark_alert(self, client_id: str, phone: str, car_id: str, status: AlertStatus, *,
                   at: datetime, wa_message_id: str | None = None,
                   reason: str | None = None) -> None:
        cursor = self._execute(
            "UPDATE alerts SET status=?, updated_at=?, wa_message_id=?, reason=? "
            "WHERE client_id=? AND phone=? AND car_id=?",
            (status, _dt(at), wa_message_id, reason, client_id, phone, car_id),
        )
        if cursor.rowcount == 0:
            raise NotFound(car_id)

    def alerted_car_ids(self, client_id: str, phone: str) -> set[str]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT car_id FROM alerts WHERE client_id=? AND phone=? "
                "AND status IN ('pending', 'sent')", (client_id, phone),
            ).fetchall()
        return {row["car_id"] for row in rows}

    def list_alerts(self, client_id: str, *, phone: str | None = None,
                    since: datetime | None = None, limit: int = 200) -> list[AlertLog]:
        sql = "SELECT * FROM alerts WHERE client_id=?"
        values: tuple = (client_id,)
        if phone is not None:
            sql += " AND phone=?"
            values += (phone,)
        if since is not None:
            sql += " AND created_at>=?"
            values += (_dt(since),)
        sql += " ORDER BY created_at DESC, car_id DESC LIMIT ?"
        with self._lock:
            rows = self._connection.execute(sql, values + (limit,)).fetchall()
        return [self._alert(row) for row in rows]

    def get_counter(self, client_id: str, name: str, day: str) -> int:
        row = self._one(
            "SELECT value FROM counters WHERE client_id=? AND name=? AND day=?",
            (client_id, name, day),
        )
        return row["value"] if row else 0

    def increment_counter(self, client_id: str, name: str, day: str, by: int = 1,
                          cap: int | None = None) -> bool:
        if cap is None:
            sql = """INSERT INTO counters VALUES (?, ?, ?, ?) ON CONFLICT (client_id, name, day)
            DO UPDATE SET value=counters.value+excluded.value"""
        else:
            sql = """INSERT INTO counters VALUES (?, ?, ?, ?) ON CONFLICT (client_id, name, day)
            DO UPDATE SET value=counters.value+excluded.value WHERE counters.value+excluded.value<=?"""
        values = (client_id, name, day, by) if cap is None else (client_id, name, day, by, cap)
        return self._execute(sql, values).rowcount == 1

    def get_cached_media(self, client_id: str, key: str, now: datetime) -> str | None:
        row = self._one(
            "SELECT media_id FROM cached_media WHERE client_id=? AND key=? AND expires_at>?",
            (client_id, key, _dt(now)),
        )
        return row["media_id"] if row else None

    def put_cached_media(self, client_id: str, key: str, media_id: str,
                         expires_at: datetime) -> None:
        self._execute(
            """INSERT INTO cached_media VALUES (?, ?, ?, ?) ON CONFLICT (client_id, key)
            DO UPDATE SET media_id=excluded.media_id, expires_at=excluded.expires_at""",
            (client_id, key, media_id, _dt(expires_at)),
        )

    def mark_event_processed(self, client_id: str, event_id: str, now: datetime,
                             ttl_days: int = 7) -> bool:
        expiry = _dt(now + timedelta(days=ttl_days))
        cursor = self._execute(
            """INSERT INTO processed_events VALUES (?, ?, ?, ?)
            ON CONFLICT (client_id, event_id) DO UPDATE SET
              processed_at=excluded.processed_at, expires_at=excluded.expires_at
            WHERE processed_events.expires_at<=excluded.processed_at""",
            (client_id, event_id, _dt(now), expiry),
        )
        return cursor.rowcount == 1

    def delete_customer_data(self, client_id: str, phone: str) -> DeletionReport:
        tables = ("messages", "pending_actions", "enquiries", "leads", "requirements", "alerts", "customers")
        counts: dict[str, int] = {}
        with self._lock:
            for table in tables:
                cursor = self._connection.execute(
                    f"DELETE FROM {table} WHERE client_id=? AND phone=?", (client_id, phone)
                )
                if cursor.rowcount:
                    counts[table] = cursor.rowcount
            self._connection.commit()
        return DeletionReport(counts)

    def purge_old_data(self, client_id: str, cutoff: datetime) -> DeletionReport:
        cutoff_text = _dt(cutoff)
        predicates = {
            "messages": "ts < ?",
            "pending_actions": "created_at < ?",
            "cached_media": "expires_at < ?",
            "processed_events": "expires_at < ?",
        }
        counts: dict[str, int] = {}
        with self._lock:
            for table, predicate in predicates.items():
                cursor = self._connection.execute(
                    f"DELETE FROM {table} WHERE client_id=? AND {predicate}",
                    (client_id, cutoff_text),
                )
                if cursor.rowcount:
                    counts[table] = cursor.rowcount
            self._connection.commit()
        return DeletionReport(counts)