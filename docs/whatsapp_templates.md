# WhatsApp templates to submit to Meta (True Choice)

Submit these in **WhatsApp Manager → Message templates → Create template**. Approval can take
from minutes to a day. Names must match `clients/truechoice/client.yaml → whatsapp.templates`.

Templates belong to the WhatsApp Business Account, not the phone number, and can't be copied:
submit them on the **test account** now for testing, and again in **True Choice's own account** at go-live.

## Low-cost plan (user, 2026-10-21)

Rough India prices: Marketing ~₹0.80, Utility ~₹0.12, a reply within 24 h of the customer's last
message is free.
- **All templates are Utility.** Alerts are ones the customer asked for (a yard visitor's lead), so
  they're worded as plain notifications: no "good news", no emoji, no sales push. Meta may still
  re-categorise one as Marketing; then reword it more plainly and resubmit.
- **No consent template.** At the yard, staff ask the customer to send "Hi" to the bot (QR code at the
  counter), so the YES question is a free reply inside the 24 h window.
- **No "still looking?" template.** A lead simply expires after 90 days.
- Up to 3 cars per alert, at most 1 alert per customer per day (enforced in Step 10).
- **Never the customer's name** in customer templates (user rule, 2026-10-20).
- **English only for now** (user, 2026-10-21). Malayalam can be added later under the same names.
- Template parameters can't contain newlines, so each car is one parameter, and there is one
  template per number of cars. A near match is labelled inside its car line, e.g.
  `… · ₹7.2 lakh (slightly above your budget)`.

---

## 1. `tc_car_alert_1`: Utility, English, **image header**

Header: Media → Image (submitted with a sample car photo, 2026-10-21). At send time the bot puts the
car's own main photo in the header (same price as text). No buttons, no footer.
```
Hello, this is the stock alert you requested from True Choice. A car matching your saved requirement is now available:

{{1}}

Reply to this message for details. Reply STOP to stop these alerts.
```
Sample: {{1}} = `Maruti Swift VXI 2019, 38,000 km, Petrol, Rs 5.5 lakh`

## 2. `tc_car_alert_2`: Utility, English

```
Hello, this is the stock alert you requested from True Choice. 2 cars matching your saved requirement are now available:

1. {{1}}
2. {{2}}

Reply to this message for details. Reply STOP to stop these alerts.
```
Samples: {{1}} as above, {{2}} = `Maruti Baleno Delta 2020, 42,000 km, Petrol, Rs 6.8 lakh`

## 3. `tc_car_alert_3`: Utility, English

```
Hello, this is the stock alert you requested from True Choice. 3 cars matching your saved requirement are now available:

1. {{1}}
2. {{2}}
3. {{3}}

Reply to this message for details. Reply STOP to stop these alerts.
```
Samples: {{1}}, {{2}} as above, {{3}} = `Hyundai i20 Asta 2019, 51,000 km, Petrol, Rs 6.9 lakh (slightly above your budget)`

## 4. `tc_staff_lead_alert`: Utility, English (sent to staff numbers)

Staff are not customers, so the customer's name may appear here.
```
New lead for True Choice: {{1}} ({{2}}).
Interest: {{3}}
Budget: {{4}}
Reason for handoff: {{5}}
Please contact them; the full chat is in the dashboard.
```
Samples: {{1}} = `Arun`, {{2}} = `+91 98470 12345`, {{3}} = `Swift / Baleno, petrol`, {{4}} = `Rs 6 lakh`, {{5}} = `Wants loan details`

> Staff who have messaged the bot in the last 24 h get a free-form summary instead (Step 9), so this
> template is only a fallback.
