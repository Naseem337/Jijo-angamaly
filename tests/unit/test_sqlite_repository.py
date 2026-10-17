from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.storage.models import AlertLog, Customer, Lead, Message, PendingAction, Requirement
from app.storage.repository import NotFound
from app.storage.sqlite_repo import SQLiteRepository


NOW = datetime(2026, 10, 16, 10, 0, tzinfo=timezone.utc)
PHONE = "+919876543210"


@pytest.fixture
def repository(tmp_path):
    repo = SQLiteRepository(tmp_path / "repository.db")
    yield repo
    repo.close()


def test_customer_consent_pause_and_tenant_isolation(repository):
    customer = repository.get_or_create_customer("one", PHONE, NOW)
    assert customer.client_id == "one"
    assert repository.get_customer("two", PHONE) is None

    repository.set_opted_out("one", PHONE, NOW)
    repository.set_consent("one", PHONE, source="chat", recorded_by="customer", at=NOW)
    repository.set_bot_paused("one", PHONE, True, at=NOW, reason="handoff")

    customer = repository.get_customer("one", PHONE)
    assert customer.can_receive_alerts
    assert customer.opted_out_at is None
    assert repository.list_paused_customers("one") == [customer]


def test_messages_are_scoped_and_return_newest_limit_oldest_first(repository):
    for index in range(3):
        repository.add_message(Message("one", PHONE, "in", str(index), NOW + timedelta(minutes=index)))
    repository.add_message(Message("two", PHONE, "in", "other tenant", NOW))

    messages = repository.list_messages("one", PHONE, limit=2)
    assert [message.text for message in messages] == ["1", "2"]
    assert repository.get_customer("one", PHONE).last_inbound_at == NOW + timedelta(minutes=2)


def test_pending_action_is_replaced_and_cleared(repository):
    first = PendingAction("one", PHONE, "save_lead", {"budget": 100}, NOW)
    second = PendingAction("one", PHONE, "save_requirement", {"budget_max": 200}, NOW)
    repository.set_pending_action(first)
    repository.set_pending_action(second)

    assert repository.get_pending_action("one", PHONE) == second
    repository.clear_pending_action("one", PHONE)
    assert repository.get_pending_action("one", PHONE) is None


def test_leads_and_requirements_round_trip_and_update(repository):
    lead = Lead("one", PHONE, NOW, name="Asha", budget=800000)
    requirement = Requirement("one", PHONE, NOW, NOW + timedelta(days=60), budget_max=800000)
    repository.create_lead(lead)
    repository.create_requirement(requirement)

    repository.update_lead_status("one", lead.id, "contacted", NOW)
    requirement.unanswered_alerts = 2
    repository.save_requirement(requirement)

    assert repository.get_lead("one", lead.id).status == "contacted"
    assert repository.get_requirement("one", requirement.id).unanswered_alerts == 2
    assert repository.reset_unanswered("one", PHONE, NOW) == 1
    assert repository.list_requirements("one", status="active", phone=PHONE)[0].unanswered_alerts == 0
    with pytest.raises(NotFound):
        repository.update_lead_status("one", "missing", "closed", NOW)


def test_seen_cars_and_alert_claims_are_idempotent(repository):
    repository.mark_cars_seen("one", ["car-1", "car-1"], NOW)
    repository.mark_cars_seen("one", ["car-1", "car-2"], NOW + timedelta(days=1))
    assert repository.get_seen_car_ids("one") == {"car-1", "car-2"}

    alert = AlertLog("one", PHONE, "car-1", "req-1", "match", NOW)
    assert repository.claim_alert(alert)
    assert not repository.claim_alert(alert)
    assert repository.alerted_car_ids("one", PHONE) == {"car-1"}
    repository.mark_alert("one", PHONE, "car-1", "failed", at=NOW, reason="retry")
    assert repository.claim_alert(alert)
    repository.mark_alert("one", PHONE, "car-1", "sent", at=NOW, wa_message_id="wamid.1")
    assert not repository.claim_alert(alert)


def test_counters_cache_and_event_dedupe(repository):
    assert repository.get_counter("one", "sends", "2026-10-16") == 0
    assert repository.increment_counter("one", "sends", "2026-10-16", by=2, cap=2)
    assert not repository.increment_counter("one", "sends", "2026-10-16", cap=2)
    assert repository.get_counter("two", "sends", "2026-10-16") == 0

    repository.put_cached_media("one", "phrase", "media-1", NOW + timedelta(days=1))
    assert repository.get_cached_media("one", "phrase", NOW) == "media-1"
    assert repository.get_cached_media("one", "phrase", NOW + timedelta(days=1)) is None

    assert repository.mark_event_processed("one", "event-1", NOW)
    assert not repository.mark_event_processed("one", "event-1", NOW + timedelta(days=1))
    assert repository.mark_event_processed("one", "event-1", NOW + timedelta(days=8))


def test_delete_customer_data_and_purge_old_data(repository):
    repository.add_message(Message("one", PHONE, "in", "old", NOW - timedelta(days=10)))
    repository.add_message(Message("one", PHONE, "in", "new", NOW))
    repository.create_lead(Lead("one", PHONE, NOW))
    repository.set_pending_action(PendingAction("one", PHONE, "save_lead", {}, NOW - timedelta(days=10)))
    repository.put_cached_media("one", "expired", "media", NOW - timedelta(days=5))
    repository.mark_event_processed("one", "old-event", NOW - timedelta(days=10), ttl_days=1)
    repository.create_requirement(Requirement("one", PHONE, NOW, NOW + timedelta(days=60)))

    purged = repository.purge_old_data("one", NOW - timedelta(days=2))
    assert purged.counts == {
        "messages": 1, "pending_actions": 1, "cached_media": 1, "processed_events": 1,
    }
    assert len(repository.list_leads("one")) == 1
    assert repository.get_pending_action("one", PHONE) is None

    deleted = repository.delete_customer_data("one", PHONE)
    assert deleted.total == 4  # 1 message, 1 lead, 1 requirement, 1 customer
    assert repository.get_customer("one", PHONE) is None
    assert repository.list_leads("one") == []
    assert repository.list_requirements("one") == []

def test_messages_with_equal_timestamps_keep_insertion_order(repository):
    for text in ["question", "answer", "follow-up"]:
        repository.add_message(Message("one", PHONE, "in", text, NOW))
    assert [m.text for m in repository.list_messages("one", PHONE)] == ["question", "answer", "follow-up"]
    assert [m.text for m in repository.list_messages("one", PHONE, limit=2)] == ["answer", "follow-up"]
