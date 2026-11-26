"""Orchestrator loop (owner: D): one agent, narrow tools, a decision tree, clean handoffs.

handle_turn() runs one resident message through the interaction tree
(flow.yaml, src/agent/flow.py) at fixed points ("hooks"):

  input screen (D2 redaction)      -> "message" tree (E1 911, E8, E7, S1 ...)
  classifier (in code, A1)         -> "after_classifier" tree (E3, F2); the result goes
                                      to the model with the message
  model <-> tools loop             -> "before_<tool>" / "after_<tool>" trees
                                      (A2, A8, D5 blocks; E3, C6, M2 guidance; E6 handoff)
  tool or model failure            -> "tool_failure" / "model_failure" (E9)
  step limit                       -> "step_limit" (M4)
  the model's reply                -> "reply" tree (D8, M3, C1)

Every step, including every tree node visited and the branch it took, is
logged to interaction_events with masked text (D6). Each reply event also
carries the conversation's outcome so far, one of the ten names in
policy.yaml (src/agent/outcome.py); the last one is the final outcome.
"""
from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass, field

from src.agent import flow
from src.agent.outcome import note_reply, outcome
from src.agent.rules import RuleState, record, unsure_reason
from src.config import app_config, load_prompt, policy
from src.guardrails import output_check
from src.guardrails.input_screen import screen
from src.guardrails.masking import mask, redact
from src.models import classifier as clf
from src.models.llm import LLMError, LLMSelection, chat
from src.tools.errors import fail, ok
from src.tools.handoff import handoff
from src.tools.registry import TOOLS, tool_schemas
from src.tools.store import get_store


class SetupError(RuntimeError):
    """The chosen model can't be used (bad key, unknown model). Shown to the user."""


@dataclass
class Session:
    interaction_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    messages: list[dict] = field(default_factory=list)      # model context
    transcript: list[dict] = field(default_factory=list)    # resident-visible turns
    state: RuleState = field(default_factory=RuleState)
    last_seen: float = field(default_factory=time.time)
    classifier: str | None = None                            # classifier setup (src/eval/setups.yaml)
    classified: dict | None = None                           # the latest classifier result


_SESSIONS: dict[str, Session] = {}


def get_session(interaction_id: str | None) -> Session:
    ttl = app_config()["agent"]["session_ttl_minutes"] * 60
    now = time.time()
    for sid in [s for s, v in _SESSIONS.items() if now - v.last_seen > ttl]:
        del _SESSIONS[sid]
    session = _SESSIONS.get(interaction_id or "")
    if session is None:
        session = Session()
        session.messages.append({"role": "system", "content": system_prompt()})
        _SESSIONS[session.interaction_id] = session
    session.last_seen = now
    return session


def system_prompt() -> str:
    return render_system_prompt(policy(), load_prompt(app_config()["agent"]["system_prompt"]))


def render_system_prompt(pol: dict, template: str) -> str:
    """Fill the prompt template from the policy. Also used by /admin to check an edit before saving."""
    catalogue = "\n".join(f"  - {code}: {e['category']} (routes to {e['unit']})"
                          for code, e in pol["service_catalogue"].items())
    return template.format(
        city=pol["city"], catalogue=catalogue, max_words=pol["thresholds"]["max_reply_words"],
        **{f"w_{k}": v for k, v in pol["wording"].items()},
    )


def log_event(session: Session, step: str, *, model: str | None = None, tokens_in: int = 0,
              tokens_out: int = 0, cost_usd: float | None = None, latency_ms: int | None = None,
              rule_id: str | None = None, outcome: str | None = None, text: str | None = None,
              error_code: str | None = None) -> None:
    """Event log contract (docs/interfaces.md). Text is always masked (D6 [hard])."""
    row = {"event_id": str(uuid.uuid4()), "interaction_id": session.interaction_id, "step": step,
           "model": model, "tokens_in": tokens_in, "tokens_out": tokens_out, "cost_usd": cost_usd,
           "latency_ms": latency_ms, "rule_id": rule_id, "outcome": outcome, "text": mask(text)}
    if error_code:   # only sent when set, so a store without the 0006 column still logs
        row["error_code"] = error_code
    try:
        get_store().log_event(row)
    except Exception as e:  # logging must never break the conversation (F5 still prints)
        print(f"[event log failed] {step}: {type(e).__name__}")


def _run_flow(session: Session, hook: str, ctx: flow.Ctx) -> flow.Outcome:
    """Run one hook's tree, logging every node visited as a "flow" event."""
    def trace(step: dict):
        if step["fired"] and step["rule"]:
            session.state.fired.add(step["rule"])
        log_event(session, "flow", rule_id=step["rule"] if step["fired"] else None,
                  outcome=step["branch"], text=json.dumps(step))
    return flow.run(hook, ctx, trace)


def _wording(key: str, **kw) -> str:
    return policy()["wording"][key].format(**kw)


def _do_handoff(session: Session, rule_id: str, reason: str) -> str:
    st = session.state
    result = handoff(rule_id, {
        "interaction_id": session.interaction_id, "code": st.code, "confidence": st.confidence,
        "fields": st.fields, "transcript": session.transcript,
        "possible_duplicates": st.possible_duplicates, "reason": reason,
    })
    event = {"reason": reason, "result": result}
    if rule_id == "E2" and st.trigger_id:   # the E2 trigger that fired, as CODE/id (policy v1.0)
        event["trigger_id"] = st.trigger_id
    log_event(session, "tool:handoff", rule_id=rule_id, outcome="ok" if result["ok"] else "error",
              text=json.dumps(event))
    st.handed_off = True
    st.handoff_rule = rule_id
    if not result["ok"]:
        # F4/E9: even the handoff failed; never claim anything was recorded.
        return "Something went wrong on our side and I couldn't record this. Please call 311 directly."
    st.known_ids.add(result["ref_id"])
    return _wording("handoff", ref_id=result["ref_id"])


_SAYS_AUTOMATED = re.compile(r"\bautomated\b", re.IGNORECASE)


def _with_intro(session: Session, reply: str, rule_id: str | None) -> str:
    """M1: the first reply of a conversation says it's an automated assistant, in the approved
    wording, unless it already does. A 911 reply (E1) keeps the emergency text first."""
    intro = policy()["wording"].get("automated_intro")
    if (not intro or any(t["role"] == "assistant" for t in session.transcript)
            or _SAYS_AUTOMATED.search(reply)):
        return reply
    intro = intro.format(city=policy()["city"])
    return f"{reply} {intro}" if rule_id == "E1" else f"{intro} {reply}"


def _check_aside(session: Session, text: str) -> str | None:
    """Text the model wrote alongside a tool call goes through the same reply checks as a reply.
    Kept (cleaned) if they pass; dropped, without ending the turn, if they would block or replace it."""
    rctx = flow.Ctx(state=session.state, reply=text)
    out = _run_flow(session, "reply", rctx)
    if out.kind in ("reply", "handoff", "block"):
        return None
    return (rctx.reply or "").strip() or None


def _finish(session: Session, reply: str, rule_id: str | None = None, asides: list[str] = ()) -> dict:
    """asides: text the model wrote alongside this turn's tool calls, already checked; shown
    before the reply (it is already in the model's history, so only the reply is added there)."""
    parts = []
    for p in [*asides, reply]:
        if p and p not in parts:
            parts.append(p)
    shown = _with_intro(session, "\n\n".join(parts), rule_id)
    model_text = shown if not asides else _with_intro(session, reply, rule_id)
    session.transcript.append({"role": "assistant", "text": shown})
    session.messages.append({"role": "assistant", "content": model_text})
    note_reply(session.state, shown)
    log_event(session, "reply", rule_id=rule_id, text=shown,   # what the resident saw
              outcome=outcome(session.state, shown))
    return {"interaction_id": session.interaction_id, "reply": shown, "rule_id": rule_id}


def _end(session: Session, out: flow.Outcome, asides: list[str] = ()) -> dict:
    """Finish the turn the way a tree's reply or handoff action decided."""
    if out.kind == "reply":
        return _finish(session, out.text, out.rule, asides)
    prefix = _wording(out.prefix) + " " if out.prefix else ""
    return _finish(session, prefix + _do_handoff(session, out.handoff_rule or out.rule, out.reason),
                   out.rule, asides)


def _chat_with_retry(session: Session, sel: LLMSelection):
    retries = policy()["thresholds"]["tool_retries"]
    for attempt in range(retries + 1):
        try:
            resp = chat(sel, session.messages, tool_schemas())
        except LLMError as e:
            log_event(session, "model", model=sel.label, outcome="error", rule_id="F1", text=str(e))
            if not e.retryable:
                raise SetupError(str(e)) from e
            if attempt == retries:
                return None
            continue
        content = resp.message.get("content") or ""
        log_event(session, "model", model=sel.label, tokens_in=resp.tokens_in, tokens_out=resp.tokens_out,
                  cost_usd=resp.cost_usd, latency_ms=resp.latency_ms,
                  outcome="tool_calls" if resp.message.get("tool_calls") else "reply", text=content)
        return resp
    return None


def _known_trigger(code: str | None, value) -> str | None:
    """The model's trigger_id as CODE/id, if it names one of the code's escalation triggers (E2).
    Anything else is dropped, so the log never records a trigger the policy doesn't have."""
    if not value:
        return None
    tid = str(value).split("/", 1)[-1].strip().lower()
    code = str(code or (str(value).split("/", 1)[0] if "/" in str(value) else "")).strip().upper()
    entry = policy()["service_catalogue"].get(code) or {}
    ids = {str(t.get("id")).lower() for t in entry.get("escalation_triggers") or []}
    return f"{code}/{tid}" if tid in ids else None


def _run_tool(session: Session, name: str, args: dict) -> tuple[dict, flow.Outcome | None]:
    """Returns (result for the model, an outcome that ends the turn or None)."""
    st = session.state
    if name == "classifier":   # runs in code now; a prompt that still asks for it gets the result
        return ok(**(session.classified or {"code": None, "confidence": 0.0, "top2": []}),
                  note="already classified for this message"), None
    if name not in TOOLS:
        return fail("bad_arguments", f"no such tool {name!r} (A9)"), None
    if name == "handoff":   # the model decided a person must review this (A6)
        rule = args.get("rule_id") or "M4"
        st.trigger_id = _known_trigger(st.code, args.get("trigger_id")) or st.trigger_id
        # E1: the 911 text comes first; M6: say why in English. Skipped if the policy lacks the wording.
        prefix = {"E1": "emergency", "M6": "english_only"}.get(rule)
        return ok(note="handled"), flow.Outcome(
            "handoff", rule, reason=args.get("reason") or rule, handoff_rule=rule,
            prefix=prefix if prefix in policy()["wording"] else None)

    ctx = flow.Ctx(state=st, tool=name, args=args)
    before = _run_flow(session, f"before_{name}", ctx)
    if before.kind == "block":
        log_event(session, f"tool:{name}", rule_id=before.rule, outcome="blocked", text=before.text,
                  error_code="blocked")
        return fail("blocked", before.text), None
    if before.kind in ("reply", "handoff"):
        if name == "create_ticket":   # E2: staff get the draft ticket with the handoff
            st.fields = {**st.fields, **{k: redact(str(v))[0] for k, v in (args.get("fields") or {}).items()
                                         if v not in (None, "")}}
        return fail("blocked", "skipped: request handed off"), before

    retries = policy()["thresholds"]["tool_retries"]
    for attempt in range(retries + 1):
        start = time.perf_counter()
        try:
            call_args = {**args, "interaction_id": session.interaction_id} if name == "create_ticket" else args
            result = TOOLS[name](**call_args)
        except TypeError as e:   # model passed wrong arguments
            result = fail("bad_arguments", f"bad arguments: {e}")
            break
        except Exception as e:   # F1: unexpected tool failure, retry once
            result = fail("unavailable", f"tool failure: {type(e).__name__}", _failure=True)
        latency = int((time.perf_counter() - start) * 1000)
        failed = bool(result.get("_failure"))
        log_event(session, f"tool:{name}", latency_ms=latency, outcome="ok" if result["ok"] else "error",
                  rule_id="F5" if failed else None, error_code=result.get("error_code"),
                  text=json.dumps({"args": args, "error": result.get("_detail") or result.get("error"),
                                   "result": {k: v for k, v in result.items() if not k.startswith("_")}}))
        if not failed:
            break
    record(st, name, args, result)   # collected fields are kept even on failure (F3)
    public = {k: v for k, v in result.items() if not k.startswith("_")}

    ctx.result = public
    if result.get("_failure"):
        return public, _run_flow(session, "tool_failure", ctx)
    after = _run_flow(session, f"after_{name}", ctx)
    if after.kind in ("reply", "handoff"):
        return public, after
    if ctx.guidance:
        public["guidance"] = " ".join(ctx.guidance)   # what the tree tells the model to do next
    return public, None


def _should_classify(st: RuleState) -> bool:
    """Classify until a code is settled: a sure result, a policy lookup, a ticket or a handoff.
    A status check (A12) isn't a new request, so it isn't classified."""
    return not (st.classifier_sure or st.looked_up or st.ticket_ids or st.handed_off
                or (st.status_signal and not st.new_report))


def _classify(session: Session) -> tuple[dict, flow.Outcome, list[str]]:
    """Run the conversation's classifier on everything the resident has said (D7: their text
    only), log it, and run the after_classifier tree (E3, F2)."""
    st = session.state
    name = session.classifier
    text = "\n".join(t["text"] for t in session.transcript if t["role"] == "resident")
    start = time.perf_counter()
    try:
        result = ok(**clf.classify_with(name, text))
    except Exception as e:   # F2: no usable result counts as confidence 0
        result = {**fail("unavailable", f"classifier failed: {type(e).__name__}"),
                  "code": None, "confidence": 0.0, "top2": []}
    # A fine-tuned model reports its own inference time, leaving out its one-off load.
    latency = result.pop("latency_ms", None)
    if latency is None:
        latency = int((time.perf_counter() - start) * 1000)
    unsure = unsure_reason(result)
    record(st, "classifier", {}, result)
    st.classifier_sure = unsure is None
    session.classified = {k: result.get(k) for k in ("code", "confidence", "top2")}
    log_event(session, "classifier", model=result.get("setup") or name, latency_ms=latency,
              outcome="error" if not result["ok"] else "unsure" if unsure else "sure",
              rule_id="E3" if unsure else None, error_code=result.get("error_code"),
              text=json.dumps({**session.classified, "why": unsure}))
    ctx = flow.Ctx(state=st, tool="classifier", result=result)
    return result, _run_flow(session, "after_classifier", ctx), ctx.guidance


def _classifier_note(result: dict) -> str:
    """What the model is told about the classification."""
    top = result.get("top2") or []
    if not result.get("code"):
        return "Classifier: no code."
    alt = f"; next {top[1]['code']} {top[1]['confidence']:.2f}" if len(top) > 1 else ""
    return f"Classifier: {result['code']} {result.get('confidence') or 0:.2f}{alt}."


def handle_turn(interaction_id: str | None, user_text: str, sel: LLMSelection,
                classifier: str | None = None) -> dict:
    """One resident message. classifier is a setup name; None keeps the conversation's
    (the default for a new one)."""
    session = get_session(interaction_id)
    if classifier or not session.classifier:
        session.classifier = clf.check(classifier)
    scr = screen(user_text)
    session.state.mentioned_ids |= output_check.ids_in(scr.text)   # M3: may be quoted back
    st = session.state                                         # A12: status check or new report?
    if scr.has("status_request") or output_check.ids_in(scr.text):
        st.status_signal, st.new_report = True, False          # ask once, even if it also says "new"
    elif st.status_signal and scr.has("new_report"):
        st.new_report = True                                   # the resident's answer, in a later message
    session.transcript.append({"role": "resident", "text": scr.text})
    log_event(session, "input_screen", outcome=",".join(scr.flags + scr.redacted) or "clean", text=scr.text)

    ctx = flow.Ctx(state=session.state, screen=scr)
    out = _run_flow(session, "message", ctx)
    if out.kind in ("reply", "handoff"):
        return _end(session, out)
    notes = list(ctx.guidance)
    if _should_classify(st):
        result, out, guidance = _classify(session)
        if out.kind in ("reply", "handoff"):
            return _end(session, out)
        notes += [_classifier_note(result), *guidance]
    content = scr.text
    if notes:
        content += "\n\n[" + " ".join(notes) + "]"
    session.messages.append({"role": "user", "content": content})

    asides: list[str] = []   # what the model said alongside its tool calls; the resident sees it too
    for _ in range(app_config()["llm"]["max_tool_steps"]):
        resp = _chat_with_retry(session, sel)
        if resp is None:                                      # F1 -> E9
            return _end(session, _run_flow(session, "model_failure", flow.Ctx(state=session.state)), asides)
        msg = resp.message
        calls = msg.get("tool_calls") or []
        if not calls:
            rctx = flow.Ctx(state=session.state, reply=(msg.get("content") or "").strip())
            out = _run_flow(session, "reply", rctx)
            if out.kind in ("reply", "handoff"):
                return _end(session, out, asides)
            return _finish(session, rctx.reply, asides=asides)

        session.messages.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls})
        aside = (msg.get("content") or "").strip()
        if aside and (aside := _check_aside(session, aside)):
            asides.append(aside)
        ending = None
        for call in calls:
            name = call["function"]["name"]
            if ending:   # the turn already ended; answer remaining calls so the context stays valid
                result = fail("blocked", "skipped: request handed off")
            else:
                try:
                    raw = call["function"].get("arguments") or "{}"
                    args = json.loads(raw) if isinstance(raw, str) else raw
                except json.JSONDecodeError:
                    args = {}
                result, ending = _run_tool(session, name, args)
            session.messages.append({"role": "tool", "tool_call_id": call.get("id", name), "name": name,
                                     "content": json.dumps(result)})
        if ending and ending.kind in ("reply", "handoff"):
            return _end(session, ending, asides)

    return _end(session, _run_flow(session, "step_limit", flow.Ctx(state=session.state)), asides)
