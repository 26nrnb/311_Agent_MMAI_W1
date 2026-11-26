"""Output check (owner: B): the checks run on every agent reply before the
resident sees it. The interaction tree (flow.yaml, "reply" hook) decides what
happens when one fires.

M3 [hard]: a ticket ID or handoff reference that no tool returned in this
    conversation means the model invented it; the reply is blocked.
C1/C2/C3/C4/C7 [hard, Policy v1.0]: commitment phrases (dates, priorities,
    fines, refunds, visits). commitment_rule() says which rule a phrase breaks;
    the "reply" tree replaces the reply with the approved wording. A phrase in a
    refusal ("I can't offer a refund") or a question is not a commitment (NB-10).
D8: policy rule IDs the model echoes from its prompt, such as "(M1)", are
    removed by strip_rule_ids() before the reply is checked or shown.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from src.config import config_cache, guardrails_config


@dataclass
class OutputResult:
    ok: bool
    flags: list[str] = field(default_factory=list)
    rule_id: str | None = None


@config_cache
def _patterns():
    cfg = guardrails_config()["output_check"]
    return (
        [re.compile(p, re.IGNORECASE) for p in cfg["commitment"]],
        re.compile(cfg["ticket_id"]),
        re.compile(cfg["handoff_ref"]),
        re.compile(cfg["ticket_claim"], re.IGNORECASE),
    )


@config_cache
def _rule_tag():
    return re.compile(guardrails_config()["output_check"]["rule_id_tag"], re.IGNORECASE)


def strip_rule_ids(reply: str) -> tuple[str, bool]:
    """D8: remove parenthesised rule IDs. Returns (clean reply, whether any were removed)."""
    clean = _rule_tag().sub("", reply)
    if clean == reply:
        return reply, False
    clean = re.sub(r"[ \t]+([.,;:!?])", r"\1", clean)   # "assistant (M1)." -> "assistant."
    clean = re.sub(r"[ \t]{2,}", " ", clean)
    return clean.strip(), True


def ids_in(text: str) -> set[str]:
    """Ticket and handoff reference IDs in a text."""
    _, ticket_re, handoff_re, _ = _patterns()
    return set(ticket_re.findall(text or "")) | set(handoff_re.findall(text or ""))


def invented_ids(reply: str, known_ids: set[str]) -> list[str]:
    """M3 [hard]: ticket or handoff IDs in the reply that no tool returned (and the
    resident didn't give: repeating their own ticket number is not inventing one)."""
    return sorted(ids_in(reply) - known_ids)


def claim_phrase(reply: str) -> str | None:
    """M3 [hard]: text claiming a request was created/filed (checked when none was)."""
    m = _patterns()[3].search(reply)
    return m.group(0) if m else None


@config_cache
def _handoff_claim():
    pattern = guardrails_config()["output_check"].get("handoff_claim")   # absent in older configs
    return re.compile(pattern, re.IGNORECASE) if pattern else None


def handoff_claim_phrase(reply: str) -> str | None:
    """M3 [hard]: text claiming the request was passed to a person, or giving a reference
    (checked when no handoff was made)."""
    pattern = _handoff_claim()
    m = pattern.search(reply) if pattern else None
    return m.group(0) if m else None


@config_cache
def _refusal_res():
    """C1-C4, C7: a phrase in a refusal ("I can't offer a refund") or a question is not a
    commitment. guardrails.yaml output_check.commitment_negation; absent = old behaviour."""
    pat = guardrails_config()["output_check"].get("commitment_negation")
    return re.compile(pat, re.IGNORECASE) if pat else None


# A clause starts after a sentence end or a line break; "but" or ";" starts a new clause,
# so "I can't say when, but a crew will visit tomorrow" is still a commitment.
_SENTENCE_START = re.compile(r"[.!?\n]")
_CLAUSE_BREAK = re.compile(r"\bbut\b|\bhowever\b|;", re.IGNORECASE)


def _is_refused(reply: str, start: int, end: int) -> bool:
    """True if the phrase at reply[start:end] sits in a negated clause or in a question."""
    neg = _refusal_res()
    if neg is None:
        return False
    before = reply[:start]
    cut = max((m.end() for m in _SENTENCE_START.finditer(before)), default=0)
    clause = before[cut:]
    breaks = list(_CLAUSE_BREAK.finditer(clause))
    if breaks:
        clause = clause[breaks[-1].end():]
    if neg.search(clause):
        return True
    rest = _SENTENCE_START.search(reply, end)   # D-030: a question promises nothing
    return bool(rest) and rest.group(0) == "?"


def _commitments(reply: str) -> list[str]:
    """Every commitment phrase in the reply that is not refused or asked (see _is_refused)."""
    return [m.group(0) for p in _patterns()[0] for m in p.finditer(reply or "")
            if not _is_refused(reply, m.start(), m.end())]


def commitment_phrase(reply: str) -> str | None:
    """C1/C2/C3/C4/C7: the first commitment phrase in the reply."""
    found = _commitments(reply)
    return found[0] if found else None


# Which C rule a commitment phrase breaks, checked in this order (a phrase such as
# "will visit tomorrow" is a visit first, C7, then a date, C1).
_RULE_WORDS = (
    ("C7", re.compile(r"visit|inspect|investigat|come (?:out|by)|stop by|look into|send ", re.I)),
    ("C4", re.compile(r"refund|compensat|reimburs|waive|\b(?:pay|cover)\b", re.I)),
    ("C3", re.compile(r"fined|ticketed|charged|violat|by-?law|breach", re.I)),
    ("C2", re.compile(r"priorit|urgent|fast[- ]?track|expedit", re.I)),
)


def commitment_rule(reply: str) -> str | None:
    """The rule (C7, C4, C3, C2 or C1, in that order) that the reply's commitment
    phrases break; None if there are none. All phrases are looked at, so "a crew
    will come by tomorrow" is C7 even though "by tomorrow" (C1) matches first."""
    phrases = " | ".join(_commitments(reply))
    if not phrases:
        return None
    return next((rule for rule, words in _RULE_WORDS if words.search(phrases)), "C1")


def check(reply: str, known_ids: set[str], ticket_created: bool = True) -> OutputResult:
    """All three checks at once. The agent runs them one by one through the
    interaction tree (flow.yaml, "reply" hook); this is kept for tests and tools."""
    invented = invented_ids(reply, known_ids)
    if invented:
        return OutputResult(ok=False, flags=[f"invented_id:{i}" for i in invented], rule_id="M3")
    if not ticket_created and claim_phrase(reply):
        return OutputResult(ok=False, flags=["claimed_ticket_without_create"], rule_id="M3")
    rule = commitment_rule(reply)
    return OutputResult(ok=True, flags=["commitment"] if rule else [], rule_id=rule)
