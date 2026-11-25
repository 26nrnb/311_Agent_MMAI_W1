"""Input screen (owner: B): runs on every resident message before the model.

Returns flags; src/agent/orchestrator.py decides what to do with them:
  emergency     -> E1 [hard] 911 wording, no ticket, no model call
  self_harm     -> E8 handoff with crisis line
  injection     -> S1/S5 flagged and logged; message still treated as data
  human_request -> E7 handoff
  status_request, new_report -> A12 status check vs new report (orchestrator keeps them in RuleState)
  redacted      -> D2 [hard] items removed before anything else sees the text
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from src.config import config_cache, guardrails_config
from src.guardrails.masking import redact


@dataclass
class ScreenResult:
    text: str                      # redacted text, safe to pass on
    flags: list[str] = field(default_factory=list)
    redacted: list[str] = field(default_factory=list)
    matches: dict[str, str] = field(default_factory=dict)   # flag -> the pattern that matched

    def has(self, flag: str) -> bool:
        return flag in self.flags


@config_cache
def _patterns() -> dict[str, list[re.Pattern]]:
    cfg = guardrails_config()["input_screen"]
    return {name: [re.compile(p, re.IGNORECASE) for p in pats] for name, pats in cfg.items()}


def screen(text: str) -> ScreenResult:
    clean, redacted = redact(text)
    matches = {}
    for name, pats in _patterns().items():
        hit = next((p.pattern for p in pats if p.search(clean)), None)
        if hit:
            matches[name] = hit
    return ScreenResult(text=clean, flags=list(matches), redacted=redacted, matches=matches)


def _strings(obj) -> list[str]:
    if isinstance(obj, str):
        return [obj]
    if isinstance(obj, dict):
        return [s for v in obj.values() for s in _strings(v)]
    if isinstance(obj, (list, tuple)):
        return [s for v in obj for s in _strings(v)]
    return []


def screen_args(args: dict | None) -> ScreenResult:
    """S5 [hard]: the same screen on a tool call's arguments (location, description and every
    other text field), so instructions hidden in a field are caught, not only in the message."""
    return screen("\n".join(_strings(args or {})))
