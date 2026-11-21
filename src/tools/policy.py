"""policy_lookup(code) — A2 (owner: D). Reads policy.yaml (Supabase config) only.

A catalogue code returns what the agent needs to take the request: unit,
required fields and their allowed values, escalation triggers, the duplicate
window, and the cases it must not ticket (not_handled, A8).

A route code (EMERG, OOS) returns where such a request goes instead: its
outcome, the approved reply, when it applies and, for OOS, where other
services go. Routes never create a ticket (A8); create_ticket refuses them.
"""
from __future__ import annotations

from src.config import policy

ROUTE_KEYS = ("name", "outcome", "rules", "sent_when", "look_alikes", "other_services")


def policy_lookup(code: str) -> dict:
    code = (code or "").strip().upper()
    pol = policy()
    entry = pol["service_catalogue"].get(code)
    if entry is None:
        route = (pol.get("routes") or {}).get(code)
        if route is None:
            return {"ok": False, "error": f"unknown code {code!r} (A1)"}
        wording_key = route.get("wording")
        return {
            "ok": True,
            "error": None,
            "code": code,
            "route": True,
            "no_ticket": True,
            **{k: route[k] for k in ROUTE_KEYS if k in route},
            "wording": (pol.get("wording") or {}).get(wording_key, wording_key),
        }
    return {
        "ok": True,
        "error": None,
        "code": code,
        "category": entry["category"],
        "unit": entry["unit"],
        "required_fields": list(entry["required_fields"]),
        "field_options": entry.get("field_options", {}),
        "escalation_triggers": list(entry["escalation_triggers"]),
        "duplicate_window": entry["duplicate_window"],
        "not_handled": list(entry.get("not_handled") or []),
    }


def valid_codes() -> list[str]:
    """Catalogue codes: the only codes a ticket can be created for."""
    return list(policy()["service_catalogue"])


def route_codes() -> list[str]:
    """Routes that never create a ticket (EMERG, OOS)."""
    return list(policy().get("routes") or {})
