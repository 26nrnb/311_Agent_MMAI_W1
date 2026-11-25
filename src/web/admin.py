"""Admin API behind the /admin page (owner: E). No login yet: prototype only.

Set ADMIN_ENABLED=0 in .env to switch the page and this API off.

- Interactions: live walkthrough of a conversation from interaction_events,
  handoffs and tickets. Everything returned here is masked (D6), as in the logs.
- Files: edit the config files (all of which live only in Supabase). A save
  is checked first (YAML parses, keys the code reads are there, regexes
  compile, the tree is valid and keeps every hard rule, the system prompt
  renders), then stored with its history and applied without a redeploy.
- Flow: the interaction tree (flow.yaml) as structured data for the tree
  editor, plus the checks and actions the code offers.
- Tickets and handoffs: live lists, detail, status changes and notes
  (kept in record_updates), each linked to the conversation behind it.
- Knowledge base: edit kb_documents; changed documents are re-chunked at once.
- Analytics: requests per hour, day or month, latency, and the running cost
  against doing the same work by hand (src/web/analytics.py).
- Re-runs: replay a logged conversation on another model (src/agent/rerun.py)
  and compare speed, tokens and decisions with the original.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone

import yaml
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field

from src.agent import flow, rerun
from src.agent.orchestrator import render_system_prompt
from src.config import EDITABLE, app_config, env, files, guardrails_config, policy, reload
from src.guardrails.masking import mask_obj
from src.models import classifier as clf
from src.models.llm import LLMError, LLMSelection
from src.rag.ingest import chunk_stale
from src.tools.store import StoreError, get_store
from src.web import analytics as stats

MAX_FILE_CHARS = 200_000

# Keys the code reads, per file: section -> keys inside it (None = any content).
# A save that drops one is refused.
REQUIRED = {
    "app.yaml": {
        "site": ["city_name", "title", "tagline", "input_placeholder"],
        "llm": ["providers", "default_provider", "default_models", "temperature", "ollama_num_ctx",
                "max_tool_steps", "request_timeout_s"],
        "agent": ["system_prompt", "session_ttl_minutes"],
        "retrieval": ["top_k", "chunk_chars", "chunk_overlap", "embedding_provider", "embedding_model",
                      "embedding_dimensions"],
    },
    "policy.yaml": {
        "city": None, "service_catalogue": None, "rules": None, "wording": ["handoff"],
        "thresholds": ["classifier_confidence", "classifier_top2_margin", "max_field_attempts",
                       "max_reply_words", "tool_retries"],
    },
    "guardrails.yaml": {
        "input_screen": None, "redaction": None, "masking": None,
        "output_check": ["commitment", "ticket_id", "handoff_ref", "ticket_claim", "rule_id_tag"],
    },
    "flow.yaml": {"hooks": None, "nodes": None},
}
CATALOGUE_KEYS = ("category", "unit", "required_fields", "escalation_triggers", "duplicate_window")


def _enabled():
    if env("ADMIN_ENABLED", "1") != "1":
        raise HTTPException(404)


router = APIRouter(prefix="/api/admin", dependencies=[Depends(_enabled)])


def _store_call(fn, *args):
    try:
        return fn(*args)
    except StoreError as e:
        raise HTTPException(502, f"database unavailable: {e}")


# ---------------------------------------------------------------- interactions

def _rerun_of() -> dict[str, str]:
    """Re-run conversation -> the original it replays."""
    out = {}
    for m in _store_call(get_store().rerun_markers, 500):
        try:
            out[m["interaction_id"]] = json.loads(m["text"])["of"]
        except (TypeError, ValueError, KeyError):
            pass
    return out


@router.get("/interactions")
def interactions(limit: int = 50):
    rows = _store_call(get_store().list_interactions, min(limit, 200))
    of = _rerun_of()
    for r in rows:
        r["rerun_of"] = of.get(r["interaction_id"])
    return {"interactions": rows}


@router.get("/interactions/{interaction_id}")
def interaction(interaction_id: str):
    store = get_store()
    events = _store_call(store.interaction_events, interaction_id)
    for e in events:
        try:
            e["data"] = json.loads(e["text"]) if e.get("text", "").startswith("{") else None
        except (json.JSONDecodeError, AttributeError):
            e["data"] = None
    ticket_ids = sorted({(e.get("data") or {}).get("result", {}).get("ticket_id")
                         for e in events if e["step"] == "tool:create_ticket"} - {None})
    return {
        "interaction_id": interaction_id,
        "events": events,
        "handoffs": _store_call(store.handoffs_for, interaction_id),
        "tickets": mask_obj(_store_call(store.tickets_by_id, ticket_ids)),
    }


@router.get("/rules")
def rules():
    pol = policy()
    return {"version": pol.get("version"), "rules": pol.get("rules", {}),
            "catalogue": {c: e["category"] for c, e in pol["service_catalogue"].items()}}


# ---------------------------------------------------------------- config files

class FileSave(BaseModel):
    content: str = Field(max_length=MAX_FILE_CHARS)
    author: str | None = Field(default=None, max_length=80)
    note: str | None = Field(default=None, max_length=500)


def _check_path(path: str):
    if not EDITABLE.match(path):
        raise HTTPException(400, "only app.yaml, policy.yaml, guardrails.yaml, flow.yaml and "
                                 "prompts/<folder>/<name>.md|.txt can be edited")


def _compile_all(obj, where: str, errors: list[str]):
    if isinstance(obj, str):
        try:
            re.compile(obj)
        except re.error as e:
            errors.append(f"{where}: bad regular expression ({e})")
    elif isinstance(obj, dict):
        for k, v in obj.items():
            _compile_all(v, f"{where}.{k}", errors)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            _compile_all(v, f"{where}[{i}]", errors)


def check_config(path: str, content: str) -> list[str]:
    """Problems that would break the agent if this edit were saved. Empty = safe."""
    texts = dict(files())
    texts[path] = content
    errors, parsed = [], {}
    for name, sections in REQUIRED.items():
        if name not in texts:
            errors.append(f"{name} is missing from Supabase config_files")
            continue
        try:
            parsed[name] = yaml.safe_load(texts[name])
        except yaml.YAMLError as e:
            errors.append(f"{name}: not valid YAML ({e})")
            continue
        if not isinstance(parsed[name], dict):
            errors.append(f"{name}: must be a YAML mapping (key: value)")
            continue
        missing = []
        for section, keys in sections.items():
            if section not in parsed[name]:
                missing.append(section)
            elif keys:
                sub = parsed[name][section] if isinstance(parsed[name][section], dict) else {}
                missing += [f"{section}.{k}" for k in keys if k not in sub]
        if missing:
            errors.append(f"{name}: the code reads these keys, which are missing: {', '.join(missing)}")
    if errors:
        return errors

    app, pol, guards = parsed["app.yaml"], parsed["policy.yaml"], parsed["guardrails.yaml"]
    for code, entry in (pol.get("service_catalogue") or {}).items():
        lacking = [k for k in CATALOGUE_KEYS if k not in (entry or {})]
        if lacking:
            errors.append(f"policy.yaml: service_catalogue.{code} is missing {', '.join(lacking)}")
    _compile_all({k: guards[k] for k in ("input_screen", "redaction", "masking", "output_check")},
                 "guardrails.yaml", errors)
    unknown = set(app["llm"].get("providers") or []) - {"ollama", "openrouter"}
    if unknown:
        errors.append(f"app.yaml: llm.providers can only be ollama and openrouter, not {', '.join(sorted(unknown))}")
    if "costs" in app:
        errors += stats.check_costs(app["costs"])
    default_clf = (app.get("agent") or {}).get("classifier")
    if default_clf and default_clf not in clf.setups():   # installed or not: a server without the weights falls back
        errors.append(f"app.yaml: agent.classifier {default_clf!r} isn't a classifier setup in src/eval/setups.yaml "
                      f"(known: {', '.join(clf.setups())})")
    if not errors:
        errors += [f"flow.yaml: {e}" for e in flow.validate(parsed["flow.yaml"], pol, guards)]

    prompt_path = f"prompts/{app['agent']['system_prompt']}"
    template = texts.get(prompt_path)
    if template is None:
        errors.append(f"app.yaml: agent.system_prompt points to {prompt_path}, which doesn't exist")
    elif not errors:
        try:
            render_system_prompt(pol, template)
        except KeyError as e:
            errors.append(f"{prompt_path}: placeholder {{{e.args[0]}}} has no value. Use the placeholders listed "
                          "in the prompt's header, or {{ }} for a literal brace")
        except (ValueError, IndexError) as e:
            errors.append(f"{prompt_path}: the template can't be filled in ({e}). Write {{{{ }}}} for a literal brace")
    return errors


def _in_use() -> set[str]:
    return set(REQUIRED) | {f"prompts/{app_config()['agent']['system_prompt']}"}


@router.get("/files")
def list_files():
    rows = {r["path"]: r for r in _store_call(get_store().list_config)}
    in_use = _in_use()
    return {"files": [{"path": p, "in_use": p in in_use, "updated_at": r.get("updated_at"),
                       "updated_by": r.get("updated_by")} for p, r in sorted(rows.items())]}


@router.get("/file")
def get_file(path: str):
    _check_path(path)
    row = next((r for r in _store_call(get_store().list_config) if r["path"] == path), None)
    if row is None:
        raise HTTPException(404, f"{path} doesn't exist")
    return {"path": path, "content": row["content"], "in_use": path in _in_use(),
            "updated_at": row.get("updated_at"), "updated_by": row.get("updated_by")}


def _save(path: str, content: str, author: str | None, note: str | None):
    errors = check_config(path, content)
    if errors:
        raise HTTPException(422, {"errors": errors})
    _store_call(get_store().save_config, path, content, author, note)
    reload()


@router.put("/file")
def save_file(path: str, body: FileSave):
    _check_path(path)
    _save(path, body.content, body.author, body.note)
    return {"ok": True, "path": path}


@router.delete("/file")
def delete_file(path: str, author: str | None = None):
    _check_path(path)
    if path in _in_use():
        raise HTTPException(422, {"errors": [f"{path} is in use by the live agent and can't be deleted"]})
    _store_call(get_store().delete_config, path, author)
    reload()
    return {"ok": True, "path": path}


@router.get("/revisions")
def revisions(path: str):
    _check_path(path)
    return {"revisions": _store_call(get_store().config_revisions, path)}


# ---------------------------------------------------------------- interaction tree

class FlowSave(BaseModel):
    doc: dict
    author: str | None = Field(default=None, max_length=80)
    note: str | None = Field(default=None, max_length=500)


FLOW_HEADER = """# Interaction tree (owners: B policy, D agent). Edited in /admin > Flow.
# The operating policy's decision logic as data: the agent runs through these
# trees at fixed points of every turn ("hooks") and logs each node it visits.
# A node is a decision (check + params -> yes / no) or an action
# (action + params [-> next]). enabled: false skips a node.
"""


@router.get("/flow")
def get_flow():
    pol, guards = policy(), guardrails_config()
    doc = flow.tree()
    return {"doc": doc, "errors": flow.validate(doc), "catalogue": flow.catalogue(),
            "rules": pol.get("rules", {}), "wording": pol.get("wording", {}),
            "screen_flags": sorted((guards.get("input_screen") or {}).keys())}


@router.put("/flow")
def save_flow(body: FlowSave):
    doc = flow.normalize(body.doc)
    text = FLOW_HEADER + yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, width=110)
    if len(text) > MAX_FILE_CHARS:
        raise HTTPException(422, {"errors": ["the tree is too large"]})
    _save("flow.yaml", text, body.author, body.note)
    return {"ok": True}


# ---------------------------------------------------------------- tickets and handoffs
# Staff tracking. The agent can't change either (A7); every change made here is
# kept in record_updates with who made it and why. Returned data is masked (D6)
# because this page has no login yet.

TICKET_STATUSES = ["open", "in_progress", "closed"]
HANDOFF_STATUSES = ["pending", "in_review", "resolved"]


class StatusUpdate(BaseModel):
    status: str | None = None
    note: str | None = Field(default=None, max_length=1000)
    author: str | None = Field(default=None, max_length=80)


@router.get("/tickets")
def tickets(status: str | None = None, code: str | None = None, q: str | None = None, limit: int = 100):
    store = get_store()
    rows = _store_call(store.list_tickets, status, code, q, min(limit, 500))
    return {"tickets": mask_obj(rows), "counts": _store_call(store.status_counts, "tickets"),
            "statuses": TICKET_STATUSES, "codes": sorted(policy()["service_catalogue"])}


@router.get("/tickets/{ticket_id}")
def ticket(ticket_id: str):
    store = get_store()
    t = _store_call(store.get_ticket_record, ticket_id)
    if t is None:
        raise HTTPException(404, "ticket not found")
    entry = policy()["service_catalogue"].get(t["code"], {})
    return {"ticket": mask_obj(t), "category": entry.get("category"),
            "required_fields": entry.get("required_fields", []),
            "updates": mask_obj(_store_call(store.record_updates, "ticket", ticket_id)),
            "statuses": TICKET_STATUSES}


@router.post("/tickets/{ticket_id}/updates")
def update_ticket(ticket_id: str, body: StatusUpdate):
    return _update("ticket", ticket_id, body, TICKET_STATUSES)


@router.get("/handoffs")
def handoffs(status: str | None = None, rule_id: str | None = None, q: str | None = None, limit: int = 100):
    store = get_store()
    rows = _store_call(store.list_handoffs, status, rule_id, q, min(limit, 500))
    return {"handoffs": mask_obj(rows), "counts": _store_call(store.status_counts, "handoffs"),
            "statuses": HANDOFF_STATUSES}


@router.get("/handoffs/{ref_id}")
def handoff_record(ref_id: str):
    store = get_store()
    h = _store_call(store.get_handoff, ref_id)
    if h is None:
        raise HTTPException(404, "handoff not found")
    return {"handoff": mask_obj(h), "updates": mask_obj(_store_call(store.record_updates, "handoff", ref_id)),
            "statuses": HANDOFF_STATUSES}


@router.post("/handoffs/{ref_id}/updates")
def update_handoff(ref_id: str, body: StatusUpdate):
    return _update("handoff", ref_id, body, HANDOFF_STATUSES)


def _update(kind: str, ref: str, body: StatusUpdate, statuses: list[str]):
    store = get_store()
    current = _store_call(store.get_ticket_record if kind == "ticket" else store.get_handoff, ref)
    if current is None:
        raise HTTPException(404, f"{kind} not found")
    if body.status and body.status not in statuses:
        raise HTTPException(422, {"errors": [f"status must be one of {', '.join(statuses)}"]})
    changed = bool(body.status) and body.status != current.get("status")
    note = (body.note or "").strip() or None
    if not changed and not note:
        raise HTTPException(422, {"errors": ["change the status or add a note"]})
    if changed:
        _store_call(store.set_ticket_status if kind == "ticket" else store.set_handoff_status, ref, body.status)
    _store_call(store.add_update, {"kind": kind, "ref": ref, "old_status": current.get("status") if changed else None,
                                   "new_status": body.status if changed else None, "note": note,
                                   "author": body.author})
    return {"ok": True}


# ---------------------------------------------------------------- re-runs

class RerunRequest(BaseModel):
    provider: str
    model: str = Field(min_length=1, max_length=200)
    classifier: str | None = Field(default=None, max_length=100)   # None = the one the original used


@router.post("/interactions/{interaction_id}/rerun")
def start_rerun(interaction_id: str, body: RerunRequest, x_openrouter_key: str | None = Header(default=None)):
    # Like /api/chat: an OpenRouter key comes with the request and is never stored.
    sel = LLMSelection(provider=body.provider, model=body.model,
                       api_key=x_openrouter_key if body.provider == "openrouter" else None)
    try:
        job = rerun.start(interaction_id, sel, body.classifier)
    except (LLMError, clf.ClassifierError) as e:
        raise HTTPException(400, str(e))
    except rerun.RerunError as e:
        raise HTTPException(409, str(e))
    except StoreError as e:
        raise HTTPException(502, f"database unavailable: {e}")
    return job.public()


@router.get("/reruns/jobs/{job_id}")
def rerun_job(job_id: str):
    job = rerun.get(job_id)
    if job is None:
        raise HTTPException(404, "no such re-run (the server may have restarted)")
    return job.public()


@router.get("/reruns")
def reruns(limit: int = 20):
    """Conversations that were re-run, newest first, each with its runs broken into turns."""
    store = get_store()
    groups: dict[str, list[str]] = {}
    for rid, of in _rerun_of().items():   # newest re-run first
        groups.setdefault(of, []).append(rid)
    chosen = list(groups.items())[:max(1, min(limit, 50))]
    by_id: dict[str, list[dict]] = {}
    for e in _store_call(store.events_for, [i for of, rs in chosen for i in (of, *rs)]):
        by_id.setdefault(e["interaction_id"], []).append(e)
    out = []
    for of, rs in chosen:
        if of not in by_id:
            continue
        original = stats.run_detail(of, by_id[of])
        runs = [stats.compare(original, stats.run_detail(r, by_id[r])) for r in reversed(rs) if r in by_id]
        out.append({"original": of, "first_message": original["turns"][0]["message"] if original["turns"] else None,
                    "last_run_at": runs[-1]["started_at"] if runs else None, "runs": [original, *runs]})
    return {"groups": out, "by_model": stats.by_model(out), "by_classifier": stats.by_classifier(out),
            "jobs": [j.public() for j in rerun._JOBS.values() if j.status == "running"]}


# ---------------------------------------------------------------- analytics

class CostSave(BaseModel):
    costs: dict
    author: str | None = Field(default=None, max_length=80)
    note: str | None = Field(default=None, max_length=500)


@router.get("/analytics")
def analytics(bucket: str = "day", periods: int | None = None, tz: str = "UTC"):
    if bucket not in stats.BUCKETS:
        raise HTTPException(422, {"errors": [f"bucket must be one of {', '.join(stats.BUCKETS)}"]})
    default, most = stats.BUCKETS[bucket]
    periods = max(1, min(periods or default, most))
    zone = stats.zone(tz)
    now = datetime.now(timezone.utc)
    edges = stats.window(bucket, periods, zone, now)
    store = get_store()
    events = _store_call(store.usage_events, edges[0], edges[-1])
    tickets = _store_call(store.usage_tickets, edges[0], edges[-1])
    costs = stats.cost_settings()
    out = stats.summarize(events, tickets, bucket, edges, now, costs)
    out["classifiers"] = stats.classifier_stats(_store_call(store.classifier_events, edges[0], edges[-1]),
                                                stats.rerun_ids(events))
    pol = policy()
    for r in out["handoff_rules"]:
        rule = (pol.get("rules") or {}).get(r["rule_id"])
        r["text"] = rule.get("text") if isinstance(rule, dict) else rule
    for c in out["codes"]:
        c["category"] = pol["service_catalogue"].get(c["code"], {}).get("category")
    configured = app_config().get("costs") or {}
    out.update({"tz": str(zone), "periods": periods, "max_periods": most, "generated_at": now.isoformat(),
                "costs": costs, "cost_defaults": sorted(set(stats.DEFAULT_COSTS) - set(configured)),
                "cost_notes": stats.COST_NOTES})
    return out


@router.put("/costs")
def save_costs(body: CostSave):
    errors = stats.check_costs(body.costs)
    if errors:
        raise HTTPException(422, {"errors": errors})
    current = next((r["content"] for r in _store_call(get_store().list_config) if r["path"] == "app.yaml"), None)
    if current is None:
        raise HTTPException(404, "app.yaml doesn't exist")
    _save("app.yaml", stats.with_costs(current, {**stats.cost_settings(), **body.costs}),
          body.author, body.note or "cost assumptions")
    return {"ok": True, "costs": stats.cost_settings()}


# ---------------------------------------------------------------- knowledge base

class KbDocument(BaseModel):
    source: str | None = Field(default=None, min_length=1, max_length=200)
    title: str = Field(min_length=1, max_length=300)
    content: str = Field(min_length=1, max_length=MAX_FILE_CHARS)
    active: bool = True


@router.get("/kb")
def kb_list():
    return {"documents": _store_call(get_store().list_documents)}


def _rechunk():
    try:
        chunk_stale(get_store())
    except Exception as e:   # the sync thread will retry
        print(f"[admin] re-chunk failed, sync will retry: {type(e).__name__}")


@router.post("/kb")
def kb_create(doc: KbDocument):
    if not doc.source:
        doc.source = re.sub(r"[^a-z0-9]+", "_", doc.title.lower()).strip("_")
    if any(d["source"] == doc.source for d in _store_call(get_store().list_documents)):
        raise HTTPException(409, f"a document with source {doc.source!r} already exists")
    row = _store_call(get_store().upsert_document, doc.model_dump())
    _rechunk()
    return {"document": row}


@router.put("/kb/{document_id}")
def kb_update(document_id: str, doc: KbDocument):
    row = _store_call(get_store().update_document, document_id, doc.model_dump(exclude_none=True))
    if row is None:
        raise HTTPException(404, "document not found")
    _rechunk()
    return {"document": row}


@router.delete("/kb/{document_id}")
def kb_delete(document_id: str):
    _store_call(get_store().delete_document, document_id)
    return {"ok": True}
