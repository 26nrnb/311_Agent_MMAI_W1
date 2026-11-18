"""Load a locked test set (owner: E, W5). The harness reads test files; it never edits them.

Two record shapes are in use, so both are accepted and mapped to one Case:

  data pipeline / 311 Interfaces doc:  id, service_code, expected_action, ticket_ref, gold_fields, ...
  docs/interfaces.md (Nov 2 draft):    interaction_id, expected_code, expected_action, ...

A record may also carry `turns`: the resident's follow-up messages after the
first one, for multi-turn cases. The scorer only ever sees Case fields.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Case:
    case_id: str
    message: str
    expected_code: str
    expected_action: str
    hazard_flag: bool = False
    lang: str = "en"
    rule_id: str = ""
    ticket_ref: str | None = None
    gold_fields: dict = field(default_factory=dict)
    difficulty: str = ""
    subtype: str = ""
    turns: list[str] = field(default_factory=list)

    @property
    def messages(self) -> list[str]:
        return [self.message, *self.turns]

    @property
    def adversarial_type(self) -> str:
        """The rule the attack tests (results_format.md: taken from the case's rule_id)."""
        return self.rule_id if self.difficulty == "adversarial" else ""


def _bool(v) -> bool:
    if isinstance(v, str):
        return v.strip().lower() in ("true", "1", "yes")
    return bool(v)


def to_case(rec: dict) -> Case:
    case_id = rec.get("id") or rec.get("interaction_id")
    code = rec.get("service_code") or rec.get("expected_code")
    if not (case_id and rec.get("message") and code and rec.get("expected_action")):
        raise ValueError(f"record is missing id, message, code or expected_action: {str(rec)[:120]}")
    gold = rec.get("gold_fields") or {}
    if isinstance(gold, str):            # label sheets store it as JSON text
        gold = json.loads(gold or "{}")
    turns = rec.get("turns") or []
    if isinstance(turns, str):
        turns = json.loads(turns or "[]")
    return Case(
        case_id=str(case_id), message=str(rec["message"]), expected_code=str(code),
        expected_action=str(rec["expected_action"]), hazard_flag=_bool(rec.get("hazard_flag")),
        lang=rec.get("lang") or "en", rule_id=rec.get("rule_id") or "",
        ticket_ref=rec.get("ticket_ref") or rec.get("expected_ticket_id") or None,
        gold_fields=gold, difficulty=rec.get("difficulty") or "", subtype=rec.get("subtype") or "",
        turns=[str(t) for t in turns],
    )


def file_hash(path: str | Path) -> str:
    """sha256 of the file bytes, as recorded in data/test/LOCK.md at the lock."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load(path: str | Path) -> list[Case]:
    path = Path(path)
    cases = [to_case(json.loads(line)) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    ids = [c.case_id for c in cases]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        raise ValueError(f"{path}: duplicate case ids {dupes[:5]}")
    return cases
