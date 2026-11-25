"""Route classifiers (owner: C, W3 Models): classify(text) -> {code, confidence, top2, ...}.

Every classifier, the keyword stub or a fine-tuned model, is called the same
way, so the agent, re-runs and the eval harness can swap one for another by
setup name (Interfaces doc, section 4):

    classify = make_classifier("distilroberta_lora")
    out = classify("The lid on my green bin cracked. 14 Alder Lane.")
    # {"code": "SW-BIN", "confidence": 0.97, "top2": [...], "setup": ..., "model": ...}

The setups are the `classifier` section of src/eval/setups.yaml. kind "hf"
loads a Hugging Face sequence classifier from a local folder (only the
.safetensors weights, never a pickle such as training_args.bin). torch and
transformers are imported only when such a model is first used, so the stub,
the tests and the rest of the site run without them (requirements-ml.txt).

classify sees only the resident's text: no ticket data, no other residents (D7).
"""
from __future__ import annotations

import re
import threading
import time
from collections import OrderedDict
from pathlib import Path

from src.tools.policy import valid_codes

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SETUP = "keyword_stub"
MAX_LOADED = 2                 # fine-tuned models kept in memory at once (least recently used goes)
ROUTE_LABELS = ["SW-BIN", "SW-MISS", "AN-DEAD", "AN-INJ", "RD-POT", "TR-TREE", "PS-PROP", "IL-DUMP", "EMERG", "OOS"]


class ClassifierError(RuntimeError):
    """The classifier can't be used: unknown setup, missing weights or libraries."""


# ------------------------------------------------------------------ keyword stub
# Stub vocabulary only; the prompted classifiers' prompts go in prompts/classifier/ (Supabase config).
_KEYWORDS = {
    "SW-BIN": ["bin", "cart", "lid", "container", "broken bin", "stolen bin"],
    "SW-MISS": ["missed", "not picked up", "wasn't collected", "collection", "pickup", "garbage day"],
    "AN-DEAD": ["dead", "carcass", "roadkill"],
    "AN-INJ": ["injured", "hurt animal", "limping", "distressed", "bleeding animal", "trapped"],
    "RD-POT": ["pothole", "road damage", "crack in the road", "sinkhole", "road"],
    "TR-TREE": ["tree", "branch", "limb", "pruning"],
    "PS-PROP": ["property", "landlord", "mould", "mold", "no heat", "overgrown", "building"],
    "IL-DUMP": ["dumped", "dumping", "mattress", "couch", "junk", "needles", "debris"],
}


def classify(text: str) -> dict:
    """The keyword stub (setup keyword_stub)."""
    text = (text or "").lower()
    scores = {code: sum(len(re.findall(r"\b" + re.escape(k) + r"\b", text)) for k in kws)
              for code, kws in _KEYWORDS.items() if code in valid_codes()}
    total = sum(scores.values())
    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    if total == 0:
        return {"code": None, "confidence": 0.0, "top2": []}
    top2 = [{"code": c, "confidence": round(s / total, 2)} for c, s in ranked[:2] if s]
    return {"code": top2[0]["code"], "confidence": top2[0]["confidence"], "top2": top2}


# ------------------------------------------------------------------ setups
def setups() -> dict[str, dict]:
    from src.eval.setups import all_setups
    return {name: dict(s or {}) for name, s in (all_setups().get("classifier") or {}).items()}


def _kind(s: dict, name: str) -> str:
    return s.get("kind") or ("keyword" if name == DEFAULT_SETUP else "hf")


def _ml_missing() -> str | None:
    try:
        import torch  # noqa: F401
        import transformers  # noqa: F401
    except ImportError:
        return "torch and transformers aren't installed (requirements-ml.txt)"
    return None


def _labels(folder: Path) -> list[str] | None:
    import json
    try:
        cfg = json.loads((folder / "config.json").read_text(encoding="utf-8"))
        return [cfg["id2label"][str(i)] for i in range(len(cfg["id2label"]))]
    except (OSError, ValueError, KeyError):
        return None


def status(name: str) -> tuple[bool, str | None]:
    """(usable, why not) for a classifier setup, without loading it."""
    s = setups().get(name)
    if s is None:
        return False, f"no classifier setup {name!r} in src/eval/setups.yaml"
    if _kind(s, name) == "keyword":
        return True, None
    if not s.get("weights"):
        return False, "no weights folder in its setup"
    folder = ROOT / s["weights"]
    missing = [f for f in ("config.json", "model.safetensors") if not (folder / f).is_file()]
    if missing:
        return False, f"weights not installed ({', '.join(missing)} missing in {s['weights']})"
    labels = _labels(folder)
    if not labels or not set(labels) <= set(ROUTE_LABELS):
        return False, "its labels aren't the route codes"
    missing_libs = _ml_missing()
    if missing_libs:
        return False, missing_libs
    return True, None


def installed() -> list[dict]:
    """Every classifier setup, with whether it can be used here. For the pickers."""
    out = []
    for name, s in setups().items():
        usable, why = status(name)
        out.append({"name": name, "label": s.get("label") or name, "kind": _kind(s, name),
                    "available": usable, "reason": why, "loaded": name in _LOADED})
    return out


def default_setup() -> str:
    from src.config import app_config
    return (app_config().get("agent") or {}).get("classifier") or DEFAULT_SETUP


def check(name: str | None) -> str:
    """The setup name to use. Raises ClassifierError if a named one can't be used; None means
    the default (app.yaml agent.classifier), or the keyword stub if that isn't installed here."""
    if not name:
        name = default_setup()
        return name if status(name)[0] else DEFAULT_SETUP
    usable, why = status(name)
    if not usable:
        raise ClassifierError(f"classifier {name!r} can't be used: {why}")
    return name


# ------------------------------------------------------------------ fine-tuned models
_LOADED: OrderedDict[str, "_HFModel"] = OrderedDict()
_LOCK = threading.Lock()


class _HFModel:
    def __init__(self, name: str, s: dict):
        import torch
        from transformers import AutoModelForSequenceClassification, AutoTokenizer
        from transformers.utils import logging as hf_logging
        hf_logging.disable_progress_bar()
        folder = ROOT / s["weights"]
        self.name, self.torch = name, torch
        self.max_length = int(s.get("max_length") or 256)
        self.temperature = float(s.get("temperature") or 1.0)
        self.tokenizer = AutoTokenizer.from_pretrained(folder, local_files_only=True)
        self.model = AutoModelForSequenceClassification.from_pretrained(
            folder, local_files_only=True, use_safetensors=True)
        self.model.eval()
        self.labels = [self.model.config.id2label[i] for i in range(self.model.config.num_labels)]
        self.model_id = s.get("model_id") or f"{self.model.config.model_type}:{Path(s['weights']).name}"

    def __call__(self, text: str) -> dict:
        enc = self.tokenizer(text or "", truncation=True, max_length=self.max_length, return_tensors="pt")
        with self.torch.inference_mode():
            logits = self.model(**enc).logits[0] / self.temperature
        probs = self.torch.softmax(logits.float(), dim=-1).tolist()
        ranked = sorted(zip(self.labels, probs), key=lambda kv: -kv[1])
        top2 = [{"code": c, "confidence": round(p, 4)} for c, p in ranked[:2]]
        return {"code": top2[0]["code"], "confidence": top2[0]["confidence"], "top2": top2}


def _hf_model(name: str, s: dict) -> _HFModel:
    with _LOCK:
        m = _LOADED.get(name)
        if m is None:
            m = _HFModel(name, s)
            _LOADED[name] = m
            while len(_LOADED) > MAX_LOADED:
                _LOADED.popitem(last=False)
        _LOADED.move_to_end(name)
        return m


def make_classifier(name: str):
    """classify(text) -> {code, confidence, top2, setup, model} for a classifier setup.
    A fine-tuned model loads on its first call."""
    name = check(name)
    s = setups()[name]
    if _kind(s, name) == "keyword":
        return lambda text: classify(text) | {"setup": name, "model": "keyword_stub"}

    def run(text: str) -> dict:
        m = _hf_model(name, s)            # loads on first use; not part of latency_ms
        start = time.perf_counter()
        out = m(text)
        return out | {"setup": name, "model": m.model_id, "latency_ms": int((time.perf_counter() - start) * 1000)}
    return run


def classify_with(name: str | None, text: str) -> dict:
    """The agent's entry point: classify with the named setup (default when None).
    The eval harness replaces this to route calls through its cache."""
    return make_classifier(name)(text)


def preload(name: str | None = None) -> None:
    """Load the default (or named) classifier now, so the first resident doesn't wait."""
    name = check(name)
    if _kind(setups()[name], name) == "hf":
        _hf_model(name, setups()[name])
