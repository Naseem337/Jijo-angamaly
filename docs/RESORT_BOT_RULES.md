# Resort bot: rules the user gave

These rules are enforced by our own prompt and code (`app/resort/chat.py`, `flow.py`, `i18n.py`), not by any one model.
Changing `LLM_CHAIN` (for example to DeepSeek) keeps them. After changing the model, run
`.venv\Scripts\python.exe scripts\live_eval_resort.py` and read `data/eval/resort_<lang>.txt`.

## Booking flow (client, 2026-11-17; replaces the older order below where they differ)
1. "Hi" -> "Hi, How may I assist you?"
2. Wants a resort -> fixed (`ask_location` tool) "Sure, Kindly confirm the Location 📍 / Alleppey or Kanthalloor ?" (exact, client 2026-11-18; towns from the properties in the dashboard; a new property is added automatically). On every fixed step the AI's own line is not sent. If the AI asks which location in its own words (a question naming 2+ of our towns), the code sends this fixed question instead (`_own_location_question`).
3. Resort known -> the form (fixed, `ask_stay_details` tool): "Please share these details: Check in : / Check out : / Number of persons :"
4. Filled -> `check_availability`. Available -> fixed "Yes, rooms are available for your dates." -> the resort's glimpse
   (`Property.intro`, editable in the dashboard; `{price}` = the real per-night price for their dates; sent once) ->
   "Please share your name and mobile number to proceed." Not available -> the AI says so and suggests other dates.
5. Name + mobile -> `create_booking` -> "Following are the glimpse of your enquiry," summary (guest, mobile, dates as
   "15th Nov 26", early check-in line, nights, PAX, rooms, meal plan, Room Cost, extras, Total Cost, then "As a procedure
   you may need to pay an advance to block the room. Ie - <amount>. Balance can be paid during the time of check in.")
   -> "Please pay the advance using this link: <link>". No percent, no other payment talk.
- Early check-in only when the guest asks: `Property.early_checkin_time` / `early_checkin_rate` (per room), added to
  the total and the advance. Meal plan line from `Property.meal_plan` ("Stay Only").
- **Never the same message twice (client, 2026-11-17, strict):** `BookingBot._text` refuses any message already sent in
  the current conversation (6 h gap = new one). Only a payment link the guest asks for again may repeat.
- **Images get no reply (client, 2026-11-17):** a guest's photo (no ID request open) is kept in the chat as "[image]"
  and is not read by the AI. Razorpay is still asked about unpaid holds, so a real payment gets its normal
  confirmation. Switch back on with `BookingBot.READ_IMAGES = True` in flow.py. ID photos the bot asked for are still saved.
- A mid-chat "hi" is never left unanswered: if the AI stays silent, "Hi sir, how can I help you further?".

## Conversation
- Free-text chat like a human at the front desk. No menus, buttons or tap lists.
- Any plain greeting ("hi", "hello", "ഹായ്") gets exactly "Hi, How can I assist you?" (client, 2026-11-17; code, no AI call),
  but only at the START of a conversation. A "hi" in the middle of a chat (bot already replied, no 6 h gap) goes to the AI
  with the chat so far and carries on from where it left off (`_chatting_now`). Always polite, concise, to the point.
- **Swipe-replies (client, 2026-11-17):** when a guest swipes to reply to an earlier message, the AI is told which one
  (quoted text) and answers about it. Every chat row stores WhatsApp's message id (`chat.wamid`) for this.
- **Resort topics only (client, 2026-11-17):** anything off-topic gets one polite line: the bot helps only with resort bookings.
- **No mixed languages:** an English reply with Manglish words, or an English reply to a Manglish guest, gets one rewrite.
  The newest message decides the language; earlier quick messages count only when it is 1-2 words.
- **English in, English out (client, 2026-11-17).** The code reads the guest's language (`latin_style`): only real Manglish
  words make a message Manglish; place names, resort words, months and names never do. A message with nothing to go on
  ("Alleppey", "2 divasam") keeps the language so far. The fixed messages (payment, confirmation) follow the same
  decision; the AI's own language label can no longer override it.
- When they want a booking or ask about rooms without naming a resort, ask only: "Vibes Kanthalloor or Pozhiyoram Resort, Alleppey?"
  (names come from all properties, open or not). Vibes Kanthalloor has no online booking until its data is entered: if the
  guest picks it, the bot gives the owner's numbers in one line.
- **No essays (client, 2026-11-17):** answer only what was asked, usually one short line, at most 2-3. Asked about rooms:
  room types in short lines, then ask the dates. **Very simple (client, 2026-11-17):** easy everyday words, short
  sentences, no formal or sales phrases ("kindly", "nestled", "serene"); room lines like "Deluxe - ₹price/night"; the
  fixed payment and ID messages were shortened too. Code guard: more than 30 words outside room/price lines, list items and
  links (`chatty_words`, `MAX_CHAT_WORDS` in chat.py) gets one "much shorter" rewrite.
- Ask in order: check-in date (or nights), number of persons, number of rooms. Never assume persons, rooms or nights.
- Quote by the guest's dates. ~~If several room types are free, list each and let them choose.~~ Replaced by room priority (below).
- Before payment: send the booking summary (always English, one fact per line), ask for confirmation, then send the payment link.
- Third person: do not add an extra bed automatically (Rs 500), ask first.
- Messages that follow each other are one conversation. Use chat history ("avde" then "engane und" then "Alleppey" is one question).
- If a message looks cut off ("njan udheshikkunath oru ..."), wait for the rest; ask only if still unclear.
- Reply only when necessary. No reply to "ok", "thanks", "haha". Do not always have the last word.
- Do not send template-sounding lines ("send Hi to book again"). No expiry message.

## Language
- Detect the language, never ask: English, Malayalam, Manglish, Hindi, Hinglish, Tamil, Tanglish.
- ONE language per reply, the guest's own. Manglish gets Manglish (English letters, never Malayalam script). Hinglish gets Hinglish. English words like room, payment, check-in are fine inside.
- The booking summary is always English.

## Address
- Call the guest "sir" (madam only if clearly a woman), only now and then, not every message.
- Never bro, dear, da, chetta, chechi, machane, and never their name.

## Money
- Never quote a price from memory: only from the availability/booking tools. Code decides price, capacity and availability.
- Never mention price hikes, seasons, peak or high-season dates or that rates change. Give the total for their stay.
- "Any offers?" is a question, not a haggle. Answer with the price, never "no offer" or "I cannot reduce".
- Negotiation (guest asks for lower): be warm and CONVINCE them with the real good points of the place, twice. Never say "no discount", "no offer" or "cannot reduce". If they still push, give the owner numbers 9497861924 / 9995855789.

## Booking and payment
- Never say booked or done before payment. Say: complete the payment to confirm.
- Payment is fully in WhatsApp (Razorpay link: UPI, cards, net banking). Confirm only after payment is received.
- If a payment fails, tell the guest once.
- Cancellation: do not handle it, give the owner numbers.
- After confirmation (or on request) send the Maps link: https://maps.app.goo.gl/4x1rKZhq8wh2Pugw8
- ID proof and feedback are asked as plain chat (photo of each adult, or "show at check-in").

## Dashboard drives the bot (client, 2026-11-17)
- No AI training: every message the bot builds its instructions from the database, so properties, room types,
  rates, season prices, owner numbers, glimpse, meal plan, early check-in and photos saved in the dashboard reach
  WhatsApp on the next message. A new property is named in the location question; it is bookable once Active with a
  priced room type. The property page shows a "WhatsApp bot" checklist of what is still missing.
- **General vs room photos (client, 2026-11-17):** the property page's "General photos" (outside, beach, common areas) are sent AUTOMATICALLY to every guest who picks the property (first 6, owner's order): before the form, or before the glimpse if the guest skipped the form. Room photos (room type pages) go only when a guest asks to see the rooms (`send_photos`, room asked first, max 4). No photo is ever sent twice in a conversation.
- Photos: uploaded on the property and room pages (`app/resort/photos.py`), shrunk to JPEG with ffmpeg, stored next
  to the database (`/data/photos` on Railway), public at /resort/media/<name>. The bot's `send_photos` tool sends up
  to 4 (room type first), only when the guest asks for pictures, never one already sent in the conversation.
- A short answer to "Kindly confirm the Location" that names a resort or town gets the form in code (no AI).
- Overlapping season prices: the shortest date range wins (festival inside a season).
- Premium look: `static/premium.css` (ocean teal + sand gold, Fraunces headings), sun-and-waves logo (`logo.svg`,
  `logo_mark` macro, regenerated PWA icons via scripts/make_pwa_icons.py).

## Memory (2026-11-18)
- The AI sees the last 20 real messages of the current conversation (`HISTORY`), plus the guest's records (name, last
  bookings) every time, and can read older chat with `recall_old_chat`.
- A conversation ends after 24 h of silence (`SESSION_GAP`, was 6 h): a guest who comes back the next morning
  continues where they left off.
- Each sent photo is stored as its own "[photo] ..." row. They no longer count towards the 20: a run of photos is one
  "[photos sent: N]" note (12 glimpse photos used to push the guest's dates and name out of view).

## Strict form, enforced in code (client, 2026-11-18)
- The AI never asks for dates, nights or persons in its own words. Any such ask (a question, or "please share your
  dates") is replaced by the fixed form; the rest of its answer is kept ("Yes, right on the beach." + form), and an
  "available" claim before dates is dropped. The form may go again for a missing part (never twice in one turn).
- The code reads the stay itself (`parse_stay`: "Check in :dec 12 ...", "12 dec to 14 dec, 2 people", "dec 12" then
  "3 persons, till 15", "dec 20 nu 2 per" then "22 vare", "25/12 2 nights 4 pax"). Once all three are there, rooms
  are checked in code instead of asking again.
- A check-out the guest never gave is not accepted from the AI (it once made up a 1-night stay): the form goes.
- Naming the place ("rooms available in allepy?") skips the location question. A room / resort request the AI
  leaves silent still gets the next fixed step.
- After rooms are found, a mobile number or "same number" books in code even if the AI did not; no name and no
  WhatsApp name -> "Please share your name."
- No prices unless the guest asked (`no_unasked_prices`): sentences with ₹ / Rs / rupees / advance / 4,000 are dropped.
- Questions are one short line (max 12 words, no "Got it" / "I see"): "You have a booking on 12-15 Dec. Is this a new one?"

## Dashboard (owner, 2026-11-18)
- Removed: the property page's "WhatsApp bot" checklist, its top section bar, and "(the bot ...)" helper texts.
- Photos: one "Add photos" button; picking photos uploads them at once.
- Room cards on the property page show only name, rate and weekend rate (no rooms / adults / children chips).
- Home page in a modern SaaS style (owner's reference, 2026-11-18): Inter font everywhere; "Welcome, <resort>";
  four colour tiles (Arrivals, Departures, In the house, Collected today); a big banner that fades through the
  resorts' own general photos (cover first) with tonight's occupancy; card columns Arriving today / Waiting for
  payment (or Leaving today when nothing waits) / Coming up; then the 14-day occupancy bars.

## "sir" once, spelling mistakes (client, 2026-11-17)
- "sir" (or "madam") at most ONCE per conversation. After that it is removed from every reply in code (`_ease_sir`):
  'Yes sir.' -> 'Yes.', 'Hi sir, how' -> 'Hi, how'. A new conversation (6 h gap) may say it once again.
- Misspelt places are understood (`close_word`, `PLACE_ALIASES`): allepy / aleppey / alappuzha = Alleppey,
  kanthaloor = Kanthalloor, poziyoram = Pozhiyoram. The bot carries on and never corrects the spelling.

## After the form (client, 2026-11-17, replaces the photo timing below)
- check_availability sends, in this order and once per set of dates: "Yes, rooms are available for your dates."
  (or "Sorry, rooms are not available for your dates.") -> the glimpse ({price}, {room} = the offered room) -> the
  first 6 general photos -> the offered room type's photos (first 6) -> "Please share your name and mobile number to
  proceed." No photos are sent when the guest picks the location any more. Never a photo twice.

## After payment: one confirmation only (client, 2026-11-17)
- Once the advance is paid (online or at the desk) the guest gets ONLY "Following are the glimpse of your booking,"
  (`_confirmed_text`): guest, mobile, dates, early check-in, nights, PAX, rooms, the given room number(s) (*#104*),
  meal plan, costs, Advance Paid, Balance Payable, *Booking Confirmed ✅*. No map line and no ID request after it
  (`flow.ASK_ID_AFTER_PAYMENT = False`; the ID flow is kept for later).

## Name and number (client, 2026-11-17)
- "same number" = the WhatsApp number. With no name given, the WhatsApp profile name is used; if there is none, the
  bot asks only for the name (the number is kept). `create_booking` has no room type: the AI once guessed one that was
  full and asked the guest to pick. A requested room that is not free falls back to the top-priority free one.

## Room priority (client, 2026-11-17)
- Each room type has a **Priority** (1 = first). The bot offers ONLY the top-priority room type that is free and fits
  the party, and never asks the guest which room (`check_availability` returns one option; `prepare_booking` and
  `create_booking` with no room type take it). A guest who names another room type still gets it.
- Each room type can list its **room numbers in priority order** (room page, ‹ moves a room up; the room count then
  follows the list). When a booking becomes paid (Razorpay, desk payment or a walk-in booked as paid) it gets the
  highest-priority room(s) free on every night of its stay (`availability.allocate_rooms`, `ResortRepo.assign_rooms`).
  The check-in form starts with those rooms; staff can change them there.

## Dashboard
- Login manager@gmail.com / 1234 (env vars `RESORT_OWNER_EMAIL`, `RESORT_OWNER_PASSWORD`).
- Simple "Today" home, guests saved as WhatsApp leads (with export), one date-range picker for collections, only needed settings, no "placeholder" wording, works on phone, tablet and laptop.
- Installable app (PWA) for the owner's phone: open the dashboard in Chrome (Android) or Safari (iPhone), then Add to Home screen.
- No greeting banner on the home page. The "Waiting for payment" card shows only when a booking is waiting.
- Discount before payment: when a guest negotiates and talks to the owner, the owner opens the booking (Waiting for payment) and taps "Give discount". The total and advance are recalculated, the old payment link is closed, and the guest gets the new link on WhatsApp.

## Auto-pause when staff reply from the phone (user, 2026-11-07)
- When staff type a reply to a guest in the WhatsApp Business app (coexistence number), Meta sends an `smb_message_echoes` webhook. `BookingBot.human_replied` logs it as a `staff` chat line and pauses the bot for that guest; every new staff message restarts the 24 h auto-resume. Needs the `smb_message_echoes` webhook field subscribed in the Meta app.

## Language and guest memory (user, 2026-11-11)
- **One language per reply, the guest's latest message only.** The code decides the reply language each turn (`_language_note`: script, Manglish, Hinglish or English) and puts "REPLY LANGUAGE" at the end of the system prompt. Earlier messages and earlier chats in other languages never decide it. A message that mixes two languages gets the language most of its words are in.
- **Memory comes from the database, not the old chat.** The AI only sees the current conversation (a 6 h gap starts a new one; `SESSION_GAP`), and every link in the shown chat is masked as `[link]`, so an old payment link can never be copied into a new booking. Earlier bookings are given as a short "GUEST RECORDS" block (dates, place, status, upcoming or past).
- **Using the records:** paid booking upcoming or in progress and the guest asks for another room: the bot says it can see that booking and asks whether the new room is for the same stay or a different one. Only past stays: a new booking, with at most one warm welcome-back line. An unpaid hold that is still valid is the only payment the bot may mention. Expired, cancelled or failed bookings are never brought up. Warm, short, no forms.

## Nothing is forgotten, lookups only when needed (user, 2026-11-11, replaces the "hide old chat" idea above)
- The bot remembers every guest. The prompt always carries a short GUEST RECORDS block (cheap); the chat shown is the current conversation (6 h gap = new one). Older chat is kept and read on demand with the `recall_old_chat` tool (last 30 earlier messages, links hidden); old bookings and payments come from the records or the `my_bookings` tool (last 5).
- Only when the guest refers to something earlier ("as I told you", "my last booking", "the link you sent", a payment query) does the bot look. Greetings, small talk and timepass get a short polite reply with no lookup (no extra tokens).
- Payment questions about an old booking are answered politely and plainly (paid, what is left at the property, on hold, expired). An old link is never pasted: only `send_payment_link` sends one, and only for a booking still on hold.

## Every language, checked (2026-11-18)
- Tanglish is now recognised (`TANGLISH_WORDS`: irukka, evlo, venum, sollunga...): a Tamil-in-English-letters guest
  gets Tanglish, not English. Hinglish / Tanglish guests answered in English (6+ words) or in Manglish get one rewrite.
- Native script is NEVER sent, even when the rewrite still has it (`_latin_only`): the English-letter sentences are
  kept, else "Sorry, could you please type that once more in a few words?" (Manglish for Malayalam guests).
- The stay is read in any script (`parse_stay` via `_latin_stay`): months, persons (പേർ / பேர் / लोग), nights
  (രാത്രി / இரவு / रात, raat, iravu) and "until" (വരെ / வரை / तक, varai, tak). Town names in native script
  (ആലപ്പുഴ, ஆலப்புழா, अलेप्पी, കാന്തല്ലൂർ) are understood (`NATIVE_PLACES`), so the form / room check follow in code.
- "Same number" in any language (yahi number, இதே நம்பர், ഇതേ നമ്പർ, यही नंबर) and Tamil / Hindi filler words
  around the name (en peyar, mera naam ... hai, thaan) are read by `contact_in`.
- Prices asked in any language (evlo, kitna, എത്ര, எவ்வளவு, कितना) count as a price question.
- "sir ji", "saar", "ma'am" count as "sir" (said once per conversation).
- Voice notes: the transcript goes through exactly the same path as typed text (place, form, dates, language); the
  reply is ALWAYS text (this bot never sends voice). A note that was not understood gets one short text.
- Fixed step messages: Malayalam guests get the Manglish versions where they exist; Tamil / Hindi (script or
  English letters) get the English ones.

## Always English letters (user, 2026-11-11, replaces "Malayalam script gets Malayalam script")
Whatever language the guest writes or speaks (typed or voice note), the reply is in English letters only:
English -> English, Malayalam (any script, or a voice note) -> Manglish, Tamil -> Tanglish, Hindi -> Hinglish.
Native script is never sent. Code: `LATIN_STYLE` in chat.py (language note, rewrite check, style) and `_LATIN_LANG`
in i18n.py `t()` (fixed messages: Malayalam -> Manglish, Tamil/Hindi -> English). A reply that still contains
native script gets one rewrite.

## Fewer confirmations, payment screenshots, latest language (user, 2026-11-11)
- A guest who chose the room and gave dates and persons is not asked "confirm?" again: `create_booking` runs in the same
  turn as `prepare_booking` (summary + payment link together). Only a missing name or unclear detail is asked.
- An image from a guest (no ID request open) is read by the AI (vision; Gemini is first in the chain). Before that the
  bot asks Razorpay about unpaid holds, so a paid one is confirmed at once. A screenshot never confirms a booking:
  only Razorpay does, and the normal confirmation message follows.
- Language: only the latest message decides. When the guest switches language, the reminder is also put on their
  message itself, and an English-style reply that contains Manglish/Hinglish words gets one rewrite.
