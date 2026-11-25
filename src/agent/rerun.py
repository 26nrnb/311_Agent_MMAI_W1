"""Re-run a logged conversation on another model or classifier (owner: E; for comparisons, W6).

The resident's messages are replayed in order into a new conversation on the
chosen model and classifier, so /admin > Analytics can set the two side by
side: speed, tokens, cost, the classifier's code and confidence, and the
decisions taken (tools called, rules fired, outcome). The classifier defaults
to the one the original used, so changing only the model (or only the
classifier) isolates its effect.

- The logs hold only masked text (D6), so that is what is replayed. Each
  mask tag gets a fixed stand-in (STAND_INS) so the model sees well-formed
  input; [REDACTED] (D2) stays as it is.
- The resident's later messages answered the original agent. If the re-run
  asks something else, they may not fit; that difference is itself a result.
- A re-run must not touch City records or see its own original's effects
  (ReplayStore): create_ticket and handoff are simulated, with IDs from a
  reserved range; search_tickets only sees tickets opened before the
  original conversation started. Status lookups, the knowledge base,
  config and the event log are the live ones.
- The new conversation starts with a "rerun" event, {"of": original id,
  "model": label, "classifier": setup}. Analytics leaves re-runs out of usage and cost.

Runs happen in a background thread; the admin page polls the job. An
OpenRouter key lives only in that job's selection while it runs.
"""
from __future__ import annotations

import itertools
import json
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from src.agent import orchestrator
from src.models import classifier as clf
from src.models.llm import LLMSelection
from src.tools import store as store_mod

MAX_RUNNING = 2   # the GPU serves the live site too
STAND_INS = {"STREET_NUMBER": "100", "UNIT": "unit 1", "PHONE": "555-0100",
             "EMAIL": "resident@example.com", "NAME": "Alex"}
_TAG = re.compile(r"\[([A-Z_]+)\]")


def unmask(text: str) -> str:
    return _TAG.sub(lambda m: STAND_INS.get(m.group(1), m.group(0)), text or "")


def _ts(value) -> float:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()


class ReplayStore:
    """The live store, except that writes to City records are simulated."""

    def __init__(self, base, before: float):
        self._base = base
        self._before = before
        self._seq = itertools.count(1)

    def __getattr__(self, name):
        return getattr(self._base, name)

    def _id(self, prefix: str) -> str:
        return f"{prefix}-{datetime.now(timezone.utc).year}-{900000 + next(self._seq):06d}"

    def create_ticket(self, row: dict) -> dict:
        return {**row, "ticket_id": self._id("311"), "status": "open",
                "opened_at": datetime.now(timezone.utc).isoformat()}

    def create_handoff(self, row: dict) -> dict:
        return {**row, "ref_id": self._id("HO"), "created_at": datetime.now(timezone.utc).isoformat()}

    def find_open_tickets(self, location, code, days):
        return [t for t in self._base.find_open_tickets(location, code, days) if _ts(t["opened_at"]) < self._before]


@dataclass
class Job:
    original: str
    model: str
    turns: int
    classifier: str = clf.DEFAULT_SETUP
    job_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    status: str = "running"      # running | done | failed
    done: int = 0
    rerun_id: str | None = None
    error: str | None = None
    started_at: float = field(default_factory=time.time)
    thread: threading.Thread | None = field(default=None, repr=False)

    def public(self) -> dict:
        return {k: getattr(self, k) for k in ("job_id", "original", "model", "classifier", "turns", "status",
                                              "done", "rerun_id", "error", "started_at")}


_JOBS: dict[str, Job] = {}


class RerunError(ValueError):
    pass


def root_of(events: list[dict], interaction_id: str) -> str:
    """The original behind a re-run (a re-run of a re-run replays the original)."""
    marker = next((e for e in events if e["step"] == "rerun"), None)
    if marker:
        try:
            return json.loads(marker["text"])["of"]
        except (TypeError, ValueError, KeyError):
            pass
    return interaction_id


def classifier_of(events: list[dict]) -> str | None:
    """The classifier setup a logged conversation used (None before classifiers were logged)."""
    return next((e.get("model") for e in events if e["step"] == "classifier" and e.get("model")), None)


def start(interaction_id: str, sel: LLMSelection, classifier: str | None = None) -> Job:
    sel.validate()
    if classifier:
        clf.check(classifier)
    if sum(j.status == "running" for j in _JOBS.values()) >= MAX_RUNNING:
        raise RerunError(f"{MAX_RUNNING} re-runs are already running; try again when one finishes")
    base = store_mod.get_store()
    events = base.interaction_events(interaction_id)
    original = root_of(events, interaction_id)
    if original != interaction_id:
        events = base.interaction_events(original)
    messages = [unmask(e.get("text")) for e in events if e["step"] == "input_screen"]
    if not messages:
        raise RerunError("this conversation has no resident messages to replay")
    used = classifier_of(events)
    classifier = classifier or (used if used and clf.status(used)[0] else clf.check(None))
    job = Job(original=original, model=sel.label, turns=len(messages), classifier=classifier)
    for old in sorted(_JOBS.values(), key=lambda j: j.started_at)[:-49]:
        _JOBS.pop(old.job_id, None)
    _JOBS[job.job_id] = job
    job.thread = threading.Thread(target=_run, args=(job, messages, sel, ReplayStore(base, _ts(events[0]["created_at"]))),
                                  daemon=True)
    job.thread.start()
    return job


def _run(job: Job, messages: list[str], sel: LLMSelection, store) -> None:
    store_mod.use_store(store)   # this thread only
    try:
        session = orchestrator.get_session(None)
        job.rerun_id = session.interaction_id
        orchestrator.log_event(session, "rerun", model=sel.label,
                               text=json.dumps({"of": job.original, "model": sel.label, "classifier": job.classifier}))
        for text in messages:
            orchestrator.handle_turn(session.interaction_id, text, sel, classifier=job.classifier)
            job.done += 1
        job.status = "done"
    except orchestrator.SetupError as e:
        job.status, job.error = "failed", str(e)
    except Exception as e:   # shown on the admin page; the live site is unaffected
        job.status, job.error = "failed", f"{type(e).__name__}: {e}"[:300]
    finally:
        sel.api_key = None


def get(job_id: str) -> Job | None:
    return _JOBS.get(job_id)
