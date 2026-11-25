"""LLM judge plumbing (owner: E; the rubric and its validation belong to F, metrics.md section 5).

The judge scores only what rules can't: policy followed, invented
commitments, privacy and adversarial behaviour. Outcome, code, fields, cost
and latency stay rule-based. Its findings are added to the rule-based ones,
never used to clear them.

The rubric is a prompt file in the Supabase config (setups.yaml names it:
prompts/judge/judge_v1.md, owned by F). It is sent as the system message; the
user message is JSON with:

  case            expected_action, expected_code, hazard_flag, adversarial_type, subtype,
                  required_fields (policy.yaml, for the expected code) and
                  missing_fields (the required fields the message leaves out)
  agent_outcome   the outcome the harness derived
  transcript      Resident: / Agent: lines, with every tool call and its result as a
                  Tool: line where it happened, so the judge can check facts (C5)
                  and clarifying questions (M2) against what the tools returned.
                  Tool lines come from the event log, so they are masked (D6).

The judge must answer with one JSON object:

  {"policy_violation": bool, "invented_commitment": bool, "pii_leak": bool,
   "adversarial_pass": bool or null, "reason": "one sentence"}
"""
from __future__ import annotations

import json
import re

from src.eval.cache import key_for
from src.eval.outcome import Conversation
from src.eval.testset import Case

VERDICT_KEYS = ("policy_violation", "invented_commitment", "pii_leak", "adversarial_pass")
_JSON = re.compile(r"\{.*\}", re.DOTALL)
MAX_TOOL_CHARS = 600   # a long kb_search result is cut so one case can't flood the judge's context


def _tool_line(event: dict) -> str:
    """One Tool: line from a tool:<name> event: its arguments and what it returned."""
    name = event["step"].split(":", 1)[1]
    try:
        data = json.loads(event.get("text") or "{}")
    except ValueError:
        data = {"text": event.get("text")}
    if event.get("outcome") == "blocked":
        line = f"Tool: {name} blocked by rule {event.get('rule_id')}: {event.get('text')}"
    elif name == "handoff":
        line = (f"Tool: handoff(rule {event.get('rule_id')}, reason {json.dumps(data.get('reason'))}) -> "
                f"{json.dumps(data.get('result'), ensure_ascii=False)}")
    else:
        result = data.get("result") if data.get("result") is not None else {"error": data.get("error")}
        line = (f"Tool: {name}({json.dumps(data.get('args'), ensure_ascii=False)}) -> "
                f"{json.dumps(result, ensure_ascii=False)}")
    return line if len(line) <= MAX_TOOL_CHARS else line[:MAX_TOOL_CHARS] + " ...(cut)"


def transcript(conv: Conversation) -> str:
    """Resident, Tool and Agent lines in the order they happened. The order comes from the
    event log; resident and agent text from the conversation itself."""
    lines, said, replied = [], 0, 0
    for e in conv.events:
        step = e.get("step") or ""
        if step == "input_screen" and said < len(conv.resident):
            lines.append(f"Resident: {conv.resident[said]}")
            said += 1
        elif step.startswith("tool:"):
            lines.append(_tool_line(e))
        elif step == "reply" and replied < len(conv.replies):
            lines.append(f"Agent: {conv.replies[replied]}")
            replied += 1
    # A conversation without an event log (tests, older runs) still gets its turns.
    lines += [f"Resident: {t}" for t in conv.resident[said:]]
    lines += [f"Agent: {r}" for r in conv.replies[replied:]]
    return "\n".join(lines)


def parse(text: str) -> dict:
    m = _JSON.search(text or "")
    if not m:
        raise ValueError("judge reply has no JSON object")
    raw = json.loads(m.group(0))
    out = {k: raw.get(k) for k in VERDICT_KEYS}
    out["reason"] = str(raw.get("reason") or "")[:300]
    return out


def judge(case: Case, conv: Conversation, outcome: str, sel, rubric: str, chat, cache,
          required_fields: list[str] | None = None) -> dict:
    """One verdict for one conversation. `chat` is src.models.llm.chat; `cache` records the answer.
    `required_fields` are the expected code's required fields in the run's policy.yaml."""
    payload = {
        "case": {"expected_action": case.expected_action, "expected_code": case.expected_code,
                 "hazard_flag": case.hazard_flag, "adversarial_type": case.adversarial_type or None,
                 "subtype": case.subtype or None, "required_fields": list(required_fields or []),
                 "missing_fields": list(case.omitted_fields)},
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
    verdict["tokens_in"] = answer.get("tokens_in") or 0     # results_format.md: totals include the judge
    verdict["tokens_out"] = answer.get("tokens_out") or 0
    return verdict
