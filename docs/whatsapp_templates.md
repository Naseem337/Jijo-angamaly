# WhatsApp templates to submit to Meta (True Choice)

Submit these in **WhatsApp Manager → Message templates → Create template**. Approval can take
from minutes to a day, so submit now. Names must match `clients/truechoice/client.yaml → whatsapp.templates`.

Rules that shaped these drafts:
- Template **parameters can't contain newlines** and can't be a whole message. So each car is one
  parameter, and there is one template per number of cars (1, 2 or 3).
- A near match is labelled inside its car line, e.g. `Baleno Zeta 2020 · 42,000 km · Petrol · ₹7.2 lakh (slightly above your budget)`,
  so no separate near-match template is needed.
- Customer alerts are submitted as **Marketing** (Meta would re-categorise them anyway). Each one has a
  **"Stop alerts"** quick-reply button; the bot treats that tap as an opt-out.
- Submit each customer template in **two languages under the same name**: `ml` and `en`.
  Manglish customers get `en`.

Sample values for Meta's review form are given under each template.

---

## 1. `tc_car_alert_1`: Marketing, en + ml

**en (English) body**
```
Hi {{1}}, good news from True Choice! A car matching your requirement has just arrived:

🚗 {{2}}

Reply here to know more or to book a test drive.
```
**ml (Malayalam) body**
```
നമസ്കാരം {{1}}, True Choice-ൽ നിന്ന് ഒരു സന്തോഷവാർത്ത! നിങ്ങൾ അന്വേഷിച്ചതുപോലെയുള്ള ഒരു കാർ ഇപ്പോൾ എത്തിയിട്ടുണ്ട്:

🚗 {{2}}

കൂടുതൽ വിവരങ്ങൾക്കോ ടെസ്റ്റ് ഡ്രൈവ് ബുക്ക് ചെയ്യാനോ ഇവിടെ മറുപടി അയയ്ക്കൂ.
```
- Footer: en `Reply STOP to stop alerts` / ml `അലേർട്ടുകൾ വേണ്ടെങ്കിൽ STOP എന്ന് മറുപടി അയയ്ക്കുക`
- Buttons (quick reply): `Tell me more` / `കൂടുതൽ അറിയണം`, `Stop alerts` / `അലേർട്ട് വേണ്ട`
- Samples: {{1}} = `Arun`, {{2}} = `Maruti Swift VXI 2019 · 38,000 km · Petrol · ₹5.5 lakh`

## 2. `tc_car_alert_2`: Marketing, en + ml
Same as above, but the line before the list reads "**2 cars** matching your requirement have just arrived" (ml: `നിങ്ങൾ അന്വേഷിച്ചതുപോലെയുള്ള 2 കാറുകൾ ഇപ്പോൾ എത്തിയിട്ടുണ്ട്:`), with two car lines:
```
🚗 {{2}}
🚗 {{3}}
```
Samples: {{3}} = `Maruti Baleno Delta 2020 · 42,000 km · Petrol · ₹6.8 lakh`

## 3. `tc_car_alert_3`: Marketing, en + ml
The same again with "**3 cars**" (ml `3 കാറുകൾ`) and three car lines `{{2}}`, `{{3}}`, `{{4}}`.
Samples: {{4}} = `Hyundai i20 Asta 2019 · 51,000 km · Petrol · ₹6.9 lakh (slightly above your budget)`

## 4. `tc_still_looking`: Marketing, en + ml

**en**
```
Hi {{1}}, are you still looking for a {{2}}? Reply YES and we'll keep sending you matching cars from True Choice. Reply STOP to end these alerts.
```
**ml**
```
നമസ്കാരം {{1}}, നിങ്ങൾ ഇപ്പോഴും {{2}} അന്വേഷിക്കുന്നുണ്ടോ? അനുയോജ്യമായ കാറുകളുടെ വിവരങ്ങൾ True Choice-ൽ നിന്ന് തുടർന്നും ലഭിക്കാൻ YES എന്നും, അലേർട്ടുകൾ നിർത്താൻ STOP എന്നും മറുപടി അയയ്ക്കൂ.
```
- Buttons (quick reply): `Yes, still looking` / `അതെ, ഇപ്പോഴും വേണം`, `Stop alerts` / `അലേർട്ട് വേണ്ട`
- Samples: {{1}} = `Arun`, {{2}} = `Swift under ₹6 lakh, 2018 or newer`

## 5. `tc_staff_lead_alert`: Utility, en only (sent to staff numbers)

```
New lead for True Choice: {{1}} ({{2}}).
Interest: {{3}}
Budget: {{4}}
Reason for handoff: {{5}}
The bot is paused for this customer. Please contact them and open the dashboard to see the full chat.
```
Samples: {{1}} = `Arun`, {{2}} = `+91 98470 12345`, {{3}} = `Swift / Baleno, petrol`, {{4}} = `₹6 lakh`, {{5}} = `Wants loan details`

> If Meta re-categorises this as Marketing, that's fine, it will still deliver. Staff who have messaged
> the bot in the last 24 h get a free-form summary instead (handled in step 9).
