"""Record and replay raw model outputs (owner: E, W5).

Every model answer a run receives is saved under results/cache/<run_id>/, so
`make reproduce` can rebuild every results file without API keys or network
(results_format.md: "Raw model outputs are cached").

Why record-and-replay works here: apart from the model, a turn is
deterministic. The harness gives every case a fresh in-memory store with the
same tickets, a frozen clock and the run's config snapshot, so the agent
sends the model exactly the same messages on replay and the cache key
(model label + messages + tool schemas) matches.

The cache holds model text, which can quote test messages, so it is
git-ignored and shared with the locked test files, never pushed (course rule).
API keys are never part of a key or a record.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

MODES = ("auto", "live", "replay")


class CacheMiss(RuntimeError):
    """Replay asked for a model answer that was never recorded."""


def key_for(*parts) -> str:
    blob = json.dumps(parts, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class Cache:
    """One JSONL file of {key, value} records, loaded into memory and appended to."""

    def __init__(self, path: Path, mode: str = "auto"):
        if mode not in MODES:
            raise ValueError(f"cache mode must be one of {MODES}")
        self.path, self.mode = Path(path), mode
        self.hits = self.misses = 0
        self._data: dict[str, dict] = {}
        if self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    rec = json.loads(line)
                    self._data[rec["key"]] = rec["value"]

    def get_or_call(self, key: str, call):
        """Return the recorded value for `key`, or call(), record and return it."""
        if self.mode != "live" and key in self._data:
            self.hits += 1
            return self._data[key]
        if self.mode == "replay":
            raise CacheMiss(f"no recorded answer in {self.path} (key {key[:12]})")
        self.misses += 1
        value = call()
        self._data[key] = value
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"key": key, "value": value}, ensure_ascii=False) + "\n")
        return value
