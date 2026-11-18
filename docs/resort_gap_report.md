# Resort bot: gap report (Step 1)

130 scenarios from `tests/eval/resort_scenarios.yaml` (the file header says 124; it holds 130), checked against the code on 2026-11-18 (app/resort/chat.py, flow.py, availability.py, db.py, i18n.py, invoice_pdf.py, dashboard).

**Status:** SUPPORTED = the code does it today. PARTIAL = some of it, or only by the AI without a code guard. MISSING = not built. CONFLICT = the scenario asks for something an owner rule from this week forbids (needs your decision).

## Counts

| Status | Count |
|---|---|
| SUPPORTED | 41 |
| PARTIAL | 45 |
| MISSING | 39 |
| CONFLICT | 5 |

| Category | Supported | Partial | Missing | Conflict |
|---|---|---|---|---|
| booking | 3 | 6 | 0 | 1 |
| modify_guests | 0 | 1 | 14 | 0 |
| modify_dates | 1 | 1 | 10 | 0 |
| cancel | 1 | 6 | 1 | 0 |
| payment | 3 | 3 | 1 | 1 |
| availability | 2 | 6 | 0 | 0 |
| language | 11 | 3 | 0 | 1 |
| voice | 5 | 4 | 1 | 0 |
| nearby | 0 | 0 | 6 | 0 |
| faq | 3 | 4 | 0 | 1 |
| edge | 6 | 4 | 0 | 0 |
| handoff | 2 | 3 | 1 | 0 |
| optout | 2 | 0 | 0 | 0 |
| identity | 1 | 2 | 1 | 0 |
| reminder | 1 | 0 | 0 | 1 |
| flow | 0 | 2 | 4 | 0 |

## Top 10 gaps

| # | Gap | Scenarios | Notes |
|---|---|---|---|
| 1 | Booking changes by the bot (add/remove guests, change dates/room, extend/shorten) | B01-B15, C01-C12, H10, N01-N04 | Today every change goes to the owner (owner rule 2026-11-18). Needs tools + summary -> YES -> save, and an owner decision to let the bot change bookings. |
| 2 | Cancellation by the bot, incl. one room of several | D01-D08, B06, N02 | Refund tiers exist (dashboard), but the bot only points to the owner. Needs cancel_booking with policy refund + confirm step (owner decision). |
| 3 | Nearby places from owner data | I01-I06, N05 | No data at all; today the AI may answer from general knowledge. Needs nearby_places config + dashboard page + tool + hard rule. |
| 4 | Talk-to-owner handoff (pause + staff summary) | L01, L02, K06, D05, F03, E06 | Only owner numbers are given; no pause/summary for guest requests; no complaint or group rule. |
| 5 | Relative dates in code (naale, mattannal, tonight, this weekend, '25th', '20 to 22') | A04, A05, A08, A09, C09, F06, H07 | parse_stay only reads explicit dates; these rely on the AI. |
| 6 | Child & extra-person policy data (age rules, charges) | A06, B02, B10, N01, N06 | Only max_children per room; no age bands or charges. |
| 7 | Alternatives when full (nearest free dates) | F01, B09, N06 | Only 'Sorry, rooms are not available'. |
| 8 | Language lock & unsupported languages | G13, G14, G15, N03 | No 'English please' lock; Hindi is supported today (conflicts with G13). |
| 9 | Emergency handling | L08 | No rule, no emergency numbers in data. |
| 10 | Guest vs booker identity & guest names | M01, M02, M06, B07, B08 | Only one name/number per booking. |

## Conflicts with owner rules (decide before building)

| Id | Today / owner rule |
|---|---|
| A01 | Bot asks location, then the form, and offers ONE room by priority with the price in the glimpse (owner rule). Scenario wants room options with prices. |
| E02 | Owner rule: images are not read or answered; payments are checked with Razorpay instead; no staff alert for screenshots. |
| G13 | Hindi is SUPPORTED today (Hinglish replies). Scenario wants Hindi unsupported. |
| J07 | Owner rule: photos only after availability is checked (with the glimpse); before that the next step is sent. |
| M04 | Owner rule: the day-before message is only 'Please bring your ID proof while coming.' (no check-in time / location). |

## Every scenario

| Id | Category | Status | What's missing / note |
|---|---|---|---|
| A01 | booking | CONFLICT | Bot asks location, then the form, and offers ONE room by priority with the price in the glimpse (owner rule). Scenario wants room options with prices. |
| A02 | booking | SUPPORTED | Form (Check in / Check out / Persons) in one message, after the owner's location question. Manglish detected. |
| A03 | booking | SUPPORTED | Tanglish detected; check-out missing -> partial form; season price comes from rate rules in the glimpse. |
| A04 | booking | PARTIAL | 'naale' is resolved only by the AI (no code parser for naale/mattannal) and not read back; summary has no booking id (client summary format omits it). |
| A05 | booking | PARTIAL | 'this weekend' resolved only by the AI; strict-form guard may ask the full form instead of confirming Sat-Sun. |
| A06 | booking | PARTIAL | Children counted against max_children, but no child-age policy or child/extra-bed charge data per age. |
| A07 | booking | PARTIAL | prepare_booking honours a named room type, but create falls back to the top-priority room if the asked one is not free (no 'not available' message). |
| A08 | booking | PARTIAL | Same-day booking works; no late-arrival / night check-in policy data. |
| A09 | booking | PARTIAL | Past dates rolled to next year in code; '25th' with no month is left to the AI, no explicit 'Dec 25?' question. |
| A10 | booking | SUPPORTED | check_dates() rejects check-out before check-in; no booking. |
| B01 | modify_guests | MISSING | No modify-booking tool; change requests go to the owner (+ staff alert). |
| B02 | modify_guests | MISSING | No modify tool; no extra-person charge data. |
| B03 | modify_guests | MISSING | No modify tool. |
| B04 | modify_guests | MISSING | No modify / partial-room cancel tool. |
| B05 | modify_guests | MISSING | No modify tool. |
| B06 | modify_guests | MISSING | No bot cancellation; owner handles cancels. |
| B07 | modify_guests | MISSING | Guest names per booking are not stored (only the booker's name). |
| B08 | modify_guests | MISSING | Guest names per booking are not stored. |
| B09 | modify_guests | MISSING | No modify tool; no alternatives offered. |
| B10 | modify_guests | MISSING | No modify tool; no per-age child rules. |
| B11 | modify_guests | MISSING | No 'which booking?' logic for modifications. |
| B12 | modify_guests | MISSING | No balance-for-extra-guests / top-up payment link. |
| B13 | modify_guests | PARTIAL | Records show the stay as past; AI offers a new booking, but no explicit 'cannot modify' rule. |
| B14 | modify_guests | MISSING | Room-level guest allocation is not modelled. |
| B15 | modify_guests | MISSING | No modify tool. |
| C01 | modify_dates | MISSING | No change_booking_dates tool; goes to the owner. |
| C02 | modify_dates | MISSING | No extend-stay tool. |
| C03 | modify_dates | MISSING | No shorten-stay tool / shortening policy. |
| C04 | modify_dates | MISSING | No change_room_type tool. |
| C05 | modify_dates | MISSING | No downgrade tool / policy. |
| C06 | modify_dates | MISSING | No same-day change policy data. |
| C07 | modify_dates | SUPPORTED | Early check-in time + charge from dashboard data; only if asked. |
| C08 | modify_dates | PARTIAL | No late check-out policy/charge field. |
| C09 | modify_dates | MISSING | No change flow; '20 to 22' with no month not read back. |
| C10 | modify_dates | MISSING | No change flow (Manglish detection fine). |
| C11 | modify_dates | MISSING | No change flow (Tanglish detection fine). |
| C12 | modify_dates | MISSING | Per-room dates not modelled (one booking = one date range). |
| D01 | cancel | PARTIAL | Bot points to the owner + staff alert (owner rule 2026-11-18); dashboard cancel suggests the refund tier. Bot does not cancel itself. |
| D02 | cancel | PARTIAL | Refund tiers exist (cancel_policy) but only the dashboard uses them; bot sends to owner. |
| D03 | cancel | PARTIAL | No bot cancel, so nothing changes; no explicit 'nothing changed' confirmation. |
| D04 | cancel | PARTIAL | Policies text only; no refund timeline field; owner numbers given. |
| D05 | cancel | SUPPORTED | Bot never grants exceptions; points to the owner. |
| D06 | cancel | MISSING | No partial (per-room) cancellation. |
| D07 | cancel | PARTIAL | Tanglish detected; cancel -> owner (no confirm step by bot). |
| D08 | cancel | PARTIAL | 'booking venda' may not match CHANGE_ASK (cancel words list); owner route. |
| E01 | payment | SUPPORTED | Advance amount from data; never the percent (owner rule). |
| E02 | payment | CONFLICT | Owner rule: images are not read or answered; payments are checked with Razorpay instead; no staff alert for screenshots. |
| E03 | payment | SUPPORTED | Advance online, balance at check-in, from data. |
| E04 | payment | MISSING | No UPI/GPay details field; payments only via Razorpay link. |
| E05 | payment | PARTIAL | Negotiation rules in the prompt (no invented discounts); no 'discount rules' data. |
| E06 | payment | PARTIAL | Negotiation rules; no explicit handoff for a counter-offer. |
| E07 | payment | SUPPORTED | gst_pct from data; invoice splits CGST/SGST. |
| E08 | payment | PARTIAL | Expired hold: send_payment_link refuses; AI should re-check availability, not enforced in code. |
| F01 | availability | PARTIAL | 'Sorry, rooms are not available' sent; no nearest free dates suggested. |
| F02 | availability | SUPPORTED | create_hold is one transaction; stress test: 100 guests, 0 oversold; loser told 'not available'. |
| F03 | availability | PARTIAL | No group-booking threshold / handoff rule. |
| F04 | availability | PARTIAL | room_options splits a party across rooms of ONE type; no mixed-type combinations; no hand-off for big groups. |
| F05 | availability | PARTIAL | Inactive room types are never offered; no per-date room blocking by the owner. |
| F06 | availability | PARTIAL | 'now' -> today only via the AI. |
| F07 | availability | PARTIAL | MAX_NIGHTS = 30 enforced; no monthly rate / long-stay rule. |
| F08 | availability | SUPPORTED | Dashboard bookings block rooms the same way (add_manual_booking -> create_hold). |
| G01 | language | SUPPORTED | English detected. |
| G02 | language | SUPPORTED | Malayalam script -> Manglish text (english_letters_only guard). |
| G03 | language | SUPPORTED | Manglish detected. |
| G04 | language | SUPPORTED | Tamil script -> Tanglish text. |
| G05 | language | SUPPORTED | TANGLISH_WORDS (venum, enakku...) beat Manglish. |
| G06 | language | SUPPORTED | Re-detected per message. |
| G07 | language | SUPPORTED | Re-detected per message. |
| G08 | language | PARTIAL | Tanglish -> Manglish switch on 'pool undo?' depends on word lists; a Tamil guest's short msg is kept Tanglish by design. |
| G09 | language | SUPPORTED | Neutral messages keep the last language. |
| G10 | language | SUPPORTED | Greeting is the fixed English line. |
| G11 | language | SUPPORTED | Mixed -> Manglish. |
| G12 | language | SUPPORTED | Tanglish (saar/irukka/evlo). |
| G13 | language | CONFLICT | Hindi is SUPPORTED today (Hinglish replies). Scenario wants Hindi unsupported. |
| G14 | language | PARTIAL | No explicit language lock; 'English please' works for that message, later neutral msgs keep English, but a Manglish word later switches back. |
| G15 | language | PARTIAL | No explicit language lock. |
| H01 | voice | SUPPORTED | Sarvam STT; text reply in Manglish; resort bot has no voice-out. |
| H02 | voice | SUPPORTED | STT; text reply in Tanglish. |
| H03 | voice | SUPPORTED | STT; English text reply. |
| H04 | voice | SUPPORTED | Text replies only. |
| H05 | voice | PARTIAL | Unclear-audio check exists; reply language for an unclear note defaults to the last language, not the STT guess. |
| H06 | voice | SUPPORTED | Long notes cut into 28 s pieces (Sarvam 30 s limit). |
| H07 | voice | PARTIAL | Self-correction ('alla alla, mattannal') only via the AI; code parser does not know naale/mattannal. |
| H08 | voice | PARTIAL | Number words in transcripts are not parsed by code; AI handles them. |
| H09 | voice | PARTIAL | Mixed-language notes: language from STT language_code + text; not tested. |
| H10 | voice | MISSING | No modify tool. |
| I01 | nearby | MISSING | No nearby_places data; prompt lets the AI answer 'places nearby' from general knowledge (risk of invented places). |
| I02 | nearby | MISSING | No nearby_places data. |
| I03 | nearby | MISSING | No nearby_places data. |
| I04 | nearby | MISSING | No data; AI may guess a distance. |
| I05 | nearby | MISSING | No transport/services data. |
| I06 | nearby | MISSING | No data (resort has no restaurant; 'Swiggy/Zomato' only in the glimpse text). |
| J01 | faq | PARTIAL | Only the free-text 'policies' field; no structured pets rule. |
| J02 | faq | PARTIAL | Policies free text. |
| J03 | faq | PARTIAL | Policies free text. |
| J04 | faq | SUPPORTED | 'Please bring your ID proof while coming.' (owner rule). |
| J05 | faq | SUPPORTED | meal_plan from data (per property, not per room). |
| J06 | faq | SUPPORTED | Owner's map link. |
| J07 | faq | CONFLICT | Owner rule: photos only after availability is checked (with the glimpse); before that the next step is sent. |
| J08 | faq | PARTIAL | Free text / handoff by numbers; no structured activities data. |
| K01 | edge | SUPPORTED | Never discounts on its own; negotiation rules. |
| K02 | edge | SUPPORTED | my_bookings is per WhatsApp number only. |
| K03 | edge | PARTIAL | Gibberish goes to the AI; may get the location question via safety net or a short reply; not tested. |
| K04 | edge | SUPPORTED | is_ack: emoji is an answer only when something is pending. |
| K05 | edge | SUPPORTED | Webhook dedupe by message id (mark_event_processed). |
| K06 | edge | PARTIAL | No complaint -> owner handoff rule; AI apologises. |
| K07 | edge | SUPPORTED | adults < 1 rejected by check_availability. |
| K08 | edge | PARTIAL | Records + prompt rule 'You have a booking on ... Is this a new one?' for a second booking. |
| K09 | edge | SUPPORTED | Jinja autoescape in the dashboard. |
| K10 | edge | PARTIAL | No explicit 'are you a bot' rule; owner numbers on request. |
| L01 | handoff | PARTIAL | Bot gives owner numbers; no pause + staff summary on a 'talk to owner' request (handoff() exists for system errors only). |
| L02 | handoff | PARTIAL | Same as L01. |
| L03 | handoff | SUPPORTED | AUTO_RESUME 24 h after a human takes over. |
| L04 | handoff | PARTIAL | Pause happens when staff reply from the phone; no 'RESUME <number>' staff command in the resort bot. |
| L05 | optout | SUPPORTED | STOP with one confirmation; opted-out guests get no messages. |
| L06 | optout | SUPPORTED | START re-subscribes. |
| L07 | handoff | SUPPORTED | AI down -> fixed apology + staff alert. |
| L08 | handoff | MISSING | No emergency rule / numbers. |
| M01 | identity | MISSING | No separate guest-vs-booker contact. |
| M02 | identity | PARTIAL | Only own-number bookings visible; no verification/handoff rule. |
| M03 | identity | SUPPORTED | my_bookings shows only this number's bookings. |
| M04 | reminder | CONFLICT | Owner rule: the day-before message is only 'Please bring your ID proof while coming.' (no check-in time / location). |
| M05 | reminder | SUPPORTED | Feedback after check-out once; STOP respected. |
| M06 | identity | PARTIAL | No access to other numbers; no explicit verification/handoff rule. |
| N01 | flow | PARTIAL | Booking flow works in Tanglish; adding 2 friends after booking needs the modify tool. |
| N02 | flow | MISSING | Needs modify + partial cancel. |
| N03 | flow | MISSING | Needs language lock + modify. |
| N04 | flow | MISSING | Needs change/upgrade tools. |
| N05 | flow | MISSING | Needs nearby_places. |
| N06 | flow | PARTIAL | Manglish + child counting fine; no alternative-date suggestion when full. |

## Owner decisions (2026-11-18)

| Id | Decision |
|---|---|
| A01 | Keep the rule: one room by priority, price only in the glimpse. Scenario expectation changes to match. |
| E02 | Keep the rule: images are not read or answered. |
| G13 | **Change:** Hindi is no longer supported. Hindi script (and Hinglish) gets a polite English reply listing English, Malayalam and Tamil. Build in Step 3. |
| J07 | Keep the rule: photos only after availability is checked. |
| M04 | Keep the rule: the day-before message is only "Please bring your ID proof while coming." |

The eval (Step 2) must judge A01, E02, J07 and M04 by the owner's rules, not by the scenario text.
