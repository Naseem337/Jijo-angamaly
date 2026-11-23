# Resort bot: the owner's current rules

> General rules (payment checks, polite and short talking, language, memory) are in [../common/RULES.md](../common/RULES.md); this file adds the resort-specific ones and wins where they differ.

Only the rules in force today (2026-11-22). Older, replaced rules were deleted; git history keeps them.
The rules live in our own prompt and code (`app/resort/chat.py`, `flow.py`, `i18n.py`), not in any one model, so
changing `LLM_CHAIN` keeps them. After a model change run `.venv\Scripts\python.exe scripts\live_eval_resort.py`.
Checks: `.venv\Scripts\python.exe -m pytest -q tests/resort` and `scripts/resort_eval.py` (mock scenarios).

## 0. How the AI and the code work together
- The CODE decides every fact and every booking step: which resort, dates, persons, rooms, extra beds, price,
  availability, photos, the enquiry summary and the payment link. It stores everything (guests, chats, bookings, each
  guest's progress).
- The AI only writes words: it answers the guest's questions and casual messages politely, in the guest's language.
  It stores nothing; every message it gets the instructions, the facts and the recent chat.
- Where both could speak, the code decides and the AI explains. Code filters remove any AI sentence that only the
  code may say (room counts, prices without a tool, its own location / dates / name questions, promises like "one
  moment" or "ready aakkam", how rooms are picked).
- Safety net: if nothing at all went out for a guest's message, the code sends the open step (never silence).

## 1. Greeting
- The first greeting ("hi", "hello", "ഹായ്") of a conversation gets exactly "Hi, How may I assist you?".
- A greeting in the middle of a chat (owner, 2026-11-22) never gets "How may I assist you?" again and is never left
  silent: "Hi. Your enquiry: Pozhiyoram, Alleppey, 13 Jan 2027 to 17 Jan 2027, 3 person(s), 1 room(s)." (or "Hi. Your
  booking at <resort>, <dates> is confirmed ✅.") and then the step still open (a short pointer if that step is just
  above). Nothing known yet: "Hi. Kindly confirm the Location 📍 ...".
- "restart" starts a fresh conversation (bookings and records stay).

## 2. Location
- A room / resort / booking request with no place named ("Rooms needed", "room venam") gets the owner's exact
  "Sure, Kindly confirm the Location 📍\n<towns> ?". Any location question the AI writes itself is replaced by this
  exact message.
- Once the resort is known (named, picked or used in a room check) the location is never asked again.
- A switched-off resort (Kanthalloor today) gets "Sorry, <town> is currently not available." The bot never suggests a
  resort that is not in the dashboard.
- Spellings are understood, also from voice notes: allepy, alappuzha, aalappee, alappi, ആലപ്പി, അലപ്പി = Alleppey.

## 3. The form, dates and persons
- Resort known: "Please share these details:\n\nCheck in :\nCheck out :\nNumber of persons :\nNumber of rooms:".
  When part is given, only the missing lines are sent.
- Dates, persons and rooms are read in code, in any language, script or voice note: "jan 1 to jan 4", "today",
  "naale", "number of persons 2" (no colon), "two persons", "moonu per", "5 പേരാണ്", "2 റൂം മതി", a bare "2." answering
  the form, "rooms 2". Nothing is assumed (never 1 person, never 1 night).
- A date that passed only days ago ("nov 20" typed on 21 Nov) gets "20 Nov has already passed. Please send your
  check-in and check-out dates again." A date months ago means next year.
- The AI never asks for dates or persons in its own words, and never asks the year or "you said N persons?".
- No limit on nights or on the number of guests: the real free rooms decide.

## 4. Rooms available
- Complete details, in this order: "Yes, rooms are available for your dates." -> the rooms sentence -> the glimpse
  (`Property.intro`) -> general photos -> each room's photos -> "Please share your name and mobile number to proceed."
- The rooms sentence always comes from the code after a room check: "For 4 persons: 2 room(s)." A group that needs
  an odd room gets both choices: "For 5 persons, 3 rooms are needed. Or 5 persons can stay in 2 room(s) with extra
  beds (...)". Only when the guest picks fewer rooms: "For 5 persons: 2 room(s) + 1 extra bed(s) (₹500 per extra bed
  per night)."
- A group bigger than every free room: "For your dates 6 room(s) are free, enough for up to 18 persons. Please tell
  me how many persons will come, or send other dates for all 60."
- Not available: "Sorry, rooms are not available for your dates." (with the nearest free dates when there are any).

## 5. Rooms, extra beds and changes
- 2 persons per room. Up to 3 per room (2 + 1 extra bed at the extra-bed rate) only when the guest asks for fewer
  rooms. Dashboard "max guests per room" = 3 means 2 + 1 extra bed.
- Too few rooms asked: the minimum is said and checked ("Max 3 persons per room ... at least 3 rooms"), no dead end.
- Everything the guest changes is re-checked in code, and the new rooms sentence goes:
  new persons ("7 ppl"), new rooms ("2 rooms mathi"), added rooms ("one more room", "1 room koode venam", "add a
  room"), new dates ("make it jan 2 to jan 5").
- "Shall I book 3 rooms?" -> "Yes" / "Ok" books 3 rooms (re-checked in code).
- Room priority: the top-priority free room type is offered first; a group it can't take gets its free rooms first,
  then the next room type (one summary, one payment link). The guest is never asked which room type and never told
  how rooms are picked (no "priority", no "the resort will assign").
- The guest may choose ANY room, at any time, in any words (owner, 2026-11-22): a room type ("cottage venam", "the
  2 bedroom one"), a room number ("room 103"), "the other room", "no standard room", or "what rooms do you have?"
  (the AI names the room types). The AI understands it and calls `change_enquiry`; the code re-checks with that room.
  The chosen room is remembered for the rest of the enquiry (also when persons or dates change) and forgotten after
  payment. If it cannot take the group on those dates: "Sorry, <room> is not free for N persons on your dates."
- Every change after the room check, before OR after the payment link (room type, room number, more / fewer rooms,
  persons, dates), uses the stay the bot already knows (memory, like a human): never the form again, never sent to
  the owner. After the link, a new summary and a new link go at once and the old link is closed. The guest's own
  open link never blocks their change. Only a change to a PAID booking goes to the owner.
- A count with a named room ("is there 2 rooms in kerala cottage", "I want 2 rooms in the cottage") is a pick of
  that room for that many rooms, checked in code (2026-11-22: the count was once read and the cottage ignored).
- Room numbers are not the guest's concern in chat (owner, 2026-11-22): "is there 2 rooms in X?" -> "Yes, 2 rooms
  available.", never "Room numbers 103 and 104." The AI says a room number only when the guest named one or asked for
  them; a code check rewrites, then drops, any other sentence with room numbers. Room numbers DO show on the photo
  captions (§6) and on the confirmed booking (§9), by the owner's choice.
- A count with the room name in between is read too: "2 cottage rooms venam", "randu cottage venam", "3 deluxe
  rooms". "2 bedroom cottage" is a room name, not a count.
- The AI is told the CURRENT ENQUIRY (resort, dates, persons, rooms, room type, link sent or not) on every message,
  and the room numbers of each room type (only to understand a guest who names one).

## 6. Photos
- No photo limit. No photo is sent twice in a conversation.
- Before every room's photos, a text with the room number and name: "*Room 108 - Beach View Standard Rooms*".
  The photos themselves have no caption (owner, 2026-11-22: keep it so).
- A swipe-reply to a room photo or to its "*Room 108 ...*" line is about THAT room (each photo's WhatsApp id is
  kept): "this one venam", "ithu mathi", "book this", "is this free?" pick it (checked in code on the stay's dates);
  "price?" gives that room's price; anything else ("nice view") is answered by the AI, which is told the room.
- Before the dates, room photos go freely (one room type after another, then the general photos), followed once by
  "To check which rooms are available, we need your check-in and check-out dates." and the form.
- After the dates: a named free room -> its photos; a named full room -> "Sorry, <room> is not free on your dates.";
  "other rooms" -> "Yes, <room> is also free for your dates..." then its photos, else "Sorry, no other room is free on
  your dates." (only said when dates were given). Everything shown -> "All the photos are sent above."

## 7. Prices
- "Price etreya", "how much", "rate?" once the stay is checked: the code sends the real price of that stay:
  "2 room(s), Beach View Standard Rooms + 1 extra bed(s), 22 Nov to 30 Nov 2026 (8 nights): total ₹52,000."
  The dates are never asked again.
- A rate question also gets the price per room per night ("Beach View Standard Room: ₹3,000 per room per night."),
  but only when every night of the stay costs the same; otherwise only the total (owner, 2026-11-22).
- A room named in the price question ("whats the cost for standard room") gets THAT room's price for the same stay.
  It is only a question, never a switch: no "should I change your booking?". This also works after the payment
  link has gone out, using the stay on that link; the booking and the link stay unchanged.
- Once the dates and persons are known (the room check, or the open payment link), the form is never sent again,
  not after "Yes", not after "same date" / "same persons" (live chat 2026-11-22).
- A price only when the guest asks, and only from the code. Never seasons, peak dates or "rates changed".
- Asked to reduce: the real good points (never "no discount"); if they keep pushing, the owner's numbers.

## 8. Name, number and payment
- Name + mobile ("same number" = the WhatsApp number) -> the enquiry summary ("Following are the glimpse of your
  enquiry," ... "Pay - X") -> "Please pay the advance using this link:\n<url>".
- Before payment the bot never says booked or confirmed. A payment screenshot or the guest's word never confirms; only the payment check does.
- HOLD (owner, 2026-11-23): a room is held only by paying the advance NOW. "hold the room", "reserve", "can I pay
  tomorrow / later" with the link out -> only "To hold the room, please pay the advance now using this link: <url>"
  (code, no AI, never an explanation of the hold or of the link expiring, never "paying tomorrow is fine"). Rooms
  checked but no link yet -> the open step (name and mobile). A hold request needs "hold" / "reserve" WITH an object
  ("hold the room", "reserve it", "hold cheyyamo", a lone "hold please"); "hold on", "block", "balance", "pay at the
  resort", "how long" are not hold requests. "Hold the cottage" with the link on another room = a room choice (new
  link); with the same room = the pay-now line.
- "ok" / "thanks" while the advance is unpaid (link out) -> "Please complete the advance payment to confirm your
  booking." (the link again if it is not the message just above). Never "see you soon" before payment.
  "ok paying now" / "paid" / "I haven't paid yet" -> the payment is checked (the guest's own link only, at most
  once every 15 s; never the guest's word). Paid -> the normal confirmation. Not paid -> "We have not received the
  payment yet. Once it goes through (usually within a minute or two) your booking is confirmed automatically; no
  need to pay again." "I haven't paid yet" / "not paid" -> the pay-now link. "will pay tomorrow" is NOT this: it
  gets the pay-now line.
- If the payment link can't be made: "We could not create the payment link just now. Please contact the owner
  directly: <numbers>." Staff are alerted and the bot keeps answering.

## 9. After payment
- "Following are the glimpse of your booking, ... *Booking Confirmed ✅*" (with the room numbers) -> the cancellation
  policy -> the stay info. Then the chat closes; the next message starts fresh.
- No invoice, no map line, no ID request after payment.

## 10. How the bot talks
- Every message gets an answer. Casual messages ("ok", "thanks a lot", "adipoli", "ok will see") get a polite reply
  in the AI's own words, then the booking step still open (for example the name and mobile ask). Any spelling of ok
  counts ("Oky", "okk", "oki"). Exception: an unpaid payment link (§8).
- Never the same message twice in a conversation. Allowed again: the code's rooms sentence after a change, the name
  ask after new photos, "rooms available" for new dates, a payment link the guest asks for, an answer asked a day later.
- Short answers (owner, 2026-11-23): ONE short line, two at most; answer only what was asked, no explanations,
  reasons or policies nobody asked for. One thing asked at a time.
- "sir" / "madam" only once, at the start of the chat; never bro, dear, chetta, chechi or the guest's name.
- Places to see, how to reach and the weather are answered from knowledge; "please contact the owner" (with the numbers) only for the resort's
  own facts that are not in the dashboard (pets, alcohol, couples ... can be added in the dashboard).
- A guest asking for the owner: the owner gets a summary; the bot keeps answering. The GUEST is told to contact the
  owner: "Thank you. Please contact the owner directly: <numbers>." (owner, 2026-11-23). Never "the owner / our team
  will contact you". The same line (with the numbers) for: a change to a paid booking, an AI outage, a payment link
  that failed, and a fact the dashboard does not have. In Manglish / Tanglish / Hinglish too.
- Too many questions at once (owner, 2026-11-23): when ONE message asks 3 or more separate questions about the resort,
  the AI does not answer them all; it says in one short line to contact the owner directly, with the numbers.
- Guest images: the AI looks at them (payment screenshot rules are in its prompt); this line used to say the opposite.

## 11. Languages
- Every text is in English letters. The guest's latest message decides the reply language: English -> English;
  Malayalam (script, Manglish or voice) -> Manglish; Tamil -> Tanglish; Hindi -> Hinglish. "ok", "2", emoji keep the
  language so far. "English please" locks a language.
- The greeting, the location question and the form keep the owner's exact English; the summary and confirmation are
  English. Voice notes of any length are transcribed and handled like text; the reply is text.

## 12. Changes and cancellations of a PAID booking
- (Changes before payment are made by the bot itself, see §5.) The bot cannot change or cancel a paid booking. A change goes to the owner (alerted). A cancellation: the bot states the
  policy charge and tells them to contact the owner directly to confirm.

## 13. Memory and conversations
- A conversation ends on "restart" or when a booking is confirmed / cancelled. After a pause of 24 h the guest's words
  decide: "Hi" or a new booking request starts afresh; anything else ("ok book it", "what was the price?") continues,
  and the AI is told how many days passed.
- The AI sees the last 20 real messages of the conversation, the guest's records, and older chat only on request.
  Old payment links are masked.

## 14. Pausing and limits
- The bot NEVER pauses itself and has no reply limit. A failed payment link, an owner request or many messages only
  alert staff.
- A chat is paused only by staff: the dashboard Pause button, a staff reply from the WhatsApp Business app, or a
  staff message sent from the dashboard. A staff pause ends after 24 h of staff silence or with Resume.
- No caps on guests, nights, photos, messages or voice-note length. New limits are added only when the owner decides.

## 15. Dashboard drives the bot
- Properties, room types, priorities, rates, season prices, extra-bed rate, owner numbers, glimpse, policies, stay
  info and photos saved in the dashboard reach WhatsApp on the next message.
- Accounting (folio, payments, receipts, collection report) stays in the dashboard.

## Waiting on the owner / go-live
- Razorpay: the test account hit its 30-link limit; live keys (or a new test account) are needed on Railway.
- Facts to add in the dashboard so the bot can answer them directly: pets, alcohol, couples.

- Several rooms of different types (owner chat, 2026-11-23): '1 standard + 1 cottage' or 'I need 109 and 103' is ONE
  change_enquiry with `room_picks` (one entry per room, type name or number); the code books exactly that mix if free
  and big enough, never replaces the first room, never says specific rooms cannot be booked. An 'ok' to the bot's own
  question is always followed by the open booking step (name and mobile), never a lone 'Great.'.
