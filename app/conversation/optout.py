"""STOP / START replies (PLAN.md Q9: say exactly what was stopped, in the customer's language).

Opting out stops alerts and marketing (chat wishlists and yard-visitor leads alike); the
customer can still chat with the bot. It is confirmed once; a second STOP gets no reply.
"""

from __future__ import annotations

from app.core.client_config import ClientConfig
from app.core.language import detect_script


def reply_language(text: str, known: str) -> str:
    """Malayalam script in this message wins; otherwise the customer's last language."""
    return "ml" if detect_script(text) == "malayalam" else known


def opt_out_confirmation(config: ClientConfig, language: str) -> str:
    name = config.display_name
    return {
        "ml": f"ശരി. ഇനി {name}-ൽ നിന്ന് കാർ അലേർട്ടുകളോ ഓഫർ മെസ്സേജുകളോ WhatsApp-ൽ വരില്ല. ഞങ്ങളോട് എപ്പോൾ "
              "വേണമെങ്കിലും ചാറ്റ് ചെയ്യാം. അലേർട്ടുകൾ വീണ്ടും വേണമെങ്കിൽ START എന്ന് അയയ്ക്കൂ.",
        "manglish": f"Sheri. Ini {name}-il ninnu car alerts-o offer messages-o WhatsApp-il varilla. Njangalodu "
                    "eppo venelum chat cheyyam. Alerts veendum venel START ennu ayakku.",
        "en": f"Done. You won't get any more car alerts or offers from {name} on WhatsApp. You can still chat "
              "with us anytime. Reply START if you want alerts again.",
    }.get(language) or opt_out_confirmation(config, "en")


def opt_in_confirmation(config: ClientConfig, language: str) -> str:
    name = config.display_name
    return {
        "ml": f"വീണ്ടും സ്വാഗതം! {name}-ൽ നിന്നുള്ള കാർ അലേർട്ടുകൾ വീണ്ടും ഓൺ ആയി. വേണ്ടെങ്കിൽ STOP എന്ന് അയച്ചാൽ മതി.",
        "manglish": f"Welcome back! {name}-il ninnulla car alerts veendum on aayi. Venda enkil STOP ennu ayachal mathi.",
        "en": f"Welcome back! Car alerts from {name} are on again. Reply STOP anytime to turn them off.",
    }.get(language) or opt_in_confirmation(config, "en")
