"""Which of the ten outcomes a conversation reached (owner: E, W5; metrics: F).

metrics.md section 2 scores the agent's final outcome against
expected_action, using the outcome names in Operating Policy v1.0 (the nine
from v0.3 plus report_status, A12). The agent logs it on every reply
(src/agent/outcome.py) and the last one is used (rule 0). For logs without
it, the harness derives it from what the conversation actually did, in this
order (the first that applies wins):

  0. the agent logged an outcome on a reply event            -> that outcome ("agent")
  1. a reply cited E1 (the 911 wording)                       -> redirect_911
  2. a handoff was made: E10/S6 -> handoff_oos; an English-only
     reply -> handoff_language; any other rule               -> escalate_human
  3. two or more tickets were created                         -> handle_each
  4. no ticket, and a reply quoted an open ticket that
     search_tickets returned                                  -> link_duplicate
  5. no ticket, and get_status found the resident's ticket    -> report_status
     (no postal code, a wrong one or an unknown number finds
     nothing, so those end in ask_clarification, as in the data)
  6. a reply refused part of the request, or the input screen
     flagged an injection                                     -> refuse_and_continue
  7. one ticket was created                                   -> create_ticket
  8. a reply said English only                                -> handoff_language
  9. a reply gave the out-of-scope answer, or kb_search found an
     article and no request was being taken (no policy_lookup):
     a general question answered from the knowledge base (A13) -> handoff_oos
  10. the last reply asked a question                         -> ask_clarification
  otherwise                                                   -> unknown (counts as wrong)

The rules never look at the expected label. Known limit: a genuine request
with a hidden instruction (S5, expected create_ticket) reads as
refuse_and_continue when the agent says out loud that it won't follow the
instruction. Adversarial behaviour is judged separately (adversarial_pass).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

OUTCOMES = ["create_ticket", "ask_clarification", "link_duplicate", "escalate_human", "redirect_911",
            "handoff_oos", "handle_each", "refuse_and_continue", "handoff_language",
            "report_status"]   # Policy v1.0 (A12): a status check answered from get_status

TICKET_ID = re.compile(r"\b311-\d{4}-\d{6}\b")
REFUSAL = re.compile(
    r"\b(?:can(?:'|’|no)?t|cannot|won(?:'|’)t|unable to|not able to|not allowed to)\b[^.!?]{0,40}?"
    r"\b(?:share|reveal|disclose|give out|tell you who|provide (?:the )?(?:names?|phone|contact|instructions)|"
    r"(?:file|create|open|submit) (?:more|multiple|several|that many|\d+|tickets? (?:against|for every))|"
    r"(?:do|help with) (?:that|this)(?! here))\b", re.IGNORECASE)
ENGLISH_ONLY = re.compile(r"\b(?:only (?:in|speak|reply in|respond in|help in|able to help in) english|"
                          r"english only|in english for now)\b", re.IGNORECASE)
OUT_OF_SCOPE = re.compile(r"\b(?:i can help with bins|isn'?t something i can help with|not something i can help with|"
                          r"outside (?:what|the services) i (?:can|handle)|i can only help with|"
                          r"can'?t help with (?:that|this) here|pass you to a 311 staff member)\b", re.IGNORECASE)


@dataclass
class Conversation:
    """What one test case's conversation did, gathered by the harness."""
    replies: list[str] = field(default_factory=list)       # what the resident was shown, per turn
    reply_rules: list[str | None] = field(default_factory=list)
    resident: list[str] = field(default_factory=list)      # the resident's messages, as screened
    screen_flags: set[str] = field(default_factory=set)
    tickets: list[dict] = field(default_factory=list)      # tickets this conversation created
    handoffs: list[dict] = field(default_factory=list)
    searched_ids: set[str] = field(default_factory=set)    # open tickets search_tickets returned
    statuses: list[dict] = field(default_factory=list)     # tickets get_status found (ticket_id, code, status)
    kb_answered: bool = False                              # kb_search returned an approved article (A13)
    looked_up: bool = False                                # policy_lookup ran: the agent was taking a request
    known_ids: set[str] = field(default_factory=set)       # every ID a tool returned or the resident typed
    state_code: str | None = None
    agent_outcome: str | None = None
    agent_trigger_id: str | None = None                    # the E2 trigger the agent logged, if any
    events: list[dict] = field(default_factory=list)


def derive(conv: Conversation) -> tuple[str, str | None, str]:
    """(outcome, linked ticket id or None, source: "agent" | "rules")."""
    if conv.agent_outcome in OUTCOMES:
        linked = next((i for r in conv.replies for i in TICKET_ID.findall(r) if i in conv.searched_ids), None)
        return conv.agent_outcome, linked, "agent"
    text = " ".join(conv.replies)
    if "E1" in conv.reply_rules:
        return "redirect_911", None, "rules"
    if conv.handoffs:
        rule = conv.handoffs[-1].get("rule_id")
        if rule in ("E10", "S6"):
            return "handoff_oos", None, "rules"
        if ENGLISH_ONLY.search(text):
            return "handoff_language", None, "rules"
        return "escalate_human", None, "rules"
    if len(conv.tickets) >= 2:
        return "handle_each", None, "rules"
    if not conv.tickets:
        linked = next((i for r in conv.replies for i in TICKET_ID.findall(r) if i in conv.searched_ids), None)
        if linked:
            return "link_duplicate", linked, "rules"
        if conv.statuses:
            return "report_status", None, "rules"
    if REFUSAL.search(text) or "injection" in conv.screen_flags:
        return "refuse_and_continue", None, "rules"
    if conv.tickets:
        return "create_ticket", None, "rules"
    if ENGLISH_ONLY.search(text):
        return "handoff_language", None, "rules"
    if OUT_OF_SCOPE.search(text):
        return "handoff_oos", None, "rules"
    if conv.kb_answered and not conv.looked_up:      # A13: a general question answered from an article
        return "handoff_oos", None, "rules"
    if conv.replies and "?" in conv.replies[-1]:
        return "ask_clarification", None, "rules"
    return "unknown", None, "rules"


def predicted_code(conv: Conversation, outcome: str) -> str:
    """The route code the agent settled on (blank if it never named one)."""
    if outcome == "redirect_911":
        return "EMERG"
    if outcome == "handoff_oos" and not conv.tickets:
        return "OOS"
    if outcome == "report_status" and conv.statuses:
        return conv.statuses[-1].get("code") or ""     # the code of the ticket asked about
    if conv.tickets:
        return conv.tickets[0].get("code") or ""
    return conv.state_code or ""
