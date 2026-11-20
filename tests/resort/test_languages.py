"""Every kind of guest gets the client's rules (2026-11-18): English, Malayalam / Tamil / Hindi script, Manglish,
Tanglish, Hinglish, a guest who switches, and voice notes. Replies are always in English letters, the fixed messages
and the strict form hold whatever the language, and the details are read from any script."""

from __future__ import annotations

from datetime import date, datetime

import pytest
from test_bot import GUEST, InboundMessage, _ids, last_tool, reply, say, tool, world  # noqa: F401  (world is a fixture)

from app.resort.chat import INDIC, ChatMixin, contact_in, no_unasked_prices, parse_stay

FORM = "Please share these details:\n\nCheck in :\nCheck out :\nNumber of persons :\nNumber of rooms:"
TODAY = date(2026, 11, 2)


def no_native_script(w) -> None:
    for text in w.wa.texts():
        assert not INDIC.search(text), f"native script sent: {text!r}"


def system_language(w) -> str:
    return w.llm.calls[-1].system.rsplit("REPLY LANGUAGE", 1)[-1]


# --------------------------------------------------------------------------- reading the guest, any script


@pytest.mark.parametrize("text, ci, co, persons", [
    ("ഡിസംബർ 12 മുതൽ 15 വരെ 3 പേർ", date(2026, 12, 12), date(2026, 12, 15), 3),        # Malayalam script
    ("டிசம்பர் 12 முதல் 15 வரை 3 பேர்", date(2026, 12, 12), date(2026, 12, 15), 3),        # Tamil script
    ("दिसंबर 12 से 15 तक 3 लोग", date(2026, 12, 12), date(2026, 12, 15), 3),              # Hindi script
    ("12 दिसंबर, 2 रात, 2 लोग", date(2026, 12, 12), date(2026, 12, 14), 2),
    ("dec 12 se 15 tak 3 log", date(2026, 12, 12), date(2026, 12, 15), 3),                 # Hinglish
    ("12 dec la irundhu 15 varai 3 per", date(2026, 12, 12), date(2026, 12, 15), 3),       # Tanglish
    ("dec 20 nu 2 per, 22 vare", date(2026, 12, 20), date(2026, 12, 22), 2),               # Manglish
    ("dec 12 2 raat 4 log", date(2026, 12, 12), date(2026, 12, 14), 4),
])
def test_stay_details_are_read_in_every_language(text, ci, co, persons):
    assert parse_stay(text, TODAY) == {"check_in": ci, "check_out": co, "persons": persons}


@pytest.mark.parametrize("text, expected", [
    ("mera naam Rahul hai, 9876543210", ("Rahul", "+919876543210")),
    ("en peyar Kumar, 9876543210", ("Kumar", "+919876543210")),
    ("ente peru Anu 9847012345", ("Anu", "+919847012345")),
    ("yahi number", ("", "same")),
    ("same number thaan", ("", "same")),
    ("இதே நம்பர்", ("", "same")),
    ("यही नंबर है", ("", "same")),
    ("ഇതേ നമ്പർ", ("", "same")),
])
def test_contact_is_read_in_every_language(text, expected):
    assert contact_in(text) == expected


def test_prices_asked_in_any_language_are_answered_and_unasked_ones_are_dropped():
    line = "Room 3,000 rupees aagum."
    for asked in ("evlo price?", "kitna hai?", "ethra aakum?", "എത്ര ആകും?", "எவ்வளவு?", "कितना है?"):
        assert no_unasked_prices(line, asked) == line, asked
    assert no_unasked_prices(line, "room irukka?") == ""


def test_native_script_place_names_are_understood(world):
    w = world
    for text, prop in (("ആലപ്പുഴ", w.p1), ("ஆலப்புழா", w.p1), ("अलेप्पी", w.p1), ("കാന്തല്ലൂർ", w.p2)):
        assert w.bot._named_place(text).id == prop.id, text


# --------------------------------------------------------------------------- the reply language


@pytest.mark.parametrize("text, label", [
    ("is a room available on 12 Dec?", "English"),
    ("room undo? ethra aakum", "Manglish"),
    ("room irukka? evlo price?", "Tanglish"),
    ("kya room milega? kitna hai?", "Hinglish"),
    ("റൂം ഉണ്ടോ?", "Manglish"),
    ("ரூம் இருக்கா?", "Tanglish"),
    ("कमरा मिलेगा?", "Hinglish"),
])
def test_each_guest_gets_their_own_language_in_english_letters(world, text, label):
    w = world
    say(w, text, GUEST, reply("[no reply]"))
    assert f": {label}" in system_language(w), (text, system_language(w))


def test_a_reply_in_native_script_is_never_sent(world):
    w = world
    say(w, "ரூம் இருக்கா?", GUEST, reply("ஆம், ரூம் இருக்கு."), reply("ஆம் சார், ரூம் இருக்கு."))
    no_native_script(w)
    assert w.wa.texts()[-1] == "Sorry, konjam short-a innoru thadava type pannunga?"  # Tanglish (owner, 2026-11-18)
    say(w, "beach pakkam?", GUEST, reply("ஆம். Yes, the beach is right there."), reply("ஆம்."))
    assert w.wa.texts()[-1] == "Yes, the beach is right there."  # the English-letter part is kept


def test_a_tanglish_guest_answered_in_manglish_gets_a_tanglish_rewrite(world):
    w = world
    say(w, "room irukka? beach pakkam irukka?", GUEST, reply("Und, beach aduthu aanu."), reply("Irukku, beach pakkam thaan."))
    # the answer in Tanglish, then the booking moves on with the location question (owner, 2026-11-20)
    assert w.wa.texts()[-2] == "Irukku, beach pakkam thaan." and w.wa.texts()[-1].startswith("Sure, Kindly confirm")


def test_a_hinglish_guest_answered_in_english_gets_a_hinglish_rewrite(world):
    w = world
    say(w, "kya beach paas mein hai? pool hai kya?", GUEST,
        reply("Yes, the beach is right next to the property and every room has a view."),
        reply("Haan ji, beach bilkul paas hai, har room se view milega."))
    assert w.wa.texts()[-1] == "Haan ji, beach bilkul paas hai, har room se view milega."


def test_sir_ji_and_saar_count_as_sir_once(world):
    w = world
    say(w, "beach paas hai kya?", GUEST, reply("Haan sir ji, beach paas hai."))
    say(w, "pool hai kya?", GUEST, reply("Nahi sir ji, pool nahi hai."))
    say(w, "food milega?", GUEST, reply("Saar, Swiggy se mil jayega."))
    assert w.wa.texts() == ["Haan sir ji, beach paas hai.", "Nahi, pool nahi hai.", "Swiggy se mil jayega."]


# --------------------------------------------------------------------------- the whole flow, any script


@pytest.mark.parametrize("hello, wants, dates, rooms_ok, ask_contact", [
    ("നമസ്കാരം", "ആലപ്പുഴ റൂം ഉണ്ടോ?", "ഡിസംബർ 12 മുതൽ 15 വരെ 2 പേർ",
     "Und, ningalude dates-il rooms available aanu.", "Interested aanenkil ningalude name-um mobile number-um ayakkamo?"),
    ("வணக்கம்", "ஆலப்புழா ரூம் இருக்கா?", "டிசம்பர் 12 முதல் 15 வரை 2 பேர்",
     "Aamaa, unga dates-ku rooms available irukku.", "Interested-na unga name-um mobile number-um anuppunga?"),
    ("नमस्ते", "अलेप्पी में कमरा मिलेगा?", "दिसंबर 12 से 15 तक 2 लोग",
     "Haan, aapki dates par rooms available hain.", "Interested hain to apna name aur mobile number bhejiye?"),
])
def test_native_script_guest_full_flow(world, hello, wants, dates, rooms_ok, ask_contact):
    w = world
    assert say(w, hello) == "greeting"
    # the AI asks for the place although it was named: the place is read in code, the form follows
    say(w, wants, GUEST, tool("ask_location"), reply("[no reply]"))
    assert w.wa.texts()[-1] == FORM
    # the AI asks for dates in its own words although they were all given: rooms are checked in code
    say(w, dates, GUEST, reply("Evlo per varuvinga? Check out date sollunga."))
    assert w.wa.texts()[-2:] == [rooms_ok, ask_contact]
    assert w.repo.get_state(GUEST)["bk"]["check_out"] == "2026-12-15"
    say(w, "Asha, 9847012345", GUEST, tool("create_booking", guest_name="Asha", mobile="9847012345"), reply("[no reply]"))
    texts = w.wa.texts()
    assert texts[-2].startswith("Following are the glimpse of your enquiry") and "Guest - Asha" in texts[-2]
    no_native_script(w)


def test_a_guest_who_switches_language_gets_the_new_language_and_fixed_messages_follow(world):
    w = world
    say(w, "room undo? Alleppey", GUEST, tool("ask_stay_details", place="Alleppey"), reply("[no reply]"))
    assert w.repo.get_state(GUEST)["lang"] == "mn"
    say(w, "Is there parking?", GUEST, reply("Yes, there is free parking."))
    assert ": English" in system_language(w) and w.repo.get_state(GUEST)["lang"] == "en"
    say(w, "kya pool hai?", GUEST, reply("Nahi, pool nahi hai."))
    assert ": Hinglish" in system_language(w)


# --------------------------------------------------------------------------- voice notes


class FakeVoice:
    def __init__(self, text):
        self.text = text

    def process(self, media_id):
        return type("R", (), {"text": self.text})()


def voice(w, transcript, *script):
    w.llm.script.extend(script)
    w.bot._voice_in = lambda: FakeVoice(transcript)
    m = InboundMessage(f"wamid.v{next(_ids)}", GUEST, "audio", datetime(2026, 11, 2, 4, 30), media_id="m1")
    return w.bot.handle(m)


@pytest.mark.parametrize("transcript, label", [
    ("ആലപ്പുഴയിൽ റൂം ഉണ്ടോ", "Manglish"),
    ("ஆலப்புழாவில் ரூம் இருக்கா", "Tanglish"),
    ("अलेप्पी में कमरा मिलेगा क्या", "Hinglish"),
    ("is there a room in Alleppey", "English"),
])
def test_voice_notes_are_answered_in_text_in_english_letters(world, transcript, label):
    w = world
    voice(w, transcript, tool("ask_stay_details", place="Alleppey"), reply("[no reply]"))
    assert w.wa.texts()[-1] == FORM  # the place in the voice note is understood; the reply is the fixed form
    assert all(kind != "audio" for _, kind, _ in w.wa.to(GUEST))  # never a voice reply
    no_native_script(w)
    voice(w, "beach " + transcript, reply("[no reply]"))
    assert f": {label}" in system_language(w)


def test_a_voice_note_that_was_not_understood_gets_one_short_text(world):
    w = world
    w.bot._voice_in = lambda: FakeVoice(None)
    m = InboundMessage(f"wamid.v{next(_ids)}", GUEST, "audio", datetime(2026, 11, 2, 4, 30), media_id="m1")
    assert w.bot.handle(m) == "voice_problem"
    assert len(w.wa.texts()) == 1 and not INDIC.search(w.wa.texts()[0])


def test_no_fixed_message_in_any_language_has_native_script():
    """The owner (2026-11-18): every text uses English letters only, whatever the guest's language or script."""
    from string import Formatter

    from app.resort.i18n import FIXED_LANGS, S, t
    for key in S:
        for lang in (*FIXED_LANGS, "ml", "ta", "hi"):
            fields = {f for _, f, _, _ in Formatter().parse(S[key].get("en", "")) if f}
            text = t(key, lang, **{f: "X" for f in fields}) if fields else t(key, lang)
            assert not INDIC.search(text), (key, lang, text)


def test_the_last_guard_never_lets_native_script_out():
    from app.resort.chat import english_letters_only
    assert english_letters_only("Guest - अनु\n98470 12345\n2 PAX") == "98470 12345\n2 PAX"
    assert english_letters_only("ഹായ്") == "Sorry, could you please type that once more in a few words?"
    assert english_letters_only("Hi, How may I assist you?") == "Hi, How may I assist you?"


def test_a_native_script_name_never_reaches_the_guest_in_text(world):
    w = world
    say(w, "Alleppey, 12 dec to 15 dec, 2 persons", GUEST,
        tool("check_availability", place="Alleppey", check_in="2026-12-12", check_out="2026-12-15", adults=2),
        reply("[no reply]"))
    say(w, "अनु, यही नंबर", GUEST, tool("create_booking", guest_name="अनु", mobile="same"), reply("[no reply]"))
    texts = w.wa.texts()
    assert texts[-2].startswith("Following are the glimpse of your enquiry") and "Guest -" not in texts[-2]
    no_native_script(w)


def test_a_price_the_ai_made_up_is_replaced_by_the_real_one(world):
    w = world
    say(w, "Alleppey, 10 nov to 12 nov, 2 persons", GUEST,
        tool("check_availability", place="Alleppey", check_in="2026-11-10", check_out="2026-11-12", adults=2),
        reply("[no reply]"))
    say(w, "how much is it?", GUEST, reply("It is Rs 2,000 a night, Rs 4,000 in total."))  # no tool: made up
    assert w.wa.texts()[-1] == "Deluxe: ₹4,000 per night, ₹8,000 for 2 nights."


def test_the_ai_does_not_ask_name_and_mobile_again_after_the_fixed_ask(world):
    w = world
    say(w, "Alleppey, 10 nov to 12 nov, 2 persons", GUEST,
        tool("check_availability", place="Alleppey", check_in="2026-11-10", check_out="2026-11-12", adults=2),
        reply("[no reply]"))
    say(w, "kya pool hai?", GUEST, reply("Nahi, pool nahi hai. Aapka naam aur mobile number share kijiye."))
    assert w.wa.texts()[-1] == "Nahi, pool nahi hai."


def test_a_name_only_question_after_the_number_is_saved_is_sent(world):
    w = world
    say(w, "Alleppey, 10 nov to 12 nov, 2 persons", GUEST,
        tool("check_availability", place="Alleppey", check_in="2026-11-10", check_out="2026-11-12", adults=2),
        reply("[no reply]"))
    say(w, "this number is fine", GUEST, reply("We have your mobile saved. What is your name?"))
    assert w.wa.texts()[-1] in ("We have your mobile saved. What is your name?", "Please share your name.")


def test_a_hindi_voice_note_with_a_native_name_asks_the_name_in_english_letters(world):
    w = world
    say(w, "Alleppey, 10 nov to 12 nov, 2 persons", GUEST,
        tool("check_availability", place="Alleppey", check_in="2026-11-10", check_out="2026-11-12", adults=2),
        reply("[no reply]"))
    voice(w, "अनु, यही नंबर", reply("Aapka naam bata dijiye?"))
    # names only in English letters (owner, 2026-11-18): the Hindi-script name is not used, the name is asked
    assert w.repo.list_bookings(phone=GUEST) == [] and w.wa.texts()[-1] == "Apna name bataiye?"  # Hinglish
    say(w, "Anu", GUEST, tool("create_booking", guest_name="Anu", mobile="same"), reply("[no reply]"))
    assert w.repo.list_bookings(phone=GUEST)[0].guest_name == "Anu" and w.wa.texts()[-1].startswith("Is link se advance pay")
    no_native_script(w)


def test_an_ignored_image_is_not_shown_to_the_ai(world):
    w = world
    say(w, "Is there parking?", GUEST, reply("Yes, free parking."))
    w.bot.handle(InboundMessage(f"wamid.i{next(_ids)}", GUEST, "image", datetime(2026, 11, 2, 4, 30), media_id="img"))
    say(w, "ok and wifi?", GUEST, reply("Yes, Wi-Fi is there."))
    assert "[image]" not in "\n".join(m.content for m in w.llm.calls[-1].messages)


def test_a_tanglish_guest_stays_tanglish_on_a_message_with_unknown_words(world):
    w = world
    say(w, "room irukka?", GUEST, reply("[no reply]"))
    say(w, "dec 20 la 2 per", GUEST, reply("[no reply]"))
    assert ": Tanglish" in system_language(w)


def test_latin_only_keeps_english_letter_sentences():
    st = {"lang": "en"}
    assert ChatMixin._latin_only("Yes. ആണ്.", "", st) == "Yes."
    assert ChatMixin._latin_only("ആണ്.", "Yes it is.", st) == "Yes it is."
    assert ChatMixin._latin_only("ആണ്.", "", {"lang": "mn"}) == "Kshamikkanam, onnu koodi churukki type cheyyamo?"


# --------------------------------------------------------------------------- Step 3 (2026-11-18): Tanglish / Hinglish
# fixed messages, language lock, Tamil vs Malayalam word lists, never an audio reply


@pytest.mark.parametrize("text, style", [
    ("enakku oru room venum", "tanglish"), ("enikk oru room venam", "manglish"),
    ("room irukka?", "tanglish"), ("room undo?", "manglish"),
    ("evlo price?", "tanglish"), ("ethra aanu rate?", "manglish"),
    ("sollunga", "tanglish"), ("parayamo", "manglish"),
    ("booking pannunga", "tanglish"), ("booking cheyyamo", "manglish"),
    ("inniki room irukka", "tanglish"), ("innu room undo", "manglish"),
    ("naalaikku room venum", "tanglish"), ("naale room venam", "manglish"),
    ("date maathanum, 28th ku", "tanglish"), ("GPay number tharo?", "manglish"),
    ("owner ne vilikkanam", "manglish"), ("kamra chahiye kya", "hinglish"),
])
def test_tamil_and_malayalam_words_in_english_letters(text, style):
    st: dict = {}
    ChatMixin._language_note(st, text)
    assert st["style"] == style


def test_neutral_messages_keep_the_last_language():
    st: dict = {}
    for text in ("room venum", "ok", "2", "👍"):
        ChatMixin._language_note(st, text)
        assert st["style"] == "tanglish", text


def test_an_asked_language_stays_until_the_guest_switches():
    st: dict = {}
    for text, style in [("entha rate", "manglish"), ("sorry English please", "en"), ("ok", "en"), ("2", "en"),
                        ("room undo", "en"),  # short: the asked language stays
                        ("ningalude resort il pool undo ennu parayamo", "manglish"),  # a long Manglish message switches
                        ("Tamil la pesunga", "tanglish"), ("pool?", "tanglish"), ("Hindi mein baat karo", "hinglish")]:
        ChatMixin._language_note(st, text)
        assert st["style"] == style, text


@pytest.mark.parametrize("wants, step", [
    ("room venum", "Sure, Kindly confirm the Location"),   # the owner's English wording for everyone
    ("kamra chahiye", "Sure, Kindly confirm the Location"),
])
def test_tamil_and_hindi_guests_get_the_owners_location_question(world, wants, step):
    assert say(world, wants) == "location" and world.wa.texts()[-1].startswith(step)


@pytest.mark.parametrize("dates, rooms_ok, ask_contact", [
    ("12 dec to 14 dec, 2 per, room venum", "Aamaa, unga dates-ku rooms available irukku.",
     "Interested-na unga name-um mobile number-um anuppunga?"),
    ("12 dec se 14 dec, 2 log, kamra chahiye", "Haan, aapki dates par rooms available hain.",
     "Interested hain to apna name aur mobile number bhejiye?"),
])
def test_tamil_and_hindi_guests_get_the_fixed_steps_in_their_language(world, dates, rooms_ok, ask_contact):
    w = world
    say(w, dates, GUEST, tool("check_availability", place="Alleppey", check_in="2026-12-12", check_out="2026-12-14",
                              adults=2), reply("[no reply]"))
    texts = w.wa.texts()
    assert rooms_ok in texts and texts[-1] == ask_contact
    no_native_script(w)


def test_the_location_re_ask_never_swallows_a_real_question(world):
    w = world
    assert say(w, "Rooms?") == "location"
    assert say(w, "ethra aanu rate?", GUEST, reply("Dates parayamo?")) != "location"
    assert say(w, "pool undo?", GUEST, reply("Illa, pool illa.")) != "location"
    assert w.repo.get_state(GUEST)["style"] == "manglish"


def test_the_bot_never_sends_audio(world):
    w = world
    voice(w, "ഡിസംബർ ഇരുപതിന് രണ്ട് പേർക്ക് റൂം വേണം", reply("Dates confirm cheyyamo?"))
    voice(w, "டிசம்பர் இருபது ரெண்டு பேருக்கு ரூம் வேணும்", reply("Check out date sollunga?"))
    assert not [k for _, k, _ in w.wa.to(GUEST) if k in ("audio", "voice")]
    no_native_script(w)


import pytest  # noqa: E402


@pytest.mark.parametrize("text", ["Are pets allowed?", "Unmarried couples allowed?", "Is alcohol allowed?",
                                  "Check in - 25th Nov\nCheck out - 26th Nov\n3 ppl", "can we bring a pet",
                                  "Do you have rooms with a sea view?", "Any offer for 2 nights?"])
def test_english_with_words_the_list_does_not_know_stays_english(text):
    """2026-11-20: unknown English words ('pets', 'unmarried', 'ppl') made English guests get Manglish replies."""
    from app.resort.chat import latin_style
    assert latin_style(text) == "en"


@pytest.mark.parametrize("text", ["Alleppey il 10 nu 2 divasam, 2 per", "enikk oru room venam", "ee room mathi",
                                  "dec 12 nu varum", "njangal 4 per und"])
def test_real_manglish_stays_manglish(text):
    from app.resort.chat import latin_style
    assert latin_style(text) == "manglish"
