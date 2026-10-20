"""Staff `LEAD <phone> <free text>` → phone + structured LeadRequirement.

The free text ("Swift 6L petrol under 60000km, 2018+, single owner") is turned into fields
by the LLM (the client's normal provider chain; no model name here). Budgets always go
through app.core.money ("6L", "6 lakh", "600000"), and model names through the client's
model aliases. If the LLM is unavailable or returns junk, a rule-based reader fills the
same fields, so a LEAD never fails just because the AI is down; anything it adds on top of
the LLM's answer only fills gaps.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from app.core.money import extract_numbers, parse_budget
from app.core.phone import try_normalize_phone
from app.inventory.aliases import compact
from app.providers.llm import ChatMessage, LLMError, LLMProvider
from app.storage.models import LeadRequirement

# Staff notes up to this many words are read by the rules alone when they find something;
# longer or unusual notes (names, colours, timings) also go to the AI.
RULES_ONLY_MAX_WORDS = 8
COMMAND_RE = re.compile(r"^\s*leads?\s*:?(?=\s|$)", re.IGNORECASE)
_PHONE_TOKEN = re.compile(r"^[+\d()\-.]+$")

FUELS = {"petrol": "petrol", "diesel": "diesel", "cng": "cng", "electric": "electric", "ev": "electric",
         "hybrid": "hybrid"}
GEARBOXES = {"automatic": "automatic", "auto": "automatic", "at": "automatic", "amt": "automatic",
             "cvt": "automatic", "dct": "automatic", "manual": "manual", "mt": "manual"}
BODIES = {"hatchback": "hatchback", "hatch": "hatchback", "sedan": "sedan", "suv": "suv", "compact suv": "suv",
          "muv": "muv", "mpv": "muv", "7 seater": "muv", "seven seater": "muv", "pickup": "pickup"}
MAKES = {"maruti": "Maruti Suzuki", "suzuki": "Maruti Suzuki", "hyundai": "Hyundai", "tata": "Tata",
         "mahindra": "Mahindra", "toyota": "Toyota", "honda": "Honda", "kia": "Kia", "skoda": "Skoda",
         "volkswagen": "Volkswagen", "vw": "Volkswagen", "renault": "Renault", "nissan": "Nissan", "mg": "MG",
         "ford": "Ford", "chevrolet": "Chevrolet", "jeep": "Jeep", "citroen": "Citroen", "bmw": "BMW",
         "audi": "Audi", "mercedes": "Mercedes-Benz", "benz": "Mercedes-Benz", "volvo": "Volvo",
         "datsun": "Datsun", "fiat": "Fiat", "mini": "Mini"}

EXTRACT_INSTRUCTION = """A car dealer's staff member typed a short note about a yard visitor who didn't find the car they want. Turn it into JSON with exactly these keys (use null or [] when not mentioned; never guess):
{"name": customer's name or null,
 "budget_min": lowest price as written (e.g. "4L") or null,
 "budget_max": highest price as written (e.g. "6L", "6 lakh", "600000") or null,
 "makes": brands, e.g. ["Maruti Suzuki"],
 "models": models, e.g. ["Swift"],
 "body_type": one of hatchback/sedan/suv/muv/pickup or null,
 "fuel": one of petrol/diesel/cng/electric/hybrid or null,
 "gearbox": manual/automatic or null,
 "max_km": highest odometer reading as a number or null,
 "max_owners": highest number of previous owners as a number or null,
 "min_year": oldest acceptable model year as a number or null,
 "notes": anything else useful (colour, timing, exchange car) in a few words, or null}
A bare number like "6" next to a budget means lakh. Reply with the JSON only."""


@dataclass(frozen=True)
class LeadCommand:
    phone: str | None  # E.164, or None if no valid number was found
    text: str  # the free text after the phone
    raw: str  # what was there instead of a valid phone (for the error message)


def is_lead_command(text: str) -> bool:
    """`LEAD …`, `LEADS …`, `lead: …` at the very start. "the lead from yesterday" is not."""
    return bool(COMMAND_RE.match(text or ""))


def split_lead_command(text: str) -> LeadCommand:
    """Find the phone right after the keyword, even written with spaces ("98470 12345")."""
    body = COMMAND_RE.sub("", text, count=1).strip()
    tokens = body.split()
    best: tuple[str, int] | None = None
    for i in range(len(tokens)):
        if not _PHONE_TOKEN.match(tokens[i]):
            break
        candidate = try_normalize_phone("".join(tokens[: i + 1]))
        if candidate:
            best = (candidate, i + 1)
    if best:
        return LeadCommand(best[0], " ".join(tokens[best[1]:]), "")
    return LeadCommand(None, body, tokens[0] if tokens else "")


# --------------------------------------------------------------------------- normalising


def _rupees(value: Any) -> int | None:
    if value in (None, "", 0):
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(round(value * 100_000 if 0 < value < 100 else value))
    return parse_budget(str(value))


def _int(value: Any, lo: int, hi: int) -> int | None:
    try:
        number = int(float(str(value).replace(",", "").lower().replace("km", "").strip()))
    except (TypeError, ValueError):
        return None
    return number if lo <= number <= hi else None


def _pick(value: Any, table: Mapping[str, str]) -> str | None:
    return table.get(" ".join(str(value).lower().split())) if value else None


def _as_list(value: Any) -> list[str]:
    if not value:
        return []
    items = value if isinstance(value, list) else [value]
    return [str(v).strip() for v in items if str(v).strip()]


def canonical_model(name: str, aliases: Mapping[str, list[str]]) -> str:
    """'crysta' → 'innova crysta', 'XUV 7OO' → 'xuv700' (client aliases); unknown names as written."""
    key = compact(name)
    for canonical, spellings in aliases.items():
        if key == compact(canonical) or key in {compact(s) for s in spellings}:
            return canonical
    return name.strip()


def _models(names: Iterable[str], aliases: Mapping[str, list[str]]) -> list[str]:
    out: list[str] = []
    for name in names:
        model = canonical_model(name, aliases)
        if model and model.lower() not in {m.lower() for m in out}:
            out.append(model)
    return out


def _makes(names: Iterable[str]) -> list[str]:
    out: list[str] = []
    for name in names:
        make = MAKES.get(compact(name), name.strip())
        if make and make not in out:
            out.append(make)
    return out


def from_llm_json(data: Mapping[str, Any], aliases: Mapping[str, list[str]]) -> tuple[LeadRequirement, str | None]:
    req = LeadRequirement(
        budget_min=_rupees(data.get("budget_min")),
        budget_max=_rupees(data.get("budget_max")),
        makes=_makes(_as_list(data.get("makes"))),
        models=_models(_as_list(data.get("models")), aliases),
        body_type=_pick(data.get("body_type"), BODIES),
        fuel=_pick(data.get("fuel"), FUELS),
        gearbox=_pick(data.get("gearbox"), GEARBOXES),
        max_km=_int(data.get("max_km"), 1, 2_000_000),
        max_owners=_int(data.get("max_owners"), 1, 10),
        min_year=_int(data.get("min_year"), 1990, 2100),
        notes=(str(data["notes"]).strip()[:200] or None) if data.get("notes") else None,
    )
    name = str(data.get("name")).strip() if data.get("name") else None
    return req, name


# --------------------------------------------------------------------------- rule-based reader


def _has(text: str, word: str) -> bool:
    return re.search(rf"(?<![a-z0-9]){re.escape(word)}(?![a-z0-9])", text) is not None


def rule_based(text: str, aliases: Mapping[str, list[str]], known_models: Iterable[str] = ()) -> LeadRequirement:
    """The fallback reader: budget, km, year, owners, fuel, gearbox, body type, make, model."""
    low = " ".join(text.lower().split())
    numbers = extract_numbers(text)
    amounts = sorted(int(n.value) for n in numbers if n.unit == "inr" and n.value >= 10_000)
    if m := re.search(r"(\d+(?:\.\d+)?)\s*(?:-|to)\s*(\d+(?:\.\d+)?)\s*(?:l|lk|lakh|lakhs|ലക്ഷം)(?![a-z])", low):
        amounts = sorted({int(float(m.group(1)) * 100_000), int(float(m.group(2)) * 100_000), *amounts})  # "12-15 lakh"
    kms = [int(n.value) for n in numbers if n.unit == "km"]
    years = [int(n.value) for n in numbers if n.unit == "none" and n.value.is_integer() and 1990 <= n.value <= 2100]
    owners = None
    if m := re.search(r"(1st|first|single|one|2nd|second|two|3rd|third|three)[\s-]*owner", low):
        owners = {"1st": 1, "first": 1, "single": 1, "one": 1, "2nd": 2, "second": 2, "two": 2}.get(m.group(1), 3)
    names = [*aliases.keys(), *(s for spellings in aliases.values() for s in spellings), *known_models]
    found = [n for n in set(names) if len(compact(n)) >= 3 and _has(low, n.lower())]
    models = sorted(found, key=lambda n: (low.find(n.lower()), -len(n)))  # in the order staff typed them
    return LeadRequirement(
        budget_min=amounts[0] if len(amounts) >= 2 else None,
        budget_max=amounts[-1] if amounts else None,
        makes=_makes(k for k in MAKES if _has(low, k)),
        models=_models(models, aliases),
        body_type=next((v for k, v in BODIES.items() if _has(low, k)), None),
        fuel=next((v for k, v in FUELS.items() if _has(low, k)), None),
        gearbox=next((v for k, v in GEARBOXES.items() if _has(low, k)), None),
        max_km=max(kms) if kms else None,
        max_owners=owners,
        min_year=min(years) if years else None,
    )


def _fill_gaps(main: LeadRequirement, extra: LeadRequirement) -> LeadRequirement:
    for key in main.__dataclass_fields__:
        if key != "notes" and getattr(main, key) in (None, []) and getattr(extra, key) not in (None, []):
            setattr(main, key, getattr(extra, key))
    return main


def extract_requirement(llm: LLMProvider | None, text: str, aliases: Mapping[str, list[str]],
                        known_models: Iterable[str] = ()) -> tuple[LeadRequirement, str | None, bool]:
    """(requirement, customer name, whether the LLM was used)."""
    known_models = list(known_models)
    fallback = rule_based(text, aliases, known_models)
    if not fallback.is_empty() and len(text.split()) <= RULES_ONLY_MAX_WORDS:
        return fallback, None, False  # short note fully understood by the rules: no AI tokens spent
    if llm is not None:
        try:
            response = llm.complete(EXTRACT_INSTRUCTION, [ChatMessage.user(text)], None, 500)
            raw = re.sub(r"^```(?:json)?|```$", "", response.text.strip(), flags=re.MULTILINE).strip()
            data = json.loads(raw)
            if isinstance(data, dict):
                req, name = from_llm_json(data, aliases)
                return _fill_gaps(req, fallback), name, True
        except (LLMError, ValueError):
            pass
    return fallback, None, False
