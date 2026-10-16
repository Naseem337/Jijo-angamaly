"""Model-name matching that copes with how customers actually type car names.

"Swift", "maruti swift", "wagonr", "Wagon-R", "XUV 7OO", "crysta" all need to land on the
right cars. Names are compared in a compact form (lower-case letters and digits only),
and business_rules.yaml `model_aliases` maps extra spellings onto a canonical name.
Used by inventory search now and by the alert matcher (step 10).
"""

from __future__ import annotations

import re
from collections.abc import Mapping

_NON_ALNUM = re.compile(r"[^a-z0-9]")


def compact(name: str) -> str:
    """'Wagon-R ' -> 'wagonr', 'XUV 700' -> 'xuv700'."""
    return _NON_ALNUM.sub("", name.lower())


class ModelAliases:
    """canonical name -> spellings, e.g. {"innova crysta": ["crysta", "innova"]}.

    A spelling may belong to several canonicals ("swift dzire" is listed under both
    swift and dzire); a query using it then matches either model.
    """

    def __init__(self, aliases: Mapping[str, list[str]] | None = None) -> None:
        self._lookup: dict[str, set[str]] = {}
        for canonical, spellings in (aliases or {}).items():
            target = compact(canonical)
            for spelling in [canonical, *spellings]:
                self._lookup.setdefault(compact(spelling), set()).add(target)

    def resolve(self, name: str) -> set[str]:
        """Compact forms a name may refer to: itself plus any canonical names."""
        key = compact(name)
        return {key, *self._lookup.get(key, ())} if key else set()

    def car_matches(self, make: str, model: str, variant: str, wanted: str) -> bool:
        """Does the customer's `wanted` name refer to this car?

        True if the names resolve to a common canonical model, or if any resolved form
        appears inside "make model variant" (so "maruti" or "swift vxi" also match).
        """
        names = self.resolve(wanted)
        if not names:
            return True
        if names & self.resolve(model):
            return True
        full = compact(f"{make} {model} {variant}")
        return any(n in full for n in names)
