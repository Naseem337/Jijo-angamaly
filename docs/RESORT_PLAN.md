# Resort WhatsApp booking bot: plan (draft, 2026-10-28)

Built on the True Choice bot. Same engine (router, agent, guardrails, voice, handoff, alerts); the car
parts become resort parts. Data (properties, rooms, rates) comes from the client later; use placeholders until then.

## Decisions so far (user)
- Properties (first client): **Pozhiyoram Resort**, **Vibes Kanthalloor**. One owner, several properties.
- Dashboard: custom web app in this project, Frappe-desk look (sidebar, list views, forms, calendar). Simple and beautiful.
- Database: free-tier hosted Postgres (Supabase) behind the Repository interface. SQLite stays for tests/local.
- Booking is completed in WhatsApp. Payment by Razorpay payment link (advance or full) plus UPI QR.
  Webhook confirms; guest gets a template message for paid / failed / pending-hold.
- Guest history is stored and shown in the dashboard.
- Languages: English, Malayalam, Tamil, Hindi (+ Manglish/Tanglish/Hinglish), text and voice, in and out.
- Free-tier AI models are fine for testing.
- Keep: voice notes, offer alerts, room photos, staff handoff.

## Booking conversation (preset options, not free typing)
1. Language is detected from the guest's message (and kept for the whole chat).
2. **Property**: WhatsApp list message with the properties (Pozhiyoram Resort / Vibes Kanthalloor).
3. **Dates**: check-in and check-out (typed, or a WhatsApp Flow date picker later).
4. **Guests**: list message with adults 1..N, then children (with ages if the property prices by age).
   Over the list limit (10 rows) the last row is "More" and the guest can type a number.
5. **Rooms**: the CODE (not the AI) works out the valid room counts from each room type's capacity.
   Only valid options are offered. 10 people in one room is never offered; the bot shows the minimum rooms
   needed and the choices above it.
6. **Room type + price**: list of available room types with the total for the stay (from the dashboard data).
7. **Confirm** (buttons: Confirm / Change), room hold for 15 minutes, Razorpay link + QR.
8. Paid → "Booking confirmed" template. Failed or expired → template with a retry link. Hold released on expiry.

Everything is a tap-to-select option (user, 2026-10-28): language, property, check-in date (list of the next
dates), number of nights, adults, children, rooms, room type, confirm. Typing is only for the guest's name,
special requests and free questions (answered by the AI in the guest's language). Replies are always text,
never voice (user, 2026-10-28); voice notes from guests are still understood.

Rules: availability, price, capacity and dates always come from tools. The AI only talks; guardrails check every
number against tool results (same idea as the car guardrails).

## Data model (replaces cars)
Property, RoomType (max adults, max children, max occupancy, extra-bed rule, base rate, photos),
Room (physical unit, optional), RateRule (season / weekend / date range overrides), Booking
(status: hold, paid, failed, expired, cancelled, checked_in, checked_out), Payment (Razorpay ids),
Guest (phone, name, language, consent, stay history), plus the existing messages / customers / handoff tables.
Double-booking is prevented by a database-level check inside one transaction when a hold is created.

## Build order (one step per commit: `Step R<n>: ...`)
- R1: `clients/resort/` config, new prompt, language rules (en/ml/ta/hi), rename car wording across the code.
- R2: models + Repository + SQLite + Supabase Postgres implementation, with tests.
- R3: availability, pricing and capacity engine (pure code, fully tested), hold and expiry.
- R4: WhatsApp interactive messages (list + reply buttons) in the client and router; agent tools for the booking flow.
- R5: Razorpay (payment link, QR, signed webhook, refunds later).
- R6: templates (paid, failed, pending, hold expiring, staff alert), submitted on the test account.
- R7: dashboard (login, properties, rooms, rates, bookings calendar, guests, chats, handoff inbox).
- R8: offer alerts to opted-in past guests; room photos.
- R9: deploy, live test on a test number, go-live checklist.

## Meta setup
- One Meta app per project; each app has its own free test number, phone number ID, app secret and webhook.
  Create a new app ("Resort Bot"), add WhatsApp, add your own phone as a recipient.
- Real resort number later: needs the client's WhatsApp Business Account and templates submitted there.

## Open items (need client data)
- Real room types, capacities, rates, seasons, photos for both properties.
- Advance % (or full payment), cancellation / refund policy, check-in and check-out times, GST on rates.
- Staff WhatsApp numbers per property.
- Razorpay account (test keys first).
