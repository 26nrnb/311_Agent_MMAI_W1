"""Classifier setups, the in-code classification step, picking one per conversation,
re-runs with another classifier, and the classifier analytics."""
import json

import pytest
from fastapi.testclient import TestClient

from conftest import tool_call
from src.agent.orchestrator import handle_turn
from src.models import classifier as clf
from src.tools.registry import tool_schemas
from src.web import analytics as stats
from src.web.server import app

client = TestClient(app)
BIN = "My recycling bin lid is cracked at 12 Oak Street"


def chat(message, iid=None, classifier=None):
    body = {"interaction_id": iid, "message": message, "provider": "ollama", "model": "m"}
    if classifier:
        body["classifier"] = classifier
    return client.post("/api/chat", json=body)


def clf_events(store, iid=None):
    return [e for e in store.events if e["step"] == "classifier" and (iid is None or e["interaction_id"] == iid)]


@pytest.fixture
def two_classifiers(monkeypatch):
    """A second installed setup, "other_stub", that always says RD-POT, so runs can differ."""
    real_setups, real_make = clf.setups, clf.make_classifier
    monkeypatch.setattr(clf, "setups", lambda: {**real_setups(), "other_stub": {"kind": "keyword", "label": "Other"},
                                                "ghost": {"kind": "hf", "label": "Ghost", "weights": "no/such/folder"}})

    def make(name):
        if name == "other_stub":
            return lambda text: {"code": "RD-POT", "confidence": 0.91,
                                 "top2": [{"code": "RD-POT", "confidence": 0.91}, {"code": "SW-BIN", "confidence": 0.05}],
                                 "setup": "other_stub", "model": "fake"}
        return real_make(name)
    monkeypatch.setattr(clf, "make_classifier", make)


# ---------------------------------------------------------------- setups

def test_installed_lists_setups_with_why_not(two_classifiers):
    by_name = {c["name"]: c for c in clf.installed()}
    assert by_name["keyword_stub"]["available"] and by_name["other_stub"]["available"]
    assert not by_name["ghost"]["available"] and "weights not installed" in by_name["ghost"]["reason"]
    with pytest.raises(clf.ClassifierError):
        clf.check("ghost")
    with pytest.raises(clf.ClassifierError):
        clf.check("no_such_setup")
    assert clf.check(None) == "keyword_stub"


def test_default_that_is_not_installed_falls_back_to_the_stub(two_classifiers, monkeypatch):
    monkeypatch.setattr(clf, "default_setup", lambda: "ghost")
    assert clf.check(None) == "keyword_stub"


def test_keyword_stub_result_names_its_setup():
    r = clf.make_classifier("keyword_stub")("cracked recycling bin lid")
    assert r["code"] == "SW-BIN" and r["setup"] == "keyword_stub" and r["top2"][0]["code"] == "SW-BIN"


# ---------------------------------------------------------------- in-code classification

def test_classifier_runs_in_code_before_the_model(llm, sel, fresh_store):
    fake = llm("Which bin is it?")
    handle_turn(None, BIN, sel)
    [ev] = clf_events(fresh_store)
    assert ev["model"] == "keyword_stub" and ev["outcome"] == "sure" and ev["latency_ms"] is not None
    assert json.loads(ev["text"])["code"] == "SW-BIN"
    assert "[Classifier: SW-BIN" in fake.calls[0][-1]["content"]          # the model is told
    assert "classifier" not in {t["function"]["name"] for t in tool_schemas()}   # no longer a tool


def test_classification_stops_once_the_code_is_settled(llm, sel, fresh_store):
    llm("Which bin is it?", "Thanks.")
    iid = handle_turn(None, BIN, sel)["interaction_id"]
    handle_turn(iid, "the recycling one", sel)
    assert len(clf_events(fresh_store)) == 1


def test_unsure_twice_hands_off_e3_without_asking_the_model_again(llm, sel, fresh_store):
    fake = llm("Could you tell me more about the problem?")
    iid = handle_turn(None, "something is wrong out front", sel)["interaction_id"]
    assert "Ask the resident ONE clarifying question" in fake.calls[0][-1]["content"]
    out = handle_turn(iid, "it's just not right", sel)
    assert out["rule_id"] == "E3" and fresh_store.handoffs and len(fake.calls) == 1
    assert [e["outcome"] for e in clf_events(fresh_store)] == ["unsure", "unsure"]


def test_a_status_check_is_not_classified(llm, sel, fresh_store):
    llm("What's the postal code on it?")
    handle_turn(None, "what's the status of 311-2026-500001?", sel)
    assert not clf_events(fresh_store)


def test_a_prompt_that_still_calls_the_classifier_tool_gets_the_result(llm, sel, fresh_store):
    fake = llm([tool_call("classifier", {"text": "bin"})], "Which bin is it?")
    handle_turn(None, BIN, sel)
    tool_msg = next(m for m in fake.calls[1] if m["role"] == "tool")
    assert json.loads(tool_msg["content"])["code"] == "SW-BIN"
    assert not any(e["step"] == "tool:classifier" for e in fresh_store.events)


def test_route_codes_count_as_sure():
    from src.agent.rules import unsure_reason
    assert unsure_reason({"code": "OOS", "confidence": 0.95, "top2": [{"code": "OOS", "confidence": 0.95}]}) is None
    assert unsure_reason({"code": "XX", "confidence": 0.95}) is not None


def test_a_failing_classifier_counts_as_unsure_f2(llm, sel, fresh_store, monkeypatch):
    def broken(name, text):
        raise RuntimeError("weights gone")
    monkeypatch.setattr(clf, "classify_with", broken)
    llm("Could you tell me more?")
    handle_turn(None, BIN, sel)
    [ev] = clf_events(fresh_store)
    assert ev["outcome"] == "error" and ev["error_code"] == "unavailable" and ev["rule_id"] == "E3"


# ---------------------------------------------------------------- picking one

def test_chat_page_lists_and_picks_a_classifier(llm, fresh_store, two_classifiers):
    d = client.get("/api/classifiers").json()
    assert d["default"] == "keyword_stub" and "other_stub" in {c["name"] for c in d["classifiers"]}
    assert client.get("/api/config").json()["default_classifier"] == "keyword_stub"
    assert chat("hello", classifier="ghost").status_code == 400
    assert chat("hello", classifier="no_such").status_code == 400

    llm("Where is it?", "Thanks.")
    iid = chat("there's a big hole", classifier="other_stub").json()["interaction_id"]
    assert clf_events(fresh_store, iid)[0]["model"] == "other_stub"


# ---------------------------------------------------------------- re-runs

def _rerun(iid, classifier=None, model="other"):
    from src.agent import rerun
    r = client.post(f"/api/admin/interactions/{iid}/rerun",
                    json={"provider": "ollama", "model": model, "classifier": classifier})
    if r.status_code == 200:
        rerun.get(r.json()["job_id"]).thread.join(10)
        return rerun.get(r.json()["job_id"])
    return r


def test_rerun_with_another_classifier_and_compare(llm, fresh_store, two_classifiers):
    llm("Which bin is it?")
    iid = chat(BIN).json()["interaction_id"]

    llm("Where is the pothole?")
    job = _rerun(iid, classifier="other_stub", model="m")
    assert job.status == "done" and job.classifier == "other_stub", job.error
    assert clf_events(fresh_store, job.rerun_id)[0]["model"] == "other_stub"
    marker = next(e for e in fresh_store.events if e["step"] == "rerun" and e["interaction_id"] == job.rerun_id)
    assert json.loads(marker["text"])["classifier"] == "other_stub"

    llm("Which bin is it?")
    same = _rerun(iid)                      # no classifier given: the original's
    assert same.classifier == "keyword_stub"
    assert _rerun(iid, classifier="ghost").status_code == 400

    d = client.get("/api/admin/reruns").json()
    runs = d["groups"][0]["runs"]
    assert [r["classifier"] for r in runs] == ["keyword_stub", "other_stub", "keyword_stub"]
    assert runs[0]["turns"][0]["classifier"]["code"] == "SW-BIN"
    assert runs[1]["turns"][0]["same_code"] is False and runs[1]["same_codes"] == 0
    assert runs[2]["turns"][0]["same_code"] is True and runs[2]["same_codes"] == 1
    by = {m["classifier"]: m for m in d["by_classifier"]}
    assert by["other_stub"]["same_code_rate"] == 0 and by["keyword_stub"]["same_code_rate"] == 1


# ---------------------------------------------------------------- analytics

def test_analytics_classifier_section(llm, fresh_store, two_classifiers):
    llm([tool_call("policy_lookup", {"code": "SW-BIN"})], "Which bin is it?")
    chat(BIN)                                                    # stub: SW-BIN, agent looked up SW-BIN
    llm([tool_call("policy_lookup", {"code": "SW-BIN"})], "Which bin is it?")
    chat(BIN, classifier="other_stub")                           # says RD-POT, agent looked up SW-BIN
    llm("Could you tell me more?")
    chat("something is wrong out front")                         # stub unsure, nothing looked up

    rows = {r["classifier"]: r for r in client.get("/api/admin/analytics", params={"bucket": "hour", "periods": 2}).json()["classifiers"]}
    stub, other = rows["keyword_stub"], rows["other_stub"]
    assert stub["classifications"] == 2 and stub["conversations"] == 2 and stub["unsure"] == 1
    assert stub["with_final"] == 1 and stub["agree_rate"] == 1
    assert other["agree_rate"] == 0 and other["unsure_rate"] == 0
    assert abs(sum(b["share"] for b in other["bands"]) - 1) < 1e-9 and other["bands"][3]["share"] == 1


def test_classifier_stats_prefers_the_ticket_code_and_skips_reruns():
    def ev(iid, step, text, outcome="ok", model=None):
        return {"interaction_id": iid, "step": step, "model": model, "latency_ms": 5, "outcome": outcome,
                "rule_id": None, "error_code": None, "text": json.dumps(text), "created_at": "2026-11-22T00:00:00+00:00"}
    events = [
        ev("a", "classifier", {"code": "TR-TREE", "confidence": 0.8}, "sure", "x"),
        ev("a", "tool:policy_lookup", {"args": {"code": "RD-POT"}}),
        ev("a", "tool:create_ticket", {"args": {"code": "TR-TREE"}}),
        ev("a", "tool:policy_lookup", {"args": {"code": "RD-POT"}}),      # after the ticket: ignored
        ev("r", "classifier", {"code": "TR-TREE", "confidence": 0.8}, "sure", "x"),
    ]
    [row] = stats.classifier_stats(events, skip={"r"})
    assert row["classifications"] == 1 and row["agree_rate"] == 1


# ---------------------------------------------------------------- the real fine-tuned models

@pytest.mark.parametrize("name", ["distilroberta_lora", "modernbert_lora"])
def test_fine_tuned_model_classifies_when_installed(name):
    usable, why = clf.status(name)
    if not usable:
        pytest.skip(why)
    r = clf.make_classifier(name)("The lid on my green recycling bin cracked. 14 Alder Lane.")
    assert r["code"] == "SW-BIN" and r["setup"] == name and 0 <= r["confidence"] <= 1
    assert len(r["top2"]) == 2 and r["top2"][0]["confidence"] >= r["top2"][1]["confidence"]
