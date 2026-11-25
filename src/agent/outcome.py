"""The conversation's outcome so far, logged on every reply (owner: D).

One of the ten outcome names in policy.yaml `outcomes` (Operating Policy
v1.0). The eval harness takes the last reply's outcome as the agent's final
outcome (src/eval/outcome.py falls back to its own rules only when none is
logged). It is decided from what the agent did, never from the expected
label, in this order (the first that applies wins):

  1. the 911 reply was given (E1)                                 redirect_911
  2. handed off under a rule listed under handoff_oos (E10, S6...)  handoff_oos
     ... under a rule listed under handoff_language (M6)            handoff_language
     ... under any other rule                                       escalate_human
  3. two or more tickets created                                    handle_each
  4. no ticket, and create_ticket refused a case the catalogue
     doesn't handle (A8, a private tree)                            its outcome (handoff_oos)
     no ticket, and a reply named an open ticket search_tickets
     returned, or used the possible_duplicate wording (C6)          link_duplicate
     no ticket, and get_status found the resident's ticket (A12)    report_status
  5. the tree fired a refusal rule (S1...), or a reply used the
     who_reported wording (D4), even if the genuine part of the
     request became a ticket (the "continue")                       refuse_and_continue
  6. one ticket created (a genuine request with instructions hidden
     in its fields is still a ticket: S5 is not a refusal rule)     create_ticket
  8. a reply used the out_of_scope wording, or kb_search answered
     a general question while no request was being taken (A13)      handoff_oos
  9. the reply asks the resident something                          ask_clarification
  Otherwise nothing is logged.
"""
from __future__ import annotations

import re

from src.agent.rules import RuleState
from src.config import config_cache, policy
from src.guardrails.output_check import ids_in

OUTCOMES = ("create_ticket", "ask_clarification", "link_duplicate", "escalate_human", "redirect_911",
            "handoff_oos", "handle_each", "refuse_and_continue", "handoff_language", "report_status")

# Used only if policy.yaml has no `outcomes` section (older versions).
_DEFAULT_RULES = {"handoff_oos": ["E10", "S6"], "handoff_language": ["M6"],
                  "refuse_and_continue": ["D3", "D4", "S1", "S2", "S4", "S7"]}
# D8 is listed under refuse_and_continue, but its tree check only strips rule tags from a
# reply; firing it doesn't mean anything was refused.
_NOT_A_REFUSAL = {"D8"}


def _rules(outcome: str) -> set[str]:
    return set((policy().get("outcomes") or {}).get(outcome) or _DEFAULT_RULES[outcome])


@config_cache
def _wording_patterns() -> dict[str, re.Pattern]:
    """Each approved wording as a pattern; {placeholders} match any text."""
    out = {}
    for key, template in (policy().get("wording") or {}).items():
        parts = re.split(r"\{\w+\}", template.strip())
        out[key] = re.compile(".+?".join(re.escape(p) for p in parts), re.IGNORECASE)
    return out


def note_reply(state: RuleState, reply: str) -> None:
    """Record what a reply did that decides the outcome: approved wording used, duplicate named."""
    state.wordings |= {k for k, p in _wording_patterns().items() if p.search(reply or "")}
    open_ids = {t["ticket_id"] for t in state.possible_duplicates}
    named = ids_in(reply or "") & open_ids
    if named and not state.linked_id:
        state.linked_id = sorted(named)[0]


def outcome(state: RuleState, reply: str) -> str | None:
    """The conversation's outcome after this reply (see the module docstring)."""
    if "E1" in state.fired or "emergency" in state.wordings:
        return "redirect_911"
    if state.handed_off:
        if state.handoff_rule in _rules("handoff_oos"):
            return "handoff_oos"
        if state.handoff_rule in _rules("handoff_language"):
            return "handoff_language"
        return "escalate_human"
    if len(state.ticket_ids) >= 2:
        return "handle_each"
    if not state.ticket_ids:
        if state.not_handled in OUTCOMES:
            return state.not_handled
        if state.linked_id or "possible_duplicate" in state.wordings:
            return "link_duplicate"
        if state.status_found:
            return "report_status"
    # Same order as the eval's own rules (src/eval/outcome.py 6, 7): a refusal outranks one ticket.
    if state.fired & (_rules("refuse_and_continue") - _NOT_A_REFUSAL) or "who_reported" in state.wordings:
        return "refuse_and_continue"
    if state.ticket_ids:
        return "create_ticket"
    if "out_of_scope" in state.wordings or (state.kb_answered and not state.looked_up):
        return "handoff_oos"
    if "?" in (reply or ""):
        return "ask_clarification"
    return None
