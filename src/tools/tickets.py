"""Ticket tools (owner: D): search_tickets (A3), create_ticket (A4), get_status (A5).

Each returns a dict with "ok", "error" and "error_code" (src/tools/errors.py). The tools expose no edit, close,
cancel or reprioritize operation (A7 [hard]).
"""
from __future__ import annotations

import re

from src.guardrails.masking import redact
from src.guardrails.location import location_usable
from src.tools.errors import fail, ok
from src.tools.policy import policy_lookup
from src.tools.store import StoreError, get_store, store_failure

POSTAL_CODE = re.compile(r"\b([A-Za-z]\d[A-Za-z])\s?(\d[A-Za-z]\d)\b")


def _postal_code(*texts: str | None) -> str | None:
    for t in texts:
        m = POSTAL_CODE.search(t or "")
        if m:
            return f"{m.group(1)} {m.group(2)}".upper()
    return None


def _location(fields: dict) -> str | None:
    return fields.get("address") or fields.get("location")


def search_tickets(location: str, code: str | None = None, days: int | None = None) -> dict:
    """Open tickets near a location. D3 [hard]: id, code, status, date only."""
    if not location:
        return fail("missing_argument", "location is required")
    try:
        rows = get_store().find_open_tickets(location, code, days)
    except StoreError as e:
        return store_failure(e)
    tickets = [{"ticket_id": r["ticket_id"], "code": r["code"], "status": r["status"],
                "date_opened": str(r["opened_at"])[:10]} for r in rows]
    return ok(tickets=tickets)


def _street(location: str | None) -> str | None:
    """The street without the house number: "12 Oak Street, G1A 1A1" -> "Oak Street"."""
    first = (location or "").split(",")[0].strip()
    return re.sub(r"^\d+[A-Za-z]?\s+", "", first) or None


def _not_handled(pol: dict, fields: dict) -> dict | None:
    """A8 [hard]: the catalogue case these fields fall under that the City doesn't handle, if any.
    Only cases that list field values can be checked here; the others rely on the model."""
    for case in pol.get("not_handled") or []:
        for name, values in (case.get("values") or {}).items():
            if str(fields.get(name, "")).strip().lower() in {str(v).strip().lower() for v in values}:
                return case
    return None


def _allowed_values(pol: dict) -> dict[str, list[str]]:
    """Closed fields and their values: field_options, plus the values reserved for escalation
    triggers and not_handled cases (those mean a rule fires, but they are not invalid)."""
    allowed = {f: list(v) for f, v in (pol.get("field_options") or {}).items()}
    for item in (pol.get("escalation_triggers") or []) + (pol.get("not_handled") or []):
        if isinstance(item, dict):
            for f, values in (item.get("values") or {}).items():
                if f in allowed:
                    allowed[f] += [v for v in values if v not in allowed[f]]
    return allowed


def _check_values(pol: dict, fields: dict) -> dict[str, list[str]]:
    """Closed fields whose value isn't allowed, with the allowed values. Allowed values are
    rewritten in place to the policy's spelling ("Recycling" -> "recycling")."""
    invalid = {}
    for name, values in _allowed_values(pol).items():
        if name not in fields:
            continue
        canonical = {str(v).strip().lower(): v for v in values}
        given = str(fields[name]).strip().lower()
        if given in canonical:
            fields[name] = canonical[given]
        else:
            invalid[name] = list(pol["field_options"][name])
    return invalid


def create_ticket(code: str, fields: dict, interaction_id: str | None = None) -> dict:
    """A4 [hard]: refuses unless the code exists and every required field is present.
    A8 [hard]: refuses route codes (EMERG, OOS) and cases the catalogue marks not_handled.
    A4 [hard]: refuses a closed field (field_options) whose value isn't one of its values;
    the field counts as missing, so E6 hands off after two tries.
    A11 [hard]: refuses a location that is no street address, intersection or landmark on a
    street; it counts as a missing field, so E6 hands off after two tries.
    interaction_id is filled in by the orchestrator (not the model) so /admin can
    link the ticket to its conversation."""
    pol = policy_lookup(code)
    if not pol["ok"]:
        return pol
    if pol.get("route"):   # EMERG, OOS (A8)
        return fail("unknown_code", f"{pol['code']} never creates a ticket; outcome {pol.get('outcome')} (A8)")
    fields = {k: redact(str(v))[0] for k, v in (fields or {}).items() if v not in (None, "")}  # D2
    case = _not_handled(pol, fields)
    if case:
        danger = f", or {case['danger_now']} if it is a danger now" if case.get("danger_now") else ""
        return fail("not_handled", f"{pol['code']} doesn't handle this: {case.get('description', case['id'])}. "
                                   f"No ticket; outcome {case.get('outcome', 'handoff_oos')}{danger} (A8)",
                    not_handled=case["id"], outcome=case.get("outcome", "handoff_oos"))
    missing = [f for f in pol["required_fields"] if not fields.get(f)]
    if fields.get("location") and not location_usable(fields["location"]):   # A11 [hard]
        return fail("missing_fields", f"location too vague to route: {fields['location']!r}. Ask for a street "
                                      "address, an intersection, or a named landmark on a street (A11)",
                    missing=["location"] + [f for f in missing if f != "location"])
    if missing:
        return fail("missing_fields", f"missing required fields: {', '.join(missing)} (A4)", missing=missing)
    invalid = _check_values(pol, fields)
    if invalid:
        detail = "; ".join(f"{f} {fields[f]!r} is not one of: {', '.join(v)}" for f, v in invalid.items())
        return fail("invalid_value", f"not an allowed value: {detail}. Ask the resident which one fits (A4)",
                    invalid=list(invalid), allowed=invalid, missing=list(invalid))
    location = _location(fields)
    contact = {k: fields.pop(k) for k in ("name", "phone", "email") if k in fields}
    row = {
        "code": pol["code"],
        "unit": pol["unit"],
        "location": location,
        "street": _street(location),
        "postal_code": fields.pop("postal_code", None) or _postal_code(location),
        "fields": fields,
        "contact": contact or None,
        "interaction_id": interaction_id,
        "source": "agent",   # x_m, y_m stay null: the agent doesn't place tickets on the grid
    }
    try:
        saved = get_store().create_ticket(row)
    except StoreError as e:
        return store_failure(e)   # F3: caller must not claim a ticket exists
    return ok(ticket_id=saved["ticket_id"], code=pol["code"], category=pol["category"],
              unit=pol["unit"], location=location)


def get_status(ticket_id: str, postal_code: str) -> dict:
    """D5 [hard]: needs the ticket number AND the postal code on that ticket."""
    if not ticket_id or not postal_code:
        return fail("missing_argument", "ticket number and postal code are both required (D5)")
    try:
        t = get_store().get_ticket(ticket_id.strip().upper())
    except StoreError as e:
        return store_failure(e)
    given = _postal_code(postal_code)
    if not t or not t.get("postal_code") or given != t["postal_code"]:
        # Same answer for "no such ticket" and "wrong postal code" so IDs can't be probed.
        return fail("not_found", "no ticket matches that number and postal code")
    return ok(ticket_id=t["ticket_id"], code=t["code"], status=t["status"], unit=t.get("unit"),
              date_opened=str(t["opened_at"])[:10])
