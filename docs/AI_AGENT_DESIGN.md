# Resort bot → AI-first agent: design + build steps

Put this file in the repo as `docs/AI_AGENT_DESIGN.md`. Claude in VS Code builds from it, one step at a time (section 8).

---

## 0. The one rule

**The AI understands and decides. Code holds the facts and sends the owner's fixed messages.**

- No regex or keyword list decides what the guest means. Ever. (Only exceptions: exact `STOP` / `START` / `restart` commands.)
- Code owns: availability, prices, totals, advance, links, payment status, the owner's exact texts, the photo order.
- When the agent fails on a message, fix it with (a) a general prompt rule, (b) a better tool description, or (c) a better tool result. Never a special case for one sentence. Every fix adds an eval scenario.

### 0.1 AI-first simplification (2026-11-26): this overrides sections 3, 4 and 6 where they differ

The live chat showed real answers replaced by "Sorry, could you say that again?" and the AI's reasoning sent to the guest. So:

- **reply(text, language)** is the only way the AI writes to the guest. Any other text it writes is a private note (logged, never sent). A round with a successful reply() ends the turn. No reply and nothing sent → the AI is asked once more ("Call reply() …"); empty twice → "Sorry, could you say that again?" (logged).
- **Tools return facts only.** Code still sends exactly: the booking summary, the payment link message, the paid confirmation, photos, room labels, the glimpse and the map link. Everything else (greeting, location question, form, rooms available, name ask, extra-bed question, not ours / closed, past date, payment not received, handoff, cash terms, "guest called") is written by the AI in the owner's wording from the prompt's OWNER'S WORDS. The greet and repeat_step tools are gone. talk_to_owner only alerts the team (staff_note) and sends the guest nothing.
- **Guard (guard.py), only:** a) a ₹ amount must appear in a tool result, FACTS or the guest's messages anywhere in this conversation; b) no booked / confirmed / reserved before payment; a) and b) get one retry, then only the offending sentence is removed; c) the payment provider's name, booking ids and links no tool gave are stripped. A non-empty answer is never replaced by a fallback.
- **Removed:** the open-step appender, the "Sure." / [ack] logic, the length limit, the banned-phrase list, fix_style, the booking-talk check, the repeats-tools drop, the sent-claim check, drop_recent for the AI's text.
- **Old reminder:** with AGENT_MODE=agent, web.py's sweep skips the old flow's "still open for N more minutes" reminder; payment confirmation and hold release keep running.
- The prompt is under 60 lines with 5 example chats (prompt.py).

---

## 1. Architecture

New code goes in `app/resort/agent/`. The old `flow.py` / `chat.py` logic is not edited; it stays as the fallback until the switch.

```
WhatsApp msg
  │
  ▼
gate.py      (code, no AI)   dedupe · voice→text · STOP/START · restart · staff pause · per-guest lock
  │
  ▼
turn.py      (the agent loop)
  build context: prompt + FACTS + GUEST RECORDS + ENQUIRY state + last 20 messages + swipe-reply note
  LLM ⇄ tools (max 6 rounds)
  │        tools change state and put messages into the OUTBOX
  ▼
guard.py     (code)          checks the AI's own text; 1 retry with feedback; else safe fallback
  │
  ▼
outbox.py    (code)          sends: AI line → queued tool messages (in order) → never-silent check
```

Reused from today unchanged: `db.py`, `models.py`, `availability.py`, `payments.py`, `paying.py`, `photos.py`, `i18n.py`, the payment webhook / sweep / `announce_paid`.

Files:

| file | job |
|---|---|
| `agent/gate.py` | everything before the AI (list above) |
| `agent/state.py` | `Enquiry` dataclass (section 2), load/save to guest state |
| `agent/tools.py` | tool schemas + handlers (section 3) |
| `agent/photos_plan.py` | the owner's photo rules 1–8, moved out of `chat.py` as-is (no regex gate) |
| `agent/prompt.py` | system prompt (section 5) + dynamic blocks |
| `agent/turn.py` | the loop |
| `agent/guard.py` | guardrails (section 6) |
| `agent/outbox.py` | ordered sending, "never same text twice", never-silent |

Switch: `AGENT_MODE` per resort (or per phone) = `old` | `shadow` | `agent`.
`shadow` = old bot answers the guest; agent runs on the same message with a dry-run outbox and logs what it would have sent.
**Shadow mode is dropped (2026-11-25):** there is no live traffic to shadow. AI-6 is now the owner testing the agent on a
test WhatsApp number (`AGENT_MODE=agent`). The `shadow` value stays accepted in config but nothing uses it.

---

## 2. State: one enquiry object

The AI sees this every turn as the `CURRENT ENQUIRY` block. Only tools change it.

```python
@dataclass
class Enquiry:
    place: str | None            # resort slug
    check_in: date | None
    check_out: date | None
    adults: int | None           # adults above 6 years only; children are not asked and not counted
    rooms: int | None            # None = code picks fewest
    room_picks: list[dict]       # [{"room_type": "cottage", "count": 1}, {"room_number": "103"}]
    extra_beds: int              # never added by itself: only after "Do you need an extra bed?" is answered yes, or asked
    checked: dict | None         # last availability result (room plan, totals) — from code
    booking_id: str | None       # internal only, never shown to guest
    link_open: bool
    paid: bool
    language: str                # en | manglish | tanglish | hinglish
    missing: list[str]           # computed: what the next step needs
    next_step: str               # computed: ask_location | ask_details | check | ask_extra_bed | ask_contact | pay | done
                                 # check = everything is known but the room check has not run yet
                                 # ask_extra_bed = a room will hold 3 adults and the question is waiting for its answer
```

Rendered to the AI as plain lines, for example:
```
CURRENT ENQUIRY
place: Kanthaloor · check-in 12 Dec · check-out 14 Dec (2 nights) · 4 adults · rooms: 2 (code picked)
checked: available — Room 108 Beach View Standard, Room 109 Beach View Standard
payment link: open, not paid
next step: guest should pay the advance
```

---

## 3. Tools (11)

Every tool result has the same shape, so the AI always knows what happened:

```json
{
  "ok": true,
  "facts": { ... },            // numbers and names the AI may use in its own words
  "sent": ["rooms_ok", "rooms_plain", "glimpse", "photos:6", "ask_contact"],
  "next_step": "ask_contact",
  "note": "Do not repeat what was sent. Add one line only if the guest asked something else."
}
```

`sent` = messages the tool already queued in the outbox (owner's exact texts / photos). The AI must not write them again.
Every tool that sends a fixed text takes `language` and picks the matching `i18n.py` variant.

### 3.1 `update_enquiry`  — the main tool (replaces check_availability, change_enquiry, prepare_booking, ask_location, ask_stay_details)

The AI passes the **whole stay as it understands it now**, not a diff. Code compares with the saved state.

```json
{
  "place": "string|null",
  "check_in": "YYYY-MM-DD|null",
  "check_out": "YYYY-MM-DD|null",
  "nights": "int|null",
  "adults": "int|null — adults above 6 years only (children are not asked or counted)",
  "rooms": "int|null — only if the guest said a number",
  "room_picks": [{"room_type": "string", "room_number": "string", "count": "int"}],
  "exclude_room_types": ["string"],
  "extra_beds": "int|null — ONLY the guest's answer to the extra-bed question (a number, 0 = no) or a bed they ask for",
  "language": "en|manglish|tanglish|hinglish"
}
```

What code does, in order:
1. Place unknown → sends `ask_location`. Place switched off → sends `closed_place`.
2. Dates/persons missing → sends `form` with only the missing lines.
3. Date already passed → sends `past_date`. Month far in the past → next year (code decides, not the AI).
4. Everything known → room check with today's rules (2 adults per room by default, up to 3 only when the guest chose fewer
   rooms, never more; priority, picks).
   - A room will hold 3 adults (and the resort offers extra beds) → sends `ask_extra_bed` ("Do you need an extra bed?")
     once instead of `ask_contact` and returns `next_step: ask_extra_bed`. `create_booking` waits (summary and link only
     after the answer). The answer comes back as `update_enquiry(extra_beds=N)` (0 = no): yes → the bed is added and charged
     per bed per night, no → nothing is added. Never asked when every room has 2 adults or fewer.
   - Owner, 2026-11-25: exactly 1 room holds 3 adults → the yes/no question; 2 or more (`rooms_with_three`) → the fixed text
     `ask_extra_beds` ("How many extra beds do you need?", all language variants in i18n.py) and the answer is a number from 0 up
     to that count. More is capped and `extra_beds_max` says the maximum. `next_step` stays `ask_extra_bed`.
   - Available and stay is new/changed → sends `rooms_ok` → rooms sentence → **photo flow step 2** → `ask_contact`.
   - Changed after a link was sent → closes old link, new summary + new link.
   - Not available → `rooms_none` + nearest free dates.
   - A picked room not free → `room_not_free` / `room_pick_no`.
5. Returns `facts` (room plan, persons, nights; prices only inside `facts.price`, which the AI may use only if asked).

### 3.2 `get_price(room_type?: string, language)`
Code price for the checked stay (or that room type on the same stay). Sends `price_answer` / `price_night`. Never switches the room.
Stay not checked → returns `next_step: ask_details` (AI then calls `update_enquiry`).

### 3.3 `send_photos(kind, room_type?, language)`
`kind`: `room_type` | `resort` | `other_room` | `all_rooms` | `default` | `more`.
The AI only says WHAT the guest asked for. `photos_plan.py` applies the owner's rules 1–8 (never twice, labels before room photos, only free rooms when dates known, location first if no resort, open step repeated after). No regex gate.

### 3.4 `send_map(language)`
Sends the resort's map/location pin.

### 3.5 `create_booking(guest_name, mobile, language)`
`mobile: "same"` = the WhatsApp number. Empty name allowed. Sends summary + `pay` link. Fails → `pay_failed_link`.

### 3.6 `check_payment(language)`
Asks the provider about the guest's own link (15 s throttle).
Paid → normal confirmation flow runs (code). Not paid → sends `pay_wait`. No link → returns `next_step`.

### 3.7 `send_payment_link(reason, language)`
`reason`: `lost` | `hold` | `not_paid_yet`. Sends `pay` or `hold_pay` (exact owner text). Only while the hold is live; hold expired → returns `hold_expired` and the AI offers to check again.

### 3.8 `my_bookings()`
Bookings, status, amounts, `if_cancelled_today`. Read only.

### 3.9 `change_name(name)`

### 3.10 `talk_to_owner(reason, language)`
Staff alert with a summary. Sends `handoff` (owner numbers). The agent keeps answering after.

### 3.11 `recall_old_chat(query)`
Read only.

### Things that are NOT tools any more (they become prompt rules)
- hold / pay later → `send_payment_link(reason="hold")`
- smaller advance / cash / pay at property → prompt rule + `talk_to_owner`
- call me → `talk_to_owner`
- greeting → prompt rule (first hi = exact greet text; mid-chat hi = short recap from CURRENT ENQUIRY + open step)
- plain "ok" → prompt rule

### Swipe-reply
`turn.py` adds a line before the guest message:
`[Guest swiped-replied to the photo of Room 108 — Beach View Standard]`
The AI then acts on THAT room (pick → `update_enquiry` with `room_picks`, price → `get_price`).

---

## 4. Message order in one turn

1. AI's own line (if any) — e.g. the answer to "is there a pool?"
2. Tool messages, in the order the tools queued them
3. Never-silent: if nothing went out, send the open step for `next_step`

---

## 5. System prompt (short version)

Keep the long rule list from the handoff doc, section 8, as the **reference**, but the live prompt should be about this size. The flow details now live in tool results (`next_step`, `note`), not in the prompt.

```
You are the WhatsApp front-desk person of {brand}, a resort in Kerala. You read every guest message yourself and
understand it in any words, spelling or language, including voice notes turned into text. Think about what THIS
guest needs now, then call tools for any fact or action.

TRUTH
- Rooms, availability, prices, totals, advance, links and payment status come ONLY from tool results of this turn
  or FACTS. Never calculate, guess or promise. Before the payment is confirmed by the system, never say booked,
  confirmed or reserved.
- Tools already send the owner's exact messages and photos (listed in "sent"). Never rewrite or repeat them.
- If you do not know or cannot do something: talk_to_owner.

BOOKING
- Persons are ADULTS above 6 years only: never ask about or count children.
- Extra bed: never offer it yourself. When a tool's next_step is ask_extra_bed, the question "Do you need an extra bed?"
  was already sent ("How many extra beds do you need?" when two or more rooms hold 3 adults): pass the guest's answer to
  update_enquiry as extra_beds (a number; 0 for no). An extra bed the
  guest asks for on their own: update_enquiry with extra_beds.
- When the guest says anything about their stay (place, dates, nights, persons, rooms, a room type or number, "one
  more room", "make it 5", "change to jan 2-5", "no cottage"): call update_enquiry with the FULL stay as you now
  understand it (CURRENT ENQUIRY + what the guest just said). The tool asks for anything missing.
- Name and mobile given → create_booking at once.
- Guest says paid / done / sends a screenshot → check_payment. Wants to hold or pay later → send_payment_link(hold).
- Price asked → get_price. Photos asked → send_photos with the kind they asked for. Location → send_map.
- After a tool, follow its next_step. Add your own line only if the guest also asked something else.

HOW YOU TALK
- Warm, polite, very simple words. One or two short lines. Answer only what was asked.
- First "hi" of a conversation: exactly "Hi, How may I assist you?". Later "hi": short reminder of their
  enquiry + the open step.
- "sir"/"madam" once at the start only. Never the guest's name. No "anything else?", no sales talk.
- Reply language = the guest's latest message: English → English, Malayalam → Manglish, Tamil → Tanglish,
  Hindi → Hinglish. Always English letters. Pass the same language to every tool.
- Every real message gets an answer. Only emoji/reactions/stickers get none.
- Questions about the resort's own rules/services: only from FACTS, else talk_to_owner. Places nearby, weather,
  how to reach: you may answer from general knowledge. Anything unrelated: one polite line.
- Never mention tools, systems, AI or the payment provider. Never show booking ids.

{FACTS} {GUEST RECORDS} {CURRENT ENQUIRY} {SWIPE NOTE} {TODAY}
```

Owner-specific rules that don't fit above (discount ask once vs twice, 3+ questions → owner, cancellation charge
from `my_bookings`, new booking after a paid one) go in a short `OWNER RULES` block after this, one line each.

---

## 6. Guardrails (`guard.py`) — the only checks on the AI's own text

| # | check | on fail |
|---|---|---|
| 1 | every ₹ amount / date / room count in the AI's text appears in this turn's tool `facts`, FACTS, or the guest's messages | retry once, telling the AI which number is unsupported |
| 2 | "booked / confirmed / reserved" only if `paid` | retry once |
| 3 | no provider name, booking id, non-Latin script, old link, guest's name | strip |
| 4 | no promise words ("will contact you", "get back", "one moment") | retry once |
| 5 | text identical to one already sent in this chat | drop (allowed: link on request, new summary after a change) |
| 6 | after retry still bad | send `handoff` + staff alert |

Retry = feed the problem back to the model as a message ("Your reply said ₹5,000 but no tool gave that; fix it"),
not regex rewriting.
AI down → `ai_down` + one staff alert per 30 min.

Everything else in today's post-filters (`_code_facts_only`, `no_unasked_prices`, `_own_dates_ask`,
`_negotiation_guard`, `_no_repeat_ask`, `CLOSING`, `ROOM_FACT`, `_ease_sir`, ...) is NOT ported.

---

## 7. Evals

Format in `tests/eval/agent_scenarios.yaml`:

```yaml
- id: manglish_dates_free_text
  turns:
    - guest: "kanthaloor 12 muthal 14 vare 4 per undu"
      expect_tools: [update_enquiry]
      expect_args: {check_in: "2026-12-12", check_out: "2026-12-14", adults: 4}
      expect_sent: [rooms_ok, rooms_plain, ask_contact]
      must_not: ["confirmed", "₹"]
- id: paid_screenshot
  turns:
    - guest: "[image]"   # with link open
      expect_tools: [check_payment]
```

- Hard checks: tools called, arguments, `sent` keys, forbidden words.
- Soft check: an AI judge scores tone (short, polite, right language) 1–5; fail below 4.
- Run every scenario 3 times (models vary). Pass = 3/3 for hard checks.
- Sources: every real failed chat (handoff 5c) becomes a scenario. Aim for 60+ before switching.

---

## 8. Build steps (paste one at a time into Claude in VS Code)

Each step: one commit `Step AI-n: ...`, run `pytest -q tests/resort` + the agent evals, stop and show results.

**AI-0 — rules file.** Add the section 0 rules to `CLAUDE.md`. Add this file as `docs/AI_AGENT_DESIGN.md`.

**AI-1 — skeleton + state.** Create `app/resort/agent/` with `state.py` (Enquiry + load/save + `render()`), empty
`turn.py`, `outbox.py` with a dry-run mode. Add `AGENT_MODE` setting (`old` default). Unit tests for state render.

**AI-2 — tools.** `tools.py` with the 11 schemas and handlers that call the existing functions in `availability.py`,
`payments.py`, `paying.py`, `i18n.py`. Move the photo plan into `photos_plan.py` unchanged except: remove the regex
gate, take `kind` from the tool call. Unit-test each tool without the LLM (call the handler, check state + outbox).

**AI-3 — prompt + loop.** `prompt.py` (section 5) and `turn.py` (context, LLM ⇄ tools, max 6 rounds, model from
`LLM_CHAIN`). Swipe-reply note. Language passed through.

**AI-4 — gate + guard + outbox.** `gate.py` (dedupe, voice, STOP/START, restart, pause, lock — reuse today's code).
`guard.py` (section 6, with retry). Outbox order + never-silent + no-duplicate.

**AI-5 — evals.** `tests/eval/agent_scenarios.yaml` + runner `scripts/eval_agent.py` (3 runs each, judge for tone).
Start with the happy path + every case in handoff section 10 + the real failed chats.

**AI-6 — owner test on a test number.** (Shadow mode is dropped: there is no live traffic.) `AGENT_MODE=agent` on a
test WhatsApp number; the owner chats with it like a guest. Every failed chat becomes an eval scenario and is fixed
with a general rule.

**AI-7 — switch.** When the evals pass and the owner's test chats look good: `AGENT_MODE=agent` for one resort first,
then all.

**AI-8 — cleanup (later).** After 2 weeks stable, delete the old `_dispatch` handlers, `_stay_in_code` and the
post-filters. Update `tests/resort` to test behaviour (tool calls + sent texts), not the old regex paths.

---

## 9. Still open (needed from the owner / you)

1. **Owner:** which messages must be word-for-word? Proposed: greet, ask_location, form, ask_contact, rooms_ok/none,
   rooms sentence, summary, pay/hold_pay, pay_wait, confirmation, handoff. Everything else = AI's own words.
2. **Real failed chats** (10–20) → first eval scenarios.
3. **Model names in `LLM_CHAIN`** → the agent needs strong tool calling and good Manglish. Use temperature ≤ 0.3.
