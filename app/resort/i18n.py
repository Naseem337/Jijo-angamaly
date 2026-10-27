"""Every guest-facing string, in English, Malayalam, Tamil and Hindi, plus script-based language detection.

`t(key, lang, **values)` returns the text; unknown languages fall back to English. Option labels
(list rows, buttons) have WhatsApp length limits (button 20, row title 24), so keep them short.
Prices and dates are never written here: they are passed in as values from the booking code.
"""

from __future__ import annotations

import re

LANGS = ("en", "ml", "ta", "hi")
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
    # ---- language, welcome, menu
    "lang_body": _t("Welcome! Please choose your language.", "സ്വാഗതം! നിങ്ങളുടെ ഭാഷ തിരഞ്ഞെടുക്കൂ.",
                    "வரவேற்கிறோம்! உங்கள் மொழியைத் தேர்ந்தெடுக்கவும்.", "स्वागत है! कृपया अपनी भाषा चुनें।"),
    "lang_button": _t("Language", "ഭാഷ", "மொழி", "भाषा"),
    "menu_body": _t("Welcome to {brand}. How can I help you today?",
                    "{brand}-ലേക്ക് സ്വാഗതം. ഞാൻ എങ്ങനെ സഹായിക്കണം?",
                    "{brand}-க்கு வரவேற்கிறோம். நான் எப்படி உதவலாம்?",
                    "{brand} में आपका स्वागत है। मैं आपकी कैसे मदद करूँ?"),
    "menu_button": _t("Choose", "തിരഞ്ഞെടുക്കൂ", "தேர்வு செய்க", "चुनें"),
    "menu_book": _t("Book a stay", "മുറി ബുക്ക് ചെയ്യാം", "அறை முன்பதிவு", "कमरा बुक करें"),
    "menu_ask": _t("Ask a question", "ഒരു സംശയം ചോദിക്കാം", "ஒரு கேள்வி", "सवाल पूछें"),
    "menu_staff": _t("Talk to our team", "ടീമിനോട് സംസാരിക്കാം", "குழுவுடன் பேச", "टीम से बात करें"),
    "menu_status": _t("My booking", "എന്റെ ബുക്കിംഗ്", "என் முன்பதிவு", "मेरी बुकिंग"),
    "ask_prompt": _t("Sure, type your question and I will answer.",
                     "തീർച്ചയായും, നിങ്ങളുടെ സംശയം ടൈപ്പ് ചെയ്യൂ.",
                     "சரி, உங்கள் கேள்வியை டைப் செய்யுங்கள்.", "ज़रूर, अपना सवाल लिखिए।"),
    # ---- booking steps
    "prop_body": _t("Which property would you like to stay at?", "ഏത് പ്രോപ്പർട്ടിയിലാണ് താമസിക്കേണ്ടത്?",
                    "எந்த இடத்தில் தங்க விரும்புகிறீர்கள்?", "आप किस प्रॉपर्टी में ठहरना चाहेंगे?"),
    "prop_button": _t("Properties", "പ്രോപ്പർട്ടികൾ", "இடங்கள்", "प्रॉपर्टी"),
    "date_body": _t("When do you want to check in?", "ചെക്ക്-ഇൻ എപ്പോഴാണ്?",
                    "எப்போது செக்-இன் செய்ய விரும்புகிறீர்கள்?", "चेक-इन कब करना है?"),
    "date_button": _t("Pick a date", "തീയതി", "தேதி", "तारीख चुनें"),
    "date_today": _t("Today", "ഇന്ന്", "இன்று", "आज"),
    "date_tomorrow": _t("Tomorrow", "നാളെ", "நாளை", "कल"),
    "date_other": _t("Other date", "മറ്റൊരു തീയതി", "வேறு தேதி", "दूसरी तारीख"),
    "date_other_desc": _t("Type it, e.g. 25 Dec", "ടൈപ്പ് ചെയ്യൂ, ഉദാ: 25 Dec", "டைப் செய்யவும், எ.கா: 25 Dec",
                          "लिखें, जैसे 25 Dec"),
    "date_type": _t("Please type the check-in date, for example 25 Dec or 25/12.",
                    "ചെക്ക്-ഇൻ തീയതി ടൈപ്പ് ചെയ്യൂ, ഉദാ: 25 Dec അല്ലെങ്കിൽ 25/12.",
                    "செக்-இன் தேதியை டைப் செய்யுங்கள், எ.கா: 25 Dec அல்லது 25/12.",
                    "चेक-इन की तारीख लिखिए, जैसे 25 Dec या 25/12।"),
    "date_bad": _t("I could not use that date. Please pick from the list or type it like 25 Dec.",
                   "ആ തീയതി മനസ്സിലായില്ല. ലിസ്റ്റിൽ നിന്ന് തിരഞ്ഞെടുക്കൂ അല്ലെങ്കിൽ 25 Dec പോലെ ടൈപ്പ് ചെയ്യൂ.",
                   "அந்த தேதி புரியவில்லை. பட்டியலில் தேர்வு செய்யுங்கள் அல்லது 25 Dec போல் டைப் செய்யுங்கள்.",
                   "वह तारीख समझ नहीं आई। सूची से चुनें या 25 Dec जैसे लिखें।"),
    "date_past": _t("That date has already passed. Please choose a date from today onwards.",
                    "ആ തീയതി കഴിഞ്ഞുപോയി. ഇന്നുമുതലുള്ള തീയതി തിരഞ്ഞെടുക്കൂ.",
                    "அந்த தேதி முடிந்துவிட்டது. இன்று முதலான தேதியைத் தேர்ந்தெடுக்கவும்.",
                    "वह तारीख निकल चुकी है। आज या उसके बाद की तारीख चुनें।"),
    "nights_body": _t("How many nights will you stay? (Check-in {date})",
                      "എത്ര രാത്രി താമസിക്കും? (ചെക്ക്-ഇൻ {date})",
                      "எத்தனை இரவுகள் தங்குவீர்கள்? (செக்-இன் {date})",
                      "कितनी रातें ठहरेंगे? (चेक-इन {date})"),
    "nights_button": _t("Nights", "രാത്രികൾ", "இரவுகள்", "रातें"),
    "night_1": _t("1 night", "1 രാത്രി", "1 இரவு", "1 रात"),
    "night_n": _t("{n} nights", "{n} രാത്രി", "{n} இரவுகள்", "{n} रातें"),
    "more": _t("More", "കൂടുതൽ", "மேலும்", "और"),
    "more_desc": _t("Type a number", "ഒരു സംഖ്യ ടൈപ്പ് ചെയ്യൂ", "எண்ணை டைப் செய்யவும்", "संख्या लिखें"),
    "nights_type": _t("Please type the number of nights (up to {max}).",
                      "രാത്രികളുടെ എണ്ണം ടൈപ്പ് ചെയ്യൂ ({max} വരെ).",
                      "இரவுகளின் எண்ணிக்கையை டைப் செய்யுங்கள் ({max} வரை).",
                      "रातों की संख्या लिखिए ({max} तक)।"),
    "adults_body": _t("How many adults?", "മുതിർന്നവർ എത്ര പേർ?", "பெரியவர்கள் எத்தனை பேர்?",
                      "वयस्क कितने हैं?"),
    "adults_button": _t("Adults", "മുതിർന്നവർ", "பெரியவர்கள்", "वयस्क"),
    "adult_1": _t("1 adult", "1 മുതിർന്നയാൾ", "1 பெரியவர்", "1 वयस्क"),
    "adult_n": _t("{n} adults", "{n} മുതിർന്നവർ", "{n} பெரியவர்கள்", "{n} वयस्क"),
    "adults_type": _t("Please type the number of adults (1 to {max}).",
                      "മുതിർന്നവരുടെ എണ്ണം ടൈപ്പ് ചെയ്യൂ (1 മുതൽ {max} വരെ).",
                      "பெரியவர்களின் எண்ணிக்கையை டைப் செய்யுங்கள் (1 முதல் {max} வரை).",
                      "वयस्कों की संख्या लिखिए (1 से {max})।"),
    "children_body": _t("How many children (under 12)?", "കുട്ടികൾ എത്ര പേർ (12 വയസ്സിൽ താഴെ)?",
                        "குழந்தைகள் எத்தனை பேர் (12 வயதுக்குக் கீழ்)?", "बच्चे कितने हैं (12 साल से कम)?"),
    "children_button": _t("Children", "കുട്ടികൾ", "குழந்தைகள்", "बच्चे"),
    "child_0": _t("No children", "കുട്ടികളില്ല", "குழந்தைகள் இல்லை", "कोई बच्चा नहीं"),
    "child_1": _t("1 child", "1 കുട്ടി", "1 குழந்தை", "1 बच्चा"),
    "child_n": _t("{n} children", "{n} കുട്ടികൾ", "{n} குழந்தைகள்", "{n} बच्चे"),
    "children_type": _t("Please type the number of children (0 to {max}).",
                        "കുട്ടികളുടെ എണ്ണം ടൈപ്പ് ചെയ്യൂ (0 മുതൽ {max} വരെ).",
                        "குழந்தைகளின் எண்ணிக்கையை டைப் செய்யுங்கள் (0 முதல் {max} வரை).",
                        "बच्चों की संख्या लिखिए (0 से {max})।"),
    "type_body": _t("Choose a room type for {nights} night(s), {guests} guest(s). Prices are for the whole stay.",
                    "{nights} രാത്രി, {guests} അതിഥികൾക്കുള്ള മുറി തിരഞ്ഞെടുക്കൂ. വില മുഴുവൻ താമസത്തിനുള്ളതാണ്.",
                    "{nights} இரவு, {guests} விருந்தினர்களுக்கான அறையைத் தேர்ந்தெடுக்கவும். விலை முழு தங்குதலுக்கானது.",
                    "{nights} रात, {guests} मेहमानों के लिए कमरा चुनें। कीमत पूरे ठहराव की है।"),
    "type_button": _t("Room types", "മുറികൾ", "அறைகள்", "कमरे"),
    "type_desc": _t("₹{total} for {rooms} room(s)", "{rooms} മുറിക്ക് ₹{total}", "{rooms} அறைக்கு ₹{total}",
                    "{rooms} कमरे के ₹{total}"),
    "rooms_body": _t("{guests} guests: how many rooms do you want? ({type} fits up to {cap} guests per room)",
                     "{guests} പേർ: എത്ര മുറി വേണം? ({type}-ൽ ഒരു മുറിയിൽ {cap} പേർ വരെ)",
                     "{guests} பேர்: எத்தனை அறைகள் வேண்டும்? ({type}-ல் ஒரு அறைக்கு {cap} பேர் வரை)",
                     "{guests} मेहमान: कितने कमरे चाहिए? ({type} में एक कमरे में {cap} तक)"),
    "rooms_button": _t("Rooms", "മുറികൾ", "அறைகள்", "कमरे"),
    "room_1": _t("1 room", "1 മുറി", "1 அறை", "1 कमरा"),
    "room_n": _t("{n} rooms", "{n} മുറികൾ", "{n} அறைகள்", "{n} कमरे"),
    "rooms_min_desc": _t("Minimum needed", "ഏറ്റവും കുറഞ്ഞത്", "குறைந்தபட்சம்", "कम से कम"),
    "no_fit": _t("Sorry, no room type is free for {guests} guest(s) on those dates in one booking. "
                 "You can try fewer guests or other dates, or talk to our team.",
                 "ക്ഷമിക്കണം, ആ തീയതികളിൽ {guests} പേർക്ക് ഒരു ബുക്കിംഗിൽ ഒഴിവുള്ള മുറിയില്ല. "
                 "ആളുകളുടെ എണ്ണമോ തീയതിയോ മാറ്റിനോക്കാം, അല്ലെങ്കിൽ ടീമിനോട് സംസാരിക്കാം.",
                 "மன்னிக்கவும், அந்த தேதிகளில் {guests} பேருக்கு ஒரே முன்பதிவில் காலி அறை இல்லை. "
                 "நபர்கள் அல்லது தேதியை மாற்றிப் பாருங்கள், அல்லது குழுவுடன் பேசுங்கள்.",
                 "क्षमा करें, उन तारीखों में {guests} मेहमानों के लिए एक बुकिंग में खाली कमरा नहीं है। "
                 "मेहमान या तारीख बदलकर देखें, या हमारी टीम से बात करें।"),
    "no_fit_change": _t("Change guests", "ആളുകളെ മാറ്റാം", "நபர்களை மாற்று", "मेहमान बदलें"),
    "no_fit_dates": _t("Change dates", "തീയതി മാറ്റാം", "தேதியை மாற்று", "तारीख बदलें"),
    "no_fit_staff": _t("Talk to team", "ടീമിനോട്", "குழுவிடம்", "टीम से"),
    "summary": _t("Please check your booking:\n{property}\n{room_type} x {rooms}\n{check_in} to {check_out} ({nights} night(s))\n"
                  "Guests: {adults} adult(s), {children} child(ren)\nTotal: ₹{total}\nPay now to confirm: ₹{advance}"
                  "{balance_line}",
                  "ബുക്കിംഗ് പരിശോധിക്കൂ:\n{property}\n{room_type} x {rooms}\n{check_in} മുതൽ {check_out} വരെ ({nights} രാത്രി)\n"
                  "അതിഥികൾ: {adults} മുതിർന്നവർ, {children} കുട്ടികൾ\nആകെ: ₹{total}\nഉറപ്പിക്കാൻ ഇപ്പോൾ അടയ്ക്കേണ്ടത്: ₹{advance}"
                  "{balance_line}",
                  "உங்கள் முன்பதிவைச் சரிபார்க்கவும்:\n{property}\n{room_type} x {rooms}\n{check_in} முதல் {check_out} வரை ({nights} இரவு)\n"
                  "விருந்தினர்கள்: {adults} பெரியவர், {children} குழந்தை\nமொத்தம்: ₹{total}\nஉறுதிசெய்ய இப்போது செலுத்த: ₹{advance}"
                  "{balance_line}",
                  "अपनी बुकिंग जाँचें:\n{property}\n{room_type} x {rooms}\n{check_in} से {check_out} ({nights} रात)\n"
                  "मेहमान: {adults} वयस्क, {children} बच्चे\nकुल: ₹{total}\nकन्फर्म करने के लिए अभी दें: ₹{advance}"
                  "{balance_line}"),
    "balance_line": _t("\nBalance ₹{balance} at the resort", "\nബാക്കി ₹{balance} റിസോർട്ടിൽ നൽകാം",
                       "\nமீதி ₹{balance} ரிசார்ட்டில் செலுத்தலாம்", "\nबाकी ₹{balance} रिसॉर्ट में दें"),
    "confirm": _t("Confirm", "ഉറപ്പിക്കുക", "உறுதி", "कन्फर्म"),
    "change": _t("Change", "മാറ്റുക", "மாற்று", "बदलें"),
    "ask_name": _t("Almost done. Please type the guest name for the booking.",
                   "ഏകദേശം കഴിഞ്ഞു. ബുക്കിംഗിലെ അതിഥിയുടെ പേര് ടൈപ്പ് ചെയ്യൂ.",
                   "கிட்டத்தட்ட முடிந்தது. முன்பதிவுக்கான விருந்தினர் பெயரை டைப் செய்யுங்கள்.",
                   "लगभग हो गया। बुकिंग के लिए मेहमान का नाम लिखिए।"),
    "soldout": _t("Sorry, those rooms were just taken. Please choose again.",
                  "ക്ഷമിക്കണം, ആ മുറികൾ ഇപ്പോൾ ബുക്ക് ആയി. വീണ്ടും തിരഞ്ഞെടുക്കൂ.",
                  "மன்னிக்கவும், அந்த அறைகள் இப்போதுதான் முன்பதிவாகிவிட்டன. மீண்டும் தேர்ந்தெடுக்கவும்.",
                  "क्षमा करें, वे कमरे अभी बुक हो गए। कृपया दोबारा चुनें।"),
    "pay": _t("Booking {id} is held for {minutes} minutes. Pay ₹{advance} here to confirm:\n{url}",
              "ബുക്കിംഗ് {id} {minutes} മിനിറ്റ് ഹോൾഡിലാണ്. ഉറപ്പിക്കാൻ ₹{advance} ഇവിടെ അടയ്ക്കൂ:\n{url}",
              "முன்பதிவு {id} {minutes} நிமிடங்கள் வைக்கப்பட்டுள்ளது. உறுதிசெய்ய ₹{advance} இங்கே செலுத்துங்கள்:\n{url}",
              "बुकिंग {id} {minutes} मिनट के लिए होल्ड पर है। कन्फर्म करने के लिए ₹{advance} यहाँ दें:\n{url}"),
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
    "failed": _t("Your payment for booking {id} did not go through. Tap below to try again while the rooms are held.",
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
    "no_booking": _t("You have no booking with us yet. Choose Book a stay to start.",
                     "നിങ്ങൾക്ക് ഇതുവരെ ബുക്കിംഗ് ഇല്ല. തുടങ്ങാൻ 'മുറി ബുക്ക് ചെയ്യാം' തിരഞ്ഞെടുക്കൂ.",
                     "உங்களுக்கு இன்னும் முன்பதிவு இல்லை. தொடங்க 'அறை முன்பதிவு' தேர்வு செய்யுங்கள்.",
                     "आपकी अभी कोई बुकिंग नहीं है। शुरू करने के लिए 'कमरा बुक करें' चुनें।"),
    "status": _t("Booking {id}: {property}, {check_in} to {check_out}, {rooms} room(s). Status: {status}.",
                 "ബുക്കിംഗ് {id}: {property}, {check_in} മുതൽ {check_out}, {rooms} മുറി. നില: {status}.",
                 "முன்பதிவு {id}: {property}, {check_in} முதல் {check_out}, {rooms} அறை. நிலை: {status}.",
                 "बुकिंग {id}: {property}, {check_in} से {check_out}, {rooms} कमरे। स्थिति: {status}।"),
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
    "use_options": _t("Please choose one of the options above, or type MENU to start again.",
                      "മുകളിലെ ഓപ്ഷനുകളിൽ ഒന്ന് തിരഞ്ഞെടുക്കൂ, അല്ലെങ്കിൽ വീണ്ടും തുടങ്ങാൻ MENU എന്ന് ടൈപ്പ് ചെയ്യൂ.",
                      "மேலுள்ள விருப்பங்களில் ஒன்றைத் தேர்ந்தெடுக்கவும், அல்லது மீண்டும் தொடங்க MENU என டைப் செய்யுங்கள்.",
                      "ऊपर दिए विकल्पों में से चुनें, या फिर से शुरू करने के लिए MENU लिखें।"),
    "opt_out": _t("Okay, we will not send you offers. Reply START any time to hear from us again.",
                  "ശരി, ഇനി ഓഫറുകൾ അയയ്ക്കില്ല. വീണ്ടും വേണമെങ്കിൽ START എന്ന് അയക്കൂ.",
                  "சரி, இனி சலுகைகள் அனுப்ப மாட்டோம். மீண்டும் வேண்டுமென்றால் START அனுப்புங்கள்.",
                  "ठीक है, अब ऑफ़र नहीं भेजेंगे। दोबारा चाहिए तो START भेजें।"),
    "opt_in": _t("Welcome back! You will hear from us again.", "വീണ്ടും സ്വാഗതം! ഇനി ഞങ്ങളുടെ സന്ദേശങ്ങൾ ലഭിക്കും.",
                 "மீண்டும் வரவேற்கிறோம்! இனி எங்கள் செய்திகள் வரும்.", "फिर से स्वागत है! अब हमारे संदेश मिलेंगे।"),
    "voice_problem": _t("Sorry, I could not understand that voice note. Could you type it or use the options?",
                        "ക്ഷമിക്കണം, ആ വോയ്സ് മനസ്സിലായില്ല. ടൈപ്പ് ചെയ്യാമോ, അല്ലെങ്കിൽ ഓപ്ഷനുകൾ ഉപയോഗിക്കാമോ?",
                        "மன்னிக்கவும், அந்தக் குரல் செய்தி புரியவில்லை. டைப் செய்யவா அல்லது விருப்பங்களைப் பயன்படுத்தவா?",
                        "क्षमा करें, वह वॉइस नोट समझ नहीं आया। कृपया टाइप करें या विकल्प चुनें।"),
    "unsupported": _t("I can read text and voice notes only. Please type or use the options.",
                      "എനിക്ക് ടെക്സ്റ്റും വോയ്സും മാത്രമേ വായിക്കാനാകൂ. ടൈപ്പ് ചെയ്യൂ അല്ലെങ്കിൽ ഓപ്ഷനുകൾ ഉപയോഗിക്കൂ.",
                      "என்னால் உரையும் குரல் செய்தியும் மட்டுமே படிக்க முடியும். டைப் செய்யுங்கள் அல்லது விருப்பங்களைப் பயன்படுத்துங்கள்.",
                      "मैं सिर्फ़ टेक्स्ट और वॉइस नोट पढ़ सकता हूँ। कृपया टाइप करें या विकल्प चुनें।"),
}


# ---- ID proof after payment, reminder, and feedback after checkout
S.update({
    "id_body_first": _t(
        "Thank you! For check-in we need an ID proof of every adult guest (required by law). It is stored securely and "
        "deleted {days} days after your stay.\n\nGuest {n} of {m}: which ID will you send?",
        "നന്ദി! ചെക്ക്-ഇന്നിന് എല്ലാ മുതിർന്ന അതിഥികളുടെയും ഐഡി തെളിവ് വേണം (നിയമപ്രകാരം ആവശ്യമാണ്). ഇത് സുരക്ഷിതമായി "
        "സൂക്ഷിക്കുകയും താമസം കഴിഞ്ഞ് {days} ദിവസത്തിനുശേഷം നീക്കം ചെയ്യുകയും ചെയ്യും.\n\nഅതിഥി {n}/{m}: ഏത് ഐഡിയാണ് അയക്കുന്നത്?",
        "நன்றி! செக்-இன் செய்ய ஒவ்வொரு பெரியவருக்கும் அடையாள அட்டை தேவை (சட்டப்படி கட்டாயம்). இது பாதுகாப்பாக "
        "வைக்கப்பட்டு, தங்கிய {days} நாட்களுக்குப் பிறகு நீக்கப்படும்.\n\nவிருந்தினர் {n}/{m}: எந்த அட்டையை அனுப்புவீர்கள்?",
        "धन्यवाद! चेक-इन के लिए हर वयस्क मेहमान का आईडी प्रूफ़ चाहिए (कानूनन ज़रूरी)। इसे सुरक्षित रखा जाता है और "
        "ठहरने के {days} दिन बाद हटा दिया जाता है।\n\nमेहमान {n}/{m}: आप कौन सी आईडी भेजेंगे?"),
    "id_body_next": _t("Guest {n} of {m}: which ID will you send?", "അതിഥി {n}/{m}: ഏത് ഐഡിയാണ് അയക്കുന്നത്?",
                       "விருந்தினர் {n}/{m}: எந்த அட்டையை அனுப்புவீர்கள்?", "मेहमान {n}/{m}: आप कौन सी आईडी भेजेंगे?"),
    "id_button": _t("Choose ID", "ഐഡി തിരഞ്ഞെടുക്കൂ", "அட்டை தேர்வு", "आईडी चुनें"),
    "id_aadhaar": _t("Aadhaar", "ആധാർ", "ஆதார்", "आधार"),
    "id_passport": _t("Passport", "പാസ്പോർട്ട്", "பாஸ்போர்ட்", "पासपोर्ट"),
    "id_dl": _t("Driving licence", "ഡ്രൈവിംഗ് ലൈസൻസ്", "ஓட்டுநர் உரிமம்", "ड्राइविंग लाइसेंस"),
    "id_voter": _t("Voter ID", "വോട്ടർ ഐഡി", "வாக்காளர் அட்டை", "वोटर आईडी"),
    "id_other": _t("Other ID", "മറ്റ് ഐഡി", "வேறு அட்டை", "अन्य आईडी"),
    "id_later": _t("Show at check-in", "ചെക്ക്-ഇന്നിൽ കാണിക്കാം", "செக்-இனில் காட்டுவேன்", "चेक-इन पर दिखाऊँगा"),
    "id_skiprest": _t("Skip the rest", "ബാക്കിയുള്ളവ ഒഴിവാക്കാം", "மீதியை தவிர்", "बाकी छोड़ें"),
    "id_skiprest_desc": _t("I will show them at check-in", "ചെക്ക്-ഇന്നിൽ കാണിക്കാം", "செக்-இனில் காட்டுவேன்",
                           "चेक-इन पर दिखाऊँगा"),
    "id_photo_ask": _t("Please send a clear photo of the {idname}. All the text should be readable.",
                       "{idname}-ന്റെ വ്യക്തമായ ഒരു ഫോട്ടോ അയക്കൂ. അക്ഷരങ്ങൾ വായിക്കാവുന്നതായിരിക്കണം.",
                       "{idname} அட்டையின் தெளிவான புகைப்படத்தை அனுப்புங்கள். எழுத்துகள் படிக்கும்படி இருக்க வேண்டும்.",
                       "{idname} की साफ़ फ़ोटो भेजें। सारा लिखा हुआ पढ़ने लायक होना चाहिए।"),
    "id_photo_bad": _t("Sorry, I can only accept a photo (JPG or PNG) of the ID. Please send it as a photo, or type "
                       "SKIP to show it at check-in.",
                       "ക്ഷമിക്കണം, ഐഡിയുടെ ഫോട്ടോ (JPG അല്ലെങ്കിൽ PNG) മാത്രമേ സ്വീകരിക്കൂ. ഫോട്ടോ ആയി അയക്കൂ, അല്ലെങ്കിൽ "
                       "ചെക്ക്-ഇന്നിൽ കാണിക്കാൻ SKIP എന്ന് ടൈപ്പ് ചെയ്യൂ.",
                       "மன்னிக்கவும், அட்டையின் புகைப்படம் (JPG அல்லது PNG) மட்டுமே ஏற்கப்படும். புகைப்படமாக அனுப்புங்கள், "
                       "அல்லது செக்-இனில் காட்ட SKIP என டைப் செய்யுங்கள்.",
                       "क्षमा करें, मैं आईडी की फ़ोटो (JPG या PNG) ही ले सकता हूँ। कृपया फ़ोटो के रूप में भेजें, या चेक-इन पर "
                       "दिखाने के लिए SKIP लिखें।"),
    "id_photo_big": _t("That file is too large. Please send a smaller photo (under 5 MB).",
                       "ആ ഫയൽ വളരെ വലുതാണ്. ചെറിയ ഫോട്ടോ (5 MB-ൽ താഴെ) അയക്കൂ.",
                       "அந்தக் கோப்பு மிகவும் பெரியது. சிறிய புகைப்படத்தை (5 MB-க்குக் கீழ்) அனுப்புங்கள்.",
                       "वह फ़ाइल बहुत बड़ी है। कृपया छोटी फ़ोटो (5 MB से कम) भेजें।"),
    "id_photo_fail": _t("Sorry, I could not download that photo. Please send it again.",
                        "ക്ഷമിക്കണം, ആ ഫോട്ടോ ലഭിച്ചില്ല. ഒന്നുകൂടി അയക്കൂ.",
                        "மன்னிக்கவும், அந்தப் புகைப்படம் வரவில்லை. மீண்டும் அனுப்புங்கள்.",
                        "क्षमा करें, वह फ़ोटो नहीं मिली। कृपया दोबारा भेजें।"),
    "id_last4_ask": _t("Received, thank you! If you like, type the last 4 digits of the ID number so the team can "
                       "match it, or tap Skip.",
                       "ലഭിച്ചു, നന്ദി! ടീമിന് ഒത്തുനോക്കാൻ ഐഡി നമ്പറിന്റെ അവസാന 4 അക്കങ്ങൾ ടൈപ്പ് ചെയ്യാം, അല്ലെങ്കിൽ Skip അമർത്തൂ.",
                       "கிடைத்தது, நன்றி! குழு சரிபார்க்க அட்டை எண்ணின் கடைசி 4 இலக்கங்களை டைப் செய்யலாம், அல்லது Skip அழுத்தவும்.",
                       "मिल गया, धन्यवाद! चाहें तो आईडी नंबर के आख़िरी 4 अंक लिखें ताकि टीम मिला सके, या Skip दबाएँ।"),
    "id_skip": _t("Skip", "ഒഴിവാക്കാം", "தவிர்", "छोड़ें"),
    "id_last4_retry": _t("Please type just 4 digits, or tap Skip.", "4 അക്കങ്ങൾ മാത്രം ടൈപ്പ് ചെയ്യൂ, അല്ലെങ്കിൽ Skip അമർത്തൂ.",
                         "4 இலக்கங்களை மட்டும் டைப் செய்யுங்கள், அல்லது Skip அழுத்தவும்.",
                         "सिर्फ़ 4 अंक लिखें, या Skip दबाएँ।"),
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
    "fb_body": _t("How was your stay?", "താമസം എങ്ങനെയുണ്ടായിരുന്നു?", "தங்கல் எப்படி இருந்தது?", "आपका ठहरना कैसा रहा?"),
    "fb_button": _t("Rate", "റേറ്റ് ചെയ്യൂ", "மதிப்பிடு", "रेट करें"),
    "fb_5": _t("★★★★★ Excellent", "★★★★★ ഉജ്ജ്വലം", "★★★★★ மிகச் சிறப்பு", "★★★★★ बहुत बढ़िया"),
    "fb_4": _t("★★★★ Very good", "★★★★ വളരെ നല്ലത്", "★★★★ மிக நன்று", "★★★★ बहुत अच्छा"),
    "fb_3": _t("★★★ Good", "★★★ നല്ലത്", "★★★ நன்று", "★★★ अच्छा"),
    "fb_2": _t("★★ Fair", "★★ ശരാശരി", "★★ சுமார்", "★★ ठीक-ठाक"),
    "fb_1": _t("★ Poor", "★ മോശം", "★ மோசம்", "★ खराब"),
    "fb_comment_ask": _t("Thank you! Would you like to add a comment? Type it here, or tap Skip.",
                         "നന്ദി! ഒരു അഭിപ്രായം ചേർക്കണോ? ഇവിടെ ടൈപ്പ് ചെയ്യൂ, അല്ലെങ്കിൽ Skip അമർത്തൂ.",
                         "நன்றி! ஒரு கருத்தைச் சேர்க்க விரும்புகிறீர்களா? இங்கே டைப் செய்யுங்கள், அல்லது Skip அழுத்தவும்.",
                         "धन्यवाद! कोई टिप्पणी जोड़ना चाहेंगे? यहाँ लिखें, या Skip दबाएँ।"),
    "fb_done": _t("Thank you for your feedback!", "അഭിപ്രായത്തിന് നന്ദി!", "உங்கள் கருத்துக்கு நன்றி!",
                  "आपकी प्रतिक्रिया के लिए धन्यवाद!"),
})


def t(key: str, lang: str, **values: object) -> str:
    table = S[key]
    text = table.get(lang) or table["en"]
    return text.format(**values) if values else text


def status_name(status: str, lang: str) -> str:
    pairs = dict(p.split("=", 1) for p in t("status_names", lang).split(","))
    return pairs.get(status, status)
