PY ?= python

.PHONY: install serve ingest test reproduce score demo

install:
	$(PY) -m pip install -r requirements.txt

serve:
	$(PY) run.py serve

ingest:
	$(PY) run.py ingest

test:
	$(PY) run.py test

# W5/W6: replay every recorded run from results/cache (no API keys) and rebuild
# results/summary.json and summary.md. Without the shared cache it rescores the
# committed results files.
reproduce:
	$(PY) run.py reproduce

score:
	$(PY) run.py score

demo:
	$(PY) run.py demo
