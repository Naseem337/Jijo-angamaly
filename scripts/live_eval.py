"""Run the customer-message exam (tests/eval/customer_scenarios.yaml) against the REAL AI.

    .venv\\Scripts\\python.exe scripts\\live_eval.py                    # all scenarios
    .venv\\Scripts\\python.exe scripts\\live_eval.py --only emi,photos  # some of them
    .venv\\Scripts\\python.exe scripts\\live_eval.py --delay 10         # pause between scenarios

Each scenario gets a fresh customer, a temporary database and the fixed mock stock, so
results are comparable between runs. Uses the AI keys and chain from .env, so it costs a
little on a paid key (roughly 3 AI calls per message); the free Gemini tier only allows
about 20 calls per model per day, which is not enough for a full run.
Results are printed and saved to data/eval/<time>.json.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.agent import Agent  # noqa: E402
from app.core.client_config import load_client_config  # noqa: E402
from app.core.clock import SystemClock  # noqa: E402
from app.core.language import detect_script  # noqa: E402
from app.inventory import MockInventoryAdapter, ModelAliases  # noqa: E402
from app.providers.llm import LLMNotConfigured, build_llm  # noqa: E402
from app.settings import EnvSecretResolver, get_settings  # noqa: E402
from app.storage.sqlite_repo import SQLiteRepository  # noqa: E402

SCENARIOS = ROOT / "tests" / "eval" / "customer_scenarios.yaml"

# Checked on every reply.
BANNED_PHRASES = r"(certainly!|absolutely!|i'd be happy to assist|great question|as an ai|feel free to reach out|" \
                 r"is there anything else i can help|here are some options:|i apologize for the inconvenience)"
NAMES_AI = r"\b(gemini|google ai|openai|chatgpt|gpt-?\d|deepseek|claude|anthropic|large language model|llm)\b"
ASKS_PHONE = r"(phone number|mobile number|contact number|number tharamo|നമ്പർ തരാമോ)"
MAX_LINES = 9


@dataclass
class Result:
    id: str
    passed: bool
    problems: list[str] = field(default_factory=list)
    reply: str = ""
    tools: list[str] = field(default_factory=list)
    handoff: str | None = None
    guardrail: str = ""
    provider: str = ""
    seconds: float = 0.0


def check(scenario: dict, reply, seconds: float) -> Result:
    text = reply.text
    problems: list[str] = []
    if reply.guardrail == "llm_error":
        problems.append("AI unavailable (quota/outage): not a bot answer")
    elif reply.guardrail == "fallback":
        problems.append(f"guardrails rejected the answer twice: {reply.problems}")
    if re.search(BANNED_PHRASES, text, re.I):
        problems.append("customer-care phrase")
    if re.search(NAMES_AI, text, re.I):
        problems.append("named the AI behind the bot")
    if re.search(ASKS_PHONE, text, re.I):
        problems.append("asked for the phone number")
    if len([line for line in text.splitlines() if line.strip()]) > MAX_LINES:
        problems.append(f"too long ({len(text.splitlines())} lines)")
    if (lang := scenario.get("lang")) and reply.guardrail != "llm_error":
        script = detect_script(text)
        want = "malayalam" if lang == "ml" else "latin"
        if script != want:
            problems.append(f"replied in {script} script, expected {want}")
    if (wanted := scenario.get("tools")) and not set(wanted) & set(reply.tools_used):
        problems.append(f"didn't use {' / '.join(wanted)} (used {reply.tools_used or 'none'})")
    if (unwanted := set(scenario.get("no_tools", [])) & set(reply.tools_used)):
        problems.append(f"shouldn't have used {', '.join(sorted(unwanted))}")
    if "handoff" in scenario and bool(reply.handoff) != scenario["handoff"]:
        problems.append("should have handed off to the team" if scenario["handoff"] else "handed off unnecessarily")
    if (pattern := scenario.get("mention")) and not re.search(pattern, text, re.I):
        problems.append(f"missing expected content /{pattern}/")
    if (pattern := scenario.get("avoid")) and re.search(pattern, text, re.I):
        problems.append(f"said something it must not /{pattern}/")
    return Result(scenario["id"], not problems, problems, text, list(reply.tools_used),
                  reply.handoff.reason if reply.handoff else None, reply.guardrail, reply.provider, round(seconds, 1))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--client", default="truechoice")
    parser.add_argument("--only", help="comma-separated scenario ids")
    parser.add_argument("--delay", type=float, default=0.0, help="seconds to wait between scenarios")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    scenarios = yaml.safe_load(SCENARIOS.read_text(encoding="utf-8"))
    if args.only:
        wanted = {s.strip() for s in args.only.split(",")}
        scenarios = [s for s in scenarios if s["id"] in wanted]
    config = load_client_config(args.client, get_settings().clients_dir)
    try:
        llm = build_llm(config, EnvSecretResolver())
    except LLMNotConfigured as exc:
        print(f"No AI provider is set up:\n{exc}")
        return 1
    stock = MockInventoryAdapter.from_file(config.config_dir / "inventory_mock.json",
                                           ModelAliases(config.business_rules.model_aliases))

    results: list[Result] = []
    outages = 0
    with tempfile.TemporaryDirectory() as tmp:
        for n, scenario in enumerate(scenarios, 1):
            repo = SQLiteRepository(Path(tmp) / f"{scenario['id']}.db")
            agent = Agent(config, llm, repo, stock, SystemClock())
            messages = scenario["say"] if isinstance(scenario["say"], list) else [scenario["say"]]
            started = time.perf_counter()
            for message in messages:
                reply = agent.handle("+919000000099", message, source=scenario.get("source", "text"))
            result = check(scenario, reply, time.perf_counter() - started)
            repo.close()
            results.append(result)
            mark = "PASS" if result.passed else "FAIL"
            print(f"[{n}/{len(scenarios)}] {mark} {result.id} ({result.seconds}s, {result.provider or '-'})")
            if not result.passed:
                print(f"      problems: {'; '.join(result.problems)}")
            print(f"      bot: {result.reply[:300].replace(chr(10), ' / ')}")
            outages = outages + 1 if reply.guardrail == "llm_error" else 0
            if outages >= 3:
                print("\nThe AI is unavailable (quota or outage) three times in a row; stopping.")
                break
            if args.delay:
                time.sleep(args.delay)

    passed = sum(r.passed for r in results)
    print(f"\n{passed}/{len(results)} passed")
    out = ROOT / "data" / "eval"
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{datetime.now():%Y%m%d-%H%M%S}.json"
    path.write_text(json.dumps([asdict(r) for r in results], ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Saved to {path}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
