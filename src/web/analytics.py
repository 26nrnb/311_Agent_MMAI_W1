"""Usage and running-cost analytics for /admin > Analytics (owner: E; cost model: G).

Everything is computed live from the event log (interaction_events, without
the flow traces) and the tickets table:

- A request is one conversation, counted in the period of its first event.
  Its outcome is "handoff" if it was handed to staff, else "ticket" if it
  filed one, else "answered" (a question answered, a status check, a 911
  redirect, or left). Emergencies (a reply under E1) are also counted apart.
- Turn latency is resident message to agent reply (input_screen -> reply),
  the G10 measure in docs/metrics 2.md. For comparing models it is also
  split by the model that answered the turn, next to the time of each model
  call alone (inference, plus the network for OpenRouter) and the output
  tokens per second. Turns the tree answered without a model are "tree".
- Running cost = model spend (logged per call: OpenRouter returns its price,
  local Ollama calls cost nothing unless an energy rate is set) + hosting
  (prorated by time) + staff time to finish each handoff. It is set against
  what the same requests would cost by hand. G9 also divides by the intake
  success rate, which only the eval harness can measure, so it isn't here.

Re-runs (a logged conversation replayed on another model, src/agent/rerun.py)
are test traffic: they are left out of usage and cost, and compared with
their originals by run_detail() / compare() at the end of this file.

The assumptions are the `costs:` block in app.yaml, edited from the
Analytics tab (or the Files tab). DEFAULT_COSTS fills any key that's missing.
"""
from __future__ import annotations

import json
import math
import re
from bisect import bisect_right
from collections import Counter
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from src.config import app_config

# bucket -> (default number of periods, most allowed)
BUCKETS = {"hour": (24, 24 * 31), "day": (30, 366), "month": (12, 36)}
HOURS_PER_MONTH = 730.0   # 365.25 days * 24 / 12
LATENCY_TARGET_MS = 10_000   # G10: p95 per agent turn under 10 s
TREE_ONLY = "tree"            # turn answered by the interaction tree, no model call

DEFAULT_COSTS = {
    "currency": "USD",
    "labour_rate_per_hour": 45.0,
    "manual_handle_time_s": 312,
    "review_time_s": 312,
    "hosting_per_month": 0.0,
    "ollama_per_million_tokens": 0.0,
}
COST_NOTES = {
    "currency": "shown next to every amount; model prices are always in USD",
    "labour_rate_per_hour": "fully loaded staff cost; metrics doc range $35-$55, G sets the base case",
    "manual_handle_time_s": "a person taking one 311 request by hand (metrics doc: 312 s)",
    "review_time_s": "staff time to finish one handoff from the agent",
    "hosting_per_month": "server power, Supabase plan, domain and tunnel",
    "ollama_per_million_tokens": "optional energy estimate for local models (0 = free)",
}
COSTS_HEADER = "# Running-cost assumptions (owner: G). Edited in /admin > Analytics.\n"
_COSTS_BLOCK = re.compile(r"(?m)^(?:# Running-cost assumptions[^\n]*\n)?costs:[^\n]*\n(?:[ \t]+[^\n]*\n|[ \t]*\n)*")


# ---------------------------------------------------------------- cost settings

def cost_settings() -> dict:
    return {**DEFAULT_COSTS, **(app_config().get("costs") or {})}


def check_costs(costs) -> list[str]:
    if not isinstance(costs, dict):
        return ["app.yaml: costs must be a mapping (key: value)"]
    errors = []
    for k, v in costs.items():
        if k not in DEFAULT_COSTS:
            errors.append(f"app.yaml: costs.{k} isn't used (known: {', '.join(DEFAULT_COSTS)})")
        elif k == "currency":
            if not isinstance(v, str) or not 1 <= len(v) <= 8:
                errors.append("app.yaml: costs.currency must be a short label such as USD or CAD")
        elif isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0:
            errors.append(f"app.yaml: costs.{k} must be a number, 0 or more")
    return errors


def with_costs(app_text: str, costs: dict) -> str:
    """app.yaml with its costs block replaced (or added), leaving every other line and comment as it was."""
    lines = [COSTS_HEADER, "costs:\n"]
    for k in DEFAULT_COSTS:
        if k in costs:
            lines.append(f"  {k}: {json.dumps(costs[k])}   # {COST_NOTES[k]}\n")
    block = "".join(lines)
    m = _COSTS_BLOCK.search(app_text)
    if m is None:
        return app_text.rstrip("\n") + "\n\n" + block
    tail = "\n" if m.group(0).endswith("\n\n") and m.end() < len(app_text) else ""
    return app_text[:m.start()] + block + tail + app_text[m.end():]


# ---------------------------------------------------------------- periods

def zone(name: str | None):
    try:
        return ZoneInfo(name or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        return timezone.utc


def _floor(ts: datetime, bucket: str, tz) -> datetime:
    t = ts.astimezone(tz)
    if bucket == "hour":
        return t.replace(minute=0, second=0, microsecond=0)
    t = t.replace(hour=0, minute=0, second=0, microsecond=0)
    return t.replace(day=1) if bucket == "month" else t


def _step(t: datetime, bucket: str, tz, n: int) -> datetime:
    """n periods later (or earlier). Days and months move by wall clock, so DST is handled."""
    if bucket == "hour":
        return (t.astimezone(timezone.utc) + timedelta(hours=n)).astimezone(tz)
    if bucket == "day":
        return (t.replace(tzinfo=None) + timedelta(days=n)).replace(tzinfo=tz)
    months = t.year * 12 + t.month - 1 + n
    return t.replace(year=months // 12, month=months % 12 + 1, day=1)


def window(bucket: str, periods: int, tz, now: datetime) -> list[datetime]:
    """Start of each period, oldest first, ending with the one now is in; then the end of that one."""
    current = _floor(now, bucket, tz)
    starts = [_step(current, bucket, tz, -i) for i in range(periods - 1, -1, -1)]
    return starts + [_step(current, bucket, tz, 1)]


# ---------------------------------------------------------------- summary

def _ts(value) -> float:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()


def _pct(values: list[float], p: float) -> int | None:
    """Nearest-rank percentile."""
    if not values:
        return None
    v = sorted(values)
    return int(round(v[max(0, math.ceil(p / 100 * len(v)) - 1)]))


def _speed(speed: dict, label: str) -> dict:
    return speed.setdefault(label, {"reply": [], "call": [], "tps": [], "tokens_in": 0})


def _speed_rows(speed: dict) -> list[dict]:
    """Per model: reply time per turn, time per model call, output tokens per second."""
    rows = []
    for label, sp in speed.items():
        calls = len(sp["call"])
        rows.append({
            "model": label, "replies": len(sp["reply"]), "calls": calls,
            "reply_p50_ms": _pct(sp["reply"], 50), "reply_p95_ms": _pct(sp["reply"], 95),
            "reply_max_ms": _pct(sp["reply"], 100),
            "call_p50_ms": _pct(sp["call"], 50), "call_p95_ms": _pct(sp["call"], 95),
            "tokens_per_s": round(sorted(sp["tps"])[len(sp["tps"]) // 2], 1) if sp["tps"] else None,
            "calls_per_reply": round(calls / len(sp["reply"]), 1) if sp["reply"] and label != TREE_ONLY else None,
            "avg_tokens_in": round(sp["tokens_in"] / calls) if calls else None,
        })
    # models first, busiest first; the tree-only baseline last
    return sorted(rows, key=lambda r: (r["model"] == TREE_ONLY, -r["replies"], -r["calls"]))


def summarize(events: list[dict], tickets: list[dict], bucket: str, edges: list[datetime],
              now: datetime, costs: dict) -> dict:
    """Per-period and total usage, latency and cost. edges = window(...)."""
    bounds = [e.timestamp() for e in edges]
    starts = edges[:-1]
    rate = costs["labour_rate_per_hour"] / 3600

    def period(ts: float) -> int | None:
        i = bisect_right(bounds, ts) - 1
        return i if 0 <= i < len(starts) else None

    rows = [{"start": s.isoformat(), "requests": 0, "ticket": 0, "handoff": 0, "answered": 0, "emergencies": 0, "turns": 0,
             "tickets": 0, "handoffs": 0, "model_calls": 0, "tokens_in": 0, "tokens_out": 0,
             "model_cost": 0.0} for s in starts]
    convs: dict[str, dict] = {}
    waiting: dict[str, float] = {}   # interaction -> when its unanswered message arrived
    turn_models: dict[str, set] = {}   # interaction -> models called since that message
    latencies: list[float] = []
    speed: dict[str, dict] = {}   # model -> reply, call and tokens/s samples
    models: dict[str, dict] = {}
    rules: Counter = Counter()
    problems = {"model_errors": 0, "tool_failures": 0, "unpriced_calls": 0}

    tests = rerun_ids(events)
    for e in events:   # oldest first
        if e["interaction_id"] in tests:
            continue
        ts = _ts(e["created_at"])
        i = period(ts)
        if i is None:
            continue
        row, iid, step = rows[i], e["interaction_id"], e["step"]
        conv = convs.setdefault(iid, {"period": i, "ticket": False, "handoff": False, "emergency": False})
        ok = e.get("outcome") == "ok"
        if step == "input_screen":
            row["turns"] += 1
            waiting[iid] = ts
            turn_models[iid] = set()
        elif step == "reply":
            conv["emergency"] |= e.get("rule_id") == "E1"
            if iid in waiting:
                ms = (ts - waiting.pop(iid)) * 1000
                latencies.append(ms)
                used = turn_models.pop(iid, set())
                label = used.pop() if len(used) == 1 else "mixed" if used else TREE_ONLY
                _speed(speed, label)["reply"].append(ms)
        elif step == "model":
            label = e.get("model") or "unknown"
            turn_models.setdefault(iid, set()).add(label)
            m = models.setdefault(label, {"model": label, "calls": 0, "errors": 0, "tokens_in": 0,
                                          "tokens_out": 0, "cost": 0.0})
            if e.get("outcome") == "error":
                m["errors"] += 1
                problems["model_errors"] += 1
                continue
            tin, tout = int(e.get("tokens_in") or 0), int(e.get("tokens_out") or 0)
            if e.get("latency_ms"):
                sp = _speed(speed, label)
                sp["call"].append(e["latency_ms"])
                sp["tokens_in"] += tin
                if tout:
                    sp["tps"].append(tout / (e["latency_ms"] / 1000))
            if e.get("cost_usd") is None:
                problems["unpriced_calls"] += 1
            cost = float(e.get("cost_usd") or 0)
            if label.startswith("ollama:"):
                cost += (tin + tout) / 1e6 * costs["ollama_per_million_tokens"]
            for target in (m, row):
                target["tokens_in"] += tin
                target["tokens_out"] += tout
            m["calls"] += 1
            m["cost"] += cost
            row["model_calls"] += 1
            row["model_cost"] += cost
        elif step == "tool:create_ticket" and ok:
            row["tickets"] += 1
            conv["ticket"] = True
        elif step == "tool:handoff" and ok:
            row["handoffs"] += 1
            conv["handoff"] = True
            rules[e.get("rule_id") or "?"] += 1
        if e.get("rule_id") == "F5":
            problems["tool_failures"] += 1

    for conv in convs.values():
        row = rows[conv["period"]]
        row["requests"] += 1
        row["emergencies"] += conv["emergency"]
        row["handoff" if conv["handoff"] else "ticket" if conv["ticket"] else "answered"] += 1

    for i, row in enumerate(rows):
        hours = max(0.0, (min(bounds[i + 1], now.timestamp()) - bounds[i]) / 3600)
        row["hosting_cost"] = costs["hosting_per_month"] * hours / HOURS_PER_MONTH
        row["review_cost"] = row["handoffs"] * costs["review_time_s"] * rate
        row["total_cost"] = row["model_cost"] + row["hosting_cost"] + row["review_cost"]
        row["manual_cost"] = row["requests"] * costs["manual_handle_time_s"] * rate

    sums = ("requests", "ticket", "handoff", "answered", "emergencies", "turns", "tickets", "handoffs", "model_calls",
            "tokens_in", "tokens_out", "model_cost", "hosting_cost", "review_cost", "total_cost", "manual_cost")
    totals = {k: sum(r[k] for r in rows) for k in sums}
    n = totals["requests"]
    totals.update({
        "cost_per_request": totals["total_cost"] / n if n else None,
        "cost_per_ticket": totals["total_cost"] / totals["tickets"] if totals["tickets"] else None,
        "manual_per_request": costs["manual_handle_time_s"] * rate,
        "savings": totals["manual_cost"] - totals["total_cost"],
        "handoff_rate": totals["handoff"] / n if n else None,
        "ticket_rate": totals["ticket"] / n if n else None,
        "busiest": max(rows, key=lambda r: r["requests"])["start"] if n else None,
    })
    for r in [*rows, totals, *models.values()]:
        for k, v in r.items():
            if isinstance(v, float):
                r[k] = round(v, 6)
    return {
        "bucket": bucket, "start": starts[0].isoformat(), "end": edges[-1].isoformat(),
        "rows": rows, "totals": totals,
        "latency": {"turns": len(latencies), "p50_ms": _pct(latencies, 50), "p95_ms": _pct(latencies, 95),
                    "max_ms": _pct(latencies, 100), "target_ms": LATENCY_TARGET_MS},
        "models": sorted(models.values(), key=lambda m: -m["calls"]),
        "speed": _speed_rows(speed),
        "handoff_rules": [{"rule_id": r, "count": c} for r, c in rules.most_common()],
        "codes": [{"code": c, "count": k} for c, k in Counter(t["code"] for t in tickets).most_common()],
        "problems": problems,
    }


# ---------------------------------------------------------------- re-runs
# A re-run (src/agent/rerun.py) replays a conversation on another model.
# Each run is broken into turns: what the resident said, the tools called,
# the rules that fired, the reply, and how long and how many tokens it took.

def rerun_ids(events: list[dict]) -> set[str]:
    return {e["interaction_id"] for e in events if e["step"] == "rerun"}


def run_detail(interaction_id: str, events: list[dict]) -> dict:
    turns: list[dict] = []
    cur = None
    marker_model = None
    models: list[str] = []
    outcome = {"ticket": False, "handoff": False, "emergency": False}
    handoff_rule = None
    for e in events:
        step, rule = e["step"], e.get("rule_id")
        if step == "rerun":
            marker_model = e.get("model")
            continue
        if step == "input_screen":
            cur = {"message": e.get("text"), "reply": None, "reply_ms": None, "tools": [], "rules": [],
                   "calls": 0, "call_ms": 0, "errors": 0, "tokens_in": 0, "tokens_out": 0, "cost": 0.0,
                   "_t": _ts(e["created_at"])}
            turns.append(cur)
            continue
        if cur is None:
            continue
        ok = e.get("outcome") == "ok"
        if step == "model":
            if e.get("model") and e["model"] not in models:
                models.append(e["model"])
            if e.get("outcome") == "error":
                cur["errors"] += 1
                continue
            cur["calls"] += 1
            cur["call_ms"] += int(e.get("latency_ms") or 0)
            cur["tokens_in"] += int(e.get("tokens_in") or 0)
            cur["tokens_out"] += int(e.get("tokens_out") or 0)
            cur["cost"] += float(e.get("cost_usd") or 0)
        elif step.startswith("tool:"):
            cur["tools"].append({"name": step[5:], "outcome": e.get("outcome")})
            if step == "tool:create_ticket" and ok:
                outcome["ticket"] = True
            if step == "tool:handoff" and ok:
                outcome["handoff"] = True
                handoff_rule = handoff_rule or rule
        elif step == "reply":
            cur["reply"] = e.get("text")
            cur["reply_ms"] = round((_ts(e["created_at"]) - cur["_t"]) * 1000)
            if rule == "E1":
                outcome["emergency"] = True
        if rule and step != "model" and rule not in cur["rules"]:
            cur["rules"].append(rule)
    for t in turns:
        t.pop("_t")
        t["cost"] = round(t["cost"], 6)
    replies = [t["reply_ms"] for t in turns if t["reply_ms"] is not None]
    return {
        "interaction_id": interaction_id,
        "model": marker_model or (" + ".join(models) if models else "tree"),
        "rerun": marker_model is not None,
        "started_at": events[0]["created_at"] if events else None,
        "outcome": "handoff" if outcome["handoff"] else "ticket" if outcome["ticket"]
                   else "emergency" if outcome["emergency"] else "answered",
        "handoff_rule": handoff_rule,
        "turns": turns,
        "replies": len(replies),
        "reply_ms_total": sum(replies),
        "reply_p50_ms": _pct(replies, 50),
        "calls": sum(t["calls"] for t in turns),
        "call_ms_total": sum(t["call_ms"] for t in turns),
        "errors": sum(t["errors"] for t in turns),
        "tokens_in": sum(t["tokens_in"] for t in turns),
        "tokens_out": sum(t["tokens_out"] for t in turns),
        "cost": round(sum(t["cost"] for t in turns), 6),
    }


def compare(original: dict, run: dict) -> dict:
    """Which turns took the same decisions as the original: same tools in order and the same rules."""
    same = []
    for i, t in enumerate(run["turns"]):
        o = original["turns"][i] if i < len(original["turns"]) else None
        tools = o is not None and [x["name"] for x in o["tools"]] == [x["name"] for x in t["tools"]]
        rules = o is not None and set(o["rules"]) == set(t["rules"])
        t["same_tools"], t["same_rules"] = tools, rules
        same.append(tools and rules)
    run["same_turns"] = sum(same)
    run["same_outcome"] = run["outcome"] == original["outcome"]
    return run


def by_model(groups: list[dict]) -> list[dict]:
    """Every run in the comparisons, pooled by model."""
    pool: dict[str, dict] = {}
    for g in groups:
        for r in g["runs"]:
            p = pool.setdefault(r["model"], {"model": r["model"], "runs": 0, "reruns": 0, "reply_ms": [], "tokens": [],
                                             "costs": [], "turns": 0, "same_turns": 0, "same_outcome": 0})
            p["runs"] += 1
            p["reply_ms"] += [t["reply_ms"] for t in r["turns"] if t["reply_ms"] is not None]
            p["tokens"] += [t["tokens_in"] + t["tokens_out"] for t in r["turns"] if t["reply_ms"] is not None]
            p["costs"].append(r["cost"])
            if r["rerun"]:
                p["reruns"] += 1
                p["turns"] += len(r["turns"])
                p["same_turns"] += r["same_turns"]
                p["same_outcome"] += r["same_outcome"]
    out = []
    for p in pool.values():
        out.append({
            "model": p["model"], "runs": p["runs"], "reruns": p["reruns"], "replies": len(p["reply_ms"]),
            "reply_p50_ms": _pct(p["reply_ms"], 50), "reply_p95_ms": _pct(p["reply_ms"], 95),
            "tokens_per_reply": round(sum(p["tokens"]) / len(p["tokens"])) if p["tokens"] else None,
            "cost_per_run": round(sum(p["costs"]) / len(p["costs"]), 6) if p["costs"] else None,
            "same_turn_rate": p["same_turns"] / p["turns"] if p["turns"] else None,
            "same_outcome_rate": p["same_outcome"] / p["reruns"] if p["reruns"] else None,
        })
    return sorted(out, key=lambda m: -m["runs"])
