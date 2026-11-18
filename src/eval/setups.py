"""Resolve setup names from src/eval/setups.yaml into classifiers and model selections (owner: E)."""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

import yaml

from src.models.llm import LLMSelection

SETUPS_FILE = Path(__file__).with_name("setups.yaml")


class SetupNotFound(KeyError):
    pass


@lru_cache
def all_setups() -> dict:
    return yaml.safe_load(SETUPS_FILE.read_text(encoding="utf-8")) or {}


def setup(kind: str, name: str) -> dict:
    """The setup's settings; kind is classifier, agent or judge."""
    found = (all_setups().get(kind) or {}).get(name)
    if found is None:
        known = ", ".join(sorted(all_setups().get(kind) or {})) or "none"
        raise SetupNotFound(f"no {kind} setup {name!r} in {SETUPS_FILE.name} (known: {known})")
    return found or {}


def classifier(name: str):
    """classify(text) -> {code, confidence, top2, ...} for a classifier setup."""
    setup("classifier", name)
    from src.models import classifier as clf
    if hasattr(clf, "make_classifier"):          # C's shared interface (Interfaces doc, section 4)
        return clf.make_classifier(name)
    if name == "keyword_stub":
        return clf.classify
    raise SetupNotFound(f"{name!r} needs make_classifier() in src/models/classifier.py, which isn't there yet")


def selection(kind: str, name: str, require_key: bool = True) -> LLMSelection:
    """The LLMSelection for an agent or judge setup. The key is read from the
    environment here and only lives in this object (never logged or saved)."""
    s = setup(kind, name)
    key = os.environ.get(s["key_env"]) if s.get("key_env") else None
    if require_key and s.get("key_env") and not key:
        raise SetupNotFound(f"setup {name!r} needs the {s['key_env']} environment variable (put it in .env)")
    return LLMSelection(provider=s["provider"], model=s["model"], api_key=key)


def label(kind: str, name: str) -> str:
    s = setup(kind, name)
    return f"{s['provider']}:{s['model']}"
