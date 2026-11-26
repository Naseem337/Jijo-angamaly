"""AI-6 owner test: the tester allowlist (AGENT_TEST_PHONES) and staff alerts only to STAFF_ALERT_PHONES."""

from __future__ import annotations

import logging

from test_agent_gate import agent, m, send  # noqa: F401  (agent is a fixture)
from test_bot import GUEST, STAFF, answer, reply, world  # noqa: F401  (world is a fixture)

from app.resort.agent.gate import is_tester, staff_override
from app.resort.config import ResortConfig, load_resort_config

OTHER = "+919847099999"
TESTER_2 = "+919847011111"


def with_config(w, **values):
    cfg = w.bot.config
    w.bot.config = ResortConfig.model_validate({**cfg.model_dump(), **values})
    return w


def test_no_testers_listed_means_the_agent_answers_everyone(agent):  # noqa: F811
    assert agent.bot.config.agent_test_phones == []
    assert send(agent, m("is there a pool?"), answer("Yes sir, there is a pool.")) == "answered"


def test_a_listed_tester_gets_the_agent(agent):  # noqa: F811
    with_config(agent, agent_test_phones=f"{TESTER_2}, {GUEST}")
    assert send(agent, m("is there a pool?"), answer("Yes sir, there is a pool.")) == "answered"
    assert agent.wa.texts(GUEST) == ["Yes sir, there is a pool."]


def test_any_other_phone_gets_no_reply_and_a_log_line(agent, caplog):  # noqa: F811
    with_config(agent, agent_test_phones=TESTER_2)
    with caplog.at_level(logging.INFO, logger="app.resort.agent.gate"):
        assert send(agent, m("is there a pool?"), answer("Yes sir, there is a pool.")) == "not_a_tester"
    assert agent.wa.texts(GUEST) == [] and agent.llm.calls == []
    assert any("is not a tester" in r.getMessage() for r in caplog.records)
    assert GUEST not in caplog.text  # the phone is masked in the log


def test_numbers_are_matched_in_any_written_form(agent):  # noqa: F811
    with_config(agent, agent_test_phones="98470 12345")  # no country code, spaces: normalized to +919847012345
    assert agent.bot.config.agent_test_phones == [GUEST]
    assert is_tester(agent.bot, GUEST) and not is_tester(agent.bot, OTHER)


def test_the_old_bot_ignores_the_tester_list(world):  # noqa: F811
    with_config(world, agent_mode="old", agent_test_phones=TESTER_2)
    assert world.bot.handle(m("hi")) != "not_a_tester"


def test_staff_alerts_go_only_to_the_staff_alert_phones(world):  # noqa: F811
    with_config(world, staff_alert_phones=OTHER)
    assert STAFF in world.bot.staff_numbers(world.p1.id)  # before: the dashboard's staff number too
    staff_override(world.bot)
    assert world.bot.staff_numbers(world.p1.id) == [OTHER] and world.bot.staff_numbers() == [OTHER]
    world.bot.alert_staff("test alert", world.p1.id)
    assert world.wa.texts(OTHER) == ["test alert"] and world.wa.texts(STAFF) == []


def test_no_staff_alert_phones_changes_nothing(world):  # noqa: F811
    before = world.bot.staff_numbers(world.p1.id)
    staff_override(world.bot)
    assert world.bot.staff_numbers(world.p1.id) == before


def test_both_lists_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("AGENT_TEST_PHONES", f"{GUEST},{TESTER_2}")
    monkeypatch.setenv("STAFF_ALERT_PHONES", OTHER)
    cfg = load_resort_config()
    assert cfg.agent_test_phones == [GUEST, TESTER_2] and cfg.staff_alert_phones == [OTHER]
    monkeypatch.setenv("AGENT_TEST_PHONES", "")
    monkeypatch.setenv("STAFF_ALERT_PHONES", "")
    cfg = load_resort_config()
    assert cfg.agent_test_phones == [] and cfg.staff_alert_phones == []
