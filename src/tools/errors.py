"""Tool result shape and error codes (311 Interfaces doc, section 3).

Every tool returns a dict with "ok", "error" (text for the model) and
"error_code" (one of ERROR_CODES, None on success), with the rest of the
result at the top level. The orchestrator logs error_code on the tool event
(interaction_events.error_code), so failures can be counted without parsing
the error text. Keys starting with "_" are internal and never reach the model.
"""
from __future__ import annotations

ERROR_CODES = {
    # From the Interfaces doc
    "unknown_code": "Code not in the catalogue or routes, or EMERG/OOS passed to create_ticket",
    "missing_argument": "A required argument is empty: no location, query, ticket number or postal code",
    "missing_fields": "A required policy field is absent or empty, or the location is too vague (A11); missing lists them",
    "invalid_value": "A value is not allowed, e.g. an unknown handoff rule_id",
    "pii_rejected": "A field holds a SIN, health card, date of birth or card number (D2)",
    "not_found": "Ticket number and postal code do not match a ticket (same reply either way, D5)",
    "policy_unavailable": "policy.yaml could not be read (F4)",
    "unavailable": "Store or service failed or timed out after one retry (F1, E9)",
    # Added in the repo: cases the doc's list doesn't cover
    "not_handled": "The catalogue marks this case as not handled by the City; no ticket (A8)",
    "blocked": "A flow check stopped the call before it ran, or the request was already handed off",
    "bad_arguments": "The model called a tool that doesn't exist or with arguments it doesn't take (A9)",
}


def ok(**result) -> dict:
    return {"ok": True, "error": None, "error_code": None, **result}


def fail(code: str, error: str, **extra) -> dict:
    assert code in ERROR_CODES, code
    return {"ok": False, "error": error, "error_code": code, **extra}
