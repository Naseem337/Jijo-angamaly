"""The storage interface. Business code depends only on this Protocol.

Implementations: SQLiteRepository (local, tests) and DynamoRepository (AWS, Day 2 evening).
Both must pass tests/storage/test_repository_contract.py.

Atomic operations (the ones that protect against double-sends) are called out
below. Implementations must make them atomic, not read-then-write.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from app.storage.models import (
    AlertLog,
    AlertStatus,
    ConsentSource,
    Customer,
    DeletionReport,
    Lead,
    LeadStatus,
    Message,
    PendingAction,
    Requirement,
    RequirementStatus,
)


class NotFound(KeyError):
    """The requested record does not exist (for this client)."""


class Repository(Protocol):
    # ------------------------------------------------------------------ customers
    def get_customer(self, client_id: str, phone: str) -> Customer | None: ...

    def get_or_create_customer(self, client_id: str, phone: str, now: datetime) -> Customer: ...

    def save_customer(self, customer: Customer) -> None:
        """Insert or fully replace."""
        ...

    def list_customers(self, client_id: str, limit: int = 100) -> list[Customer]:
        """Most recently active first (by last inbound/outbound message)."""
        ...

    def list_paused_customers(self, client_id: str) -> list[Customer]: ...

    def set_consent(self, client_id: str, phone: str, *, source: ConsentSource,
                    recorded_by: str, at: datetime) -> None:
        """Record consent and clear any earlier opt-out. Creates the customer if needed."""
        ...

    def set_opted_out(self, client_id: str, phone: str, at: datetime | None) -> None:
        """at=None means opted back in (START)."""
        ...

    def set_bot_paused(self, client_id: str, phone: str, paused: bool, *,
                       at: datetime, reason: str | None = None) -> None: ...

    # ------------------------------------------------------------------ messages
    def add_message(self, message: Message) -> None:
        """Also updates the customer's last_inbound_at / last_outbound_at."""
        ...

    def list_messages(self, client_id: str, phone: str, limit: int = 50) -> list[Message]:
        """The newest `limit` messages, returned oldest-first (ready for an LLM prompt)."""
        ...

    # ------------------------------------------------------------------ pending (voice confirm)
    def set_pending_action(self, action: PendingAction) -> None:
        """At most one per customer; replaces any previous one."""
        ...

    def get_pending_action(self, client_id: str, phone: str) -> PendingAction | None: ...

    def clear_pending_action(self, client_id: str, phone: str) -> None: ...

    # ------------------------------------------------------------------ leads
    def create_lead(self, lead: Lead) -> None: ...

    def get_lead(self, client_id: str, lead_id: str) -> Lead | None: ...

    def update_lead_status(self, client_id: str, lead_id: str, status: LeadStatus,
                           at: datetime) -> None:
        """Raises NotFound."""
        ...

    def list_leads(self, client_id: str, status: LeadStatus | None = None,
                   limit: int = 200) -> list[Lead]:
        """Newest first."""
        ...

    # ------------------------------------------------------------------ requirements
    def create_requirement(self, req: Requirement) -> None: ...

    def get_requirement(self, client_id: str, req_id: str) -> Requirement | None: ...

    def save_requirement(self, req: Requirement) -> None:
        """Fully replace an existing requirement. Raises NotFound."""
        ...

    def list_requirements(self, client_id: str, status: RequirementStatus | None = None,
                          phone: str | None = None) -> list[Requirement]:
        """Newest first."""
        ...

    def reset_unanswered(self, client_id: str, phone: str, at: datetime) -> int:
        """A customer replied: zero unanswered_alerts on their active requirements.

        Returns how many requirements changed.
        """
        ...

    # ------------------------------------------------------------------ seen cars
    def get_seen_car_ids(self, client_id: str) -> set[str]: ...

    def mark_cars_seen(self, client_id: str, car_ids: list[str], at: datetime) -> None:
        """Idempotent; keeps the original first-seen time."""
        ...

    # ------------------------------------------------------------------ alerts log
    def claim_alert(self, alert: AlertLog) -> bool:
        """ATOMIC. Insert the row as 'pending' before sending.

        Returns False (and changes nothing) if a row for (client_id, phone, car_id)
        already exists, unless that row is 'failed' or 'dry_run', in which case it is
        replaced. A 'pending' row left by a crash is never reclaimed, so we would rather
        miss one alert than send it twice.
        """
        ...

    def mark_alert(self, client_id: str, phone: str, car_id: str, status: AlertStatus, *,
                   at: datetime, wa_message_id: str | None = None,
                   reason: str | None = None) -> None:
        """Raises NotFound."""
        ...

    def alerted_car_ids(self, client_id: str, phone: str) -> set[str]:
        """Cars already sent (or in flight) to this customer, so they are never repeated."""
        ...

    def list_alerts(self, client_id: str, *, phone: str | None = None,
                    since: datetime | None = None, limit: int = 200) -> list[AlertLog]:
        """Newest first."""
        ...

    # ------------------------------------------------------------------ counters / caches
    def get_counter(self, client_id: str, name: str, day: str) -> int: ...

    def increment_counter(self, client_id: str, name: str, day: str, by: int = 1,
                          cap: int | None = None) -> bool:
        """ATOMIC. Add `by` unless that would push the counter above `cap`.

        Returns True if incremented. Used for the daily send cap and TTS character cap.
        `day` is an IST date string ("2026-10-16").
        """
        ...

    def get_cached_media(self, client_id: str, key: str, now: datetime) -> str | None:
        """WhatsApp media id for a cached TTS phrase, if not expired."""
        ...

    def put_cached_media(self, client_id: str, key: str, media_id: str,
                         expires_at: datetime) -> None: ...

    def mark_event_processed(self, client_id: str, event_id: str, now: datetime,
                             ttl_days: int = 7) -> bool:
        """ATOMIC. Webhook dedupe: True the first time an event id is seen, False after."""
        ...

    # ------------------------------------------------------------------ privacy / retention
    def delete_customer_data(self, client_id: str, phone: str) -> DeletionReport:
        """Erase everything we hold about one customer (right-to-erasure request).

        Alert rows are erased too, so if the same person later consents again, old
        cars might be alerted again. That is acceptable after an erasure request.
        """
        ...

    def purge_old_data(self, client_id: str, cutoff: datetime) -> DeletionReport:
        """Retention job: delete messages, pending actions, expired cache entries and
        dedupe events older than `cutoff`. Leads, requirements and the alert log are
        kept, because they are business records, and the alert log is what stops
        repeat alerts.
        """
        ...
