"""Fixed guest-facing strings (payment and ID messages, fallbacks), in English, Malayalam, Tamil and Hindi, plus script-based language detection.

`t(key, lang, **values)` returns the text; unknown languages fall back to English. Option labels
(list rows, buttons) have WhatsApp length limits (button 20, row title 24), so keep them short.
Prices and dates are never written here: they are passed in as values from the booking code.
"""

from __future__ import annotations

import re

LANGS = ("en", "ml", "ta", "hi")
# "mn" / "tn" / "hn" = Manglish / Tanglish / Hinglish: the fixed messages in English letters (owner, 2026-11-18)
FIXED_LANGS = (*LANGS, "mn", "tn", "hn")
LANG_NAMES = {"en": "English", "ml": "മലയാളം", "ta": "தமிழ்", "hi": "हिन्दी"}

_SCRIPTS = (("ml", re.compile(r"[ഀ-ൿ]")), ("ta", re.compile(r"[஀-௿]")),
            ("hi", re.compile(r"[ऀ-ॿ]")))


def detect_script(text: str, default: str = "en") -> str:
    """ml / ta / hi when the text is mostly in that script; Latin text keeps `default`."""
    best, best_count = default, 0
    for lang, pattern in _SCRIPTS:
        count = len(pattern.findall(text))
        if count > best_count:
            best, best_count = lang, count
    return best


def _t(en: str, ml: str, ta: str, hi: str) -> dict[str, str]:
    return {"en": en, "ml": ml, "ta": ta, "hi": hi}


S: dict[str, dict[str, str]] = {
    # ---- first message and payment
    # the client's own words (2026-11-17), the same for every language
    "greet": _t(*["Hi, How may I assist you?"] * 4),
    # a 'hi' in the middle of a chat (owner, 2026-11-22): 'Hi' and what we already know, never 'How may I assist you?'
    "hello_back": _t(*["Hi. Your enquiry: {stay}."] * 4),
    "hello_booked": _t(*["Hi. Your booking at {place}, {dates} is confirmed ✅."] * 4),
    # the client's words (2026-11-17); {towns} comes from the properties in the dashboard
    "ask_location": _t(*["Sure, Kindly confirm the Location 📍\n{towns} ?"] * 4),
    # the client's booking form (2026-11-17), always this English template
    "form": _t(*["Please share these details:\n\nCheck in :\nCheck out :\nNumber of persons :\nNumber of rooms:"] * 4),
    "ask_contact": _t(*["Please share your name and mobile number to proceed."] * 4),
    "ask_name": _t(*["Please share your name."] * 4),
    "closed_place": _t(*["Sorry, {place} is currently not available."] * 4),
    "owner_will_help": _t(*["The owner will contact you shortly."] * 4),
    "room_not_free": _t(*["Sorry, {room} is not free on your dates."] * 4),
    "no_other_room": _t(*["Sorry, no other room is free on your dates."] * 4),
    "other_room_free": _t(*["Yes, {room} is also free for your dates. Photos below; tell me if you want this room."] * 4),
    # the guest picked a room type (owner, 2026-11-22: 'I only need cottage' got the form again)
    "other_free": _t(*["Yes, {room} is also free for your dates. Tell me if you want this room."] * 4),
    "room_picked": _t(*["Sure, {room} is free for your dates."] * 4),
    "room_pick_no": _t(*["Sorry, {room} is not free for {persons} persons on your dates."] * 4),
    "fill_form": _t(*["Please share the details above."] * 4),
    "owner_numbers": _t(*["The owner can speak with you directly: {numbers}."] * 4),
    "name_changed": _t(*["Done, the name is changed to {name}."] * 4),
    "contact_again": _t(*["Please send your name and mobile number here to go ahead with the booking."] * 4),
    "say_again": _t(*["Sorry, could you please type that once more in a few words?"] * 4),
    "price_line": _t(*["{room}: {per} per night, {total} for {nights} night{s}."] * 4),
    "rooms_ok": _t(*["Yes, rooms are available for your dates."] * 4),
    "rooms_none": _t(*["Sorry, rooms are not available for your dates."] * 4),
    "anything_else": _t(*["Is there anything else?"] * 4),
    "noted": _t(*["Noted, rooms are still available."] * 4),
    "photos_sent": _t(*["All the photos are sent above."] * 4),
    "dates_for_availability": _t(*["To check which rooms are available, we need your check-in and check-out dates."] * 4),
    "rooms_plain": _t(*["For {persons} persons: {rooms} room(s)."] * 4),
    "rooms_note": _t(*["For {persons} persons, {rooms} rooms are needed. Or {persons} persons can stay in {fewer} "
                       "room(s) with extra beds (₹{rate} per extra bed per night)."] * 4),
    "extra_bed": _t(*["{beds} extra bed(s) added: ₹{rate} per bed per night."] * 4),
    "price_answer": _t(*["{rooms} room(s), {room}{beds}, {dates} ({nights} nights): total {total}."] * 4),
    "price_beds": _t(*[" + {beds} extra bed(s)"] * 4),
    "price_night": _t(*["{room}: {rate} per room per night."] * 4),
    "past_date": _t(*["{day} has already passed. Please send your check-in and check-out dates again."] * 4),
    "rooms_beds": _t(*["For {persons} persons: {rooms} room(s) + {beds} extra bed(s) (₹{rate} per extra bed per night)."] * 4),
    "rooms_none_alt": _t(*["Sorry, rooms are not available for your dates. Rooms are free on {dates}."] * 4),
    "beds_offer": _t(*["{persons} persons fit in {rooms} rooms with extra beds (2 + 1 per room, ₹{rate} per extra bed "
                       "per night). Shall I go ahead with {rooms} rooms?"] * 4),
    "rooms_most": _t(*["For your dates {rooms} room(s) are free, enough for up to {cap} persons. Please tell me how many "
                       "persons will come, or send other dates for all {persons}."] * 4),
    "min_rooms": _t(*["Max 3 persons per room (2 + 1 extra bed), so {persons} persons need at least {rooms} rooms."] * 4),
    "or": _t("or", "അല്ലെങ്കിൽ", "அல்லது", "या"),
    # the client wants no talk about payment here (2026-11-17): just the link and "please pay"
    "pay": _t('Please pay the advance using this link:\n{url}',
                'ബുക്കിംഗ് ഉറപ്പിക്കാൻ {minutes} മിനിറ്റിനുള്ളിൽ ₹{advance} പേയ്മെന്റ് പൂർത്തിയാക്കൂ. UPI (GPay, PhonePe, Paytm), കാർഡ്, നെറ്റ് ബാങ്കിംഗ് എല്ലാം ഉപയോഗിക്കാം:\n{url}\n\nബുക്കിംഗ് ID: {id}',
                'முன்பதிவை உறுதிசெய்ய {minutes} நிமிடங்களுக்குள் ₹{advance} கட்டணத்தை முடிக்கவும். UPI (GPay, PhonePe, Paytm), கார்டு, நெட் பேங்கிங் அனைத்தும் பயன்படுத்தலாம்:\n{url}\n\nமுன்பதிவு ID: {id}',
                'बुकिंग कन्फर्म करने के लिए {minutes} मिनट में ₹{advance} का पेमेंट पूरा करें। UPI (GPay, PhonePe, Paytm), कार्ड और नेट बैंकिंग सब चलेगा:\n{url}\n\nबुकिंग ID: {id}'),
    "discount": _t("Good news: the owner has given you a special price. Your new total is ₹{total}, and the advance to pay now is ₹{advance}.",
                   "ശുഭവാർത്ത: ഉടമ നിങ്ങൾക്ക് പ്രത്യേക വില അനുവദിച്ചിട്ടുണ്ട്. പുതിയ ആകെ തുക ₹{total}, ഇപ്പോൾ അടയ്ക്കേണ്ട അഡ്വാൻസ് ₹{advance}.",
                   "நல்ல செய்தி: உரிமையாளர் உங்களுக்கு சிறப்பு விலை வழங்கியுள்ளார். புதிய மொத்தம் ₹{total}, இப்போது செலுத்த வேண்டிய முன்பணம் ₹{advance}.",
                   "खुशखबरी: मालिक ने आपको खास कीमत दी है। नया कुल ₹{total} है और अभी देने वाला एडवांस ₹{advance} है।"),
    "pay_failed_link": _t("We could not create the payment link just now. Our team will contact you shortly.",
                          "പേയ്മെന്റ് ലിങ്ക് ഇപ്പോൾ ഉണ്ടാക്കാനായില്ല. ടീം ഉടൻ ബന്ധപ്പെടും.",
                          "கட்டண இணைப்பை இப்போது உருவாக்க முடியவில்லை. எங்கள் குழு விரைவில் தொடர்புகொள்ளும்.",
                          "अभी पेमेंट लिंक नहीं बन सका। हमारी टीम जल्द संपर्क करेगी।"),
    "paid": _t("Payment received. Your booking {id} is confirmed!\n{property}\n{check_in} to {check_out}, {rooms} room(s).\n"
               "Check-in from {checkin_time}. We look forward to hosting you.",
               "പേയ്മെന്റ് ലഭിച്ചു. നിങ്ങളുടെ ബുക്കിംഗ് {id} ഉറപ്പായി!\n{property}\n{check_in} മുതൽ {check_out} വരെ, {rooms} മുറി.\n"
               "ചെക്ക്-ഇൻ {checkin_time} മുതൽ. നിങ്ങളെ സ്വീകരിക്കാൻ കാത്തിരിക്കുന്നു.",
               "கட்டணம் பெறப்பட்டது. உங்கள் முன்பதிவு {id} உறுதியானது!\n{property}\n{check_in} முதல் {check_out} வரை, {rooms} அறை.\n"
               "செக்-இன் {checkin_time} முதல். உங்களை வரவேற்கக் காத்திருக்கிறோம்.",
               "पेमेंट मिल गया। आपकी बुकिंग {id} कन्फर्म है!\n{property}\n{check_in} से {check_out}, {rooms} कमरे।\n"
               "चेक-इन {checkin_time} से। आपके स्वागत का इंतज़ार रहेगा।"),
    "failed": _t("Looks like the payment didn't go through. Please try once more:",
                'പേയ്മെന്റ് നടന്നില്ലെന്ന് തോന്നുന്നു. ഒന്നുകൂടി ശ്രമിക്കൂ:',
                'கட்டணம் நடைபெறவில்லை போலிருக்கிறது. மீண்டும் முயற்சிக்கவும்:',
                'लगता है पेमेंट नहीं हो पाया। एक बार फिर कोशिश करें:'),
    "retry_link": _t('{url}',
                '{url}',
                '{url}',
                '{url}'),
    "pending": _t('Your payment link is still open for {minutes} more minutes: {url}',
                'പേയ്മെന്റ് ലിങ്ക് ഇനി {minutes} മിനിറ്റ് കൂടി തുറന്നിരിക്കും: {url}',
                'உங்கள் கட்டண இணைப்பு இன்னும் {minutes} நிமிடங்கள் செயலில் இருக்கும்: {url}',
                'आपका पेमेंट लिंक अभी {minutes} मिनट और खुला है: {url}'),
    "status_names": _t("hold=on hold,paid=confirmed,failed=payment failed,expired=expired,cancelled=cancelled",
                       "hold=ഹോൾഡിൽ,paid=ഉറപ്പായി,failed=പേയ്മെന്റ് പരാജയം,expired=കാലഹരണപ്പെട്ടു,cancelled=റദ്ദാക്കി",
                       "hold=நிறுத்தத்தில்,paid=உறுதியானது,failed=கட்டணம் தோல்வி,expired=காலாவதி,cancelled=ரத்து",
                       "hold=होल्ड पर,paid=कन्फर्म,failed=पेमेंट विफल,expired=समाप्त,cancelled=रद्द"),
    # ---- staff, errors, opt-out
    "handoff": _t("Thank you. Our team will message you here shortly.",
                  "നന്ദി. ഞങ്ങളുടെ ടീം ഉടൻ ഇവിടെ സന്ദേശമയയ്ക്കും.",
                  "நன்றி. எங்கள் குழு விரைவில் இங்கே தொடர்புகொள்ளும்.",
                  "धन्यवाद। हमारी टीम जल्द ही यहाँ संदेश भेजेगी।"),
    "ai_down": _t("Sorry, I could not answer that just now. Our team will help you shortly.",
                  "ക്ഷമിക്കണം, ഇപ്പോൾ ഉത്തരം നൽകാനായില്ല. ടീം ഉടൻ സഹായിക്കും.",
                  "மன்னிக்கவும், இப்போது பதில் சொல்ல முடியவில்லை. எங்கள் குழு விரைவில் உதவும்.",
                  "क्षमा करें, अभी जवाब नहीं दे सका। हमारी टीम जल्द मदद करेगी।"),
    "opt_out": _t("Okay, we will not send you offers. Reply START any time to hear from us again.",
                  "ശരി, ഇനി ഓഫറുകൾ അയയ്ക്കില്ല. വീണ്ടും വേണമെങ്കിൽ START എന്ന് അയക്കൂ.",
                  "சரி, இனி சலுகைகள் அனுப்ப மாட்டோம். மீண்டும் வேண்டுமென்றால் START அனுப்புங்கள்.",
                  "ठीक है, अब ऑफ़र नहीं भेजेंगे। दोबारा चाहिए तो START भेजें।"),
    "opt_in": _t("Welcome back! You will hear from us again.", "വീണ്ടും സ്വാഗതം! ഇനി ഞങ്ങളുടെ സന്ദേശങ്ങൾ ലഭിക്കും.",
                 "மீண்டும் வரவேற்கிறோம்! இனி எங்கள் செய்திகள் வரும்.", "फिर से स्वागत है! अब हमारे संदेश मिलेंगे।"),
    "voice_problem": _t("Sorry, I could not understand that voice note. Could you type it?",
                        "ക്ഷമിക്കണം, ആ വോയ്സ് മനസ്സിലായില്ല. ടൈപ്പ് ചെയ്യാമോ?",
                        "மன்னிக்கவும், அந்தக் குரல் செய்தி புரியவில்லை. டைப் செய்ய முடியுமா?",
                        "क्षमा करें, वह वॉइस नोट समझ नहीं आया। कृपया टाइप करें।"),
    "unsupported": _t("I can read text and voice notes only. Please type your message.",
                      "എനിക്ക് ടെക്സ്റ്റും വോയ്സും മാത്രമേ വായിക്കാനാകൂ. സന്ദേശം ടൈപ്പ് ചെയ്യൂ.",
                      "என்னால் உரையும் குரல் செய்தியும் மட்டுமே படிக்க முடியும். செய்தியை டைப் செய்யுங்கள்.",
                      "मैं सिर्फ़ टेक्स्ट और वॉइस नोट पढ़ सकता हूँ। कृपया संदेश टाइप करें।"),
}


# ---- ID proof after payment, reminder, and feedback after checkout
S.update({
    "id_body_first": _t('Thank you! Please send a photo of an ID proof (Aadhaar, passport, licence or voter ID) of guest {n} of {m}. It is needed by law and deleted {days} days after your stay.\n\nOr just show it at check-in.',
                'നന്ദി! ചെക്ക്-ഇന്നിന് എല്ലാ മുതിർന്ന അതിഥികളുടെയും ഐഡി തെളിവ് (ആധാർ, പാസ്പോർട്ട്, ഡ്രൈവിംഗ് ലൈസൻസ് അല്ലെങ്കിൽ വോട്ടർ ഐഡി) വേണം, നിയമപ്രകാരം ആവശ്യമാണ്. ഇത് സുരക്ഷിതമായി സൂക്ഷിക്കുകയും താമസം കഴിഞ്ഞ് {days} ദിവസത്തിനുശേഷം നീക്കം ചെയ്യുകയും ചെയ്യും.\n\nഅതിഥി {n}/{m}-ന്റെ ഐഡിയുടെ വ്യക്തമായ ഫോട്ടോ ഇവിടെ അയക്കൂ. ചെക്ക്-ഇന്നിൽ കാണിക്കാനാണെങ്കിൽ അങ്ങനെ പറഞ്ഞാൽ മതി.',
                'நன்றி! செக்-இன் செய்ய ஒவ்வொரு பெரியவருக்கும் அடையாள அட்டை (ஆதார், பாஸ்போர்ட், ஓட்டுநர் உரிமம் அல்லது வாக்காளர் அட்டை) தேவை, சட்டப்படி கட்டாயம். இது பாதுகாப்பாக வைக்கப்பட்டு, தங்கிய {days} நாட்களுக்குப் பிறகு நீக்கப்படும்.\n\nவிருந்தினர் {n}/{m} அவர்களின் அட்டையின் தெளிவான புகைப்படத்தை இங்கே அனுப்புங்கள். செக்-இனில் காட்ட விரும்பினால் அப்படிச் சொல்லுங்கள்.',
                'धन्यवाद! चेक-इन के लिए हर वयस्क मेहमान का आईडी प्रूफ़ (आधार, पासपोर्ट, ड्राइविंग लाइसेंस या वोटर आईडी) चाहिए, यह कानूनन ज़रूरी है। इसे सुरक्षित रखा जाता है और ठहरने के {days} दिन बाद हटा दिया जाता है।\n\nमेहमान {n}/{m} की आईडी की साफ़ फ़ोटो यहाँ भेजें। चेक-इन पर दिखाना चाहें तो बस बता दें।'),
    "id_body_next": _t('Got it, thanks! Now the ID photo of guest {n} of {m}, or say if the rest will show it at check-in.',
                'ലഭിച്ചു, നന്ദി! അതിഥി {n}/{m}-ന്റെ ഐഡി ഫോട്ടോയും അയക്കൂ, അല്ലെങ്കിൽ ബാക്കിയുള്ളവർ ചെക്ക്-ഇന്നിൽ കാണിക്കുമെന്ന് പറയൂ.',
                'கிடைத்தது, நன்றி! விருந்தினர் {n}/{m} அவர்களின் அட்டை புகைப்படத்தையும் அனுப்புங்கள், அல்லது மீதிப்பேர் செக்-இனில் காட்டுவார்கள் எனச் சொல்லுங்கள்.',
                'मिल गया, धन्यवाद! मेहमान {n}/{m} की आईडी फ़ोटो भी भेजें, या बता दें कि बाकी लोग चेक-इन पर दिखाएँगे।'),
    "id_photo_bad": _t('Sorry, please send the ID as a photo (JPG or PNG), or show it at check-in.',
                'ക്ഷമിക്കണം, ഐഡിയുടെ ഫോട്ടോ (JPG അല്ലെങ്കിൽ PNG) മാത്രമേ സ്വീകരിക്കൂ. ഫോട്ടോ ആയി അയക്കൂ, അല്ലെങ്കിൽ ചെക്ക്-ഇന്നിൽ കാണിക്കുമെന്ന് പറയൂ.',
                'மன்னிக்கவும், அட்டையின் புகைப்படம் (JPG அல்லது PNG) மட்டுமே ஏற்கப்படும். புகைப்படமாக அனுப்புங்கள், அல்லது செக்-இனில் காட்டுவேன் எனச் சொல்லுங்கள்.',
                'क्षमा करें, मैं आईडी की फ़ोटो (JPG या PNG) ही ले सकता हूँ। कृपया फ़ोटो के रूप में भेजें, या बताएँ कि चेक-इन पर दिखाएँगे।'),
    "id_photo_big": _t("That file is too large. Please send a smaller photo (under 5 MB).",
                       "ആ ഫയൽ വളരെ വലുതാണ്. ചെറിയ ഫോട്ടോ (5 MB-ൽ താഴെ) അയക്കൂ.",
                       "அந்தக் கோப்பு மிகவும் பெரியது. சிறிய புகைப்படத்தை (5 MB-க்குக் கீழ்) அனுப்புங்கள்.",
                       "वह फ़ाइल बहुत बड़ी है। कृपया छोटी फ़ोटो (5 MB से कम) भेजें।"),
    "id_photo_fail": _t("Sorry, I could not download that photo. Please send it again.",
                        "ക്ഷമിക്കണം, ആ ഫോട്ടോ ലഭിച്ചില്ല. ഒന്നുകൂടി അയക്കൂ.",
                        "மன்னிக்கவும், அந்தப் புகைப்படம் வரவில்லை. மீண்டும் அனுப்புங்கள்.",
                        "क्षमा करें, वह फ़ोटो नहीं मिली। कृपया दोबारा भेजें।"),
    "id_later_ok": _t("No problem, please show your ID at check-in.", "കുഴപ്പമില്ല, ചെക്ക്-ഇന്നിൽ ഐഡി കാണിച്ചാൽ മതി.",
                      "பரவாயில்லை, செக்-இனில் அட்டையைக் காட்டுங்கள்.", "कोई बात नहीं, चेक-इन पर आईडी दिखा दीजिए।"),
    "id_done": _t("All set, thank you! We look forward to welcoming you.", "എല്ലാം ശരിയായി, നന്ദി! നിങ്ങളെ സ്വാഗതം ചെയ്യാൻ കാത്തിരിക്കുന്നു.",
                  "எல்லாம் தயார், நன்றி! உங்களை வரவேற்கக் காத்திருக்கிறோம்.", "सब तैयार है, धन्यवाद! आपके स्वागत का इंतज़ार रहेगा।"),
    # the client (2026-11-18): only "bring your ID proof", no ID types, nothing sent on WhatsApp
    "id_reminder": _t(*["Please bring your ID proof while coming."] * 4),
    "fb_thanks": _t("Thank you for staying with us at {property}! We hope you had a wonderful time.",
                    "{property}-ൽ താമസിച്ചതിന് നന്ദി! മനോഹരമായ സമയം ആയിരുന്നെന്ന് കരുതുന്നു.",
                    "{property}-ல் தங்கியதற்கு நன்றி! இனிய நேரமாக அமைந்திருக்கும் என நம்புகிறோம்.",
                    "{property} में ठहरने के लिए धन्यवाद! आशा है आपका समय बहुत अच्छा बीता।"),
    "fb_body": _t('How was your stay? Please reply with a rating from 1 (poor) to 5 (excellent), and any comment you would like to add.',
                'താമസം എങ്ങനെയുണ്ടായിരുന്നു? 1 (മോശം) മുതൽ 5 (ഉജ്ജ്വലം) വരെ ഒരു റേറ്റിംഗ് അയക്കൂ, ആവശ്യമെങ്കിൽ ഒരു അഭിപ്രായവും.',
                'தங்கல் எப்படி இருந்தது? 1 (மோசம்) முதல் 5 (மிகச் சிறப்பு) வரை மதிப்பீடு அனுப்புங்கள், விரும்பினால் ஒரு கருத்தும்.',
                'आपका ठहरना कैसा रहा? 1 (खराब) से 5 (बहुत बढ़िया) तक रेटिंग भेजें, और चाहें तो कोई टिप्पणी भी।'),
    "fb_done": _t("Thank you for your feedback!", "അഭിപ്രായത്തിന് നന്ദി!", "உங்கள் கருத்துக்கு நன்றி!",
                  "आपकी प्रतिक्रिया के लिए धन्यवाद!"),
})


# Manglish (Malayalam in English letters) for the fixed messages. Keys not listed here fall back to English.
MN = {
    "hello_back": "Hi. Ningalude enquiry: {stay}.",
    "hello_booked": "Hi. {place}-il {dates} ningalude booking confirm aanu ✅.",
    "discount": "Nalla vaartha: owner ningalkku special price tharunnu. Puthiya total ₹{total}, ippol pay cheyyenda advance ₹{advance}.",
    "or": "allenkil",
    "pay": "Ee link vazhi advance pay cheyyuka:\n{url}",
    "rooms_ok": "Und, ningalude dates-il rooms available aanu.",
    "rooms_none": "Sorry, ningalude dates-il rooms available alla.",
    "ask_contact": "Interested aanenkil ningalude name-um mobile number-um ayakkamo?",
    "ask_name": "Ningalude name parayamo?",
    "closed_place": "Sorry, {place} ippol available alla.",
    "anything_else": "Vere enthenkilum veno?",
    "noted": "Sheri, rooms ippozhum available aanu.",
    "photos_sent": "Ella photos-um mukalil ayachittund.",
    "dates_for_availability": "Rooms available aano ennu nokkan check-in, check-out dates venam.",
    "rooms_plain": "{persons} perkku {rooms} room.",
    "rooms_note": "{persons} perkku {rooms} rooms venam. Allenkil {fewer} room-il extra bed vechu {persons} perkkum "
                  "nikkam (oru extra bed-inu ₹{rate} per night).",
    "extra_bed": "{beds} extra bed add cheyyum: oru bed-inu ₹{rate} per night.",
    "price_answer": "{rooms} room, {room}{beds}, {dates} ({nights} night): motham {total}.",
    "price_beds": " + {beds} extra bed",
    "price_night": "{room}: oru room-inu oru night {rate}.",
    "past_date": "{day} kazhinju poyi. Check-in, check-out dates onnu koodi ayakkamo?",
    "rooms_beds": "{persons} perkku {rooms} room + {beds} extra bed venam (oru extra bed-inu ₹{rate} per night).",
    "rooms_none_alt": "Sorry, ningalude dates-il rooms available alla. {dates} dates-il rooms und.",
    "beds_offer": "{persons} perkku {rooms} rooms-il extra bed vechu set aakam (oru room-il 2 + 1, extra bed-inu "
                  "₹{rate} per night). {rooms} rooms book cheyyatte?",
    "rooms_most": "Ningalude dates-il {rooms} rooms free undu, {cap} perkku vare. Ethra per varum ennu parayamo, "
                  "allenkil {persons} perkkum vere dates ayakkamo.",
    "min_rooms": "Oru room-il max 3 per (2 + 1 extra bed), athukond {persons} perkku kurachu {rooms} rooms venam.",
    "owner_will_help": "Owner udan ningale contact cheyyum.",
    "room_not_free": "Sorry, ningalude dates-il {room} free alla.",
    "no_other_room": "Sorry, ningalude dates-il vere room free alla.",
    "other_room_free": "Und, {room} um ningalude dates-il free aanu. Photos thazhe; ee room venamenkil parayoo.",
    "other_free": "Und, {room} um ningalude dates-il free aanu. Ee room venamenkil parayoo.",
    "room_picked": "Sheri, {room} ningalude dates-il free aanu.",
    "room_pick_no": "Sorry, ningalude dates-il {persons} perkku {room} free alla.",
    "fill_form": "Mukalile details share cheyyamo?",
    "name_changed": "Sheri, peru {name} ennu maatti.",
    "contact_again": "Booking munnott pokan ningalude name-um mobile number-um ivide ayakkamo?",
    "say_again": "Kshamikkanam, onnu koodi churukki type cheyyamo?",
    "price_line": "{room}: oru night-inu {per}, {nights} night-inu motham {total}.",
    "paid": "Payment kitti. Ningalude booking {id} confirmed aayi!\n{property}\n{check_in} to {check_out}, {rooms} room.\n"
            "Check-in {checkin_time} muthal. Ningale kaanaan kaathirikkunnu.",
    "failed": "Payment nadannilla ennu thonnunnu. Onnu koodi try cheyyuka:",
    "retry_link": "{url}",
    "pending": "Payment link ini {minutes} minute koodi undu: {url}",
    "handoff": "Thanks. Njangalude team udan ivide message ayakkum.",
    "ai_down": "Kshamikkanam, ippo answer tharaan pattiyilla. Njangalude team udan sahayikkum.",
    "voice_problem": "Kshamikkanam, aa voice note manasilaayilla. Type cheyyamo?",
    "unsupported": "Enikku text-um voice note-um maathrame vaayikkaan pattooo. Message type cheyyuka.",
    "id_body_first": "Thanks! Guest {n}/{m}-nte ID photo (Aadhaar, passport, licence allenkil voter ID) ayakkuka. Niyamam "
                     "aanu, stay kazhinju {days} divasathil delete cheyyum.\n\nAllenkil check-in-nu kaanichaal mathi.",
    "id_body_next": "Kitti, thanks! Guest {n}/{m}-nte ID photo koodi ayakkuka, allenkil bakkiyullavar check-in-nu kaanikkum ennu paranjaal mathi.",
    "id_later_ok": "Sheri, check-in-nu ID kaanichaal mathi.",
    "id_done": "Ellaam ready, thanks! Ningale kaanaan kaathirikkunnu.",
    "fb_thanks": "{property}-il thaamasichathinu thanks! Nalla samayam aayirunnu ennu karuthunnu.",
    "fb_body": "Stay engane undaayirunnu? 1 (mosham) muthal 5 (ugran) vare oru rating ayakkuka, venamenkil oru comment-um.",
    "fb_done": "Feedback-inu thanks!",
}
for _key, _text in MN.items():
    S[_key]["mn"] = _text


# Tanglish (Tamil in English letters) and Hinglish (Hindi in English letters) for the fixed messages (owner,
# 2026-11-18: Tamil and Hindi guests get their language everywhere, like Malayalam guests get Manglish). The greeting,
# the location question and the form keep the owner's exact English wording for everyone; summaries and the booking
# confirmation stay English. Keys not listed here fall back to English.
TN = {
    "hello_back": "Hi. Unga enquiry: {stay}.",
    "hello_booked": "Hi. {place}-la {dates} unga booking confirm aayiduchu ✅.",
    "discount": "Nalla news: owner ungalukku special price tharaanga. Pudhu total ₹{total}, ippo pay panna vendiya advance ₹{advance}.",
    "or": "illa",
    "pay": "Indha link-la advance pay pannunga:\n{url}",
    "rooms_ok": "Aamaa, unga dates-ku rooms available irukku.",
    "rooms_none": "Sorry, unga dates-ku rooms available illa.",
    "ask_contact": "Interested-na unga name-um mobile number-um anuppunga?",
    "ask_name": "Unga name sollunga?",
    "closed_place": "Sorry, {place} ippo available illa.",
    "owner_will_help": "Owner seekiram ungala contact pannuvaanga.",
    "room_not_free": "Sorry, unga dates-la {room} free illa.",
    "no_other_room": "Sorry, unga dates-la vera room free illa.",
    "other_room_free": "Aamaa, {room} um unga dates-la free. Photos keezha; indha room venumna sollunga.",
    "other_free": "Aamaa, {room} um unga dates-la free. Indha room venumna sollunga.",
    "room_picked": "Sari, {room} unga dates-la free.",
    "room_pick_no": "Sorry, unga dates-la {persons} perukku {room} free illa.",
    "fill_form": "Mela irukkura details share pannunga?",
    "name_changed": "Sari, peyar {name} nu maathiyachu.",
    "contact_again": "Booking continue panna unga name-um mobile number-um inga anuppunga.",
    "say_again": "Sorry, konjam short-a innoru thadava type pannunga?",
    "price_line": "{room}: oru night-ku {per}, {nights} night-ku total {total}.",
    "anything_else": "Vera edhavadhu venuma?",
    "noted": "Sari, rooms innum available.",
    "photos_sent": "Ella photos-um mela anupitten.",
    "dates_for_availability": "Rooms available-a nu paakka check-in, check-out dates venum.",
    "rooms_plain": "{persons} perukku {rooms} room.",
    "rooms_note": "{persons} perukku {rooms} rooms venum. Illana {fewer} room-la extra bed vechu {persons} perum "
                  "thangalam (oru extra bed ₹{rate} per night).",
    "extra_bed": "{beds} extra bed add pannuvom: oru bed-ku ₹{rate} per night.",
    "price_answer": "{rooms} room, {room}{beds}, {dates} ({nights} night): total {total}.",
    "price_beds": " + {beds} extra bed",
    "price_night": "{room}: oru room-ku oru night {rate}.",
    "past_date": "{day} mudinjiduchu. Check-in, check-out dates innoru thadava anuppunga.",
    "rooms_beds": "{persons} perukku {rooms} room + {beds} extra bed venum (oru extra bed ₹{rate} per night).",
    "rooms_none_alt": "Sorry, unga dates-la rooms illa. {dates} dates-la rooms irukku.",
    "beds_offer": "{persons} perukku {rooms} rooms-la extra bed vechu adjust pannalam (2 + 1 per room, extra bed "
                  "₹{rate} per night). {rooms} rooms book pannatuma?",
    "rooms_most": "Unga dates-la {rooms} rooms free irukku, {cap} per varaikkum. Evlo per varuvaanga nu sollunga, "
                  "illa {persons} perukkum vera dates anuppunga.",
    "min_rooms": "Oru room-la max 3 per (2 + 1 extra bed), so {persons} perukku kammiyaa {rooms} rooms venum.",
    "failed": "Payment nadakkala pola irukku. Innoru thadava try pannunga:",
    "retry_link": "{url}",
    "pending": "Payment link innum {minutes} nimisham irukku: {url}",
    "handoff": "Thanks. Engal team seekiram inga message pannuvaanga.",
    "ai_down": "Sorry, ippo reply panna mudiyala. Engal team seekiram help pannuvaanga.",
    "voice_problem": "Sorry, andha voice note puriyala. Type pannunga?",
    "unsupported": "Text-um voice note-um mattum thaan padikka mudiyum. Message type pannunga.",
    "opt_out": "Sari, inimel offers anuppa maattom. Thirumba venumna START nu reply pannunga.",
    "opt_in": "Welcome back! Inimel engal messages varum.",
    "fb_thanks": "{property}-la thangiyadhukku thanks! Nalla time-a irundhirukkum nu nambrom.",
    "fb_body": "Stay eppadi irundhuchu? 1 (mosam) mudhal 5 (super) varai oru rating anuppunga, venumna oru comment-um.",
    "fb_done": "Feedback-ku thanks!",
}
HN = {
    "hello_back": "Hi. Aapki enquiry: {stay}.",
    "hello_booked": "Hi. {place} mein {dates} aapki booking confirm hai ✅.",
    "discount": "Achhi khabar: owner aapko special price de rahe hain. Naya total ₹{total}, abhi pay karne wala advance ₹{advance}.",
    "or": "ya",
    "pay": "Is link se advance pay kijiye:\n{url}",
    "rooms_ok": "Haan, aapki dates par rooms available hain.",
    "rooms_none": "Sorry, aapki dates par rooms available nahi hain.",
    "ask_contact": "Interested hain to apna name aur mobile number bhejiye?",
    "ask_name": "Apna name bataiye?",
    "closed_place": "Sorry, {place} abhi available nahi hai.",
    "owner_will_help": "Owner jaldi aapse contact karenge.",
    "room_not_free": "Sorry, aapki dates par {room} free nahi hai.",
    "no_other_room": "Sorry, aapki dates par koi aur room free nahi hai.",
    "other_room_free": "Haan, {room} bhi aapki dates par free hai. Photos neeche; yeh room chahiye to bataiye.",
    "other_free": "Haan, {room} bhi aapki dates par free hai. Yeh room chahiye to bataiye.",
    "room_picked": "Theek hai, {room} aapki dates par free hai.",
    "room_pick_no": "Sorry, aapki dates par {persons} logon ke liye {room} free nahi hai.",
    "fill_form": "Upar wali details share kijiye?",
    "name_changed": "Theek hai, naam {name} kar diya.",
    "contact_again": "Booking aage badhane ke liye apna name aur mobile number yahan bhejiye.",
    "say_again": "Sorry, thoda chhota karke dobara type kijiye?",
    "price_line": "{room}: ek night ka {per}, {nights} night ka total {total}.",
    "anything_else": "Aur kuch chahiye?",
    "noted": "Theek hai, rooms abhi bhi available hain.",
    "photos_sent": "Saari photos upar bhej di hain.",
    "dates_for_availability": "Rooms available hain ya nahi dekhne ke liye check-in aur check-out dates chahiye.",
    "rooms_plain": "{persons} logon ke liye {rooms} room.",
    "rooms_note": "{persons} logon ke liye {rooms} rooms chahiye. Ya {fewer} room mein extra bed ke saath {persons} "
                  "log reh sakte hain (ek extra bed ₹{rate} per night).",
    "extra_bed": "{beds} extra bed add karenge: ek bed ka ₹{rate} per night.",
    "price_answer": "{rooms} room, {room}{beds}, {dates} ({nights} night): total {total}.",
    "price_beds": " + {beds} extra bed",
    "price_night": "{room}: ek room ka ek night {rate}.",
    "past_date": "{day} nikal chuka hai. Check-in aur check-out dates phir se bhejiye.",
    "rooms_beds": "{persons} logon ke liye {rooms} room + {beds} extra bed chahiye (ek extra bed ₹{rate} per night).",
    "rooms_none_alt": "Sorry, aapki dates par rooms available nahi hain. {dates} ko rooms free hain.",
    "beds_offer": "{persons} logon ke liye {rooms} rooms mein extra bed ke saath ho jayega (2 + 1 per room, extra bed "
                  "₹{rate} per night). {rooms} rooms book karun?",
    "rooms_most": "Aapki dates par {rooms} rooms free hain, {cap} logon tak. Kitne log aayenge bataiye, "
                  "ya sabhi {persons} ke liye doosri dates bhejiye.",
    "min_rooms": "Ek room mein max 3 log (2 + 1 extra bed), isliye {persons} logon ke liye kam se kam {rooms} rooms chahiye.",
    "failed": "Lagta hai payment nahi hua. Ek baar phir try kijiye:",
    "retry_link": "{url}",
    "pending": "Payment link abhi {minutes} minute aur chalega: {url}",
    "handoff": "Thanks. Hamari team jaldi yahan message karegi.",
    "ai_down": "Sorry, abhi jawab nahi de pa rahe. Hamari team jaldi help karegi.",
    "voice_problem": "Sorry, voice note samajh nahi aaya. Type kar denge?",
    "unsupported": "Hum sirf text aur voice note padh sakte hain. Message type kijiye.",
    "opt_out": "Theek hai, ab offers nahi bhejenge. Dobara chahiye to START reply kijiye.",
    "opt_in": "Welcome back! Ab aapko hamare messages milenge.",
    "fb_thanks": "{property} mein rukne ke liye thanks! Ummeed hai achha time raha.",
    "fb_body": "Stay kaisa raha? 1 (kharab) se 5 (bahut badhiya) tak rating bhejiye, chahein to comment bhi.",
    "fb_done": "Feedback ke liye thanks!",
}
for _code, _table in (("tn", TN), ("hn", HN)):
    for _key, _text in _table.items():
        S[_key][_code] = _text


# Fixed messages are never sent in native script (user, 2026-11-11): Malayalam -> Manglish, Tamil -> Tanglish,
# Hindi -> Hinglish (owner, 2026-11-18).
_LATIN_LANG = {"ml": "mn", "ta": "tn", "hi": "hn"}


def t(key: str, lang: str, **values: object) -> str:
    lang = _LATIN_LANG.get(lang, lang)
    table = S[key]
    text = table.get(lang) or table["en"]
    return text.format(**values) if values else text


def status_name(status: str, lang: str) -> str:
    pairs = dict(p.split("=", 1) for p in t("status_names", lang).split(","))
    return pairs.get(status, status)
