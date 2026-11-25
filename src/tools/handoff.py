"""handoff(rule_id, package) — A6 (owner: D).

Package (policy section 5): route code and confidence, masked transcript,
fields collected, rule ID that fired, and any possible duplicate tickets.
The resident gets a reference number, never a time.
"""
from __future__ import annotations

from src.config import policy
from src.guardrails.masking import mask_obj
from src.tools.errors import fail, ok
from src.tools.store import StoreError, get_store, store_failure


def handoff(rule_id: str, package: dict | None = None) -> dict:
    try:
        rules = policy()["rules"]
    except Exception as e:   # F4
        return fail("policy_unavailable", f"policy unavailable: {type(e).__name__} (F4)")
    if rule_id not in rules:
        return fail("invalid_value", f"unknown rule_id {rule_id!r}")
    package = package or {}
    row = {
        "rule_id": rule_id,
        "interaction_id": package.get("interaction_id"),
        "code": package.get("code"),
        "confidence": package.get("confidence"),
        "fields": mask_obj(package.get("fields") or {}),
        "transcript": mask_obj(package.get("transcript") or []),     # D6 [hard]
        "possible_duplicates": package.get("possible_duplicates") or [],
        "reason": mask_obj(package.get("reason")),
    }
    try:
        saved = get_store().create_handoff(row)
    except StoreError as e:
        return store_failure(e)
    return ok(ref_id=saved["ref_id"], rule_id=rule_id)
