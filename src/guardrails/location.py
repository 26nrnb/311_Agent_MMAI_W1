"""A11 [hard] location check (owner: B).

create_ticket accepts only a usable location: a street address, an
intersection, or a named landmark on a street ("the library on Elm St").
The patterns are guardrails.yaml `location_check.usable`, compiled
case-sensitive ((?i:...) inside a pattern ignores case). A location that
matches none of them ("near the big tree") is refused as a missing field,
so the agent asks again and E6 hands off after two tries.
"""
from __future__ import annotations

import re

from src.config import config_cache, guardrails_config


@config_cache
def _patterns() -> list[re.Pattern]:
    cfg = guardrails_config().get("location_check") or {}
    return [re.compile(p) for p in cfg.get("usable") or []]


def location_usable(text: str | None) -> bool:
    """True if `text` names a street address, an intersection or a landmark on a street.
    With no patterns configured (an older guardrails.yaml) every location is accepted."""
    pats = _patterns()
    if not pats:
        return True
    return any(p.search(str(text or "")) for p in pats)
