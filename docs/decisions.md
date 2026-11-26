# Decisions

Every choice of model, prompt, data or architecture: what we chose, the
alternatives, and why. The report draws on this file directly.

## D-001 Supabase for tickets, handoffs, event log and knowledge base (2026-11-02)

* **Chose:** Supabase (hosted Postgres + pgvector), accessed from the server with the service key over its REST API.
* **Alternatives:** `data/tickets.db` SQLite as in the original plan; a local Postgres.
* **Why:** collaborators can inspect and edit tickets and knowledge-base documents in the Supabase dashboard without running the server; pgvector gives vector search in the same database. An in-memory store with the same methods keeps tests and CI key-free.
* **Affects:** W2 seeds tickets into Supabase (`supabase/seed.sql` is a 5-row placeholder) instead of SQLite; W4 tools read and write through `src/tools/store.py` only.

## D-002 Policy store stays in `config/policy.yaml` (2026-11-02, superseded by D-012)

* **Chose:** the service catalogue and rule IDs stay in YAML in the repo, read by `policy_lookup`.
* **Alternatives:** a `policy` table in Supabase next to the knowledge base.
* **Why:** the policy is versioned, reviewed through pull requests, and every test cites it; a dashboard edit would bypass review. The knowledge base is for facts the agent may quote (C5), not routing rules.

## D-003 Web front end instead of Streamlit/Gradio for the demo (2026-11-02)

* **Chose:** FastAPI serving a static HTML/JS chat page (`src/web/`).
* **Alternatives:** Streamlit or Gradio, as listed in W5.
* **Why:** the site is public through a Cloudflare tunnel and should look like a plain chat app; a static page also lets the OpenRouter key stay in page memory only. The five scripted demo cases can still run through the same `/api/chat`.

## D-004 Swappable LLM backbone: Ollama or OpenRouter (2026-11-02)

* **Chose:** one OpenAI-compatible `chat()` for both providers; the user picks provider and model in the page.
* **Why:** lets the team compare small local models against hosted ones without code changes, and the event log records `provider:model` for every call so W6 ablations can be filtered by model.
* **Key handling:** OpenRouter keys are never stored server-side or in browser storage; the page sends the key per request and it is discarded after the call (test: `tests/test_api.py`).

## D-005 Hybrid retrieval with full-text fallback (2026-11-02)

* **Chose:** Postgres full-text search always, plus optional pgvector search (Ollama `nomic-embed-text`, 768 dims) fused by RRF.
* **Why:** works with zero setup (no embedding model needed) and improves when embeddings are enabled. The embedding size is fixed in the migration; changing models means changing `vector(768)` and re-ingesting.

## D-006 kb\_search added as a tool (proposed, 2026-11-02)

* **Chose:** a seventh tool, `kb_search`, alongside the policy's six actions.
* **Why:** C5 allows facts about City services only when a tool returned them; retrieval needs to be a tool for that to hold. Needs B's and D's sign-off at the Nov 5 interface session (A9 lists permitted actions).

## D-007 Docker on the team server (2026-11-02)

* **Chose:** run the site as a Docker container (`Dockerfile`, `docker-compose.yml`), built on the server from the repo with `docker compose up -d --build`.
* **Alternatives:** a native Python process started by a Windows scheduled task (used briefly on 2026-11-02, then removed).
* **Why:** matches how the other services on that server run, one command to update, and the same image runs on any teammate's machine. Ollama stays on the host and is reached via `host.docker.internal`. (Config was mounted from `config/` until D-012 moved it to Supabase.)

## D-008 Supabase security hardening (2026-11-02)

* **Chose:** `supabase/migrations/0002_security_hardening.sql`: pgvector moved to the `extensions` schema, `kb_documents_stale` made `security_invoker`, fixed `search_path` on functions, `match_kb_chunks` executable by `service_role` only.
* **Why:** fixes every Supabase security-advisor finding except "RLS enabled, no policy", which is intended: only the server's secret key and the dashboard may access the tables.

## D-009 Knowledge base lives only in Supabase, synced live (2026-11-02)

* **Chose:** `kb_documents` in Supabase is the single source of knowledge-base content; the `data/kb/` files were removed (their content was already in Supabase). The server re-chunks new and edited rows every `retrieval.sync_interval_s` seconds in a background thread.
* **Alternatives:** Markdown files in the repo loaded by `python run.py ingest` (the first version); chunking inside Postgres with a trigger.
* **Why:** the RAG team can change what the agent knows from the dashboard without touching the repo or the server, and the deployed site only changes for structural changes. Chunking stays in Python so it can also call the embedding model, which a Postgres trigger can't.

## D-010 Admin page; config edits stored in Supabase (2026-11-02, config part superseded by D-012)

* **Chose:** an `/admin` page with a live, step-by-step walkthrough of each conversation from `interaction_events`, and browser editing of `config/` files and the knowledge base. A saved config file goes to Supabase `config_files` and overrides the repo file until reverted; every save is kept in `config_revisions`.
* **Alternatives:** writing edits to the server's checkout (a dirty checkout breaks `git pull`, and edits would vanish on the next clone); editing only through pull requests (D-002).
* **Why:** teammates tuning prompts, policy and guardrails see the effect on the live agent at once, without a redeploy. This amends D-002: the repo files stay the reviewed defaults and the tests run against them; a browser edit worth keeping is downloaded from `/admin` and committed. Saves that would break the agent (bad YAML, a missing key the code reads, a bad regex, a prompt that won't render) are refused.
* **Risk:** no login yet, by request, for the prototype. See docs/deployment.md section 3.

## D-011 D2 card pattern skips ticket numbers (2026-11-02)

* **Chose:** `payment_card` in `config/guardrails.yaml` no longer matches `311-YYYY-NNNNNN`.
* **Why:** ticket numbers have 13 digits, so the card pattern redacted them: in the logs, and in a resident's own status request before the model saw it, which broke A5 status lookups. Found by the admin walkthrough test (`tests/test_guardrails.py::test_d2_keeps_ticket_numbers`).

## D-012 All configuration lives only in Supabase (2026-11-02)

* **Chose:** `app.yaml`, `policy.yaml`, `guardrails.yaml`, `flow.yaml` and the prompts are rows in Supabase `config_files` and nowhere else; `config/` was removed from the repo. The site loads them from Supabase only. The tests use a fixture (`tests/fixtures/config/`) the site never reads; `python run.py config pull|push` copies between the two.
* **Alternatives:** repo files as defaults with Supabase overrides (D-010, the first version of `/admin`), which left two copies that could disagree.
* **Why:** one copy can't go stale, and what the team sees in `/admin` is exactly what the agent runs. Supersedes D-002 and the work plan's "prompts and policy live in config/" convention; review now happens through the save notes and `config_revisions` instead of pull requests.
* **Risk:** the site can't start a conversation without Supabase (it answers 503); once loaded, the config stays in memory through outages.

## D-013 The policy's decision logic is an editable tree (2026-11-02)

* **Chose:** `flow.yaml` holds the decision logic (input screen outcomes, gates before tools, guidance after tools, failure handling, reply checks) as trees of yes/no checks and actions that the agent runs at fixed hooks; `src/agent/flow.py` provides the checks and actions. Every node visited is logged, and `/admin` shows a conversation's path through the tree.
* **Alternatives:** keep the rules in code (`src/agent/rules.py`, the first version) and only document the flow.
* **Why:** policy owners can change the logic and see exactly why the agent did what it did, decision by decision, without code. Saves that would drop a [hard] rule (E1, A2, A8, D5, E6, E9, M3) are refused; D2 redaction and D6 masking stay in code.
* **Behaviour added by the default tree:** E3 (ask one clarifying question when the classifier is unsure; the repeat-handoff step is switched off until the real classifier and threshold land), C6 duplicate guidance, M2 ask-one-field guidance, and M3 confirmation guidance.

## D-014 Ticket and handoff tracking in /admin (2026-11-02)

* **Chose:** Tickets and Handoffs tabs with live lists, detail, status changes and notes; every change is kept in `record_updates` with its author. Tickets now record the conversation that created them (`tickets.interaction_id`). Migration `0005_tracking.sql`.
* **Why:** the team can follow what happens to each request after the agent hands it on, and jump between a record and the conversation behind it. Only staff change status; the agent still can't edit anything (A7). Shown masked (D6) while the page has no login.

## D-015 M3 accepts IDs the resident typed (2026-11-02)

* **Chose:** the reply check treats ticket and handoff IDs that the resident wrote as known, as well as those a tool returned.
* **Why:** a resident asking about their own ticket was handed off under M3 when the agent repeated the number back while asking for the postal code (A5). M3 is about the agent inventing IDs; IDs nobody gave are still blocked (`tests/test_agent.py`).

## D-016 Versioned static files (2026-11-02)

* **Chose:** pages link `/static/*` with a content hash and are sent `Cache-Control: no-cache`.
* **Why:** Cloudflare kept serving the previous `admin.js` for hours after a deploy, so the new Flow tab "did nothing" (the old script had no Flow tab).

## D-017 Analytics tab in /admin (2026-11-16)

* **Chose:** an Analytics tab computed live from `interaction_events` and `tickets`: requests per hour, day or month with outcome split, reply-time percentiles against G10, tokens and spend per model, handoff reasons, and running cost (model + prorated hosting + staff review of handoffs) against manual cost. Cost assumptions live in a new optional `costs` block in `app.yaml`, edited from the tab; defaults are the metrics doc's 312 s handle time and the midpoint ($45/h) of its $35 to $55 range until G sets the base case.
* **Why:** the team asked to see volume and running cost at a glance. Reading the event log keeps one source of truth with the Interactions tab, and needs no new tables. Live traffic can't measure intake success, so G9 itself stays with the eval harness.

## D-018 Model speed in Analytics (2026-11-16)

* **Chose:** per-model reply time (whole turn) and model-call time (p50, p95), output tokens per second and calls per reply, from the latency already logged in `interaction_events`. A turn belongs to the model that answered it; tree-only turns are a separate baseline.
* **Why:** the team wants to compare the local Ollama model with a commercial model on inference time. Live traffic compares them on real requests, but only fairly with enough replies per model (about 20 or more) on similar requests; a side-by-side run on a fixed prompt set would control for that.

## D-019 Re-run conversations on another model (2026-11-16)

* **Chose:** a button in /admin > Interactions that replays a logged conversation on a chosen model, and a side-by-side comparison in Analytics (speed, tokens, cost, and each turn's tools, rules and outcome). Writes to City records are simulated in a re-run; duplicate search sees only tickets that existed before the original; re-runs are left out of usage and cost.
* **Why:** the team wants to compare Ollama with a commercial model on the requests residents actually make, not only on speed. Replaying the masked log keeps raw resident text out of storage (D6), at the cost of stand-in details and follow-up messages written for the original agent; re-running on the original model gives a like-for-like baseline.

## D-020 Labour cost range for the cost model (proposed, 2026-11-17)

* **Proposed:** test $55 to $85 per productive hour, base $70, for manual 311 intake. That is the City's posted wage for 311 representatives ($35.17 to $38.53), plus benefits at the 2026 budget ratio (32.8%), divided by the share of paid time spent on contacts (60% to 85%, base 70%).
* **Alternatives:** the proposal's $35 to $55 an hour built up from the Job Bank regional wage ($17 to $31); the top down figure of $15.46 per interaction (division budget over total interactions).
* **Why:** the Job Bank range is for the region's private call centres and is well below what the City pays. The top down figure includes management, technology and self serve channels that an intake agent would not remove. Sources and working are in docs/sources.md and docs/assumptions.md.
* **Affects:** gate G9 in docs/metrics.md (cost per successful intake below manual cost at the base case labour rate), the Analytics tab default of $45, the W3 fine tuning payoff volume, and the decision memo. The workbook still runs on $45 until the team agrees; its Sensitivity sheet shows $35 to $85. The share of paid time on contacts is an assumption to confirm.

## D-021 Eval harness records and replays model answers (2026-11-18)

* **Chose:**
  * `src/eval/harness.py` runs each case in a fresh in-memory store, with the same tickets, a frozen clock (the ticket store's as-of date) and the run's config snapshot.
  * Every model, classifier and judge answer is cached under a key made from the model and the exact messages. That includes errors and retries.
  * `make reproduce` replays every run from that cache with no API keys, then rescores every results file.
* **Alternatives:**
  * Rescore the results CSVs only. Cheaper, but nothing checks that the numbers come from the recorded model outputs.
  * Re-run the models. That needs keys and money, and model drift changes the numbers.
* **Why:**
  * The rubric's "Great" bar asks for key results reproduced with one command, and metrics.md asks for cached raw outputs.
  * Because the agent is deterministic around the model call, a replay sends the same messages, and the harness can report that a rebuilt results file is byte-identical to the committed one.
* **Affects:**
  * Results CSVs, manifests and the summary are committed; none of them holds message text.
  * `results/cache/` and `results/events/` hold test-derived text, so they are git-ignored and shared with the locked test files (course rule).

## D-022 The harness derives the outcome and checks policy independently (2026-11-18)

* **Chose:**
  * Until the agent logs its outcome (open item for D), the harness derives it from what the conversation did: tickets, handoffs and their rules, the 911 wording, quoted duplicates, refusals, questions. Fixed, ordered rules that never read the expected label.
  * Commitments, invented IDs, personal data and M1/M2/M7/D8/D1 are checked with patterns kept in `src/eval/checks.yaml`, separate from the guardrail patterns in `guardrails.yaml`.
* **Why:**
  * metrics.md keeps outcome, code and fields rule-based.
  * If the harness reused the reply check's own patterns, anything that slipped past the guardrail would slip past the measurement too, and G6 would read 0 by construction.
  * The LLM judge adds findings and never clears one.
* **Known limit:** a genuine request with a hidden instruction (S5, expected `create_ticket`) reads as `refuse_and_continue` when the agent says aloud that it won't follow the instruction. Adversarial behaviour is scored through `adversarial_pass` and the judge.

## D-023 The agent logs its outcome on every reply (2026-11-20)

* **Chose:** each `reply` event carries the conversation's outcome so far, one of the ten names in `policy.yaml` `outcomes`; the last one is the final outcome the harness scores. It is decided from what the agent did (rules the tree fired, tickets created, the handoff rule, get_status and kb_search results, approved wording used, a duplicate named), in a fixed order (`src/agent/outcome.py`). Which handoff rules mean `handoff_oos` or `handoff_language` is read from `policy.yaml` `outcomes`. Nothing is logged when none applies, and the harness then uses its own rules.
* **Why:** policy v1.0 action item for W4. The harness's derivation reads reply text with patterns; the agent knows which rule fired and which wording it used. A genuine request with a hidden instruction now scores `create_ticket` (S5), the known limit of the text-based derivation.
* **Open:** the A8 check blocks any second ticket in a conversation, so the agent can't reach `handle_each` yet (two issues in one message need two tickets).

## D-024 Migration 0006: ticket grid columns, error_code, agent ticket range (2026-11-20)

* **Chose:** `tickets` gets `street`, `x_m`, `y_m` and `source` (seed, fixture or agent), and `interaction_events` gets `error_code`, as the 311 Interfaces doc (sections 5 and 7) proposed. Agent-created ticket IDs now start at `311-YYYY-500001`, in Supabase (`ticket_seq`) and in the in-memory store the tests and harness use.
* **Why:** the pipeline's seeded tickets carry these columns, and `radius_m` duplicate windows need grid positions. Starting the agent's IDs at 500001 keeps them clear of the seeds (100000 up), the fixtures (001045 to 001048) and re-run simulations (900001 up), so seeding Supabase can't collide with tickets the agent made.
* **Not yet:** `search_tickets` still matches on location text, not on the grid.

## D-025 Tool error codes (2026-11-21)

* **Chose:** every tool result carries `error_code` next to `ok` and `error`, with the rest of the result still at the top level, as section 3 of the 311 Interfaces doc locks (not a `data` wrapper). Codes are the doc's eight plus three the doc's list doesn't cover: `not_handled` (A8), `blocked` (a flow check stopped the call) and `bad_arguments` (A9). The orchestrator logs the code to `interaction_events.error_code`.
* **Why:** failures can be counted by kind without parsing the error text, and keeping results at the top level means the guardrail tests and the eval harness that read `ticket_id`, `missing` and the rest keep working.
* **Later:** `invalid_value` for closed fields is D-026, `pii_rejected` is D-027.

## D-026 create_ticket checks closed field values (2026-11-21)

* **Chose:** `create_ticket` refuses a closed field (any field with `field_options` in policy.yaml, required or optional) whose value isn't one of its values, with `error_code: invalid_value`, the fields in `invalid` and their allowed values in `allowed`. Matching ignores case and spaces, and the stored value is the policy's spelling. Values reserved for escalation triggers and `not_handled` cases also pass, since those rules handle them. An invalid field also counts as `missing`, like a vague location (A11), so E6 hands off after two tries.
* **Why:** policy v1.0 says `field_options` lists the closed values for every closed field, matching the data pipeline; tickets with other values wouldn't match seeded tickets or the eval's gold fields.

## D-027 create_ticket rejects D2 data in fields (2026-11-21)

* **Chose:** `create_ticket` refuses, with `error_code: pii_rejected`, when any field holds what the D2 redaction patterns find (SIN, health card, date of birth, driver's licence, payment card). Nothing is stored; `pii_fields` and the error name the fields and the kinds found, never the values. Before, it redacted the value and filed the ticket anyway.
* **Why:** D2 says the City never takes or stores this data. Filing a ticket with a `[REDACTED]` hole hides that the model put it there; refusing tells the model to leave it out and not ask for it, and lets the event log count it.
* **Note:** the resident's own message is still redacted by the input screen before the model sees it, so this mostly catches a model that asks for the data or rebuilds it. policy.yaml v1.0.2 names `create_ticket` in D2's `enforced_by`.

## D-028 Pickable classifiers, run in code (2026-11-22)

* **Chose:** the classifier is no longer a tool the model may call. The orchestrator runs the conversation's classifier on each resident message (on everything the resident has said, until the code is settled), logs a `classifier` event, applies E3/F2 through the `after_classifier` tree, and hands the model one line with the result. Classifiers are setups in `src/eval/setups.yaml` (`keyword_stub`, `distilroberta_lora`, `modernbert_lora`), picked per conversation on the chat page, per re-run in /admin, and by default in `app.yaml` `agent.classifier`. /admin > Analytics gets a Classifiers section and the re-run comparison shows each turn's code. EMERG and OOS count as valid codes for E3 (A1 names all 10 route codes).
* **Why:** to compare classifiers, every conversation needs a classifier result, and small models often skipped the tool. In code, a re-run that changes only the classifier isolates its effect. Logging the setup on the event needs no migration.
* **Not yet:** live traffic has no labels, so Analytics shows agreement with the code the conversation settled on, not accuracy (the eval harness measures that). The fine-tuned models' confidences aren't calibrated (`temperature` 1; ModernBERT gives 1.00 on clear cases), so E3's 0.70 / 0.10 thresholds mean less for them until a temperature is fitted. The frozen-base probes need the LoRA runs' training split. The agent prompt's step 2 now says the result comes with the message (repo and Supabase, saved 2026-11-22 before the server rebuild); a model that still calls a classifier tool gets the same result.

## D-029 Text with tool calls is shown; M1 in code; a refusal outranks one ticket (2026-11-23)

* **Chose:** (1) Text the model writes alongside a tool call is no longer dropped. It goes through the reply tree like any reply; if it passes, it is shown before the turn's reply (if the tree would block or replace it, only that text is dropped and the turn carries on). The model's history keeps it once, on its tool call. (2) M1 is said in code: the new wording `automated_intro` (policy.yaml v1.0.4) opens a conversation's first reply, after the 911 text on an E1 reply, and isn't added if the reply already says it is automated. The prompt's step 1 is gone. (3) The agent's logged outcome puts a refusal rule (S1, S2, S4, S7, D3, D4) ahead of a single ticket, the same order as the eval's own rules: a direct injection with a genuine request is `refuse_and_continue` with its ticket filed (demo case 5). S5 (instructions hidden in a field) is not a refusal rule, so that stays `create_ticket`.
* **Why:** testing after PR #15 found Haiku's refusal and M1 line, sent with its first tool call, never reached the resident; the model skipped M1 in five of six first replies; and the prompt-injection case logged `create_ticket` though the policy outcome is `refuse_and_continue`.
* **Known limit:** a model that narrates each step ("Let me check that.") now shows those lines too, and they count toward M7's word limit.

## D-030 The C1 date check skips questions (2026-11-25)

* **Chose:** the three C1 patterns in `src/eval/checks.yaml` no longer match a sentence that ends in a question mark. Each pattern starts with `(?![^?]*\?)`; the scorer already splits replies into sentences at `.`, `!` and `?`. Statements are still caught ("Crews will be there by Tuesday.", "Repairs usually take 5 days.").
* **Why:** Shawn's review of the Nov 25 Haiku runs found the agent's questions scored as date promises. "What day of the week is your recycling normally collected?" matched the `normally collected` pattern. C1 is about promising a date; asking the resident for one is not a promise. The C1 flag fell on 4 of 774 held-out and 2 of 340 production-like conversations before this change.
* **Effect:** a scoring change only, so no run is repeated: `make reproduce` rescores every cached run with the new patterns, and all setups are scored the same way. The number of C1 flags removed is reported in `results/summary.md`.
* **Not changed:** the agent's own reply check (`guardrails.yaml` `commitment`, `(by|on) <weekday>`) still replaces a reply that asks "Is your pickup on Tuesday?". That is an agent change and would need the agent re-run; listed as an improvement.
## D-030 M6: the model detects another language and hands off (2026-11-25)

* **Chose:** the model, not code, decides a message isn't in English. The prompt lists M6 among the handoff rules, and the `handoff` tool's `rule_id` now allows M6. An M6 handoff's reply opens with the new wording `english_only` ("I can only help in English for now.", policy.yaml v1.0.5) before the handoff wording, so the logged outcome is `handoff_language` and the eval's English-only pattern reads it the same way. An emergency in any language is a handoff under E1, whose reply now opens with the 911 wording (outcome `redirect_911`); a hazard is E2 (`escalate_human`).
* **Why:** testing found non-English requests were taken in English. M6 was enforced by "prompt", but the prompt only said to reply in English, and the tool didn't accept M6, so nothing could reach `handoff_language`. The team chose the LLM over a language-detection library.
* **Known limit:** detection depends on the model; a small model may still carry on in English. The input screen's E1 patterns are English, so a non-English emergency relies on the model calling E1.
