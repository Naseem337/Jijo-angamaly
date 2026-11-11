# Resort bot: rules the user gave

These rules are enforced by our own prompt and code (`app/resort/chat.py`, `flow.py`, `i18n.py`), not by any one model.
Changing `LLM_CHAIN` (for example to DeepSeek) keeps them. After changing the model, run
`.venv\Scripts\python.exe scripts\live_eval_resort.py` and read `data/eval/resort_<lang>.txt`.

## Conversation
- Free-text chat like a human at the front desk. No menus, buttons or tap lists.
- "hi" gets "Hi", then ask what they are looking for. Pozhiyoram, Alleppey is the only open property (Vibes Kanthalloor is off until its data is entered).
- Ask in order: check-in date (or nights), number of persons, number of rooms. Never assume persons, rooms or nights.
- Quote by the guest's dates. If several room types are free, list each on its own line with its price and let them choose.
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
