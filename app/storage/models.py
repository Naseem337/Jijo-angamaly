"""Storage records. Plain dataclasses, the same shape for SQLite and DynamoDB.

Conventions:
  - Every record carries client_id (tenant isolation); every lookup is scoped by it.
  - Phones are E.164 ("+919847012345"), already normalised by app.core.phone.
  - Datetimes are timezone-aware UTC. Callers pass them (from a Clock); the
    repository never reads the system time, so tests stay deterministic.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

Direction = Literal["in", "out"]
MessageSource = Literal["text", "voice", "template", "staff", "system"]
ConsentSource = Literal["chat", "voice", "dashboard", "staff_command"]
LeadStatus = Literal["new", "contacted", "closed"]
RequirementStatus = Literal["active", "fulfilled", "expired", "deleted"]
AlertKind = Literal["match", "near", "still_looking"]
AlertStatus = Literal["pending", "sent", "failed", "dry_run", "skipped"]

LEAD_STATUSES: tuple[LeadStatus, ...] = ("new", "contacted", "closed")
# Alert rows in these states may be claimed again by a later run.
RECLAIMABLE_ALERT_STATUSES: frozenset[str] = frozenset({"failed", "dry_run"})


def new_id() -> str:
    return uuid.uuid4().hex


@dataclass
class Customer:
    client_id: str
    phone: str
    created_at: datetime
    name: str | None = None
    language: str = "en"  # "ml" | "manglish" | "en", the language of the last message
    # Consent to receive alert templates. Who/what recorded it is kept for audit.
    consent: bool = False
    consent_at: datetime | None = None
    consent_source: ConsentSource | None = None
    consent_recorded_by: str | None = None  # staff username / staff phone / "customer"
    opted_out_at: datetime | None = None
    bot_paused: bool = False
    paused_at: datetime | None = None
    paused_reason: str | None = None
    last_inbound_at: datetime | None = None
    last_outbound_at: datetime | None = None

    @property
    def opted_out(self) -> bool:
        return self.opted_out_at is not None

    @property
    def can_receive_alerts(self) -> bool:
        return self.consent and not self.opted_out


@dataclass
class Message:
    client_id: str
    phone: str
    direction: Direction
    text: str
    ts: datetime
    source: MessageSource = "text"
    id: str = field(default_factory=new_id)  # WhatsApp message id when we have one
    alert_id: str | None = None  # alert batch this message replied to / belongs to
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class PendingAction:
    """A save_lead / save_requirement held until the customer confirms a voice read-back."""

    client_id: str
    phone: str
    tool: str
    args: dict[str, Any]
    created_at: datetime


@dataclass
class Lead:
    client_id: str
    phone: str
    created_at: datetime
    id: str = field(default_factory=new_id)
    name: str | None = None
    interest: str | None = None
    budget: int | None = None
    notes: str | None = None
    status: LeadStatus = "new"
    source: str = "chat"  # chat | voice | handoff | dashboard
    updated_at: datetime | None = None


@dataclass
class Requirement:
    """A wishlist entry the alert engine matches new cars against."""

    client_id: str
    phone: str
    created_at: datetime
    expires_at: datetime
    id: str = field(default_factory=new_id)
    budget_max: int | None = None
    min_year: int | None = None
    make: str | None = None
    model: str | None = None
    fuel: str | None = None
    max_km: int | None = None
    notes: str | None = None
    status: RequirementStatus = "active"
    source: str = "chat"  # chat | voice | dashboard | staff_command
    unanswered_alerts: int = 0
    still_looking_sent_at: datetime | None = None
    last_near_match_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass
class AlertLog:
    """One car alerted (or about to be) to one customer.

    Unique per (client_id, phone, car_id): the row is claimed as 'pending' *before* the
    WhatsApp send, so a crashed or retried run can never send the same car twice.
    The 'still looking?' nudge uses car_id = f"still_looking:{requirement_id}".
    """

    client_id: str
    phone: str
    car_id: str
    requirement_id: str
    kind: AlertKind
    created_at: datetime
    status: AlertStatus = "pending"
    batch_id: str | None = None  # all cars sent in one template share a batch id
    wa_message_id: str | None = None
    reason: str | None = None
    updated_at: datetime | None = None


@dataclass
class DeletionReport:
    """Rows removed by delete_customer_data / purge_old_data, per record type."""

    counts: dict[str, int] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return sum(self.counts.values())
