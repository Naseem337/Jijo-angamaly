"""Guest text in Malayalam / Tamil / Hindi script (live voice notes, 2026-11-26).

The guest's text always goes to the AI unchanged. These helpers only let code compare a place name the guest wrote in
native script with the dashboard's Latin names (ആലപ്പുഴ ~ Alappuzha, കാന്തല്ലൂർ ~ Kanthalloor), the same fuzzy way a
misspelling is compared. Nothing here reads what the guest means.
"""

from __future__ import annotations

import re

INDIC = re.compile(r"[ऀ-ॿ஀-௿ഀ-ൿ]")

# Malayalam: a rough sound-alike in English letters (good enough for a fuzzy name match)
_ML_VOWELS = dict(zip("അആഇഈഉഊഋഎഏഐഒഓഔ", ["a", "aa", "i", "ee", "u", "oo", "ru", "e", "e", "ai", "o", "o", "au"]))
_ML_SIGNS = dict(zip("ാിീുൂൃെേൈൊോൌൗ", ["aa", "i", "ee", "u", "oo", "ru", "e", "e", "ai", "o", "o", "au", "au"]))
_ML_CONS = dict(zip("കഖഗഘങചഛജഝഞടഠഡഢണതഥദധനപഫബഭമയരലവശഷസഹളഴറ",
                    ["k", "kh", "g", "gh", "ng", "ch", "chh", "j", "jh", "nj", "t", "th", "d", "dh", "n", "th", "th", "d",
                     "dh", "n", "p", "ph", "b", "bh", "m", "y", "r", "l", "v", "sh", "sh", "s", "h", "l", "zh", "r"]))
_ML_CHILLU = dict(zip("ൺൻർൽൾൿ", ["n", "n", "r", "l", "l", "k"]))
_ML_VIRAMA, _ML_ANUSVARA = "്", "ം"

# Tamil and Devanagari: the same idea
_TA_VOWELS = dict(zip("அஆஇஈஉஊஎஏஐஒஓஔ", ["a", "aa", "i", "ee", "u", "oo", "e", "e", "ai", "o", "o", "au"]))
_TA_SIGNS = dict(zip("ாிீுூெேைொோௌ", ["aa", "i", "ee", "u", "oo", "e", "e", "ai", "o", "o", "au"]))
_TA_CONS = dict(zip("கஙசஞடணதநபமயரலவழளறனஜஷஸஹ",
                    ["k", "ng", "s", "nj", "d", "n", "th", "n", "p", "m", "y", "r", "l", "v", "zh", "l", "r", "n", "j",
                     "sh", "s", "h"]))
_HI_VOWELS = dict(zip("अआइईउऊऋएऐओऔ", ["a", "aa", "i", "ee", "u", "oo", "ri", "e", "ai", "o", "au"]))
_HI_SIGNS = dict(zip("ािीुूृेैोौ", ["aa", "i", "ee", "u", "oo", "ri", "e", "ai", "o", "au"]))
_HI_CONS = dict(zip("कखगघङचछजझञटठडढणतथदधनपफबभमयरलवशषसह",
                    ["k", "kh", "g", "gh", "ng", "ch", "chh", "j", "jh", "nj", "t", "th", "d", "dh", "n", "t", "th", "d",
                     "dh", "n", "p", "ph", "b", "bh", "m", "y", "r", "l", "v", "sh", "sh", "s", "h"]))

_SCRIPTS = [(_ML_VOWELS, _ML_SIGNS, _ML_CONS, _ML_CHILLU, _ML_VIRAMA, _ML_ANUSVARA),
            (_TA_VOWELS, _TA_SIGNS, _TA_CONS, {}, "்", "ஂ"),
            (_HI_VOWELS, _HI_SIGNS, _HI_CONS, {}, "्", "ं")]


def to_latin(text: str) -> str:
    """Native-script letters in rough English letters; other characters as they are."""
    if not INDIC.search(text or ""):
        return text or ""
    out: list[str] = []
    pending = False  # a consonant waiting for its vowel (the inherent 'a' unless a sign or virama follows)
    for ch in text:
        for vowels, signs, cons, chillu, virama, anusvara in _SCRIPTS:
            if ch in cons:
                if pending:
                    out.append("a")
                out.append(cons[ch])
                pending = True
                break
            if ch in signs:
                out.append(signs[ch])
                pending = False
                break
            if ch == virama:
                pending = False
                break
            if ch in vowels or ch in chillu or ch == anusvara:
                if pending:
                    out.append("a")
                out.append(vowels.get(ch) or chillu.get(ch) or "m")
                pending = False
                break
        else:
            if pending:
                out.append("a")
                pending = False
            out.append(ch)
    if pending:
        out.append("a")
    return "".join(out)
