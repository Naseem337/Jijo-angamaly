import pytest

from app.core.language import (
    classify_confirmation,
    detect_language,
    detect_script,
    is_opt_in,
    is_opt_out,
)

# --------------------------------------------------------------------------- language


@pytest.mark.parametrize("text, expected", [
    ("Do you have a Swift under 6 lakh?", "en"),
    ("Hi, I'm looking for a diesel SUV", "en"),
    ("swift undo?", "manglish"),
    ("ethra aanu vila?", "manglish"),
    ("enikku oru diesel car venam, budget 8 lakh", "manglish"),
    ("സ്വിഫ്റ്റ് ഉണ്ടോ?", "ml"),
    ("Swift ഉണ്ടോ? വില എത്രയാണ്", "ml"),     # mixed script, mostly Malayalam
    ("എട്ട് ലക്ഷം budget", "ml"),
])
def test_detect_language(text: str, expected: str) -> None:
    assert detect_language(text) == expected


def test_no_letters_falls_back_to_previous_language() -> None:
    assert detect_language("👍", default="ml") == "ml"
    assert detect_language("2019", default="manglish") == "manglish"
    assert detect_script("🙏") == "none"


# --------------------------------------------------------------------------- opt-out


@pytest.mark.parametrize("text", [
    "STOP", "stop", "Stop alerts", "stop pls", "unsubscribe", "Not interested",
    "I am not interested", "venda", "ini venda", "Venda chetta", "msg ayakkanda",
    "please don't send these messages anymore", "വേണ്ട", "ഇനി വേണ്ട",
    "താൽപ്പര്യമില്ല", "മെസ്സേജ് അയക്കണ്ട",
    "ഇനി മെസ്സേജ് ഒന്നും അയക്കേണ്ട",
])
def test_opt_out_detected(text: str) -> None:
    assert is_opt_out(text), text


@pytest.mark.parametrize("text", [
    "ee car venda, vere undo?",              # "not this car, any other?"
    "swift venda, baleno undo",              # rejecting a model, not alerts
    "not interested in diesel, need petrol",
    "ഈ കാർ വേണ്ട, വേറെ ഉണ്ടോ?",
    "Where is your showroom? Near the bus stop?",
    "yes interested",
    "",
])
def test_opt_out_not_triggered_by_normal_chat(text: str) -> None:
    assert not is_opt_out(text), text


def test_extra_opt_out_phrases_from_config() -> None:
    assert not is_opt_out("nirthu")
    assert is_opt_out("nirthu", extra_phrases=["nirthu"])


@pytest.mark.parametrize("text, expected", [
    ("START", True), ("start alerts", True), ("resume", True),
    ("start the car?", False), ("hello", False),
])
def test_opt_in(text: str, expected: bool) -> None:
    assert is_opt_in(text) is expected


# --------------------------------------------------------------------------- yes / no


@pytest.mark.parametrize("text, expected", [
    ("yes", "yes"), ("Ok", "yes"), ("sheri", "yes"), ("athe correct", "yes"),
    ("ശരി", "yes"), ("അതെ", "yes"), ("👍 ok", "yes"),
    ("no", "no"), ("alla", "no"), ("അല്ല", "no"), ("no, budget is 7 lakh", "no"),
    ("budget is 7 lakh", None), ("what about baleno?", None), ("", None),
])
def test_classify_confirmation(text: str, expected: str | None) -> None:
    assert classify_confirmation(text) == expected


# --------------------------------------------------------------------------- structured reply language (2026-10-21)

@pytest.mark.parametrize("text, expected", [
    ("Nee parnjath avde unden aan", "manglish"),   # spellings no word list knows
    ("ullath vid", "manglish"),
    ("Porche undo", "manglish"),
    ("Swift nte price ethra", "manglish"),
    ("Is the Creta automatic available in white?", "en"),
    ("wat is d prize of swift", "en"),             # English with typos stays English
    ("Zest available", "en"),
])
def test_detect_language_manglish_spellings_and_english_typos(text: str, expected: str) -> None:
    assert detect_language(text, default="ml") == expected


@pytest.mark.parametrize("text", ["Hi", "ok thanks", "crysta", "innova crysta", "Pattambi"])
def test_neutral_messages_keep_the_customers_language(text: str) -> None:
    assert detect_language(text, default="ml") == "ml"
    assert detect_language(text, default="en") == "en"


@pytest.mark.parametrize("reply, language, wrong", [
    ("Cresta venamo? ഏത് മോഡല്‍, ബജറ്റ് പറയാമോ.", "ml", True),      # Manglish mixed into Malayalam
    ("ഇന്നോവ Crysta ഇപ്പോൾ stock-ൽ ഇല്ല. Budget എത്രയാണ്?", "ml", False),  # car words may stay English
    ("Got no Innova Crysta in stock. Similar SUVs nokkamo?", "en", True),
    ("Yes, the 2019 Swift VXi is available for ₹5.5 lakh (TC-1E6434).", "en", False),
    ("Swift undu, 2019 model, ₹5.5 lakh (TC-1E6434). Kaananamo?", "manglish", False),
    ("Swift undu. ഏത് വേണം?", "manglish", True),                    # Malayalam script in Manglish
    ("We have 2 Swift cars under 6 lakh. Which one would you like to see?", "manglish", True),
])
def test_reply_language_problem(reply: str, language: str, wrong: bool) -> None:
    from app.core.language import reply_language_problem
    assert (reply_language_problem(reply, language) is not None) == wrong


@pytest.mark.parametrize("text, expected", [
    ("👍", True), ("🙏🙏", True), ("❤️", True), ("👍 👍", True), ("😊!", True),
    ("?", False), ("2019", False), ("ok 👍", False), ("ശരി 🙏", False), ("", False), ("   ", False),
])
def test_is_emoji_only(text: str, expected: bool) -> None:
    from app.core.language import is_emoji_only
    assert is_emoji_only(text) is expected
