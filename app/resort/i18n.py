"""Fixed guest-facing strings (payment and ID messages, fallbacks), in English, Malayalam, Tamil and Hindi, plus script-based language detection.

`t(key, lang, **values)` returns the text; unknown languages fall back to English. Option labels
(list rows, buttons) have WhatsApp length limits (button 20, row title 24), so keep them short.
Prices and dates are never written here: they are passed in as values from the booking code.
"""

from __future__ import annotations

import re

LANGS = ("en", "ml", "ta", "hi")
FIXED_LANGS = (*LANGS, "mn")  # "mn" = Manglish: fixed messages in Malayalam written in English letters
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
    "greet_ask": _t('{hi}! Where are you looking to stay: {places}?',
                'ഹായ്! എവിടെയാണ് താമസിക്കാൻ ഉദ്ദേശിക്കുന്നത്: {places}?',
                'வணக்கம்! எங்கே தங்க விரும்புகிறீர்கள்: {places}?',
                'नमस्ते! आप कहाँ ठहरना चाहते हैं: {places}?'),
    "greet_one": _t('{hi}! Are you looking for a stay at {place}?',
                'ഹായ്! {place}-ൽ താമസിക്കാനാണോ നോക്കുന്നത്?',
                'வணக்கம்! {place}-ல் தங்க பார்க்கிறீர்களா?',
                'नमस्ते! क्या आप {place} में ठहरने की सोच रहे हैं?'),
    "or": _t("or", "അല്ലെങ്കിൽ", "அல்லது", "या"),
    "pay": _t('Please complete the payment of ₹{advance} within {minutes} minutes to confirm your booking. UPI (GPay, PhonePe, Paytm), cards and net banking all work:\n{url}\n\nBooking ID: {id}',
                'ബുക്കിംഗ് ഉറപ്പിക്കാൻ {minutes} മിനിറ്റിനുള്ളിൽ ₹{advance} പേയ്മെന്റ് പൂർത്തിയാക്കൂ. UPI (GPay, PhonePe, Paytm), കാർഡ്, നെറ്റ് ബാങ്കിംഗ് എല്ലാം ഉപയോഗിക്കാം:\n{url}\n\nബുക്കിംഗ് ID: {id}',
                'முன்பதிவை உறுதிசெய்ய {minutes} நிமிடங்களுக்குள் ₹{advance} கட்டணத்தை முடிக்கவும். UPI (GPay, PhonePe, Paytm), கார்டு, நெட் பேங்கிங் அனைத்தும் பயன்படுத்தலாம்:\n{url}\n\nமுன்பதிவு ID: {id}',
                'बुकिंग कन्फर्म करने के लिए {minutes} मिनट में ₹{advance} का पेमेंट पूरा करें। UPI (GPay, PhonePe, Paytm), कार्ड और नेट बैंकिंग सब चलेगा:\n{url}\n\nबुकिंग ID: {id}'),
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
    "map_line": _t("Location: {url}", "ലൊക്കേഷൻ: {url}", "இருப்பிடம்: {url}", "लोकेशन: {url}"),
    "failed": _t("Your payment for booking {id} did not go through. Please try again with the link below while the rooms are held.",
                 "ബുക്കിംഗ് {id}-ന്റെ പേയ്മെന്റ് നടന്നില്ല. മുറികൾ ഹോൾഡിലുള്ളപ്പോൾ വീണ്ടും ശ്രമിക്കൂ.",
                 "முன்பதிவு {id}-க்கான கட்டணம் நடைபெறவில்லை. அறைகள் வைக்கப்பட்டிருக்கும்போது மீண்டும் முயற்சிக்கவும்.",
                 "बुकिंग {id} का पेमेंट नहीं हो सका। कमरे होल्ड पर हैं, कृपया दोबारा कोशिश करें।"),
    "retry_link": _t("Pay again: {url}", "വീണ്ടും അടയ്ക്കാൻ: {url}", "மீண்டும் செலுத்த: {url}", "दोबारा पेमेंट: {url}"),
    "pending": _t("Booking {id} is on hold and waiting for payment. Please complete the payment within {minutes} minutes: {url}",
                  "ബുക്കിംഗ് {id} പേയ്മെന്റിനായി ഹോൾഡിലാണ്. {minutes} മിനിറ്റിനുള്ളിൽ അടയ്ക്കൂ: {url}",
                  "முன்பதிவு {id} கட்டணத்துக்காகக் காத்திருக்கிறது. {minutes} நிமிடங்களுக்குள் செலுத்துங்கள்: {url}",
                  "बुकिंग {id} पेमेंट के इंतज़ार में होल्ड पर है। {minutes} मिनट में पेमेंट करें: {url}"),
    "expired": _t("Booking {id} expired because payment was not completed, and the rooms were released. "
                  "Send Hi to book again.",
                  "പേയ്മെന്റ് പൂർത്തിയാകാത്തതിനാൽ ബുക്കിംഗ് {id} കാലഹരണപ്പെട്ടു, മുറികൾ വിട്ടുനൽകി. വീണ്ടും ബുക്ക് ചെയ്യാൻ Hi അയക്കൂ.",
                  "கட்டணம் முடிக்கப்படாததால் முன்பதிவு {id} காலாவதியானது, அறைகள் விடுவிக்கப்பட்டன. மீண்டும் பதிவு செய்ய Hi அனுப்புங்கள்.",
                  "पेमेंट पूरा न होने से बुकिंग {id} समाप्त हो गई और कमरे छोड़ दिए गए। दोबारा बुक करने के लिए Hi भेजें।"),
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
    "id_body_first": _t('Thank you! For check-in we need an ID proof (Aadhaar, passport, driving licence or voter ID) of every adult guest, as required by law. It is stored securely and deleted {days} days after your stay.\n\nPlease send a clear photo of the ID of guest {n} of {m} here. If you prefer to show it at check-in, just say so.',
                'നന്ദി! ചെക്ക്-ഇന്നിന് എല്ലാ മുതിർന്ന അതിഥികളുടെയും ഐഡി തെളിവ് (ആധാർ, പാസ്പോർട്ട്, ഡ്രൈവിംഗ് ലൈസൻസ് അല്ലെങ്കിൽ വോട്ടർ ഐഡി) വേണം, നിയമപ്രകാരം ആവശ്യമാണ്. ഇത് സുരക്ഷിതമായി സൂക്ഷിക്കുകയും താമസം കഴിഞ്ഞ് {days} ദിവസത്തിനുശേഷം നീക്കം ചെയ്യുകയും ചെയ്യും.\n\nഅതിഥി {n}/{m}-ന്റെ ഐഡിയുടെ വ്യക്തമായ ഫോട്ടോ ഇവിടെ അയക്കൂ. ചെക്ക്-ഇന്നിൽ കാണിക്കാനാണെങ്കിൽ അങ്ങനെ പറഞ്ഞാൽ മതി.',
                'நன்றி! செக்-இன் செய்ய ஒவ்வொரு பெரியவருக்கும் அடையாள அட்டை (ஆதார், பாஸ்போர்ட், ஓட்டுநர் உரிமம் அல்லது வாக்காளர் அட்டை) தேவை, சட்டப்படி கட்டாயம். இது பாதுகாப்பாக வைக்கப்பட்டு, தங்கிய {days} நாட்களுக்குப் பிறகு நீக்கப்படும்.\n\nவிருந்தினர் {n}/{m} அவர்களின் அட்டையின் தெளிவான புகைப்படத்தை இங்கே அனுப்புங்கள். செக்-இனில் காட்ட விரும்பினால் அப்படிச் சொல்லுங்கள்.',
                'धन्यवाद! चेक-इन के लिए हर वयस्क मेहमान का आईडी प्रूफ़ (आधार, पासपोर्ट, ड्राइविंग लाइसेंस या वोटर आईडी) चाहिए, यह कानूनन ज़रूरी है। इसे सुरक्षित रखा जाता है और ठहरने के {days} दिन बाद हटा दिया जाता है।\n\nमेहमान {n}/{m} की आईडी की साफ़ फ़ोटो यहाँ भेजें। चेक-इन पर दिखाना चाहें तो बस बता दें।'),
    "id_body_next": _t('Received, thank you! Please send the ID photo of guest {n} of {m} too, or tell me if the rest will show theirs at check-in.',
                'ലഭിച്ചു, നന്ദി! അതിഥി {n}/{m}-ന്റെ ഐഡി ഫോട്ടോയും അയക്കൂ, അല്ലെങ്കിൽ ബാക്കിയുള്ളവർ ചെക്ക്-ഇന്നിൽ കാണിക്കുമെന്ന് പറയൂ.',
                'கிடைத்தது, நன்றி! விருந்தினர் {n}/{m} அவர்களின் அட்டை புகைப்படத்தையும் அனுப்புங்கள், அல்லது மீதிப்பேர் செக்-இனில் காட்டுவார்கள் எனச் சொல்லுங்கள்.',
                'मिल गया, धन्यवाद! मेहमान {n}/{m} की आईडी फ़ोटो भी भेजें, या बता दें कि बाकी लोग चेक-इन पर दिखाएँगे।'),
    "id_photo_bad": _t('Sorry, I can only accept a photo (JPG or PNG) of the ID. Please send it as a photo, or tell me you will show it at check-in.',
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
    "id_reminder": _t("Reminder: your stay {id} at {property} starts tomorrow. Please send an ID photo of each adult "
                      "guest here, or show it at check-in.",
                      "ഓർമ്മപ്പെടുത്തൽ: {property}-ലെ നിങ്ങളുടെ താമസം ({id}) നാളെ തുടങ്ങുന്നു. ഓരോ മുതിർന്ന അതിഥിയുടെയും ഐഡി ഫോട്ടോ "
                      "ഇവിടെ അയക്കൂ, അല്ലെങ്കിൽ ചെക്ക്-ഇന്നിൽ കാണിക്കൂ.",
                      "நினைவூட்டல்: {property}-ல் உங்கள் தங்கல் ({id}) நாளை தொடங்குகிறது. ஒவ்வொரு பெரியவரின் அடையாள அட்டை "
                      "புகைப்படத்தையும் இங்கே அனுப்புங்கள், அல்லது செக்-இனில் காட்டுங்கள்.",
                      "याद दिलाना: {property} में आपका ठहरना ({id}) कल शुरू हो रहा है। कृपया हर वयस्क मेहमान की आईडी फ़ोटो यहाँ भेजें, "
                      "या चेक-इन पर दिखाएँ।"),
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
    "greet_ask": "{hi}! Evide thaamasikkaan aanu nokkunnath: {places}?",
    "greet_one": "{hi}! {place}-il stay aanu nokkunnath alle?",
    "or": "allenkil",
    "pay": "Booking confirm cheyyaan {minutes} minute-nullil ₹{advance} payment complete cheyyuka. "
           "UPI (GPay, PhonePe, Paytm), card, net banking ellaam use cheyyam:\n{url}\n\nBooking ID: {id}",
    "paid": "Payment kitti. Ningalude booking {id} confirmed aayi!\n{property}\n{check_in} to {check_out}, {rooms} room.\n"
            "Check-in {checkin_time} muthal. Ningale kaanaan kaathirikkunnu.",
    "failed": "Booking {id}-nte payment nadannilla. Rooms hold-il ullappol thazhe ulla link vech veendum try cheyyuka.",
    "retry_link": "Veendum pay cheyyaan: {url}",
    "pending": "Booking {id} payment-inu vendi wait cheyyunnu. {minutes} minute-nullil payment complete cheyyuka: {url}",
    "expired": "Payment complete aakaathathinaal booking {id} expire aayi, rooms release cheythu. Veendum book cheyyaan Hi ayakkuka.",
    "handoff": "Thanks. Njangalude team udan ivide message ayakkum.",
    "ai_down": "Kshamikkanam, ippo answer tharaan pattiyilla. Njangalude team udan sahayikkum.",
    "voice_problem": "Kshamikkanam, aa voice note manasilaayilla. Type cheyyamo?",
    "unsupported": "Enikku text-um voice note-um maathrame vaayikkaan pattooo. Message type cheyyuka.",
    "map_line": "Location: {url}",
    "id_body_first": "Thanks! Check-in-nu ellaa adult guests-inteyum ID proof (Aadhaar, passport, driving licence allenkil voter ID) venam, "
                     "niyamaprakaaram aavashyamaanu. Ath surakshithamaayi sookshikkum, stay kazhinju {days} divasam kazhiyumbol delete cheyyum.\n\n"
                     "Guest {n}/{m}-nte ID-yude clear photo ivide ayakkuka. Check-in-nu kaanikkaanenkil athu paranjaal mathi.",
    "id_body_next": "Kitti, thanks! Guest {n}/{m}-nte ID photo koodi ayakkuka, allenkil bakkiyullavar check-in-nu kaanikkum ennu paranjaal mathi.",
    "id_later_ok": "Sheri, check-in-nu ID kaanichaal mathi.",
    "id_done": "Ellaam ready, thanks! Ningale kaanaan kaathirikkunnu.",
    "fb_thanks": "{property}-il thaamasichathinu thanks! Nalla samayam aayirunnu ennu karuthunnu.",
    "fb_body": "Stay engane undaayirunnu? 1 (mosham) muthal 5 (ugran) vare oru rating ayakkuka, venamenkil oru comment-um.",
    "fb_done": "Feedback-inu thanks!",
}
for _key, _text in MN.items():
    S[_key]["mn"] = _text


def t(key: str, lang: str, **values: object) -> str:
    table = S[key]
    text = table.get(lang) or table["en"]
    return text.format(**values) if values else text


def status_name(status: str, lang: str) -> str:
    pairs = dict(p.split("=", 1) for p in t("status_names", lang).split(","))
    return pairs.get(status, status)
