# 311 Intake Agent: City of Gilead

A RAG-backed chatbot that takes non-emergency 311 service requests for the
fictional City of Gilead. It records and routes requests, it decides nothing
on the City's behalf. The rules it follows are in the operating policy
(`policy.yaml`) and its decision logic in the interaction tree (`flow.yaml`);
every guardrail and test cites a rule ID from it. All configuration lives
only in Supabase and is edited at `/admin`.

**All data is synthetic.** No real residents, addresses or City systems.

- Chat site: plain chat page, choose the model in the top-right corner.
- Admin page at `/admin`: walk through any conversation decision by
  decision through the interaction tree, and edit the tree, config, prompts
  and the knowledge base in the browser (live, with history). No login yet.
- LLM backbone: local **Ollama** or **OpenRouter** (bring your own key, kept in
  the browser tab's memory only and never saved).
- Database and knowledge base: **Supabase** (Postgres + pgvector).
- Public access: Cloudflare Tunnel to the local server
  ([docs/deployment.md](docs/deployment.md)).

## Quick start

Requires Python 3.11+ and, for local models, [Ollama](https://ollama.com)
with at least one tool-capable model (for example `ollama pull qwen3:8b`).

```bash
python -m venv .venv
.venv/Scripts/activate        # Windows; on macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # then fill in the Supabase values
python run.py serve           # http://127.0.0.1:8311
```

No Supabase yet? Set `USE_MEMORY_STORE=1` in `.env` to run with an in-memory
store (nothing persists across restarts).

With Docker instead (how the team server runs it):

```bash
cp .env.example .env          # fill in the Supabase values
docker compose up -d --build  # http://127.0.0.1:8311
```

See [docs/deployment.md](docs/deployment.md) for updates, logs and the Cloudflare tunnel.

## Supabase setup (once per project)

1. Create a project at supabase.com.
2. SQL editor: run the files in `supabase/migrations/` in order, then `supabase/seed.sql`.
3. Project Settings > API: copy the project URL and the **service_role /
   secret** key into `.env` (`SUPABASE_URL`, `SUPABASE_SERVICE_KEY`).
   The secret key stays on the server; RLS blocks the public anon key.
4. Add knowledge-base articles as rows in `kb_documents` (see below).

Tables: `tickets`, `handoffs`, `interaction_events` (event log),
`kb_documents`, `kb_chunks`, `config_files` and `config_revisions` (every
config file and its history). Collaborators can browse and edit them in the
Supabase Table Editor, or use `/admin`.

New Supabase project? Seed the config once:
`python run.py config push tests/fixtures/config`.

## Working on the knowledge base (RAG)

The knowledge base lives only in Supabase; there are no article files in
the repo. The agent may state facts about City services only when `kb_search`
returned them (policy rule C5).

- Add, edit or delete rows in `kb_documents` in the Supabase Table Editor.
  Fill `source` (any unique label), `title` and `content` (Markdown).
  Set `active = false` to hide a document without deleting it.
- The running site re-chunks new and changed rows on its own within
  `retrieval.sync_interval_s` (60 s). No pull, restart or redeploy.
- One topic per document; short paragraphs separated by blank lines (chunks
  are cut on blank lines). Synthetic content only. Nothing here overrides
  `policy.yaml`.
- Bulk upload a folder of `.md`/`.txt` files: `python run.py ingest --import DIR`.
- Retrieval is Postgres full-text search by default. For hybrid vector
  search: `ollama pull nomic-embed-text`, set
  `retrieval.embedding_provider: ollama` in `app.yaml` (in `/admin`), and set
  `chunked_at = null` on every `kb_documents` row so they are re-embedded.
- Tuning knobs (`top_k`, chunk size, overlap) are in `app.yaml`.

## Admin page (`/admin`)

- **Interactions:** every conversation, live from `interaction_events`.
  Pick one and press *Walk through* (arrow keys step; Shift+arrow jumps to
  the next rule that fired). Each tree run shows every decision, what it
  saw (e.g. "matched /smell of gas/", "creating SW-BIN; looked up: none")
  and the branch it took, with the tree beside it and the path highlighted.
  Model and tool calls show their arguments and results; tickets and
  handoffs come from the database. Tick *Follow newest* and chat on the
  main site to watch a conversation live. *Re-run with another model*
  replays the conversation's messages on a model you pick (Ollama or
  OpenRouter); tickets and handoffs in a re-run are simulated, never filed.
- **Tickets / Handoffs:** every ticket and handoff, live. Filter by status,
  code or rule, or search; open one to see what was collected, why it was
  handed off, the transcript, and the conversation behind it. Change its
  status (tickets: open, in progress, closed; handoffs: pending, in review,
  resolved) or add a note; every change is kept with who made it.
- **Flow:** the interaction tree, one tree per point in a turn. Click a
  step to edit its question, check, branches, wording or rule, switch it
  off, or add steps. Saves are checked (no missing nodes or loops, every
  [hard] rule still enforced) and go live for the next message.
- **Files:** every config file, raw (`policy.yaml`, `guardrails.yaml`,
  `app.yaml`, `flow.yaml`, prompts). Saves are checked first; every version
  is kept (*History*: load an old one and save to restore). *New prompt*
  starts e.g. `prompts/agent/system_v2.md`; point `agent.system_prompt` in
  `app.yaml` at it to switch.
- **Knowledge base:** create, edit, hide or delete documents; they are
  searchable by the agent as soon as you save.
- **Analytics:** requests per hour, day or month (pick the period), split
  into tickets filed, handoffs and the rest; reply time against the 10 s
  target (G10); reply and inference time per model, to compare Ollama with
  a commercial model; tokens and spend per model; why requests were handed off;
  re-runs side by side with their originals (speed, tokens, cost, and the
  tools, rules and outcome of every turn); and the running cost (model +
  hosting + staff time on handoffs) against
  doing the same requests by hand. The cost assumptions (staff rate, handle
  time, hosting) are edited on the same tab and saved to `app.yaml`.

All of these live only in Supabase: there is no copy in the repo to go
stale. The tests use their own fixture (`tests/fixtures/config/`), which
the site never reads.

## Working on the LLM / prompts

- Agent system prompt: `prompts/agent/` in the Supabase config (`/admin`
  > Files). Classifier, judge and data-generation prompts go in their own
  folders under `prompts/`. Prompts, policy and the tree live in the
  Supabase config, never in code.
- Record every prompt change in [docs/prompt_changelog.md](docs/prompt_changelog.md),
  including ones made in `/admin` (add a note when you save; the history keeps it too).
- The classifier is a keyword stub behind `classify(text) -> {code, confidence, top2}`
  in `src/models/classifier.py`; swap in the real model without changing the signature.
- Every model call is logged to `interaction_events` with `provider:model`,
  tokens, latency and cost, so runs can be compared by model.

## Repository layout

```
src/agent/         orchestrator loop, interaction tree engine (flow.py), conversation state
src/tools/         policy_lookup, search_tickets, create_ticket, get_status, handoff, kb_search
src/models/        LLM backbone (Ollama/OpenRouter), classify()
src/guardrails/    input screen, masking/redaction, output check
src/rag/           knowledge-base sync (chunking) and embeddings
src/web/           FastAPI server, static chat page, /admin page and API
src/data_gen/      synthetic data generation (W2)
src/eval/          evaluation harness (W5/W6): harness.py runs setups, score.py builds the summary,
                   outcome.py/checks.py score a conversation, setups.yaml names every setup
supabase/          SQL migrations and seed data
data/test/         locked test sets (read only by the eval harness; git-ignored, only LOCK.md is committed)
data/demo/         the five scripted demo cases and their seed tickets (demo material, not test data)
tests/             pytest suite (no keys or network needed); fixtures/config is the test config
results/           results files, manifests and summary (committed); cache/ and events/ (shared, not committed)
docs/              interfaces, architecture, decisions, evaluation, demo script, prompt changelog, deployment
```

## Commands

| Command | Make | What it does |
|---|---|---|
| `python run.py serve` | `make serve` | Start the chat site |
| `python run.py ingest` | `make ingest` | Re-chunk changed Supabase KB documents now (the server also does this every 60 s) |
| `python run.py config pull DIR` / `push DIR` | | Copy the Supabase config to / from a folder |
| `python run.py test` | `make test` | Run the tests (in-memory store, fixture config, scripted model) |
| `python run.py eval classify ...` / `eval agent ...` | | Run a setup over a locked test set ([docs/evaluation.md](docs/evaluation.md)) |
| `python run.py reproduce` | `make reproduce` | Rebuild every results table from cached model outputs, no API keys |
| `python run.py score` | `make score` | Score `results/*.csv` into `results/summary.json` and `summary.md` |
| `python run.py demo` | `make demo` | Rehearse the five scripted demo cases ([docs/demo_cases.md](docs/demo_cases.md)) |
| `docker compose up -d --build` | | Build and start the site in Docker |

## Evaluation and reproducing the results

Every number in the report comes from a file in `results/`, and one command rebuilds them all:

```bash
make reproduce        # or: python run.py reproduce
```

- **With the shared data bundle** (the locked test sets in `data/test/` plus `results/cache/` and `results/events/`, shared on Teams because test-derived text stays off GitHub): it replays every recorded run from its cached model answers, with no API keys or network. It reports whether each rebuilt results file is identical to the committed one, then rescores everything.
- **From a fresh clone without that bundle:** it rescores the committed results files into `results/summary.json` and `results/summary.md`.

To run a setup yourself (setups are named in `src/eval/setups.yaml`):

```bash
python run.py eval classify --setup keyword_stub --test data/test/test_heldout.jsonl --name heldout
python run.py eval agent --setup haiku --test data/test/test_heldout.jsonl --name heldout --limit 20
```

How cases are scored, what each file holds and the known limits are in [docs/evaluation.md](docs/evaluation.md).

## Demo

The five scripted cases (routine missed collection, ambiguous dumping report, road hazard, duplicate, prompt injection) run on the chat page. Rehearse them first with `python run.py demo`. The script, what to point out, and what to do if something goes wrong are in [docs/demo_cases.md](docs/demo_cases.md).

## How to extend: add a service category in an afternoon

The agent never hard-codes a category, so a new one is mostly configuration:

1. **Policy** (`/admin > Files > policy.yaml`): add the code under `service_catalogue`:
   - `category` and `unit`
   - `required_fields`
   - `field_options` for closed values
   - `escalation_triggers`
   - `duplicate_window`

   Bump `version` and add a `change_log` line. The tools, the code list the model may choose from, and the catalogue in the system prompt all read this file, so nothing else in `src/` changes.
2. **Escalation and wording:** if a trigger must be enforced in code rather than only by the model, add a node in `/admin > Flow`, or a pattern in `guardrails.yaml` for an emergency. Add any new approved wording under `wording`.
3. **Classifier:**
   - Today (stub): add the code's keywords to `_KEYWORDS` in `src/models/classifier.py`.
   - For the real classifiers: add the label to the prompts, and retrain the fine-tuned model on data that includes it.
4. **Data:** add the code to `SERVICE_CODES` (and `CONFUSABLE`) in `src/data_gen/synth_311_pipeline.py`, regenerate, and write some cases by hand.
5. **Tests:**
   - Add the code to `tests/fixtures/config/policy.yaml`.
   - Add a `create_ticket` test with its required fields to `tests/test_tools.py`.
   - Run `python run.py test`.
6. **Evaluate:** the harness reads codes from the data and the policy, so it needs no change. Run `python run.py eval agent ...` on the new test set, then `make reproduce`.

## Team docs

- [docs/interfaces.md](docs/interfaces.md): the shared contracts. Changing one needs the affected owner's review.
- [docs/architecture.md](docs/architecture.md): how a turn flows through the system.
- [docs/architecture_diagram.html](docs/architecture_diagram.html): the whole site as a swim-lane diagram (open in a browser).
- [docs/decisions.md](docs/decisions.md): what we chose and why.
- [docs/metrics 2.md](docs/metrics%202.md) and [docs/results_format 1.md](docs/results_format%201.md): what we measure, the go thresholds, and the results file columns.
- [docs/evaluation.md](docs/evaluation.md): the eval harness, `make reproduce`, and how a case is scored.
- [docs/demo_cases.md](docs/demo_cases.md): the five scripted demo cases.
- [docs/deployment.md](docs/deployment.md): running behind Cloudflare Tunnel.

Keys only in `.env` (git-ignored). Never commit real personal data.
