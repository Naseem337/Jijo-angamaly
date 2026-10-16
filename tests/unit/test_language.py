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
