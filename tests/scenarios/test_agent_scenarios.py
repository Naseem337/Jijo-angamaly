"""End-to-end agent turns with a scripted LLM, real tools, real SQLite and the mock stock."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.agent import Agent
from app.core.client_config import load_client_config
from app.core.clock import FixedClock
from app.inventory import build_inventory_adapter
from app.providers.llm import LLMError, LLMResponse, MockLLM, ToolCall
from app.storage.sqlite_repo import SQLiteRepository

PHONE = "+919847012345"


@pytest.fixture
def repo(tmp_path: Path):
    repository = SQLiteRepository(tmp_path / "agent.db")
    yield repository
    repository.close()


@pytest.fixture
def make_agent(clients_dir: Path, repo: SQLiteRepository, clock: FixedClock):
    config = load_client_config("truechoice", clients_dir)
    inventory = build_inventory_adapter(config)

    def factory(*script) -> tuple[Agent, MockLLM]:
        llm = MockLLM(list(script))
        return Agent(config, llm, repo, inventory, clock), llm

    return factory


def tool_result(llm: MockLLM, call: int = 1) -> dict:
    """The JSON the model saw as the result of its first tool call, in LLM call `call`."""
    return json.loads(llm.calls[call].messages[-1].content)


def test_search_then_quote_real_prices(make_agent, repo) -> None:
    agent, llm = make_agent(
        MockLLM.tool("search_cars", model="swift", budget_max="7 lakh"),
        MockLLM.text("Swift und: *2021 Maruti Suzuki Swift ZXi AMT* – ₹6,95,000 (₹6.95 lakh), 28,500 km (TC-1002)"),
    )
    reply = agent.handle(PHONE, "Swift undo 7 lakhinu thazhe?")

    assert reply.guardrail == "ok" and reply.language == "manglish"
    assert reply.tools_used == ["search_cars"]
    assert [c.split(" | ")[0] for c in tool_result(llm)["cars"]] == ["TC-1002", "TC-1001"]  # one line per car
    first = llm.calls[0]
    assert "Reply language: Manglish" in first.messages[-1].content
    assert first.messages[-1].content.endswith("Swift undo 7 lakhinu thazhe?")
    assert [t.name for t in first.tools][:2] == ["search_cars", "get_car_details"]
    stored = repo.list_messages("truechoice", PHONE)
    assert [(m.direction, m.text) for m in stored] == [("in", "Swift undo 7 lakhinu thazhe?"), ("out", reply.text)]
    assert repo.get_customer("truechoice", PHONE).language == "manglish"


def test_history_is_sent_on_the_next_turn(make_agent) -> None:
    agent, llm = make_agent(MockLLM.text("Namaskaram! Ethu car aanu nokkunnath?"), MockLLM.text("Sure."))
    agent.handle(PHONE, "Hi")
    agent.handle(PHONE, "SUV venam")
    second = llm.calls[1].messages
    assert [m.role for m in second] == ["user", "assistant", "user"]
    assert second[0].content == "Hi" and second[1].content.startswith("Namaskaram")


def test_invented_price_is_rewritten(make_agent) -> None:
    agent, llm = make_agent(MockLLM.text("Swift ₹4 lakh-nu kittum!"),
                            MockLLM.text("Njan stock check cheythittu parayam."))
    reply = agent.handle(PHONE, "Swift ethra?")
    assert reply.guardrail == "rewritten" and reply.text == "Njan stock check cheythittu parayam."
    assert "unsourced_amount" in reply.problems[0]
    assert llm.calls[1].tools == []  # the rewrite gets no tools
    assert "System check" in llm.calls[1].messages[-1].content


def test_repeated_violation_falls_back_and_hands_off(make_agent, repo) -> None:
    agent, _ = make_agent(MockLLM.text("Loan approval guaranteed!"), MockLLM.text("Still guaranteed!"))
    reply = agent.handle(PHONE, "Can I get a loan?")
    assert reply.guardrail == "fallback" and reply.handoff.reason == "guardrail"
    assert reply.text.startswith("Let me check that with the team")
    assert repo.list_messages("truechoice", PHONE)[-1].meta["guardrail"] == "fallback"


def test_emi_figures_from_the_tool_are_allowed(make_agent) -> None:
    def answer(system, messages, tools):
        result = json.loads(messages[-1].content)
        option = result["options"][-1]
        return LLMResponse(f"5 varsham: {option['emi_text']}. {result['disclaimer']}")

    agent, llm = make_agent(MockLLM.tool("emi_estimate", car_id="tc-1002"), answer)
    reply = agent.handle(PHONE, "TC-1002 EMI ethra?")
    assert reply.guardrail == "ok"
    assert "₹12,000 – ₹12,900 per month" in reply.text and "indicative" in reply.text
    assert tool_result(llm)["down_payment"] == 139000


def test_requirement_needs_explicit_consent(make_agent, repo) -> None:
    agent, llm = make_agent(
        MockLLM.tool("save_requirement", model="fortuner", budget_max=2500000, consent=False),
        MockLLM.text("Puthiya Fortuner vannal WhatsApp-il ariyikkatte?"),
    )
    agent.handle(PHONE, "Fortuner 25 lakhinu undo?")
    assert "consent=true" in tool_result(llm)["error"]
    assert repo.list_requirements("truechoice") == []

    agent, llm = make_agent(MockLLM.tool("save_requirement", model="fortuner", budget_max=2500000, consent=True),
                            MockLLM.text("Sheri, ariyikkam!"))
    reply = agent.handle(PHONE, "sheri, ariyikkuka")
    [req] = repo.list_requirements("truechoice", phone=PHONE)
    assert (req.model, req.budget_max, req.status) == ("fortuner", 2_500_000, "active")
    assert (req.expires_at - req.created_at).days == 60
    customer = repo.get_customer("truechoice", PHONE)
    assert customer.consent and customer.consent_source == "chat" and customer.consent_recorded_by == "customer"
    assert reply.saved == [f"requirement:{req.id}"]


def test_voice_lead_waits_for_read_back_confirmation(make_agent, repo) -> None:
    agent, llm = make_agent(
        MockLLM.tool("save_enquiry", name="Arun", interest="test drive TC-1002", budget=700000),
        MockLLM.text("Arun, TC-1002 test drive, budget ₹7 lakh. Sheri aano?"),
    )
    agent.handle(PHONE, "ente peru Arun, TC-1002 test drive venam, budget 7 lakh", source="voice")
    assert tool_result(llm)["status"] == "needs_confirmation"
    assert repo.list_enquiries("truechoice") == []
    assert repo.get_pending_action("truechoice", PHONE).tool == "save_enquiry"

    agent, llm = make_agent(MockLLM.text("Nanni Arun! Team udane vilikkum."))
    reply = agent.handle(PHONE, "sheri", source="voice")
    [enquiry] = repo.list_enquiries("truechoice")
    assert (enquiry.name, enquiry.budget, enquiry.source) == ("Arun", 700_000, "voice")
    assert "was saved" in llm.calls[0].messages[-1].content
    assert repo.get_pending_action("truechoice", PHONE) is None
    assert repo.get_customer("truechoice", PHONE).name == "Arun"
    assert reply.saved == [f"enquiry:{enquiry.id}"]


def test_voice_read_back_rejected_saves_nothing(make_agent, repo) -> None:
    agent, _ = make_agent(MockLLM.tool("save_enquiry", interest="test drive"), MockLLM.text("Sheri aano?"))
    agent.handle(PHONE, "test drive venam", source="voice")
    agent, llm = make_agent(MockLLM.text("Enthanu maattendath?"))
    agent.handle(PHONE, "alla", source="voice")
    assert repo.list_enquiries("truechoice") == []
    assert "Nothing was saved" in llm.calls[0].messages[-1].content


def test_handoff_tool_sets_handoff(make_agent) -> None:
    agent, _ = make_agent(
        MockLLM.tool("handoff_to_human", reason="wants discount", summary="Swift ZXi price negotiation"),
        MockLLM.text("Nammude sales team udane ningale vilikkum."),
    )
    reply = agent.handle(PHONE, "Discount tharumo?")
    assert reply.handoff.reason == "wants discount" and reply.guardrail == "ok"


def test_llm_outage_sends_technical_reply_and_hands_off(make_agent, repo) -> None:
    agent, _ = make_agent(LLMError("fallback", "all providers failed"))
    reply = agent.handle(PHONE, "ഹലോ, സ്വിഫ്റ്റ് ഉണ്ടോ?")
    assert reply.guardrail == "llm_error" and reply.language == "ml"
    assert reply.handoff.reason == "llm_unavailable"
    assert reply.text.startswith("ഇപ്പോൾ ചെറിയൊരു പ്രശ്നമുണ്ട്")


def test_bad_tool_arguments_go_back_to_the_model(make_agent) -> None:
    bad = LLMResponse("", [ToolCall("c1", "search_cars", {}, "arguments are not valid JSON")], "tool_calls")
    agent, llm = make_agent(bad, MockLLM.tool("get_car_details", call_id="c2", car_id="TC-9999"),
                            MockLLM.text("Ee car ippol illa."))
    reply = agent.handle(PHONE, "TC-9999?")
    assert "not valid JSON" in llm.calls[1].messages[-1].content
    assert "no car with id TC-9999" in llm.calls[2].messages[-1].content
    assert reply.text == "Ee car ippol illa."


def test_sold_car_is_flagged_by_get_car_details(make_agent) -> None:
    agent, llm = make_agent(MockLLM.tool("get_car_details", car_id="TC-1030"), MockLLM.text("Sorry, that one is sold."))
    agent.handle(PHONE, "Seltos TC-1030 available?")
    assert tool_result(llm)["note"] == "This car is sold. Do not offer it."


def test_endless_tool_calls_stop_with_a_fallback(make_agent) -> None:
    agent, _ = make_agent(*[MockLLM.tool("search_cars", call_id=f"c{i}") for i in range(6)])
    reply = agent.handle(PHONE, "anything")
    assert reply.guardrail == "fallback" and "too many tool rounds" in reply.problems[0]


def test_off_topic_question_gets_plain_text(make_agent) -> None:
    agent, llm = make_agent(MockLLM.text("I can only help with True Choice cars. What car are you looking for?"))
    reply = agent.handle(PHONE, "Who will win the election?")
    assert reply.guardrail == "ok" and reply.handoff is None
    assert "bring it back to True Choice's cars" in llm.calls[0].system


def test_token_use_is_counted_per_reply_and_per_day(make_agent, repo) -> None:
    from app.providers.llm import Usage
    first = MockLLM.tool("search_cars", model="swift")
    first.usage = Usage(2500, 40, 2000)
    second = MockLLM.text("Swift und.")
    second.usage = Usage(2700, 25, 2400)
    agent, _ = make_agent(first, second)
    reply = agent.handle(PHONE, "Swift undo?")
    assert reply.usage == {"calls": 2, "tokens_in": 5200, "tokens_out": 65, "tokens_cached": 4400}
    assert repo.get_counter("truechoice", "llm_tokens_in", "2026-10-16") == 5200
    assert repo.get_counter("truechoice", "llm_calls", "2026-10-16") == 2
    assert repo.list_messages("truechoice", PHONE)[-1].meta["usage"]["tokens_out"] == 65


def test_search_results_are_compact_lines(make_agent) -> None:
    agent, llm = make_agent(MockLLM.tool("search_cars", budget_max=1500000), MockLLM.text("ok"))
    agent.handle(PHONE, "15 lakhinu thazhe?")
    cars = tool_result(llm)["cars"]
    assert len(cars) == 3 and all(isinstance(c, str) and " | ₹" in c and " km | " in c for c in cars)


def test_voice_then_typed_follow_up_is_one_conversation_with_the_last_reply_whole(make_agent) -> None:
    car_list = "\n".join(f"*Hyundai Creta {name}* · Diesel · Manual · first owner · good tyres · full service history "
                         "· Makkaraparamba showroom · test drive possible any day except Sunday"
                         for name in ("SX", "E", "S"))  # longer than 300 characters, with no figures to source
    agent, llm = make_agent(MockLLM.text(f"Undu, ithu nokku:\n{car_list}"), MockLLM.text("Message ayachittund, ithaa."))
    agent.handle(PHONE, "Creta undo?", source="voice")  # a voice note first
    agent.handle(PHONE, "Evde")  # then a typed follow-up
    second = llm.calls[1].messages
    assert [m.role for m in second] == ["user", "assistant", "user"]
    assert second[0].content == "Creta undo?" and car_list in second[1].content  # the whole list, not cut at 300
    assert "ONE conversation" in llm.calls[1].system and "refers to YOUR LAST message" in llm.calls[1].system


INVITE = "Ennaa pinne direct showroom-lekku vaa, namukku oru Sulaimani-okke kudichu kaaryangal urappikkaam!"


def test_sulaimani_invitation_is_used_once_per_customer(make_agent, repo, clock) -> None:
    agent, llm = make_agent(MockLLM.text(f"Nalla car aanu. {INVITE}"), MockLLM.text(f"Ok, ethra time aanu? {INVITE}"))
    first = agent.handle(PHONE, "Njan nale varunnund")
    assert "Sulaimani" in first.text  # the first time it is welcome
    assert "ALREADY used the Sulaimani" not in llm.calls[0].messages[-1].content
    second = agent.handle(PHONE, "ok")
    assert "ALREADY used the Sulaimani invitation" in llm.calls[1].messages[-1].content
    assert second.text == "Ok, ethra time aanu?"  # and the code cuts it even if the AI repeats it


def test_the_prompt_has_the_gulf_uncle_voice_and_the_invitation(clients_dir) -> None:
    from app.agent import prompts
    config = load_client_config("truechoice", clients_dir)
    system = prompts.build_system_prompt(config)
    assert "TRUSTED GULF UNCLE" in system and "ellaam clear aayi paranju tharaam" in system
    assert "Sulaimani-okke kudichu kaaryangal urappikkaam" in system and "ONCE per customer" in system
    assert "Gulf-returned uncle" in config.business_rules.speaking_style


def test_the_stock_is_searched_in_code_when_the_message_names_a_car(make_agent) -> None:
    agent, llm = make_agent(MockLLM.text("Swift und, ithil ethu nokkanam?"))
    reply = agent.handle(PHONE, "Swift undo 7 lakhinu thazhe?")
    context = llm.calls[0].messages[-1].content
    assert "Stock search ALREADY DONE" in context and "search_cars(" in context and "TC-" in context
    assert len(llm.calls) == 1  # one AI call, not a tool round first
    assert "search_cars" in reply.tools_used  # so the car list still goes out as usual


def test_no_stock_search_for_a_greeting_or_a_follow_up(make_agent) -> None:
    agent, llm = make_agent(MockLLM.text("Namaskaram!"), MockLLM.text("Evide aanu."))
    agent.handle(PHONE, "Hi")
    agent.handle(PHONE, "evde")
    assert all("Stock search ALREADY DONE" not in call.messages[-1].content for call in llm.calls)


def test_a_profile_name_without_letters_still_gets_sir(make_agent) -> None:
    agent, llm = make_agent(MockLLM.text("Sir, we sell pre-owned cars only."))
    reply = agent.handle(PHONE, "I need a new Brezza", profile_name="~")
    assert reply.text == "Sir, we sell pre-owned cars only."  # always sir/madam (user, 2026-10-27)
    assert "profile name unknown" in llm.calls[0].messages[-1].content
