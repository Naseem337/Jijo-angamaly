"""The live exam's scenario file stays well-formed (the exam itself needs a real AI)."""

from __future__ import annotations

import re
from pathlib import Path

import yaml



SCENARIOS = Path(__file__).resolve().parents[1] / "eval" / "customer_scenarios.yaml"
TOOLS = {"search_cars", "get_car_details", "emi_estimate", "save_lead", "save_requirement", "handoff_to_human"}
KEYS = {"id", "say", "source", "lang", "tools", "no_tools", "handoff", "mention", "avoid"}


def test_scenarios_are_valid() -> None:
    scenarios = yaml.safe_load(SCENARIOS.read_text(encoding="utf-8"))
    assert len(scenarios) >= 40
    ids = [s["id"] for s in scenarios]
    assert len(ids) == len(set(ids))
    for s in scenarios:
        assert set(s) <= KEYS, s["id"]
        assert isinstance(s["say"], (str, list)) and s["say"]
        assert set(s.get("tools", [])) <= TOOLS and set(s.get("no_tools", [])) <= TOOLS, s["id"]
        assert s.get("lang") in (None, "ml", "latin") and s.get("source") in (None, "text", "voice")
        for key in ("mention", "avoid"):
            if key in s:
                re.compile(s[key])
