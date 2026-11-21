"""W4 tool contracts (docs/interfaces.md): every tool returns a dict with ok, error and error_code."""
import time

from src.models.classifier import classify
from src.tools.handoff import handoff
from src.tools.knowledge import kb_search
from src.tools.policy import policy_lookup
from src.tools.tickets import create_ticket, get_status, search_tickets

BIN = {"location": "12 Oak Street, G1A 1A1", "bin_type": "recycling", "damage_type": "cracked lid"}


def test_policy_lookup_known_and_unknown():
    r = policy_lookup("SW-BIN")
    assert r["ok"] and r["unit"] == "Solid Waste" and r["required_fields"] == ["location", "bin_type", "damage_type"]
    assert not policy_lookup("XX-NOPE")["ok"]          # A1


def test_a4_create_ticket_requires_all_fields():
    r = create_ticket("SW-BIN", {"location": "12 Oak Street"})
    assert not r["ok"] and set(r["missing"]) == {"bin_type", "damage_type"}


def test_create_then_status_needs_postal_code():
    r = create_ticket("SW-BIN", BIN)
    assert r["ok"] and r["ticket_id"].startswith("311-")
    assert not get_status(r["ticket_id"], "")["ok"]           # D5
    assert not get_status(r["ticket_id"], "Z9Z 9Z9")["ok"]    # D5: wrong postal code
    s = get_status(r["ticket_id"], "g1a1a1")
    assert s["ok"] and s["status"] == "open"


def test_d3_search_returns_no_personal_data():
    create_ticket("SW-BIN", {**BIN, "name": "Jane Doe", "phone": "416-555-0199"})
    r = search_tickets("12 Oak Street", "SW-BIN")
    assert r["ok"] and len(r["tickets"]) == 1
    assert set(r["tickets"][0]) == {"ticket_id", "code", "status", "date_opened"}


def test_d2_redacted_before_storage(fresh_store):
    create_ticket("SW-BIN", {**BIN, "damage_type": "cracked, my SIN is 123 456 789"})
    assert "123 456 789" not in str(fresh_store.tickets)


def test_handoff_masks_transcript(fresh_store):
    r = handoff("E7", {"transcript": [{"role": "resident", "text": "call me at 416-555-0199"}]})
    assert r["ok"] and r["ref_id"].startswith("HO-")
    assert "416-555-0199" not in str(fresh_store.handoffs)


def test_classifier_interface():
    r = classify("my recycling bin lid is cracked")
    assert set(r) == {"code", "confidence", "top2"} and r["code"] == "SW-BIN"


KB_DOC = {"source": "waste_collection", "title": "Waste collection",
          "content": "# Waste collection\n\nCarts must be at the curb by 7:00 a.m. on collection day."}


def test_kb_search_returns_synced_passages(fresh_store):
    from src.rag.ingest import chunk_stale
    fresh_store.upsert_document(KB_DOC)                      # a row added in the dashboard
    assert chunk_stale(fresh_store) == 1
    r = kb_search("what time do carts need to be at the curb")
    assert r["ok"] and any("7:00" in p["text"] for p in r["passages"])


def test_kb_dashboard_edit_goes_live_without_redeploy(fresh_store):
    from src.rag.ingest import start_sync
    fresh_store.upsert_document(KB_DOC)
    stop = start_sync(0.05, store=fresh_store)
    try:
        fresh_store.upsert_document({**KB_DOC, "content": "Carts must be at the curb by 6:30 a.m."})
        deadline = time.time() + 5
        while time.time() < deadline and not any("6:30" in c["content"] for c in fresh_store.chunks):
            time.sleep(0.05)
    finally:
        stop.set()
    texts = [p["text"] for p in kb_search("curb carts time")["passages"]]
    assert any("6:30" in t for t in texts) and not any("7:00" in t for t in texts)


def test_kb_bulk_import(fresh_store, tmp_path):
    from src.rag.ingest import chunk_stale, import_files
    (tmp_path / "parks.md").write_text("# Parks\n\nPark washrooms open May to October.", encoding="utf-8")
    (tmp_path / "README.md").write_text("not a KB article", encoding="utf-8")
    assert import_files(fresh_store, tmp_path) == 1
    chunk_stale(fresh_store)
    assert kb_search("park washrooms")["passages"][0]["title"] == "Parks"


def test_policy_lookup_lists_cases_not_to_ticket():
    tree = policy_lookup("TR-TREE")
    assert [c["id"] for c in tree["not_handled"]] == ["private_tree"]
    assert tree["not_handled"][0]["outcome"] == "handoff_oos"
    assert policy_lookup("SW-BIN")["not_handled"] == []


def test_policy_lookup_routes_give_the_outcome_and_approved_reply():
    from src.config import policy
    oos = policy_lookup("oos")
    assert oos["ok"] and oos["route"] and oos["no_ticket"] and oos["outcome"] == "handoff_oos"
    assert oos["wording"] == policy()["wording"]["out_of_scope"]
    assert {"request": "Streetlight out", "route": "OOS"} in oos["other_services"]
    emerg = policy_lookup("EMERG")
    assert emerg["outcome"] == "redirect_911" and emerg["wording"] == policy()["wording"]["emergency"]
    assert "required_fields" not in emerg and "patterns" not in emerg


def test_routes_never_create_a_ticket(fresh_store):
    from src.tools.tickets import create_ticket
    out = create_ticket("OOS", {"location": "12 Oak Street"})
    assert not out["ok"] and "never creates a ticket" in out["error"] and fresh_store.tickets == []


def test_route_lookup_is_offered_to_the_model_but_not_as_a_ticket_code():
    from src.agent.rules import RuleState, record
    from src.tools.registry import tool_schemas
    schemas = {s["function"]["name"]: s["function"]["parameters"]["properties"] for s in tool_schemas()}
    assert {"EMERG", "OOS"} <= set(schemas["policy_lookup"]["code"]["enum"])
    assert "OOS" not in schemas["create_ticket"]["code"]["enum"]
    state = RuleState()
    record(state, "policy_lookup", {"code": "OOS"}, policy_lookup("OOS"))
    assert state.looked_up == set() and state.code is None   # A2 still needs a catalogue lookup


def test_create_ticket_refuses_a_case_the_city_does_not_handle(fresh_store):
    fields = {"location": "12 Oak Street", "city_or_private_tree": "Private tree", "what_is_wrong": "needs pruning"}
    out = create_ticket("TR-TREE", fields)
    assert not out["ok"] and out["not_handled"] == "private_tree" and out["outcome"] == "handoff_oos"
    assert "redirect_911 if it is a danger now" in out["error"] and fresh_store.tickets == []
    # refused before asking for the rest: a private tree never gets a ticket
    assert create_ticket("TR-TREE", {"city_or_private_tree": "private tree"})["not_handled"] == "private_tree"
    # a City tree is still filed
    assert create_ticket("TR-TREE", {**fields, "city_or_private_tree": "park tree"})["ok"]



def test_create_ticket_refuses_dumping_on_private_property(fresh_store):
    # policy v1.0.1: the IL-DUMP private_property case is marked by a field value, so code refuses it (A8)
    assert [c["id"] for c in policy_lookup("IL-DUMP")["not_handled"]] == ["private_property"]
    fields = {"location": "45 Elm Street", "material_type": "old mattress", "public_or_private_land": "Private property"}
    out = create_ticket("IL-DUMP", fields)
    assert not out["ok"] and out["not_handled"] == "private_property" and out["outcome"] == "handoff_oos"
    assert "redirect_911 if it is a danger now" in out["error"] and fresh_store.tickets == []
    # public land is still filed, and the field stays optional: never required, never asked for (D1)
    assert create_ticket("IL-DUMP", {**fields, "public_or_private_land": "public land"})["ok"]
    assert create_ticket("IL-DUMP", {"location": "Elm Park, Elm Street", "material_type": "tires"})["ok"]
    assert "public_or_private_land" not in policy_lookup("IL-DUMP")["required_fields"]

def test_agent_tickets_fill_migration_0006_columns(fresh_store):
    out = create_ticket("SW-BIN", BIN)
    t = fresh_store.tickets[0]
    # above the pipeline's seeds (100000 up) and fixtures (001045 to 001048)
    assert out["ticket_id"].endswith("-500001")
    assert (t["street"], t["source"], t.get("x_m")) == ("Oak Street", "agent", None)
    from src.tools.store import TICKET_COLUMNS
    assert {"street", "source"} <= set(TICKET_COLUMNS.split(",")) and "contact" not in TICKET_COLUMNS


# --- error codes (src/tools/errors.py, Interfaces doc section 3) ---

def test_every_result_carries_error_code():
    from src.tools.errors import ERROR_CODES
    good = [policy_lookup("SW-BIN"), policy_lookup("EMERG"), create_ticket("SW-BIN", BIN),
            search_tickets("12 Oak Street"), kb_search("bins"), handoff("E3", {})]
    assert all(r["ok"] and r["error_code"] is None for r in good)
    bad = {
        "unknown_code": [policy_lookup("XX-NOPE"), create_ticket("EMERG", BIN), create_ticket("XX-NOPE", BIN)],
        "missing_argument": [search_tickets(""), kb_search(" "), get_status("311-2026-500001", "")],
        "missing_fields": [create_ticket("SW-BIN", {"location": "12 Oak Street"}),
                           create_ticket("SW-BIN", {**BIN, "location": "near the park"})],
        "not_found": [get_status("311-2026-999999", "G1A 1A1")],
        "invalid_value": [handoff("Z9", {})],
    }
    for code, results in bad.items():
        assert code in ERROR_CODES
        for r in results:
            assert not r["ok"] and r["error"] and r["error_code"] == code, (code, r)


def test_store_failure_is_unavailable(fresh_store):
    fresh_store.fail.add("create_ticket")
    r = create_ticket("SW-BIN", BIN)
    assert r["error_code"] == "unavailable" and r["_failure"]


def test_policy_unavailable(monkeypatch):
    import src.tools.policy as pmod
    def broken():
        raise RuntimeError("no config")
    monkeypatch.setattr(pmod, "policy", broken)
    assert policy_lookup("SW-BIN")["error_code"] == "policy_unavailable"
    assert create_ticket("SW-BIN", BIN)["error_code"] == "policy_unavailable"
