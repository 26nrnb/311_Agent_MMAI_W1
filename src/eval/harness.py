"""Eval harness (owner: E, W5 Repo, harness and demo; metrics and judge: F, W6).

Runs a setup over a locked test set and writes one results row per test case
(docs/results_format.md). Two kinds of run:

  classify  harness v1: the classifier alone, through classify(text)
  agent     harness v2: the whole agent end to end, through handle_turn()

    python -m src.eval.harness classify --setup keyword_stub --test data/test/test_heldout.jsonl --name heldout
    python -m src.eval.harness agent --setup haiku --test data/test/test_heldout.jsonl --name heldout
    python -m src.eval.harness reproduce          # what `make reproduce` runs
    python -m src.eval.harness setups             # list the setup names

Every run writes, under results/:

  <run_id>.csv                  the results file (no message text: safe to commit)
  manifests/<run_id>.json       what was run: setup, test-set hash, config version (safe to commit)
  cache/<run_id>/               every model answer, the config snapshot and the tickets used
  events/<run_id>_events.jsonl  the masked event log of every case

cache/ and events/ hold text derived from the test set, so they are
git-ignored and shared with the locked test files (metrics.md: generated
data stays off GitHub). With them, `reproduce` replays every run without API
keys; without them, it rescores the committed results files.

Each case gets a fresh in-memory store (the same tickets every time), a
frozen clock (the ticket store's as-of date) and the run's config snapshot,
so a replay sends the model exactly what the live run did.
"""
from __future__ import annotations

import argparse
import contextlib
import csv
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RESULTS = ROOT / "results"
HARNESS_VERSION = "2.0"
# The ticket store's clock (src/data_gen/synth_311_pipeline.py STORE_AS_OF) plus half a day,
# so "opened today" fixtures are inside every duplicate window.
STORE_AS_OF = datetime(2026, 11, 11, tzinfo=timezone.utc)
FROZEN_NOW = STORE_AS_OF + timedelta(hours=12)

# docs/results_format.md, in order; then extra columns (allowed at the end).
COLUMNS = ["run_id", "setup", "test_set", "interaction_id", "expected_code", "predicted_code",
           "expected_action", "actual_action", "hazard_flag", "lang", "fields_complete",
           "expected_ticket_id", "linked_ticket_id", "policy_violation", "invented_commitment", "pii_leak",
           "adversarial_type", "adversarial_pass", "tokens_in", "tokens_out", "cost_usd",
           "turn_latencies_ms", "failure_type"]
# Extra columns, in the order docs/results_format.md lists them.
EXTRA = ["expected_trigger_id", "agent_trigger_id", "difficulty", "subtype", "outcome_source", "violation_rules",
         "pii_findings", "tickets_created", "handoff_rules", "confidence", "judge_reason", "error"]


class RunError(RuntimeError):
    """The run can't go on (bad setup, missing data, replay mismatch). Nothing is scored."""


def _b(v) -> str:
    return "" if v is None else ("true" if v else "false")


def _git_commit() -> str | None:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True,
                              text=True, timeout=5).stdout.strip() or None
    except Exception:
        return None


def run_id_for(setup: str, test_name: str, test_hash: str, day: str | None = None) -> str:
    """date_setup_testset_hash, e.g. 2026-11-20_haiku_heldout_a1b2c3 (Interfaces doc section 6)."""
    return f"{day or date.today().isoformat()}_{setup}_{test_name}_{test_hash[:6]}"


# ------------------------------------------------------------------ tickets
def _aware(ts: str) -> str:
    dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).isoformat()


def _store_row(ticket_id, code, unit, status, location, postal_code, fields, opened_at) -> dict:
    return {"ticket_id": ticket_id, "code": code, "unit": unit, "status": status, "location": location,
            "postal_code": postal_code, "fields": fields or {}, "opened_at": _aware(opened_at),
            "location_norm": " ".join((location or "").lower().split()), "interaction_id": None, "contact": None}


def load_tickets(path: str | None) -> tuple[list[dict], str]:
    """Tickets for every case's store: a pipeline tickets.db (old or Interface 7 columns), a
    JSON list, or by default the pipeline's fixture tickets. Returns (rows, where they came from)."""
    if path:
        p = Path(path)
        if p.suffix == ".json":
            rows = json.loads(p.read_text(encoding="utf-8"))
            return [_store_row(r["ticket_id"], r["code"], r.get("unit"), r["status"], r["location"],
                               r.get("postal_code"), r.get("fields"), r["opened_at"]) for r in rows], p.name
        con = sqlite3.connect(p)
        con.row_factory = sqlite3.Row
        cols = {r[1] for r in con.execute("PRAGMA table_info(service_requests)")}
        out = []
        for r in con.execute("SELECT * FROM service_requests"):
            r = dict(r)
            if "fields_json" in cols:      # Interface 7 columns (pipeline data 2.2+)
                fields = json.loads(r["fields_json"] or "{}")
                out.append(_store_row(r["ticket_id"], r["code"], r["unit"], r["status"], r["address"],
                                      r.get("postal_code"), fields, r["created_at"]))
            else:                          # pipeline data 2.1 and earlier
                out.append(_store_row(r["ticket_id"], r["issue_code"], None, r["status"], r["address"],
                                      None, {"detail": r.get("detail")}, r["created_at"]))
        con.close()
        return out, p.name
    try:
        from src.data_gen.synth_311_pipeline import FIXTURE_TICKETS, SERVICE_CODES   # needs pandas
    except Exception:
        return [], "none (install src/data_gen/requirements.txt or pass --tickets)"
    rows = []
    for tid, code, address, street, fields, status, days_ago in FIXTURE_TICKETS:
        unit = (SERVICE_CODES.get(code) or {}).get("unit")
        rows.append(_store_row(tid, code, unit, status, address, None, fields,
                               (STORE_AS_OF - timedelta(days=days_ago)).isoformat()))
    return rows, "pipeline FIXTURE_TICKETS"


# ------------------------------------------------------------------ run context
@contextlib.contextmanager
def _patched(llm_cache, clf_cache, classify_fn, clf_name):
    """Route the agent's model and classifier calls through the cache and freeze the store's
    clock, for this run only. Patching the module attributes keeps src/agent untouched."""
    from src.agent import orchestrator
    from src.eval.cache import key_for
    from src.models import llm
    from src.models.llm import LLMError, LLMResponse
    from src.tools import registry
    from src.tools import store as store_mod

    seen: Counter = Counter()   # the same request twice in a case (an F1 retry) gets its own entry

    def cached_chat(sel, messages, tools=None):
        base = key_for("chat", sel.label, messages, tools)
        seen[base] += 1

        def call():
            try:
                r = llm.chat(sel, messages, tools)
            except LLMError as e:       # recorded too, so a replay retries exactly as the live run did
                return {"llm_error": str(e), "retryable": e.retryable}
            return {"message": r.message, "tokens_in": r.tokens_in, "tokens_out": r.tokens_out,
                    "cost_usd": r.cost_usd, "latency_ms": r.latency_ms}

        v = llm_cache.get_or_call(f"{base}:{seen[base]}", call)
        if "llm_error" in v:
            raise LLMError(v["llm_error"], retryable=v["retryable"])
        return LLMResponse(**v)

    def cached_classifier(text):
        base = key_for("classify", clf_name, text)
        seen[base] += 1
        out = clf_cache.get_or_call(f"{base}:{seen[base]}", lambda: classify_fn(text))
        return dict(out) | {"ok": True, "error": None, "error_code": None}

    old_chat, old_now, old_clf = orchestrator.chat, store_mod._now, registry.TOOLS["classifier"]
    orchestrator.chat, store_mod._now = cached_chat, (lambda: FROZEN_NOW)
    registry.TOOLS["classifier"] = cached_classifier
    try:
        yield seen
    finally:
        orchestrator.chat, store_mod._now, registry.TOOLS["classifier"] = old_chat, old_now, old_clf


def _snapshot_config(cache_dir: Path, mode: str) -> tuple[dict, str]:
    """The config this run uses: recorded live, or read back from the cache on replay."""
    from src import config
    snap = cache_dir / "config"
    if mode == "replay":
        if not snap.exists():
            raise RunError(f"no config snapshot in {snap}")
        return {p.relative_to(snap).as_posix(): p.read_text(encoding="utf-8")
                for p in snap.rglob("*") if p.is_file()}, "snapshot"
    try:
        files = dict(config.files())
    except config.ConfigUnavailable as e:
        raise RunError(str(e)) from e
    from src.tools.store import MemoryStore, get_store
    source = "fixture (tests/fixtures/config)" if isinstance(get_store(), MemoryStore) else "supabase"
    for rel, text in files.items():
        target = snap / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    return files, source


# ------------------------------------------------------------------ one case
def _turn_latencies(events: list[dict]) -> list[int]:
    """Model time per resident turn, from the event log (G10). Tool calls against the
    in-memory store are left out: they say nothing about production and vary run to run."""
    turns: list[int] = []
    for e in events:
        if e["step"] == "input_screen":
            turns.append(0)
        elif e["step"] == "model" and turns:
            turns[-1] += int(e.get("latency_ms") or 0)
    return turns


def _json(text):
    try:
        value = json.loads(text) if isinstance(text, str) else None
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


# Policy v1.0: "Logs record the code and the ID, for example RD-POT/hospital_route."
CODE_TRIGGER = re.compile(r"\b(?:SW-BIN|SW-MISS|AN-DEAD|AN-INJ|RD-POT|TR-TREE|PS-PROP|IL-DUMP)/([a-z_]+)\b")


def _trigger_only(value) -> str | None:
    """'RD-POT/hospital_route' or 'hospital_route' -> 'hospital_route' (the record's form)."""
    if not value:
        return None
    return str(value).split("/", 1)[-1].strip() or None


def agent_trigger(handoffs: list[dict], events: list[dict]) -> str | None:
    """The E2 trigger the agent logged, if it logged one, as the record writes it (no code prefix).
    The agent records only a rule ID and a reason today (open item for D), so this reads a
    `trigger_id` wherever it may land (the handoff row, an event row, a tool:handoff or flow
    event's JSON), or a CODE/trigger_id pair in a handoff reason or event text (policy v1.0)."""
    found = None
    for h in handoffs:
        m = CODE_TRIGGER.search(str(h.get("reason") or ""))
        found = _trigger_only(h.get("trigger_id")) or (m.group(1) if m else None) or found
    for e in events:
        data = _json(e.get("text")) or {}
        result = data["result"] if isinstance(data.get("result"), dict) else {}
        m = CODE_TRIGGER.search(str(e.get("text") or ""))
        found = (_trigger_only(e.get("trigger_id") or data.get("trigger_id") or result.get("trigger_id"))
                 or (m.group(1) if m else None) or found)
    return found


def case_errors(events: list[dict], limit: int = 300) -> str:
    """results_format.md `error`: every tool or model error in the case, separated by ';'.
    A tool's error_code is used when the event carries one (migration 0006), else its message."""
    found = []
    for e in events:
        step = e.get("step") or ""
        if e.get("outcome") != "error" or not (step == "model" or step.startswith("tool:")):
            continue
        detail = e.get("error_code")
        if not detail:
            data = _json(e.get("text")) or {}
            detail = data.get("error") or (data.get("result") or {}).get("error") or e.get("text") or "error"
        found.append(f"{step}: {str(detail)[:80]}")
    text = ";".join(dict.fromkeys(found))          # keep order, drop repeats (an F1 retry logs twice)
    return text if len(text) <= limit else text[:limit] + "..."


def _conversation(store, session, outs, events):
    from src.eval.outcome import OUTCOMES, Conversation
    conv = Conversation(events=events)
    conv.replies = [o["reply"] for o in outs]
    conv.reply_rules = [o.get("rule_id") for o in outs]
    conv.resident = [t["text"] for t in session.transcript if t["role"] == "resident"]
    iid = session.interaction_id
    conv.tickets = [t for t in store.tickets if t.get("interaction_id") == iid]
    conv.handoffs = [h for h in store.handoffs if h.get("interaction_id") == iid]
    conv.known_ids = set(session.state.known_ids) | set(session.state.mentioned_ids)
    conv.state_code = session.state.code
    for e in events:
        if e["step"] == "input_screen" and e.get("outcome") not in (None, "clean"):
            conv.screen_flags |= set(e["outcome"].split(","))
        elif e["step"] == "tool:search_tickets" and e.get("outcome") == "ok":
            try:
                conv.searched_ids |= {t["ticket_id"] for t in json.loads(e["text"])["result"].get("tickets", [])}
            except (TypeError, ValueError, KeyError):
                pass
        elif e["step"] == "tool:get_status" and e.get("outcome") == "ok":   # found the resident's ticket
            result = (_json(e.get("text")) or {}).get("result") or {}
            if result.get("ok"):
                conv.statuses.append({k: result.get(k) for k in ("ticket_id", "code", "status")})
        elif e["step"] == "tool:kb_search" and e.get("outcome") == "ok":     # A13: an approved article was found
            result = (_json(e.get("text")) or {}).get("result") or {}
            conv.kb_answered = conv.kb_answered or bool(result.get("passages"))
        elif e["step"] == "tool:policy_lookup" and e.get("outcome") == "ok":
            conv.looked_up = True
        elif e["step"] == "reply" and e.get("outcome") in OUTCOMES:
            conv.agent_outcome = e["outcome"]
    conv.agent_trigger_id = agent_trigger(conv.handoffs, events)
    return conv


def run_agent_case(case, sel, cfg_files, tickets, judge_ctx=None) -> tuple[dict, list[dict]]:
    """Run one test case through the agent; returns (results row fields, masked events)."""
    from src import config
    from src.agent import orchestrator
    from src.eval import checks, judge as judge_mod
    from src.eval.outcome import derive, predicted_code
    from src.tools import store as store_mod
    from src.tools.store import MemoryStore

    store = MemoryStore(config=cfg_files)
    store.tickets = [dict(t) for t in tickets]
    token = store_mod.use_store(store)
    orchestrator._SESSIONS.clear()
    try:
        config.reload()
        outs, iid = [], None
        for text in case.messages:
            out = orchestrator.handle_turn(iid, text, sel)
            outs.append(out)
            iid = out["interaction_id"]
        session = orchestrator._SESSIONS[iid]
        events = [e for e in store.events if e["interaction_id"] == iid]
        conv = _conversation(store, session, outs, events)
        outcome, linked, source = derive(conv)
        pol = config.policy()
        found = checks.check(conv, pol["thresholds"].get("max_reply_words", 80))
        catalogue = pol["service_catalogue"]
        fields_ok = None
        if conv.tickets:
            fields_ok = all(all(t["fields"].get(f) for f in catalogue.get(t["code"], {}).get("required_fields", []))
                            for t in conv.tickets)
        invented, leak, violated = bool(found.commitments), bool(found.pii), bool(found.policy)
        verdict = {}
        if judge_ctx:
            required = catalogue.get(case.expected_code, {}).get("required_fields", [])
            verdict = judge_mod.judge(case, conv, outcome, judge_ctx["sel"], judge_ctx["rubric"],
                                      judge_ctx["chat"], judge_ctx["cache"], required_fields=required)
            invented |= bool(verdict.get("invented_commitment"))
            leak |= bool(verdict.get("pii_leak"))
            violated |= bool(verdict.get("policy_violation"))
        adv_pass = None
        if case.adversarial_type:
            # metrics.md G7: the response the policy requires for the attack AND no rule broken at all
            adv_pass = outcome == case.expected_action and not invented and not leak and not violated
            if verdict.get("adversarial_pass") is not None:
                adv_pass = adv_pass and bool(verdict["adversarial_pass"])
        models = [e for e in events if e["step"] == "model"]
        row = {
            "predicted_code": predicted_code(conv, outcome), "actual_action": outcome,
            "fields_complete": _b(fields_ok), "linked_ticket_id": linked or "",
            "policy_violation": _b(violated), "invented_commitment": _b(invented), "pii_leak": _b(leak),
            "adversarial_pass": _b(adv_pass),
            # results_format.md: tokens and cost are summed over the conversation, judge included
            "tokens_in": sum(e.get("tokens_in") or 0 for e in models) + (verdict.get("tokens_in") or 0),
            "tokens_out": sum(e.get("tokens_out") or 0 for e in models) + (verdict.get("tokens_out") or 0),
            "cost_usd": round(sum(e.get("cost_usd") or 0 for e in models) + (verdict.get("cost_usd") or 0), 8),
            "turn_latencies_ms": ";".join(str(t) for t in _turn_latencies(events)),
            "outcome_source": source, "violation_rules": ";".join(found.rules),
            "pii_findings": ";".join(found.pii),
            "tickets_created": ";".join(t.get("ticket_id") or "" for t in conv.tickets),
            "handoff_rules": ";".join(h.get("rule_id") or "" for h in conv.handoffs),
            "judge_reason": verdict.get("reason", ""),
            "error": case_errors(events),
            "agent_trigger_id": conv.agent_trigger_id or "",
            "_replies": conv.replies,   # unmasked, for the demo rehearsal only; never written to a file
        }
        return row, events
    finally:
        store_mod._override.reset(token)
        orchestrator._SESSIONS.clear()


# ------------------------------------------------------------------ runs
def _base_row(run_id, setup_name, test_name, case) -> dict:
    return {"run_id": run_id, "setup": setup_name, "test_set": test_name, "interaction_id": case.case_id,
            "expected_code": case.expected_code, "expected_action": case.expected_action,
            "hazard_flag": _b(case.hazard_flag), "lang": case.lang, "expected_ticket_id": case.ticket_ref or "",
            "adversarial_type": case.adversarial_type, "failure_type": "",
            "difficulty": case.difficulty, "subtype": case.subtype,
            "expected_trigger_id": case.trigger_id or ""}


def _write_csv(path: Path, rows: list[dict]) -> None:
    tmp = path.with_suffix(".csv.part")
    with tmp.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS + EXTRA, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in COLUMNS + EXTRA})
    os.replace(tmp, path)


def run(kind: str, setup_name: str, test_path: str, test_name: str, *, mode: str = "auto",
        run_id: str | None = None, tickets_path: str | None = None, limit: int | None = None,
        judge_name: str | None = None, results_dir: Path = RESULTS, day: str | None = None,
        quiet: bool = False) -> Path:
    """Run one setup over one test set. Returns the results CSV path."""
    from src.eval import setups, testset
    from src.eval.cache import Cache, CacheMiss, key_for

    if kind not in ("classify", "agent"):
        raise RunError(f"unknown run kind {kind!r}")
    test_hash = testset.file_hash(test_path)
    cases = testset.load(test_path)[: limit or None]
    run_id = run_id or run_id_for(setup_name, test_name, test_hash, day)
    cache_dir = results_dir / "cache" / run_id
    manifest_path = results_dir / "manifests" / f"{run_id}.json"
    if mode != "replay" and manifest_path.exists():
        raise RunError(f"run {run_id} already exists; pass --run-id for a new one, or use `reproduce`")
    s = setups.setup("classifier" if kind == "classify" else "agent", setup_name)
    clf_name = setup_name if kind == "classify" else s.get("classifier", "keyword_stub")
    classify_fn = setups.classifier(clf_name)
    clf_cache = Cache(cache_dir / "classifier.jsonl", mode)
    rows, log = [], (lambda msg: None) if quiet else print
    started = time.perf_counter()

    try:
        if kind == "classify":
            for case in cases:
                def call(text=case.message):
                    t0 = time.perf_counter()
                    out = dict(classify_fn(text))
                    out.setdefault("latency_ms", int((time.perf_counter() - t0) * 1000))
                    return out
                out = clf_cache.get_or_call(key_for("classify", clf_name, case.message, 1), call)
                rows.append(_base_row(run_id, setup_name, test_name, case) | {
                    "predicted_code": out.get("code") or "", "confidence": out.get("confidence", ""),
                    "tokens_in": out.get("tokens_in", 0), "tokens_out": out.get("tokens_out", 0),
                    "cost_usd": out.get("cost_usd", 0.0), "turn_latencies_ms": str(out.get("latency_ms", ""))})
            extra = {"classifier": clf_name}
        else:
            from src.agent.orchestrator import SetupError
            sel = setups.selection("agent", setup_name, require_key=(mode != "replay"))
            cfg_files, config_source = _snapshot_config(cache_dir, mode)
            tickets_file = cache_dir / "tickets.json"
            if mode == "replay":
                if not tickets_file.exists():
                    raise RunError(f"no ticket snapshot in {tickets_file}")
                tickets, tickets_source = json.loads(tickets_file.read_text(encoding="utf-8")), "snapshot"
            else:
                tickets, tickets_source = load_tickets(tickets_path)
                tickets_file.parent.mkdir(parents=True, exist_ok=True)
                tickets_file.write_text(json.dumps(tickets, ensure_ascii=False), encoding="utf-8")
            judge_ctx = None
            if judge_name:
                from src.models import llm
                js = setups.setup("judge", judge_name)
                rubric = cfg_files.get(f"prompts/{js['rubric']}")
                if rubric is None:
                    raise RunError(f"judge rubric prompts/{js['rubric']} is not in the config (F adds it in /admin)")
                judge_ctx = {"sel": setups.selection("judge", judge_name, require_key=(mode != "replay")),
                             "rubric": rubric, "chat": llm.chat, "cache": Cache(cache_dir / "judge.jsonl", mode)}
            llm_cache = Cache(cache_dir / "llm.jsonl", mode)
            events_path = results_dir / "events" / f"{run_id}_events.jsonl"
            events_path.parent.mkdir(parents=True, exist_ok=True)
            all_events = []
            from src.config import reload
            try:
                with _patched(llm_cache, clf_cache, classify_fn, clf_name) as seen:
                    for i, case in enumerate(cases, 1):
                        seen.clear()
                        try:
                            fields, events = run_agent_case(case, sel, cfg_files, tickets, judge_ctx)
                        except SetupError as e:
                            raise RunError(f"setup problem on {case.case_id}: {e}") from e
                        rows.append(_base_row(run_id, setup_name, test_name, case) | fields)
                        all_events += [e | {"run_id": run_id, "setup": setup_name, "case_id": case.case_id}
                                       for e in events]
                        log(f"  [{i}/{len(cases)}] {case.case_id}: expected {case.expected_action}, "
                            f"got {fields['actual_action']}")
            finally:
                reload()   # back to the normal config for anything that runs next
            with events_path.open("w", encoding="utf-8") as fh:
                for e in all_events:
                    fh.write(json.dumps(e, ensure_ascii=False, default=str) + "\n")
            pol_version = None
            try:
                import yaml
                pol_version = (yaml.safe_load(cfg_files.get("policy.yaml", "")) or {}).get("version")
            except Exception:
                pass
            extra = {"model": setups.label("agent", setup_name), "classifier": clf_name,
                     "config_source": config_source, "policy_version": pol_version,
                     "tickets_source": tickets_source, "n_tickets": len(tickets), "judge": judge_name,
                     "llm_calls_recorded": llm_cache.misses, "llm_calls_replayed": llm_cache.hits}
    except CacheMiss as e:
        raise RunError(f"replay of {run_id} needed an answer that was never recorded: {e}") from e

    out_path = results_dir / f"{run_id}.csv"
    results_dir.mkdir(parents=True, exist_ok=True)
    _write_csv(out_path, rows)
    if mode != "replay":
        manifest = {"run_id": run_id, "kind": kind, "setup": setup_name,
                    "setup_settings": {k: v for k, v in s.items() if k != "key_env"} | (
                        {"key_env": s["key_env"]} if s.get("key_env") else {}),
                    "test_set": test_name, "test_file": Path(test_path).resolve().relative_to(ROOT).as_posix()
                    if Path(test_path).resolve().is_relative_to(ROOT) else str(test_path),
                    "test_set_hash": test_hash, "n_cases": len(rows), "limit": limit,
                    "tickets_path": tickets_path, "created": day or date.today().isoformat(),
                    "git_commit": _git_commit(), "harness_version": HARNESS_VERSION,
                    "frozen_clock": FROZEN_NOW.isoformat(), **extra}
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    log(f"{run_id}: {len(rows)} cases in {time.perf_counter() - started:.1f} s -> {out_path.relative_to(results_dir.parent)}")
    return out_path


def reproduce(results_dir: Path = RESULTS) -> int:
    """Replay every recorded run from its cache (where the shared data is present), then
    rescore every results file. Returns a process exit code."""
    from src.eval import score, testset
    manifests = sorted((results_dir / "manifests").glob("*.json"))
    problems = 0
    for mp in manifests:
        m = json.loads(mp.read_text(encoding="utf-8"))
        test_file = ROOT / m["test_file"] if not Path(m["test_file"]).is_absolute() else Path(m["test_file"])
        cache_dir = results_dir / "cache" / m["run_id"]
        if not test_file.exists() or not cache_dir.exists():
            print(f"rescore only  {m['run_id']}: test file or cache not here (shared with the locked test set)")
            continue
        if testset.file_hash(test_file) != m["test_set_hash"]:
            print(f"MISMATCH      {m['run_id']}: {m['test_file']} changed since the run (hash differs)")
            problems += 1
            continue
        csv_path = results_dir / f"{m['run_id']}.csv"
        before = csv_path.read_bytes() if csv_path.exists() else None
        try:
            run(m["kind"], m["setup"], str(test_file), m["test_set"], mode="replay", run_id=m["run_id"],
                limit=m.get("limit"), judge_name=m.get("judge"), results_dir=results_dir, quiet=True)
        except RunError as e:
            print(f"FAILED        {m['run_id']}: {e}")
            problems += 1
            continue
        same = before is not None and before == csv_path.read_bytes()
        print(f"replayed      {m['run_id']}: {'identical to the committed file' if same else 'results file rewritten'}")
    csvs = score.result_files(results_dir)
    if not csvs:
        print("no results files yet, so nothing to rebuild: run the harness first (README, Evaluation)")
        return 1 if problems else 0
    summary = score.write_summary(csvs, results_dir)
    print(f"scored {len(csvs)} results file(s) -> {summary.relative_to(results_dir.parent)} and summary.md")
    return 1 if problems else 0


# ------------------------------------------------------------------ CLI
def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m src.eval.harness", description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    for kind in ("classify", "agent"):
        q = sub.add_parser(kind, help=f"run a {kind} setup over a test set")
        q.add_argument("--setup", required=True)
        q.add_argument("--test", required=True, help="locked test set (JSONL)")
        q.add_argument("--name", required=True, help="test set name: heldout, prod or adversarial")
        q.add_argument("--mode", default="auto", choices=["auto", "live", "replay"])
        q.add_argument("--run-id")
        q.add_argument("--limit", type=int, help="first N cases only (smoke runs)")
        if kind == "agent":
            q.add_argument("--tickets", help="tickets.db or tickets.json for every case's store")
            q.add_argument("--judge", help="judge setup name from setups.yaml")
    sub.add_parser("reproduce", help="replay recorded runs and rescore every results file")
    sub.add_parser("setups", help="list setup names")
    a = p.parse_args(argv)
    try:
        if a.cmd == "reproduce":
            return reproduce()
        if a.cmd == "setups":
            from src.eval.setups import all_setups
            for kind, names in all_setups().items():
                print(f"{kind}: {', '.join(names or {})}")
            return 0
        run(a.cmd, a.setup, a.test, a.name, mode=a.mode, run_id=a.run_id, limit=a.limit,
            tickets_path=getattr(a, "tickets", None), judge_name=getattr(a, "judge", None))
        return 0
    except (RunError, KeyError, FileNotFoundError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
