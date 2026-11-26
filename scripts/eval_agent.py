"""Agent evals with the REAL AI (docs/AI_AGENT_DESIGN.md section 7). Nothing goes to WhatsApp and no real payment link
is made: each run gets a fresh scratch database, fake payments and a fake WhatsApp that only records.

    .venv\\Scripts\\python.exe scripts\\eval_agent.py                 all scenarios, 3 runs each, with the tone judge
    .venv\\Scripts\\python.exe scripts\\eval_agent.py --only happy_en --runs 1
    .venv\\Scripts\\python.exe scripts\\eval_agent.py --no-judge

Scenarios: tests/eval/agent_scenarios.yaml (the format is at the top of that file). The AI is the LLM_CHAIN from .env
(the same one the bot uses; nothing here names a model). Hard checks per turn: tools, arguments, sent keys, must_not,
expected text, gate outcome. Tone: one AI-judge call per run scores the whole chat 1-5; below 4 fails.

Why not a pure dry-run outbox: a dry run never writes the bot's messages into the chat log, so in a multi-turn scenario
the AI would not see its own earlier replies. The fake WhatsApp gives the same safety (nothing leaves this machine)
and keeps the chat log exactly as it is live.

Full transcripts of failed runs go to tests/eval/out/ (git-ignored).
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
import re
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["RESORT_STAFF_NUMBERS"] = "+919800000009"  # a fake staff number: alerts are only recorded

import yaml  # noqa: E402

from app.core.clock import IST, FixedClock  # noqa: E402
from app.providers.llm import ChatMessage, build_llm  # noqa: E402
from app.resort import photos as ph  # noqa: E402
from app.resort import seed  # noqa: E402
from app.resort.agent import gate  # noqa: E402
from app.resort.agent.outbox import near_same  # noqa: E402
from app.resort.config import load_resort_config  # noqa: E402
from app.resort.db import ResortRepo  # noqa: E402
from app.resort.flow import BookingBot  # noqa: E402
from app.resort.payments import MockPayments  # noqa: E402
from app.settings import EnvSecretResolver  # noqa: E402
from app.whatsapp.parse import InboundMessage  # noqa: E402
from scripts.agent_test_data import add_test_photos  # noqa: E402

SCENARIOS = ROOT / "tests" / "eval" / "agent_scenarios.yaml"
OUT = ROOT / "tests" / "eval" / "out"
TODAY = datetime(2026, 11, 25, 10, 0, tzinfo=IST)  # fixed, so the dates in the scenarios never go stale
GUEST = "+919847000777"
PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 64
PAUSE_S = 0.0  # seconds between AI turns; --pause 3 on a free-tier key with tight rate limits
RATE_WAIT_S = 30  # a run where the AI was down is run once more after this wait
_ids = itertools.count(1)


class RecordWA:
    """A WhatsApp client that sends nothing: it records what the guest (and staff) would get."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []  # (to, kind, text or media id)

    def send_text(self, to, text):
        self.sent.append((to, "text", text))
        return f"wamid.out{next(_ids)}"

    def send_image(self, to, media_id, caption=""):
        self.sent.append((to, "image", media_id))
        return f"wamid.img{next(_ids)}"

    def upload_media(self, data, mime_type="image/jpeg", filename="x"):
        return f"media-{len(data)}-{filename}"

    def send_document(self, to, media_id, filename, caption=""):
        self.sent.append((to, "document", filename))
        return f"wamid.doc{next(_ids)}"

    def send_template(self, to, name, language, params, **kw):
        self.sent.append((to, "template", name))
        return f"wamid.tpl{next(_ids)}"

    def send_interactive_list(self, to, *a, **k):
        return "wamid.x"

    def send_reply_buttons(self, to, *a, **k):
        return "wamid.x"

    def mark_read(self, *a, **k):
        return True

    def download_media(self, *a, **k):
        return PNG, "image/png"


@dataclass
class World:
    repo: ResortRepo
    bot: BookingBot
    wa: RecordWA
    pay: MockPayments
    clock: FixedClock
    last: list[Any] = field(default_factory=list)  # the TurnResult of the last AI turn (empty: the gate answered)


def scratch_world(llm: Any) -> World:
    """A fresh database: the seed resorts, Kanthalloor switched on, fake photos for every resort and room type."""
    tmp = Path(tempfile.mkdtemp(prefix="agent-eval-"))
    os.environ["RESORT_DB_PATH"] = str(tmp / "r.sqlite3")  # the photo files go next to the scratch database
    repo = ResortRepo(tmp / "r.sqlite3")
    seed.seed(repo)
    add_test_photos(repo)  # Kanthalloor switched on, general photos, room numbers and each room's own photo
    cfg = load_resort_config().model_copy(update={"agent_mode": "agent"})
    wa, pay, clock = RecordWA(), MockPayments(), FixedClock(TODAY)
    bot = BookingBot(cfg, repo, wa, pay, clock, llm=lambda: llm)
    return World(repo, bot, wa, pay, clock)


# ------------------------------------------------------------------ checks


def _norm(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False, default=str).lower() if isinstance(v, (list, dict)) else str(v).lower()


def arg_ok(actual: Any, want: Any) -> bool:
    if isinstance(want, bool) or isinstance(want, (int, float)):
        try:
            return float(actual) == float(want)
        except (TypeError, ValueError):
            return False
    return str(want).lower() in _norm(actual)


def key_hit(want: str, keys: list[str]) -> bool:
    if want == "staff":
        return any(k.startswith("staff:") for k in keys)
    return any(k == want or k.startswith(want + ":") for k in keys)


# AI-first simplification (2026-11-26): the AI writes these owner texts itself with reply(), so a scenario's key is
# checked as the owner's wording in what the guest got. expect_sent: any of the markers; not_sent: the exact sentence.
OWNER_WORDS: dict[str, tuple[str, ...]] = {
    "ask_contact": ("name and mobile",), "ask_extra_bed": ("extra bed",), "ask_extra_beds": ("extra bed",),
    "ask_location": ("confirm the location",), "closed_place": ("not available",), "form": ("share these details",),
    "guest_called": ("let me know if you need any help",), "handoff": ("contact the team directly", "call them directly"),
    "greet": ("how may i assist you",), "hello": ("hi",), "hello_back": ("hi",), "min_rooms": ("room",),
    "not_our_place": ("properties only in",), "other_room_free": ("also free",), "past_date": ("passed",),
    "pay_terms": ("detailed discussion",), "pay_wait": ("not received the payment",), "photos_sent": ("photos",),
    "price_answer": ("₹",), "rooms_ok": ("available",), "rooms_plain": ("room",), "rooms_note": ("room",),
    "rooms_beds": ("extra bed",), "sure": ("sure.",),
}
OWNER_SENTENCE = {"ask_contact": "please share your name and mobile number to proceed",
                  "ask_location": "kindly confirm the location", "form": "please share these details",
                  "greet": "how may i assist you", "handoff": "please contact the team directly",
                  "not_our_place": "we have properties only in", "past_date": "has already passed",
                  "rooms_ok": "yes, rooms are available for your dates", "sure": "sure."}


def owner_hit(want: str, keys: list[str], texts: list[str], calls: list[dict[str, Any]], exact: bool = False) -> bool:
    """A scenario key: a code-sent key as before; an owner text the AI now writes, by its wording; room_picked by the
    tool fact."""
    if want == "room_picked":
        return any("room_picked" in ((c.get("result") or {}).get("facts") or {}) for c in calls)
    if want not in OWNER_WORDS:
        return key_hit(want, keys)
    blob = "\n".join(texts).lower()
    if exact:
        return OWNER_SENTENCE.get(want, OWNER_WORDS[want][0]) in blob
    return any(w in blob for w in OWNER_WORDS[want])


def check_turn(turn: dict[str, Any], outcome: str, calls: list[dict[str, Any]], keys: list[str],
               texts: list[str], ai: str = "") -> list[str]:
    bad: list[str] = []
    names = [c["name"] for c in calls]
    if turn.get("none") and names:
        bad.append(f"expected no tool, got {names}")
    for name in turn.get("expect_tools") or []:
        if name not in names:
            bad.append(f"tool {name} not called (called: {names or 'none'})")
    for name, args in (turn.get("expect_args") or {}).items():
        mine = [c for c in calls if c["name"] == name]
        if not mine:
            if name not in (turn.get("expect_tools") or []):
                bad.append(f"tool {name} not called (for its arguments)")
            continue
        got = dict(mine[-1]["args"] or {})
        facts = (mine[-1].get("result") or {}).get("facts") or {}
        if "place" in got and (facts.get("resort") or facts.get("place")):
            got["place"] = facts.get("resort") or facts.get("place")  # the resort CODE resolved, not the AI's spelling
        for k in ("check_in", "check_out"):  # the AI passes {day, month, year}: the DATE CODE BUILT is what is checked
            if isinstance(got.get(k), dict) or (k == "check_out" and k not in got and facts.get(k)):
                got[k] = facts.get(k, got.get(k))
        for k, v in args.items():
            if not arg_ok(got.get(k), v):
                bad.append(f"{name}.{k} = {got.get(k)!r}, expected {v!r}")
    for want in turn.get("expect_sent") or []:
        if not owner_hit(want, keys, texts, calls):
            bad.append(f"'{want}' not sent (sent: {keys or 'nothing'})")
    for nope in turn.get("not_sent") or []:
        if owner_hit(nope, keys, texts, calls, exact=True):
            bad.append(f"'{nope}' was sent")
    blob = "\n".join(texts)
    for word in turn.get("must_not") or []:  # the AI's OWN words only: the owner's fixed texts are his exact wording
        if word.lower() in ai.lower():
            bad.append(f"forbidden '{word}' in the AI's reply")
    if (most := turn.get("max_words")) is not None and len(ai.split()) > int(most):
        bad.append(f"the AI's own line has {len(ai.split())} words (max {most}): {ai[:80]!r}")
    if turn.get("ai_empty") and ai:
        bad.append(f"the AI wrote its own line, expected nothing: {ai[:80]!r}")
    if turn.get("ai_required") and not ai:
        bad.append("the AI wrote no line of its own")
    if want_order := turn.get("in_order"):  # these keys, in this order (other keys may come between)
        at = 0
        for want in [w for w in want_order if not any(x in OWNER_WORDS or x == "room_picked"
                                                       for x in str(w).split("|"))]:  # code-sent keys keep their order
            hit = next((i for i in range(at, len(keys)) if any(key_hit(w, [keys[i]]) for w in str(want).split("|"))),
                       None)
            if hit is None:
                bad.append(f"'{want}' not sent in order (sent: {keys})")
                break
            at = hit + 1
    if (only := turn.get("only_sent")) is not None:  # the guest got exactly these keys (staff alerts aside)
        got = sorted({k for k in keys if not k.startswith("staff:")})
        only = sorted({"ai" if k in OWNER_WORDS else k for k in only})  # an owner text is now the AI's reply
        if got != only:
            bad.append(f"sent {got}, expected only {only}")
    if turn.get("no_duplicates"):
        for i, a in enumerate(texts):
            if any(near_same(a, b) for b in texts[i + 1:]):
                bad.append(f"the same text went twice: {a[:60]!r}")
                break
    if (want := turn.get("expect_text")) and want not in blob:
        bad.append(f"text missing: {want!r}")
    if (want := turn.get("outcome")) and outcome != want:
        bad.append(f"outcome {outcome}, expected {want}")
    if outcome == "ai_down":
        bad.append("the AI did not answer (ai_down)")
    return bad


# ------------------------------------------------------------------ one run


def run_scenario(sc: dict[str, Any], llm: Any) -> dict[str, Any]:
    w = scratch_world(llm)
    for name in sc.get("closed") or []:  # resorts switched OFF in the dashboard for this scenario
        for p in w.repo.list_properties(active_only=False):
            if name.lower() in p.name.lower():
                p.active = False
                w.repo.save_property(p)
    real_turn = gate.run_turn

    def capture(*a, **k):
        res = real_turn(*a, **k)
        w.last.append(res)
        return res

    gate.run_turn = capture
    log: list[dict[str, Any]] = []
    failures: list[str] = []
    try:
        for n, turn in enumerate(sc["turns"], 1):
            guest = str(turn.get("guest", ""))
            entry: dict[str, Any] = {"n": n, "guest": guest}
            if m := re.fullmatch(r"\[wait (\d+)h\]", guest):
                w.clock.advance(hours=int(m.group(1)))
                log.append(entry)
                continue
            if guest == "[paid]":
                held = w.repo.list_bookings(phone=GUEST, status="hold", limit=1)
                if held and held[0].payment_link_id:
                    w.pay.paid_links[held[0].payment_link_id] = "pay_eval"
                else:
                    failures.append(f"turn {n}: [paid] but there is no open payment link")
                log.append(entry)
                continue
            if guest.startswith("[image]"):
                msg = InboundMessage(f"wamid.in{next(_ids)}", GUEST, "image", w.clock.now(),
                                     text=guest[len("[image]"):].strip(), media_id="img1")
            else:
                msg = InboundMessage(f"wamid.in{next(_ids)}", GUEST, "text", w.clock.now(), text=guest)
            mark, w.last[:] = len(w.wa.sent), []
            started = time.monotonic()
            outcome = w.bot.handle(msg)
            res = w.last[-1] if w.last else None
            out = w.wa.sent[mark:]
            texts = [x for to, kind, x in out if to == GUEST and kind == "text"]
            photos = sum(1 for to, kind, _ in out if to == GUEST and kind == "image")
            calls = res.calls if res else []
            keys = res.sent if res else []
            bad = check_turn(turn, outcome, calls, keys, texts, res.reply if res else "")
            failures += [f"turn {n} ({guest[:40]}): {b}" for b in bad]
            entry.update(outcome=outcome, seconds=round(time.monotonic() - started, 1), sent=keys, texts=texts,
                         photos=photos, staff=[x for to, kind, x in out if to != GUEST],
                         calls=[{"name": c["name"], "args": c["args"], "ok": c["result"].get("ok"),
                                 "next_step": c["result"].get("next_step")} for c in calls],
                         guard=res.guard if res else [], bad=bad, ai=res.reply if res else "",
                         tokens=_tokens(res),
                         fixed=[k for k in keys if k != "ai" and not k.startswith(("staff:", "photos"))])
            log.append(entry)
            w.clock.advance(minutes=1)
            if res is not None and PAUSE_S:
                time.sleep(PAUSE_S)  # free-tier rate limits: a short gap between AI turns
    finally:
        gate.run_turn = real_turn
    # every scenario (AI-first rebalance 2026-11-25): the guest never gets the same text twice in a row
    said = [x for to, kind, x in w.wa.sent if to == GUEST and kind == "text"]
    failures += [f"the same text twice in a row: {b[:60]!r}" for a, b in zip(said, said[1:]) if near_same(a, b)]
    return {"log": log, "failures": failures, "tokens": _sum_tokens(log)}


TOKEN_KEYS = ("input", "cache_read", "cache_write", "output", "calls")


def _tokens(res: Any) -> dict[str, int]:
    """One AI turn's token use (all zero when the gate answered without the AI)."""
    if res is None:
        return dict.fromkeys(TOKEN_KEYS, 0)
    return {"input": res.input_tokens, "cache_read": res.cache_read_tokens, "cache_write": res.cache_write_tokens,
            "output": res.output_tokens, "calls": res.llm_calls}


def _sum_tokens(log: list[dict[str, Any]]) -> dict[str, int]:
    total = dict.fromkeys(TOKEN_KEYS, 0)
    turns = 0
    for e in log:
        if t := e.get("tokens"):
            turns += 1 if t["calls"] else 0
            for k in TOKEN_KEYS:
                total[k] += t[k]
    total["turns"] = turns
    return total


def token_text(t: dict[str, int]) -> str:
    total = t["input"] + t["output"]
    per = total // t["turns"] if t.get("turns") else 0
    hit = f"{100 * t['cache_read'] // t['input']}%" if t["input"] else "-"
    return (f"{total:,} tokens ({per:,}/AI turn; in {t['input']:,}, cache read {t['cache_read']:,}, "
            f"cache write {t['cache_write']:,}, out {t['output']:,}; cache hit {hit})")


JUDGE = """You grade one WhatsApp chat between a guest and a resort's front-desk bot. Score the BOT 1-5:
5 = short (1-2 lines of its own), polite and warm, in the right language, and it answers what the guest asked.
4 = good, one small slip. 3 = clearly too long, cold, the wrong language, or it missed a question. 1-2 = bad.
The right language of a BOT line is the language of the GUEST message just before it, nothing else: an English
message -> English is correct; Malayalam / Tamil / Hindi written in English letters -> that style in English letters.
Score ONLY the lines that start with "BOT:". A turn with "BOT: (nothing of its own)" is NOT scored: skip it.
Lines that start with "SYSTEM:" were sent by the system, not by the bot (the owner's fixed texts, forms, the open
booking step, summaries, payment links, team numbers, photos). NEVER score them and NEVER blame the bot for their
length, wording or content. They DO count as answering the guest: a link, form or photo the guest asked for that
went out as SYSTEM is answered. Mark the bot down only when its OWN line repeats a SYSTEM line.
The resort OWNER's style rules (follow them when scoring): a busy, warm front-desk person on WhatsApp. Short and
answer-first: one short sentence is best ("Yes sir, there is a pool."). No explanations, no closing lines, no "anything
else?". A short reply is NOT cold. "sir" once at the start and in short greetings. Saying nothing of its own after
SYSTEM lines is right. The owner's texts, the open booking step, links and summaries are sent by code, never by the bot.
Answer ONLY with JSON: {"score": <1-5>, "reason": "<one short line>"}"""


def judge(llm: Any, sc: dict[str, Any], log: list[dict[str, Any]]) -> tuple[int | None, str]:
    if not any(e.get("ai") for e in log):
        return None, "not judged: the AI wrote no words of its own (only fixed texts)"
    lines = []
    for e in log:
        if "outcome" not in e:
            continue
        lines.append(f"GUEST: {e['guest']}")
        own = e.get("ai") or ""
        lines.append(f"BOT: {own}" if own else "BOT: (nothing of its own)")  # only this line is scored
        for text in e.get("texts") or []:  # what code sent: shown so the judge knows the guest was answered
            if own and near_same(text, own):
                continue
            short = " ".join(text.split())
            lines.append(f"SYSTEM: {short[:140]}{' …' if len(short) > 140 else ''}")
        if e.get("photos"):
            lines.append(f"SYSTEM: [{e['photos']} photos]")
    try:
        r = llm.complete(JUDGE, [ChatMessage("user", "\n".join(lines))], None, 300, temperature=0)
        m = re.search(r"\{.*\}", r.text or "", re.S)
        data = json.loads(m.group(0)) if m else {}
        return int(data["score"]), str(data.get("reason", ""))[:200]
    except Exception as exc:  # the judge failing never fails a run: it is reported as not scored
        return None, f"judge failed: {type(exc).__name__}: {str(exc)[:120]}"


def save_transcript(sc: dict[str, Any], run_no: int, result: dict[str, Any]) -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"{sc['id']}-run{run_no}.txt"
    lines = [f"SCENARIO {sc['id']}  run {run_no}", "FAILURES:", *[f"  - {f}" for f in result["failures"]], ""]
    for e in result["log"]:
        lines.append(f"--- turn {e['n']}: GUEST: {e['guest']}")
        if "outcome" not in e:
            continue
        lines.append(f"    outcome={e['outcome']}  {e['seconds']}s  sent={e['sent']}  tokens={e.get('tokens')}")
        for c in e["calls"]:
            lines.append(f"    TOOL {c['name']}({json.dumps(c['args'], ensure_ascii=False)}) ok={c['ok']} "
                         f"next_step={c['next_step']}")
        if e["guard"]:
            lines.append(f"    GUARD: {e['guard']}")
        lines += [f"    BOT: {t}" for t in e["texts"]]
        if e["photos"]:
            lines.append(f"    BOT: [{e['photos']} photos]")
        lines += [f"    STAFF: {s[:200]}" for s in e["staff"]]
        lines += [f"    ✗ {b}" for b in e["bad"]]
    lines.append(f"\nJUDGE: {result.get('score')} {result.get('reason', '')}")
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*", help="scenario ids")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--no-judge", action="store_true")
    ap.add_argument("--pause", type=float, default=0.0, help="seconds between AI turns (rate limits)")
    a = ap.parse_args()
    global PAUSE_S
    PAUSE_S = a.pause
    ph._shrink = lambda data: None  # fake photos are not real images
    scenarios = yaml.safe_load(SCENARIOS.read_text(encoding="utf-8"))
    if a.only:
        scenarios = [s for s in scenarios if s["id"] in a.only]
    llm = build_llm(load_resort_config(), EnvSecretResolver())
    print(f"{len(scenarios)} scenarios x {a.runs} runs, AI chain: {', '.join(p.name for p in llm.providers)}\n")
    if OUT.exists():
        for old in OUT.glob("*.txt"):
            old.unlink()
    summary: list[tuple[str, int, str]] = []
    started, rate_retries = time.monotonic(), 0
    grand = dict.fromkeys((*TOKEN_KEYS, "turns"), 0)
    total, width = len(scenarios), max((len(s["id"]) for s in scenarios), default=0)
    for i, sc in enumerate(scenarios, 1):
        passed, first_reason, sc_started = 0, "", time.monotonic()
        sc_tokens = dict.fromkeys((*TOKEN_KEYS, "turns"), 0)
        for run_no in range(1, a.runs + 1):
            result = run_scenario(sc, llm)
            if any("ai_down" in f for f in result["failures"]):  # a rate limit, not the agent: wait and run it again once
                time.sleep(RATE_WAIT_S)
                result = run_scenario(sc, llm)
                rate_retries += 1
            for k, v in result["tokens"].items():
                sc_tokens[k] += v
                grand[k] += v
            if not a.no_judge:
                result["score"], result["reason"] = judge(llm, sc, result["log"])
                if result["score"] is not None and result["score"] < 4:
                    result["failures"].append(f"tone {result['score']}/5: {result['reason']}")
            if result["failures"]:
                save_transcript(sc, run_no, result)
                first_reason = first_reason or result["failures"][0]
            else:
                passed += 1
        mark = "PASS" if passed == a.runs else "FAIL"
        summary.append((sc["id"], passed, first_reason))
        ok_so_far = sum(1 for _, p, _ in summary if p == a.runs)
        line = (f"[{i}/{total} {i * 100 // total}%] {sc['id']:<{width}} ... {mark} {passed}/{a.runs} "
                f"({time.monotonic() - sc_started:.0f}s)  [pass {ok_so_far}/{i}]")
        line += f"  {token_text(sc_tokens)}"
        if first_reason:
            line += f" — {first_reason[:150]}"
        print(line, flush=True)
    full = sum(1 for _, p, _ in summary if p == a.runs)
    runs_ok = sum(p for _, p, _ in summary)
    print(f"\nSUMMARY: {full}/{len(summary)} scenarios pass every run; {runs_ok}/{len(summary) * a.runs} runs pass; "
          f"{(time.monotonic() - started) / 60:.1f} min; {rate_retries} run(s) repeated after the AI was down. "
          f"Failed transcripts: {OUT.relative_to(ROOT)}. Tokens: {token_text(grand)}")
    return 0 if full == len(summary) else 1


if __name__ == "__main__":
    raise SystemExit(main())
