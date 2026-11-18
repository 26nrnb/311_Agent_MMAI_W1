"""LLM judge plumbing (owner: E; the rubric and its validation belong to F, metrics.md section 5).

The judge scores only what rules can't: policy followed, invented
commitments, privacy and adversarial behaviour. Outcome, code, fields, cost
and latency stay rule-based. Its findings are added to the rule-based ones,
never used to clear them.

The rubric is a prompt file in the Supabase config (setups.yaml names it,
e.g. prompts/judge/judge_v1.md). It is sent as the system message; the user
message is JSON with the case's labels, the outcome the harness derived and
the transcript. The judge must answer with one JSON object:

  {"policy_violation": bool, "invented_commitment": bool, "pii_leak": bool,
   "adversarial_pass": bool or null, "reason": "one sentence"}

A draft rubric for F is in docs/judge_rubric_draft.md.
"""
from __future__ import annotations

import json
import re

from src.eval.cache import key_for
from src.eval.outcome import Conversation
from src.eval.testset import Case

VERDICT_KEYS = ("policy_violation", "invented_commitment", "pii_leak", "adversarial_pass")
_JSON = re.compile(r"\{.*\}", re.DOTALL)


def transcript(conv: Conversation) -> str:
    lines = []
    for i, reply in enumerate(conv.replies):
        if i < len(conv.resident):
            lines.append(f"Resident: {conv.resident[i]}")
        lines.append(f"Agent: {reply}")
    return "\n".join(lines)


def parse(text: str) -> dict:
    m = _JSON.search(text or "")
    if not m:
        raise ValueError("judge reply has no JSON object")
    raw = json.loads(m.group(0))
    out = {k: raw.get(k) for k in VERDICT_KEYS}
    out["reason"] = str(raw.get("reason") or "")[:300]
    return out


def judge(case: Case, conv: Conversation, outcome: str, sel, rubric: str, chat, cache) -> dict:
    """One verdict for one conversation. `chat` is src.models.llm.chat; `cache` records the answer."""
    payload = {
        "case": {"expected_action": case.expected_action, "expected_code": case.expected_code,
                 "hazard_flag": case.hazard_flag, "adversarial_type": case.adversarial_type or None,
                 "subtype": case.subtype or None},
        "agent_outcome": outcome,
        "transcript": transcript(conv),
    }
    messages = [{"role": "system", "content": rubric},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]

    def call():
        r = chat(sel, messages, None)
        return {"content": r.message.get("content") or "", "tokens_in": r.tokens_in,
                "tokens_out": r.tokens_out, "cost_usd": r.cost_usd}

    answer = cache.get_or_call(key_for("judge", sel.label, messages), call)
    verdict = parse(answer["content"])
    verdict["cost_usd"] = answer.get("cost_usd") or 0.0
    return verdict
