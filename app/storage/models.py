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
EnquiryStatus = Literal["new", "contacted", "closed"]
RequirementStatus = Literal["active", "fulfilled", "expired", "deleted"]
AlertKind = Literal["match", "near", "still_looking"]
AlertStatus = Literal["pending", "sent", "failed", "dry_run", "skipped"]

ENQUIRY_STATUSES: tuple[EnquiryStatus, ...] = ("new", "contacted", "closed")
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
    """A save_enquiry / save_requirement held until the customer confirms a voice read-back."""

    client_id: str
    phone: str
    tool: str
    args: dict[str, Any]
    created_at: datetime


@dataclass
class Enquiry:
    client_id: str
    phone: str
    created_at: datetime
    id: str = field(default_factory=new_id)
    name: str | None = None
    interest: str | None = None
    budget: int | None = None
    notes: str | None = None
    status: EnquiryStatus = "new"
    source: str = "chat"  # chat | voice | handoff | dashboard
    updated_at: datetime | None = None


LeadStatus = Literal["pending_consent", "active", "notified", "expired", "opted_out"]
LeadStage = Literal["new", "interested", "visit_booked", "sold", "lost"]
LEAD_STATUSES: tuple[LeadStatus, ...] = ("pending_consent", "active", "notified", "expired", "opted_out")
LEAD_STAGES: tuple[LeadStage, ...] = ("new", "interested", "visit_booked", "sold", "lost")
OPEN_LEAD_STATUSES: tuple[LeadStatus, ...] = ("pending_consent", "active")


@dataclass
class LeadRequirement:
    """What a yard visitor is looking for. Every field is optional; at least one must be set."""

    budget_min: int | None = None
    budget_max: int | None = None
    makes: list[str] = field(default_factory=list)
    models: list[str] = field(default_factory=list)  # canonical names from business_rules model_aliases
    body_type: str | None = None  # hatchback | sedan | suv | muv | pickup
    fuel: str | None = None  # petrol | diesel | cng | electric | hybrid
    gearbox: str | None = None  # manual | automatic
    max_km: int | None = None
    max_owners: int | None = None
    min_year: int | None = None
    notes: str | None = None

    def is_empty(self) -> bool:
        """True if there is nothing an alert could match on (notes alone don't count)."""
        return not any([self.budget_min, self.budget_max, self.makes, self.models, self.body_type,
                        self.fuel, self.gearbox, self.max_km, self.max_owners, self.min_year])

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v not in (None, [], "")}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LeadRequirement":
        known = {k: v for k, v in (data or {}).items() if k in cls.__dataclass_fields__}
        return cls(**known)

    def describe(self, language: str = "en") -> str:
        """For the customer, in plain words: 'a Swift, petrol, up to ₹6 lakh, under 10,000 km'."""
        from app.core.money import format_lakh

        def car_name(name: str) -> str:  # 'swift' -> 'Swift', 'xuv700' -> 'XUV700', 'i20' stays
            return " ".join(w.upper() if any(c.isdigit() for c in w) and len(w) > 3 else
                            (w if w[:1].isdigit() or w[:1] == "i" and w[1:].isdigit() else w.capitalize())
                            for w in name.split())

        cars = [car_name(m) for m in self.models] or list(self.makes)
        ml = language in ("ml", "manglish")
        words = {
            "or": " / " if not ml else " അല്ലെങ്കിൽ " if language == "ml" else " allenkil ",
            "fuel": {"petrol": "പെട്രോൾ", "diesel": "ഡീസൽ", "cng": "CNG", "electric": "ഇലക്ട്രിക്",
                     "hybrid": "ഹൈബ്രിഡ്"} if language == "ml" else {},
            "gearbox": {"automatic": "ഓട്ടോമാറ്റിക്", "manual": "മാനുവൽ"} if language == "ml" else {},
        }
        parts: list[str] = []
        if cars:
            parts.append(words["or"].join(cars))
        elif self.body_type:
            parts.append(self.body_type.upper() if self.body_type in ("suv", "muv") else self.body_type)
        if self.fuel:
            parts.append(words["fuel"].get(self.fuel, self.fuel))
        if self.gearbox:
            parts.append(words["gearbox"].get(self.gearbox, self.gearbox))
        low, high = self.budget_min, self.budget_max
        if language == "ml":
            if low and high:
                parts.append(f"{format_lakh(low)} മുതൽ {format_lakh(high)} വരെ")
            elif high:
                parts.append(f"{format_lakh(high)} വരെ")
            if self.max_km:
                parts.append(f"{self.max_km:,} km-ൽ താഴെ")
            if self.min_year:
                parts.append(f"{self.min_year}-നു ശേഷമുള്ള മോഡൽ")
        else:
            if low and high:
                parts.append(f"{format_lakh(low)} to {format_lakh(high)}")
            elif high:
                parts.append(f"up to {format_lakh(high)}" if language == "en" else f"{format_lakh(high)} vare")
            if self.max_km:
                parts.append(f"under {self.max_km:,} km" if language == "en" else f"{self.max_km:,} km-il thazhe")
            if self.min_year:
                parts.append(f"{self.min_year} or newer" if language == "en" else f"{self.min_year}-nu shesham ulla model")
        return ", ".join(parts)

    def summary(self) -> str:
        """One line for staff: 'Swift · petrol · manual · ≤ ₹6 lakh · ≤ 60,000 km · 2018+'."""
        from app.core.money import format_lakh

        parts = [" / ".join(self.models or self.makes)] if (self.models or self.makes) else []
        parts += [p for p in (self.body_type, self.fuel, self.gearbox) if p]
        if self.budget_min and self.budget_max:
            parts.append(f"{format_lakh(self.budget_min)}–{format_lakh(self.budget_max)}")
        elif self.budget_max:
            parts.append(f"≤ {format_lakh(self.budget_max)}")
        elif self.budget_min:
            parts.append(f"≥ {format_lakh(self.budget_min)}")
        if self.max_km:
            parts.append(f"≤ {self.max_km:,} km")
        if self.min_year:
            parts.append(f"{self.min_year}+")
        if self.max_owners:
            parts.append(f"≤ {self.max_owners} owner{'s' if self.max_owners > 1 else ''}")
        if self.notes:
            parts.append(self.notes)
        return " · ".join(parts) or "(no details)"


@dataclass
class Lead:
    """A yard visitor who didn't find their car and wants a WhatsApp alert when one arrives.

    Created by staff with `LEAD <phone> <free text>`; it only becomes `active` (and alertable,
    step 10) after the customer replies YES to the confirmation message.
    """

    client_id: str
    phone: str
    created_at: datetime
    expires_at: datetime
    created_by: str  # staff phone
    requirement: LeadRequirement
    id: str = field(default_factory=new_id)
    name: str | None = None
    source: str = "staff"
    consent_at: datetime | None = None
    status: LeadStatus = "pending_consent"
    stage: LeadStage = "new"
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
