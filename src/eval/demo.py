"""Rehearse the six scripted demo cases (owner: E, W5) before showing them on the chat page.

    python run.py demo                     # default agent setup from setups.yaml (Ollama)
    python run.py demo --setup haiku       # OpenRouter; needs OPENROUTER_API_KEY in .env
    python run.py demo --replay            # show the last rehearsal again, offline

Each case runs through the real agent (in-memory store, config from Supabase or the test
fixture) and prints what the resident would see, the outcome reached and the outcome the
script expects. The store is the v3 ticket store, data/tickets.db (404 tickets, git-ignored:
copy it from the team's data_v3 folder). On a fresh clone without it, the demo uses
data/demo/demo_tickets.json: the two store tickets the script needs (the duplicate in case 4
and the status check in case 6), copied unchanged. The cases and what to point out live in
data/demo/demo_cases.jsonl and docs/demo_cases.md. They are demo material, not test cases:
keep them out of the locked test sets.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from src.eval import harness, setups, testset
from src.eval.cache import Cache

DEMO_CASES = harness.ROOT / "data" / "demo" / "demo_cases.jsonl"
DEMO_TICKETS = harness.ROOT / "data" / "demo" / "demo_tickets.json"
STORE = harness.ROOT / "data" / "tickets.db"   # the v3 ticket store (data_v3), git-ignored


def ticket_source(path: str | None = None) -> Path:
    """The ticket store the rehearsal uses: --tickets, else data/tickets.db, else the demo's two tickets."""
    if path:
        return Path(path)
    return STORE if STORE.exists() else DEMO_TICKETS


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python run.py demo")
    p.add_argument("--setup", default="ollama_qwen3_4b", help="agent setup from src/eval/setups.yaml")
    p.add_argument("--replay", action="store_true",
                   help="replay the last rehearsal's recorded answers offline instead of calling the model")
    p.add_argument("--tickets", help="tickets.db or tickets.json (default: data/tickets.db if present)")
    a = p.parse_args(argv)

    cases = testset.load(DEMO_CASES)
    tickets, where = harness.load_tickets(str(ticket_source(a.tickets)))
    missing = sorted({c.ticket_ref for c in cases if c.ticket_ref} - {t["ticket_id"] for t in tickets})
    if missing:
        print(f"error: {where} has no ticket {', '.join(missing)}, which the demo needs", file=sys.stderr)
        return 2
    try:
        sel = setups.selection("agent", a.setup)
    except setups.SetupNotFound as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    s = setups.setup("agent", a.setup)
    clf_name = s.get("classifier", "keyword_stub")
    work = harness.RESULTS / "demo" / a.setup
    mode = "replay" if a.replay else "live"    # a rehearsal always asks the model unless told not to
    try:
        cfg_files, source = harness._snapshot_config(work, mode)
    except harness.RunError:
        print(f"error: no rehearsal recorded yet in {work.relative_to(harness.ROOT)}: run `python run.py demo"
              f"{'' if a.setup == 'ollama_qwen3_4b' else ' --setup ' + a.setup}` first", file=sys.stderr)
        return 2
    llm_cache, clf_cache = Cache(work / "llm.jsonl", mode), Cache(work / "classifier.jsonl", mode)
    print(f"Demo rehearsal on {setups.label('agent', a.setup)} (config: {source}; tickets: {where}, "
          f"{len(tickets)})\n")
    passed = 0
    with harness._patched(llm_cache, clf_cache, setups.classifier(clf_name), clf_name) as seen:
        for case in cases:
            seen.clear()
            from src.agent.orchestrator import SetupError
            try:
                row, events = harness.run_agent_case(case, sel, cfg_files, tickets)
            except SetupError as e:
                print(f"error: {e}", file=sys.stderr)
                return 2
            replies = row["_replies"]
            ok = row["actual_action"] == case.expected_action
            passed += ok
            print(f"== {case.case_id} ({case.subtype}): expected {case.expected_action}, "
                  f"got {row['actual_action']}  [{'OK' if ok else 'CHECK'}]")
            for resident, reply in zip(case.messages, replies):
                print(f"  Resident: {resident}")
                print(f"  Agent:    {reply}")
            if row["violation_rules"]:
                print(f"  Rules flagged by the harness checks: {row['violation_rules']}")
            print()
    from src.config import reload
    reload()
    print(f"{passed}/{len(cases)} demo cases reached the scripted outcome. Answers are saved in "
          f"{work.relative_to(harness.ROOT)} (git-ignored); `python run.py demo --replay` shows them again offline.")
    return 0 if passed == len(cases) else 1


if __name__ == "__main__":
    sys.exit(main())
