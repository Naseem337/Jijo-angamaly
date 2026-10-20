from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from app.core.client_config import load_client_config
from app.core.clock import FixedClock
from app.leads.parse import extract_requirement, is_lead_command, rule_based, split_lead_command
from app.providers.llm import LLMError, MockLLM
from app.storage.models import Lead, LeadRequirement
from app.storage.sqlite_repo import SQLiteRepository

ALIASES = {"swift": ["maruti swift"], "innova crysta": ["crysta", "innova"], "xuv700": ["xuv 7oo"]}


# --------------------------------------------------------------------------- command recognition


@pytest.mark.parametrize("text", ["LEAD 98470 12345 Swift", "lead 9847012345 swift", "LEADS 9847012345 swift",
                                  "LEAD: 9847012345 swift", "lead: 9847012345 swift"])
def test_lead_keyword_variants(text: str) -> None:
    assert is_lead_command(text)


@pytest.mark.parametrize("text", ["the lead from yesterday", "leader board", "please LEAD 9847012345 swift",
                                  "misleading"])
def test_normal_messages_are_not_lead_commands(text: str) -> None:
    assert not is_lead_command(text)
    # (A customer's "Lead time ethra?" starts with the word, but only staff numbers are checked
    # for commands at all; see tests/scenarios/test_staff_flow.py.)


@pytest.mark.parametrize(("text", "phone", "rest"), [
    ("LEAD 98470 12345 Swift 6L petrol", "+919847012345", "Swift 6L petrol"),
    ("LEAD +91 98470 12345 Swift", "+919847012345", "Swift"),
    ("leads: 09847012345 crysta 15L", "+919847012345", "crysta 15L"),
    ("LEAD +971 50 123 4567 Innova diesel", "+971501234567", "Innova diesel"),
    ("LEAD 9847012345 2018 swift", "+919847012345", "2018 swift"),
])
def test_phone_and_text_are_split(text: str, phone: str, rest: str) -> None:
    command = split_lead_command(text)
    assert (command.phone, command.text) == (phone, rest)


def test_invalid_phone_is_reported() -> None:
    command = split_lead_command("LEAD 12345 swift")
    assert command.phone is None and command.raw == "12345"
    assert split_lead_command("LEAD swift 6L").phone is None


# --------------------------------------------------------------------------- requirement extraction


def test_llm_extraction_with_money_and_alias_normalising() -> None:
    llm = MockLLM([MockLLM.text('```json\n{"name": "Rahul", "budget_max": "6L", "budget_min": null, '
                                '"makes": ["maruti"], "models": ["Maruti Swift"], "fuel": "Petrol", '
                                '"gearbox": "AMT", "body_type": null, "max_km": "60,000", "max_owners": 1, '
                                '"min_year": 2018, "notes": "red colour"}\n```')])
    req, name, used_llm = extract_requirement(llm, "Rahul swift 6L petrol amt under 60000km 2018+ 1 owner red",
                                              ALIASES)
    assert used_llm and name == "Rahul"
    assert req.to_dict() == {"budget_max": 600_000, "makes": ["Maruti Suzuki"], "models": ["swift"],
                             "fuel": "petrol", "gearbox": "automatic", "max_km": 60_000, "max_owners": 1,
                             "min_year": 2018, "notes": "red colour"}
    assert "Malayali" not in llm.calls[0].system and "JSON" in llm.calls[0].system


def test_llm_gaps_are_filled_by_the_rule_based_reader() -> None:
    llm = MockLLM([MockLLM.text('{"models": ["crysta"], "budget_max": null}')])
    req, _, _ = extract_requirement(llm, "crysta 15 lakh diesel", ALIASES)
    assert (req.models, req.budget_max, req.fuel) == (["innova crysta"], 1_500_000, "diesel")


@pytest.mark.parametrize("failure", [LLMError("gemini", "429 quota"), MockLLM.text("Sure! Here you go: swift")])
def test_rule_based_fallback_when_ai_fails(failure) -> None:
    req, name, used_llm = extract_requirement(MockLLM([failure]), "Swift 6L petrol under 60000km", ALIASES)
    assert not used_llm and name is None
    assert req.to_dict() == {"budget_max": 600_000, "models": ["swift"], "fuel": "petrol", "max_km": 60_000}


@pytest.mark.parametrize(("text", "expected"), [
    ("crysta 12-15 lakh diesel 2018 model single owner automatic",
     {"budget_min": 1_200_000, "budget_max": 1_500_000, "models": ["innova crysta"], "fuel": "diesel",
      "gearbox": "automatic", "max_owners": 1, "min_year": 2018}),
    ("SUV 8 lakh automatic", {"budget_max": 800_000, "body_type": "suv", "gearbox": "automatic"}),
    ("XUV 7OO 18L", {"budget_max": 1_800_000, "models": ["xuv700"]}),
    ("Creta or Nexon below 10L", {"budget_max": 1_000_000, "models": ["Creta", "Nexon"]}),
])
def test_rule_based_reader(text: str, expected: dict) -> None:
    assert rule_based(text, ALIASES, known_models=["Creta", "Nexon", "City"]).to_dict() == expected


def test_nothing_usable_is_empty_and_summary_reads_well() -> None:
    assert rule_based("call him tomorrow evening", ALIASES).is_empty()
    req = LeadRequirement(budget_max=600_000, models=["swift"], fuel="petrol", max_km=60_000, min_year=2018)
    assert req.summary() == "swift · petrol · ≤ ₹6 lakh · ≤ 60,000 km · 2018+"
    assert LeadRequirement.from_dict(req.to_dict()) == req


# --------------------------------------------------------------------------- storage + config


@pytest.fixture
def repo(tmp_path: Path):
    repository = SQLiteRepository(tmp_path / "leads.db")
    yield repository
    repository.close()


def make_lead(clock: FixedClock, phone: str = "+919847012345", **kw) -> Lead:
    now = clock.now()
    return Lead("truechoice", phone, now, now + timedelta(days=90), "+919447000000",
                LeadRequirement(models=["swift"], budget_max=600_000), **kw)


def test_lead_round_trip_update_and_find_open(repo, clock) -> None:
    lead = make_lead(clock, name="Rahul")
    repo.create_lead(lead)
    assert repo.get_lead("truechoice", lead.id) == lead
    assert repo.find_open_lead("truechoice", lead.phone).id == lead.id
    lead.stage, lead.requirement = "interested", LeadRequirement(fuel="diesel")
    repo.update_lead(lead)
    assert repo.get_lead("truechoice", lead.id).requirement.fuel == "diesel"
    assert repo.find_open_lead("other_client", lead.phone) is None


def test_status_changes_across_all_of_a_customers_leads(repo, clock) -> None:
    first, second, other = make_lead(clock), make_lead(clock, status="active"), make_lead(clock, "+919847000000")
    for lead in (first, second, other):
        repo.create_lead(lead)
    assert repo.set_leads_status("truechoice", first.phone, ("pending_consent", "active"), "opted_out",
                                 clock.now()) == 2
    assert {lead.status for lead in repo.list_leads("truechoice", phone=first.phone)} == {"opted_out"}
    assert repo.get_lead("truechoice", other.id).status == "pending_consent"
    assert repo.find_open_lead("truechoice", first.phone) is None


def test_expiry(repo, clock) -> None:
    lead = make_lead(clock)
    repo.create_lead(lead)
    assert repo.expire_leads("truechoice", clock.now() + timedelta(days=89)) == 0
    assert repo.expire_leads("truechoice", clock.now() + timedelta(days=91)) == 1
    assert repo.get_lead("truechoice", lead.id).status == "expired"


def test_expiry_default_is_90_days(clients_dir: Path) -> None:
    assert load_client_config("truechoice", clients_dir).leads.expiry_days == 90


def test_old_leads_table_becomes_enquiries(tmp_path: Path) -> None:
    import sqlite3
    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE leads (client_id TEXT, id TEXT, phone TEXT, created_at TEXT, name TEXT, interest TEXT, "
                "budget INTEGER, notes TEXT, status TEXT, source TEXT, updated_at TEXT, PRIMARY KEY (client_id, id))")
    con.execute("INSERT INTO leads VALUES ('truechoice', 'e1', '+919847012345', '2026-10-01T00:00:00+00:00', 'A', "
                "'test drive', 700000, NULL, 'new', 'chat', NULL)")
    con.commit()
    con.close()
    repo = SQLiteRepository(db)
    assert [e.interest for e in repo.list_enquiries("truechoice")] == ["test drive"]
    assert repo.list_leads("truechoice") == []  # the new leads table starts empty
    repo.close()


def test_short_notes_skip_the_ai_and_long_ones_use_it() -> None:
    llm = MockLLM([MockLLM.text('{"models": ["Swift"], "budget_max": "6L", "notes": "red, sunroof"}')])
    req, _, used = extract_requirement(llm, "Swift 6L petrol under 60000km", ALIASES)
    assert not used and llm.calls == [] and req.budget_max == 600_000  # no tokens spent
    req, _, used = extract_requirement(llm, "Swift around 6L, red colour if possible, sunroof would be nice",
                                       ALIASES)
    assert used and req.notes == "red, sunroof"
