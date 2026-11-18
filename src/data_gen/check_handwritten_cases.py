"""Check the hand-written test cases against the pipeline's codes, fields, outcome
names and the ticket store. Read-only: it never changes the generator or the store.

Usage (from data_gen/):
    python check_handwritten_cases.py data/protected/handwritten_cases.jsonl [data/tickets.db]
If the ticket store path is missing, a fresh copy is seeded into a temp file.
"""
import json
import re
import sys
import tempfile
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import synth_311_pipeline as p  # noqa: E402

ROUTE = set(p.SERVICE_CODES) | {"EMERG", "OOS"}
LANGS = set(p.LANGUAGES)
# Field values outside the closed lists that a case uses on purpose (flagged in its notes).
KNOWN_OFF_LIST = set()   # data 2.4: dog, cat etc. are now on the closed lists


def main(path, db=None):
    if not db or not Path(db).exists():
        db = str(Path(tempfile.mkdtemp()) / "tickets.db")
        p.seed_ticket_db(db)
    tickets = p.load_tickets(db, include_closed=True)
    ids = {t["ticket_id"]: t for t in tickets}
    rows = [json.loads(l) for l in Path(path).read_text(encoding="utf-8").splitlines() if l.strip()]
    errs, warns = [], []
    E = lambda r, m: errs.append(f"{r['id']}: {m}")
    W = lambda r, m: warns.append(f"{r['id']}: {m}")

    if [r["id"] for r in rows] != [f"hw_{i:03d}" for i in range(1, len(rows) + 1)]:
        errs.append("ids are not hw_001..hw_N in order")
    for m, n in Counter(r["message"] for r in rows).items():
        if n > 1:
            errs.append(f"duplicate message: {m[:60]}")

    for r in rows:
        code, act, f = r["service_code"], r["expected_action"], r["gold_fields"]
        if code not in ROUTE: E(r, f"bad code {code}")
        if act not in p.ACTIONS: E(r, f"bad action {act}")
        if r["lang"] not in LANGS: E(r, f"bad lang {r['lang']}")
        if r["channel"] not in p.CHANNELS: E(r, f"bad channel {r['channel']}")
        if r["required_fields_present"] != (not r["omitted_fields"]): E(r, "required_fields_present vs omitted_fields")
        if code in ("EMERG", "OOS") and f: E(r, "EMERG/OOS must have empty gold_fields")
        if code == "EMERG" and act != "redirect_911": E(r, "EMERG must redirect_911")
        if act == "redirect_911" and (code != "EMERG" or not r["hazard_flag"]): E(r, "redirect_911 needs EMERG + hazard")
        if r["lang"] not in p.SUPPORTED_LANGS and act not in ("handoff_language", "redirect_911", "escalate_human"):
            E(r, "non-English must be handoff_language unless it is a hazard")
        if r["lang"] in p.SUPPORTED_LANGS and act == "handoff_language": E(r, "English case labelled handoff_language")
        if act == "link_duplicate" and not r["ticket_ref"]: E(r, "link_duplicate needs ticket_ref")
        if r["ticket_ref"] and r["ticket_ref"] not in ids: E(r, f"ticket_ref {r['ticket_ref']} not in store")
        for c in r["secondary_codes"]:
            if c not in ROUTE: E(r, f"bad secondary code {c}")
        if code in p.SERVICE_CODES:
            spec = p.SERVICE_CODES[code]
            for k, v in f.items():
                if k not in spec["required_fields"]: E(r, f"field {k} not required for {code}")
                vals = spec["field_values"].get(k)
                if vals and v not in vals and (code, k, v) not in KNOWN_OFF_LIST:
                    E(r, f"{k}={v!r} not in field_values")
                elif vals and v not in vals:
                    W(r, f"{k}={v!r} off the closed list (known, see notes)")
            for k in r["omitted_fields"]:
                if k not in spec["required_fields"]: E(r, f"omitted {k} not a required field")
                if k in f: E(r, f"omitted {k} also in gold_fields")
            if r["trigger_id"] and r["trigger_id"] not in [t[0] for t in spec["e2_triggers"]]:
                E(r, f"trigger {r['trigger_id']} not a {code} trigger")
            # A value reserved for an E2 trigger must sit on a case with that trigger.
            # 311 v3: one value can be reserved for several triggers (TR-TREE "fallen limb":
            # limb_down and hospital_route), so the case may carry any of them.
            owners = {}
            for tid, tf in p.TRIGGER_FIELD_VALUES.get(code, {}).items():
                for k, reserved in tf.items():
                    for v in reserved:
                        owners.setdefault((k, v), []).append(tid)
            for (k, v), tids in owners.items():
                if f.get(k) == v and r["trigger_id"] not in tids:
                    E(r, f"{k}={v!r} is reserved for trigger {'/'.join(tids)} (case has {r['trigger_id']})")
        if r["rule_id"] == "E2" and not (r["trigger_id"] and r["hazard_flag"]): E(r, "E2 needs trigger_id and hazard_flag")
        if r["hazard_flag"] and r["rule_id"] not in ("E1", "E2"): E(r, "hazard_flag without E1/E2")

        # Duplicate check against the store, for street-address locations.
        loc = f.get("location", "")
        if code in p.SERVICE_CODES and re.match(r"^\d+ [A-Z]\w* \w+$", loc):
            hits = [t["ticket_id"] for t in p.find_duplicates(code, loc, f, tickets)]
            if act == "link_duplicate":
                if r["ticket_ref"] not in hits: E(r, f"ticket_ref not a duplicate in the store (hits: {hits})")
            elif hits and r["subtype"] != "e2_trigger_store_history":
                E(r, f"accidental duplicate of {hits} (gold says {act})")
            elif hits:
                W(r, f"open duplicate {hits} exists; gold keeps {act} by rule order")

    print(f"{len(rows)} cases, {len(errs)} errors, {len(warns)} warnings")
    for m in errs: print("ERROR", m)
    for m in warns: print("warn ", m)
    return not errs


if __name__ == "__main__":
    sys.exit(0 if main(*sys.argv[1:3]) else 1)
