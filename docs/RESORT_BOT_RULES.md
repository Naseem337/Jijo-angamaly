# Resort bot: the owner's current rules

Only the rules in force today (2026-11-19). Older, replaced rules were deleted; git history keeps them.
The rules live in our own prompt and code (`app/resort/chat.py`, `flow.py`, `i18n.py`), not in any one model, so
changing `LLM_CHAIN` keeps them. After a model change run `.venv\Scripts\python.exe scripts\live_eval_resort.py`.
Checks: `.venv\Scripts\python.exe -m pytest -q tests/resort` and `scripts/resort_eval.py --mock` (130 scenarios).
Main tests: `tests/resort/test_owner_flow.py`, `test_mixed_and_today.py`, `test_room_photos.py`, `test_never_silent.py`.

## 1. Greeting
- Any greeting ("hi", "hello", "Hi bro", "ഹായ്"), at the start or in the middle of a chat, gets exactly
  "Hi, How may I assist you?" (code, no AI call).

## 2. Location
- A room / resort / booking request with no place named gets "Sure, Kindly confirm the Location 📍\n<towns> ?"
  (`ask_location`; the towns come from the properties in the dashboard, so a new property is added automatically).
- A switched-off resort (Kanthalloor today) gets "Sorry, <town> is currently not available."
- Naming the place ("rooms available in allepy?") skips the question. Misspellings are understood (`close_word`,
  `PLACE_ALIASES`, `NATIVE_PLACES`): allepy / alappuzha = Alleppey. The bot never corrects the spelling.

## 3. The form, dates and persons
- Resort known: "Please share these details:\n\nCheck in :\nCheck out :\nNumber of persons :". When part is given,
  only the missing lines are sent.
- Dates and persons are read in code (`parse_stay`), in any language or script: today / tomorrow / innu / naale /
  aaj, "for a day", "2 disam", "3 പേരാണ്", "oru kutti" (child), "25/12 2 nights 4 pax". The NEWEST date wins; only
  details given since the last full form count. Nothing is assumed (never 1 person, never 1 night).
- The AI never asks for dates, nights or persons in its own words: such an ask is replaced by the form in code.

## 4. Rooms available
- Complete details, in this order, once per set of dates: "Yes, rooms are available for your dates." -> the glimpse
  (`Property.intro`, `{price}` = the real price for their dates) -> ALL general photos -> ALL photos of ONLY the
  one room offered -> "Please share your name and mobile number to proceed."
- No photo limits anywhere (`MAX_GENERAL_PHOTOS` / `MAX_ROOM_PHOTOS` = None). No photo is sent twice in a conversation.
- Never "fully booked" without a real room check. Not available: "Sorry, rooms are not available for your dates."

## 5. Room priority and mixed rooms
- Each room type has a Priority (1 = first). The room offered = the top-priority room type that is free on the
  guest's dates and fits the party. The bot never asks which room type and never lists room types or prices unasked.
- A group the top room cannot take alone gets its free rooms first, then the next room type (4 guests, 1 Basic left
  -> 1 Basic + 1 Standard): one summary, one payment link, one confirmation. Never more rooms than one room type would
  need (10 guests -> 3 Family Suites). Two room types are shown only for such a split group.
- Room numbers per room type are in priority order; a paid booking gets the highest-priority free room numbers
  (`availability.allocate_rooms`).

## 6. Photo requests (code, `_photo_plan`)
- A named free room -> its photos. A named full room -> "Sorry, <room> is not free on your dates."
- "Other rooms" / "available rooms" -> the other FREE rooms by priority (room name on the first photo), else
  "Sorry, no other room is free on your dates."
- Beach / resort / outside photos -> the general photos only. Plain "photos" -> the offered room.
- Everything already shown -> "All the photos are sent above." After photos the open booking step is sent again.
- The AI's `send_photos` tool follows the same rules; the AI only calls it when the guest asks for photos.

## 7. Prices
- A price only when the guest asks, and only from a tool in that turn (`no_unasked_prices`, `_tool_prices_only`).
- Never seasons, peak dates or "rates changed". "Any offers?" is answered with the price.
- Asked to reduce: convince with the real good points (never "no discount"); if they keep pushing, the owner's numbers.

## 8. Name, number and payment
- Name + mobile ("same number" = the WhatsApp number; no name -> the WhatsApp profile name, else "Please share your
  name.") -> the enquiry summary ("Following are the glimpse of your enquiry," ... "Pay - X", no booking id, no
  percent) -> "Please pay the advance using this link:\n<url>".
- Before payment the bot never says booked or confirmed. A payment screenshot never confirms; only Razorpay does.

## 9. After payment
- "Following are the glimpse of your booking, ... *Booking Confirmed ✅*" (with the room numbers) -> the cancellation
  policy -> the stay info. Then the chat closes; the next message starts fresh.
- NO invoice is ever sent (the invoice feature was removed). No map line and no ID request after payment
  (`flow.ASK_ID_AFTER_PAYMENT = False`; the ID flow is kept for later).

## 10. How the bot talks
- Never the same message twice in a conversation (`BookingBot._text`); only a payment link the guest asks for again.
- Never silent on a real message (`_never_silent`): a request the AI leaves silent still gets the next fixed step.
- Very short replies: one line, questions max 12 words, no lead-ins; long replies get one "much shorter" rewrite
  (`MAX_CHAT_WORDS`). No reply to "ok", "thanks", "haha"; a final "ok" with nothing open gets "Is there anything
  else?" once after 10 s (`followup_s`).
- "sir" (or "madam") at most once per conversation (`_ease_sir`). Never bro, dear, chetta, chechi or their name.
- Resort topics only; stay questions (parking, food, pool, Wi-Fi) are answered from the FACTS.
- Guest images get no reply and are not read by the AI (`READ_IMAGES = False`).

## 11. Languages
- Every text is in English letters, always. The guest's LATEST message decides the reply language (`_language_note`):
  English -> English; Malayalam (script, Manglish or voice) -> Manglish; Tamil -> Tanglish; Hindi -> Hinglish.
- "ok", "2", emoji keep the language so far. "English please" / "Tamil la pesunga" lock a language until the guest
  asks for another or writes a long message clearly in another one.
- Fixed step messages follow the guest's language (i18n "mn" / "tn" / "hn"); the greeting, the location question and
  the form keep the owner's exact English; the summary and the confirmation are English.
- Native script is never sent (`english_letters_only`, `_latin_only`); a mixed-language reply gets one rewrite.
- Voice notes are transcribed and handled like text; the reply is always text.

## 12. Changes and cancellations
- The bot cannot change or cancel a booking. A change goes to the owner (alerted automatically). A cancellation:
  the bot states only the policy charge (`my_bookings` -> if_cancelled_today) and says the owner will confirm.
  Anything outside the policy -> `talk_to_owner`.

## 13. Memory and conversations
- A conversation ends after 24 h of silence (`SESSION_GAP`) or when a booking is confirmed / cancelled.
- The AI sees the last 20 real messages of the current conversation (`HISTORY`; a run of photos is one note), the
  guest's records (name, last bookings) every turn, and older chat only through `recall_old_chat`.
- Links in the shown chat are masked, so an old payment link is never copied; `send_payment_link` resends one only
  for a booking still on hold. Log rows like "[invoice]" or "[photos sent]" are never sent to a guest.
- When staff reply from the WhatsApp Business app (`smb_message_echoes`), the bot pauses for that guest (24 h).

## 14. Dashboard drives the bot
- Properties, room types, priorities, rates, season prices (shortest range wins), owner numbers, glimpse, meal plan,
  early check-in, policies, stay info and photos saved in the dashboard reach WhatsApp on the next message.
- Photos: one "Add photos" button; shrunk to JPEG, stored next to the database, public at /resort/media/<name>.
- Discount before payment: "Give discount" on a waiting booking recalculates, closes the old link, sends a new one.
- Accounting (folio, payments, receipts, collection report) stays in the dashboard; there is no invoice page.
