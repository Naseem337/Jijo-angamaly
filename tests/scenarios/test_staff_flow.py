"""Step 9 end to end: staff commands (LEAD / RESUME), lead consent, opt-out/in, handoff, auto-resume."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app.core.client_config import load_client_config
from app.core.clock import FixedClock
from app.main import create_app
from app.providers.llm import LLMError, MockLLM
from app.runtime import Runtime
from app.storage.sqlite_repo import SQLiteRepository
from test_webhook import GRAPH, PHONE_ID, DictSecrets, payload, signed

STAFF = "+919447000000"
CUSTOMER = "+919847012345"
NEW_CUSTOMER = "+919847099999"
LEAD_JSON = '{"models": ["Swift"], "budget_max": "6L", "fuel": "petrol", "max_km": 60000}'


def msg(sender: str, body: str, msg_id: str) -> dict:
    return {"from": sender.lstrip("+"), "id": msg_id, "timestamp": "1792219612", "type": "text", "text": {"body": body}}


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, clients_dir: Path, tmp_path: Path, clock: FixedClock):
    monkeypatch.setenv("TRUECHOICE_WHATSAPP_PHONE_NUMBER_ID", PHONE_ID)
    monkeypatch.setenv("TRUECHOICE_STAFF_NUMBERS", STAFF)
    config = load_client_config("truechoice", clients_dir)
    repo = SQLiteRepository(tmp_path / "staff.db")
    llm = MockLLM()
    runtime = Runtime({"truechoice": config}, repo, DictSecrets(), clock, llm=llm)
    with respx.mock(assert_all_called=False) as graph:
        sent = graph.post(GRAPH).mock(return_value=httpx.Response(200, json={"messages": [{"id": "wamid.out"}]}))
        client = TestClient(create_app(runtime))
        counter = iter(range(1, 10_000))

        def say(sender: str, body: str) -> None:
            data = json.dumps(payload(msg(sender, body, f"wamid.{next(counter)}"))).encode()
            assert client.post("/webhook/truechoice", content=data, headers=signed(data)).status_code == 200

        yield say, llm, repo, sent
    repo.close()


def texts_to(sent, phone: str) -> list[str]:
    bodies = [json.loads(c.request.content) for c in sent.calls]
    return [b["text"]["body"] for b in bodies if b.get("type") == "text" and b["to"] == phone.lstrip("+")]


def test_lead_for_customer_in_24h_window_asks_for_consent(env, clock) -> None:
    say, llm, repo, sent = env
    llm.script.append(MockLLM.text("Hi! How can I help?"))
    say(CUSTOMER, "Hi")  # the customer messaged us → within WhatsApp's 24 h window
    llm.script.append(MockLLM.text(LEAD_JSON))
    say(STAFF, "LEAD 98470 12345 Swift 6L petrol under 60000km")

    [lead] = repo.list_leads("truechoice")
    assert (lead.phone, lead.status, lead.stage, lead.created_by) == (CUSTOMER, "pending_consent", "new", STAFF)
    assert lead.requirement.to_dict() == {"budget_max": 600_000, "models": ["swift"], "fuel": "petrol",
                                          "max_km": 60_000}
    assert (lead.expires_at - lead.created_at).days == 90
    staff_reply = texts_to(sent, STAFF)[-1]
    assert staff_reply.startswith("✅ Saved new lead for +919847012345: swift · petrol · ≤ ₹6 lakh · ≤ 60,000 km.")
    assert "Asked the customer to reply YES" in staff_reply
    consent = texts_to(sent, CUSTOMER)[-1]
    assert "YES" in consent and "STOP" in consent and "Swift, petrol, up to ₹6 lakh, under 60,000 km" in consent
    assert "≤" not in consent and "·" not in consent  # plain words for customers, not staff shorthand

    say(CUSTOMER, "yes")
    lead = repo.get_lead("truechoice", lead.id)
    assert lead.status == "active" and lead.consent_at == clock.now()
    assert texts_to(sent, CUSTOMER)[-1].startswith("Thanks!")
    assert repo.get_customer("truechoice", CUSTOMER).consent


def test_lead_outside_window_is_saved_but_not_messaged(env) -> None:
    say, llm, repo, sent = env
    llm.script.append(MockLLM.text(LEAD_JSON))
    say(STAFF, "lead: 9847099999 swift 6L petrol")
    [lead] = repo.list_leads("truechoice")
    assert lead.status == "pending_consent"
    assert texts_to(sent, NEW_CUSTOMER) == []
    assert "template needed" in texts_to(sent, STAFF)[-1]


def test_lead_parsing_works_without_ai(env) -> None:
    say, llm, repo, sent = env
    llm.script.append(LLMError("gemini", "429 quota"))
    say(STAFF, "LEADS 9847099999 Swift 6L petrol under 60000km")
    [lead] = repo.list_leads("truechoice")
    assert lead.requirement.to_dict() == {"budget_max": 600_000, "models": ["swift"], "fuel": "petrol",
                                          "max_km": 60_000}


@pytest.mark.parametrize(("text", "expected"), [
    ("LEAD 12345 swift 6L", "couldn't read a phone number ('12345')"),
    ("LEAD 9847099999", "say what the customer wants"),
])
def test_bad_lead_commands_get_a_fix_message(env, text, expected) -> None:
    say, llm, repo, sent = env
    say(STAFF, text)
    assert expected in texts_to(sent, STAFF)[-1] and "LEAD <phone>" in texts_to(sent, STAFF)[-1]
    assert repo.list_leads("truechoice") == []


def test_lead_with_no_usable_requirement_is_not_saved(env) -> None:
    say, llm, repo, sent = env
    llm.script.append(MockLLM.text('{"notes": "call tomorrow"}'))
    say(STAFF, "LEAD 9847099999 call him tomorrow evening")
    assert "couldn't find what the customer wants" in texts_to(sent, STAFF)[-1]
    assert repo.list_leads("truechoice") == []


def test_only_staff_can_send_lead_and_customers_saying_lead_reach_the_agent(env) -> None:
    say, llm, repo, sent = env
    llm.script += [MockLLM.text("Sure, what car?"), MockLLM.text("Delivery usually takes a few days.")]
    say(CUSTOMER, "LEAD 9847099999 Swift 6L")
    say(CUSTOMER, "Lead time ethra aanu delivery-ku?")
    assert repo.list_leads("truechoice") == []
    assert len(llm.calls) == 2  # both went to the agent as normal chat
    assert texts_to(sent, CUSTOMER)[-1] == "Delivery usually takes a few days."


def test_resending_lead_updates_instead_of_duplicating(env) -> None:
    say, llm, repo, sent = env
    llm.script += [MockLLM.text(LEAD_JSON), MockLLM.text('{"models": ["Baleno"], "budget_max": "7L"}')]
    say(STAFF, "LEAD 9847099999 Swift 6L petrol")
    say(STAFF, "LEAD 9847099999 Baleno 7L")
    [lead] = repo.list_leads("truechoice")
    assert lead.requirement.models == ["baleno"] and lead.requirement.budget_max == 700_000  # client alias name
    assert texts_to(sent, STAFF)[-1].startswith("✅ Updated the existing lead")


def test_stop_opts_out_of_all_leads_once_and_start_brings_them_back(env, clock) -> None:
    say, llm, repo, sent = env
    llm.script += [MockLLM.text("Hi!"), MockLLM.text(LEAD_JSON)]
    say(CUSTOMER, "Hi")
    say(STAFF, "LEAD 9847012345 Swift 6L petrol")
    say(CUSTOMER, "YES")
    from app.storage.models import Lead, LeadRequirement
    from datetime import timedelta
    extra = Lead("truechoice", CUSTOMER, clock.now(), clock.now() + timedelta(days=90), STAFF,
                 LeadRequirement(models=["creta"]))
    repo.create_lead(extra)  # a second, still-pending lead

    say(CUSTOMER, "STOP")
    statuses = {lead.status for lead in repo.list_leads("truechoice", phone=CUSTOMER)}
    assert statuses == {"opted_out"}
    assert repo.get_customer("truechoice", CUSTOMER).opted_out
    confirmation = texts_to(sent, CUSTOMER)[-1]
    assert "won't get any more car alerts" in confirmation and "START" in confirmation
    count = len(texts_to(sent, CUSTOMER))
    say(CUSTOMER, "stop")
    assert len(texts_to(sent, CUSTOMER)) == count  # confirmed only once

    say(CUSTOMER, "START")
    by_id = {lead.id: lead.status for lead in repo.list_leads("truechoice", phone=CUSTOMER)}
    assert by_id[extra.id] == "pending_consent" and "active" in by_id.values()
    assert texts_to(sent, CUSTOMER)[-1].startswith("Welcome back!")


def test_lead_for_opted_out_customer_is_refused(env, clock) -> None:
    say, llm, repo, sent = env
    repo.get_or_create_customer("truechoice", NEW_CUSTOMER, clock.now())  # they chatted before, then said STOP
    repo.set_opted_out("truechoice", NEW_CUSTOMER, clock.now())
    llm.script.append(MockLLM.text(LEAD_JSON))
    say(STAFF, "LEAD 9847099999 Swift 6L")
    assert "opted out" in texts_to(sent, STAFF)[-1] and repo.list_leads("truechoice") == []


def test_handoff_pauses_notifies_staff_and_resume_works(env, clock) -> None:
    say, llm, repo, sent = env
    llm.script.append(MockLLM.text("Hi"))
    say(STAFF, "hello bot")  # staff messaged recently → summary can go as plain text
    llm.script += [MockLLM.tool("handoff_to_human", reason="wants discount", summary="Swift ZXi price"),
                   MockLLM.text("Sales team will call you shortly.")]
    say(CUSTOMER, "Discount tharumo?")
    customer = repo.get_customer("truechoice", CUSTOMER)
    assert customer.bot_paused and customer.paused_reason == "wants discount"
    summary = texts_to(sent, STAFF)[-1]
    assert "+919847012345" in summary and "wants discount" in summary and "RESUME +919847012345" in summary

    calls = len(llm.calls)
    say(CUSTOMER, "Hello? Anyone there?")
    assert len(llm.calls) == calls  # paused: stored, not answered
    assert repo.list_messages("truechoice", CUSTOMER)[-1].text == "Hello? Anyone there?"

    say(STAFF, "RESUME")
    assert "+919847012345: wants discount" in texts_to(sent, STAFF)[-1]
    say(STAFF, "resume 98470 12345")
    assert texts_to(sent, STAFF)[-1] == "✅ The bot is answering +919847012345 again."
    assert not repo.get_customer("truechoice", CUSTOMER).bot_paused


def test_paused_chat_resumes_by_itself_after_24_hours(env, clock) -> None:
    say, llm, repo, sent = env
    repo.set_bot_paused("truechoice", CUSTOMER, True, at=clock.now(), reason="wants discount")
    clock.advance(hours=23)
    say(CUSTOMER, "still waiting")
    assert llm.calls == []
    clock.advance(hours=2)
    llm.script.append(MockLLM.text("Sorry for the wait! How can I help?"))
    say(CUSTOMER, "hello?")
    assert not repo.get_customer("truechoice", CUSTOMER).bot_paused
    assert texts_to(sent, CUSTOMER)[-1] == "Sorry for the wait! How can I help?"


def test_system_handoff_notifies_once_a_day_without_pausing(env) -> None:
    say, llm, repo, sent = env
    llm.script.append(MockLLM.text("Hi"))
    say(STAFF, "hi")  # staff inside the 24 h window → notices go as text
    llm.script += [LLMError("fallback", "all providers failed")] * 3
    for question in ("Swift undo?", "hello?", "anyone?"):
        say(CUSTOMER, question)
    assert not repo.get_customer("truechoice", CUSTOMER).bot_paused
    notices = [t for t in texts_to(sent, STAFF) if "llm_unavailable" in t]
    assert len(notices) == 1  # an outage doesn't spam staff on every message
    assert len([t for t in texts_to(sent, CUSTOMER) if "technical problem" in t]) == 3


def test_staff_help(env) -> None:
    say, llm, repo, sent = env
    say(STAFF, "help")
    assert "RESUME <phone>" in texts_to(sent, STAFF)[-1] and "LEAD <phone>" in texts_to(sent, STAFF)[-1]
