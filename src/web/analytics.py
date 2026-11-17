"""Usage and running-cost analytics for /admin > Analytics (owner: E; cost model: G).

Everything is computed live from the event log (interaction_events, without
the flow traces) and the tickets table:

- A request is one conversation, counted in the period of its first event.
  Its outcome is "handoff" if it was handed to staff, else "ticket" if it
  filed one, else "answered" (a question answered, a status check, a 911
  redirect, or left). Emergencies (a reply under E1) are also counted apart.
- Turn latency is resident message to agent reply (input_screen -> reply),
  the G10 measure in docs/metrics 2.md.
- Running cost = model spend (logged per call: OpenRouter returns its price,
  local Ollama calls cost nothing unless an energy rate is set) + hosting
  (prorated by time) + staff time to finish each handoff. It is set against
  what the same requests would cost by hand. G9 also divides by the intake
  success rate, which only the eval harness can measure, so it isn't here.

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
    latencies: list[float] = []
    models: dict[str, dict] = {}
    rules: Counter = Counter()
    problems = {"model_errors": 0, "tool_failures": 0, "unpriced_calls": 0}

    for e in events:   # oldest first
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
        elif step == "reply":
            conv["emergency"] |= e.get("rule_id") == "E1"
            if iid in waiting:
                latencies.append((ts - waiting.pop(iid)) * 1000)
        elif step == "model":
            label = e.get("model") or "unknown"
            m = models.setdefault(label, {"model": label, "calls": 0, "errors": 0, "tokens_in": 0,
                                          "tokens_out": 0, "cost": 0.0})
            if e.get("outcome") == "error":
                m["errors"] += 1
                problems["model_errors"] += 1
                continue
            tin, tout = int(e.get("tokens_in") or 0), int(e.get("tokens_out") or 0)
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
        "handoff_rules": [{"rule_id": r, "count": c} for r, c in rules.most_common()],
        "codes": [{"code": c, "count": k} for c, k in Counter(t["code"] for t in tickets).most_common()],
        "problems": problems,
    }
