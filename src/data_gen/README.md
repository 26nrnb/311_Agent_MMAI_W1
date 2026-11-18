# 311 synthetic data pipeline (W2 Data, owner: Shravan)

`synth_311_pipeline.py` is the course reference pipeline (`synth_data_pipeline.py`,
airline example) re-aimed at our Municipal 311 intake agent. The machinery is the
reference one; the 311 changes are marked `311:` in the code.

## Run

```bash
pip install -r requirements.txt

# 1. Offline smoke test: no key, no cost (template stand-in)
python synth_311_pipeline.py --offline --n 120 --out data/smoke

# 2. Pilot with real models (~60 messages, a few cents): read ALL of it as a team
python synth_311_pipeline.py --n 60 --out data/pilot --max-cost 0.25

# 3. Full run (~3,000 messages, roughly $1-2 on the default models)
python synth_311_pipeline.py --n 3000 --out data/synthetic --seal-adversarial --max-cost 3.00
```

The Kaggle notebook loads this same file (from its own folder, a Kaggle dataset, or GitHub);
it no longer carries its own copy of the code, so edit the pipeline here only.

**Key:** one OpenRouter key (`sk-or-...`) as `OPENROUTER_API_KEY` in your environment,
a local `.env`, or a Kaggle secret. Set a credit limit on the key. The course does
not reimburse top-ups, so agree the budget with Saif.

**Before step 3:** put every hand-written test input (John's 35, Uday's trigger
examples, Abi's and Saif's cases) in `data/protected/*.txt`, one message per line.
Generated messages too similar to them are rejected (`matches_protected_test`).

**Sealed adversarial set:** with `--seal-adversarial`, the adversarial rows of the test split
leave `test.jsonl` and go to `data/protected/adversarial_sealed.jsonl` (plus
`adversarial_sealed_review.csv`). Use it only for the final evaluation; never tune on it or
commit it (`data/` is git-ignored).

## Outputs (in `--out`)

| File | What it is |
|---|---|
| `raw.jsonl` | every generated record, before QC |
| `kept.jsonl` | records that passed QC |
| `rejected.jsonl` | rejected records **with reasons**: evidence for the report |
| `train.jsonl` / `test.jsonl` | grouped split by scenario template + cross-split de-dup |
| `review.csv` | test rows for human review: `keep` / `fix` / `reject`, plus `corrected_action`. Rows where the generating model disagreed with the gold label are listed first. |
| `run_manifest.json` | prompt version, seed, models, temperatures, languages, counts, cost |
| `data/tickets.db` | synthetic ticket store, table `service_requests` with the Interface 7 columns (`code`, `postal_code`, `x_m`/`y_m`, `fields_json`, `unit`, `source`, ...). Seeded if missing or on the old columns. Duplicate cases point at its open tickets. The store's clock is `2026-11-11` (`store_meta.as_of`): measure duplicate windows from that date. Each eval run should start from a fresh copy of this file. |

## What each record carries

Gold labels (designed, not guessed): `service_code` (10 route codes), `expected_action`,
`rule_id`, `hazard_flag`, `required_fields_present`, `omitted_fields`, `secondary_codes`,
`trigger_id`, `ticket_ref`, `gold_fields` (for scoring field extraction).
Provenance: `model`, `temperature`, `seed`, `prompt_version`, `lang`, `persona`, `channel`,
`faker_slots`, `cost_usd`, `qc_reasons`, `review_flags`, `template_id`.

## Coverage design (for the report)

| Lever | Setting |
|---|---|
| Difficulty | 50% easy / 30% hard / 20% adversarial (course guide) |
| Hard subtypes | missing field, vague category, 2 and 3+ issues, conflicting info, E2 hazard trigger, **hazard look-alike**, emergency, **emergency look-alike**, duplicate, **near-miss duplicate** |
| Adversarial subtypes | prompt injection, prompt extraction, PII extraction (fake councillor/police), policy pressure ("your chatbot promised"), volunteered PII, hidden instruction, bulk/targeted tickets, abuse, off-topic |
| Personas x channels | 8 personas x chat / mobile app / web form / email |
| Models x temperatures | 3 providers (OpenAI, Google, Meta) x 0.3 / 0.7 / 1.0 / 1.2 |
| Languages | English only (decision Nov 12). On easy cases and hazards: en 68%, fr 12%, Franglais / Mandarin / Punjabi / Spanish 5% each, so ~18% of all messages are non-English (~16% `handoff_language`, rule M6) |
| Personal data | placeholders filled by Faker: fictional streets, `example.com` emails, 555 phones, 9xx SINs |
| Split | grouped by scenario template (3 paraphrases each) + cross-split TF-IDF de-dup |

## Decisions the team still owns

1. **Languages (policy rule M6): decided Nov 12, English only.** `SUPPORTED_LANGS = {"en"}`, so
   non-English messages are labelled `handoff_language` with rule `M6` (hazards and emergencies
   keep their safety action). Someone who reads each language should still review a sample.
2. **City name:** `CITY_NAME = "Gilead"`.
3. **Policy fields and triggers:** `SERVICE_CODES` mirrors `policy.yaml` v0.1. Update it if Uday's policy changes.
4. **Action names:** `ACTIONS` must match what Nick's agent outputs and Shawn's grader expects.
5. **Ticket store:** SQLite `data/tickets.db` (Interface 7). If the agent uses Supabase, load the same rows into a table with the same columns. `find_duplicates()` and `address_xy()` in the pipeline are the reference for the gold duplicate labels; the agent's `search_tickets` should agree with them.

## Known limits (say so in the report)

- Near-duplicate checks are TF-IDF (word overlap); paraphrases with different wording get through.
- `langdetect` is unreliable on short text and is skipped for Franglais.
- The generating model's guess at the label is only a soft flag; humans settle disagreements in `review.csv`.

Do not commit `.env`, `data/` or `.cache/` (see `.gitignore`).

## Prompt changelog (rubric: prompt iteration)

| Version | Date | Change | Why |
|---|---|---|---|
| 311-v1 | Nov 11 | First 311 version of the course pipeline | — |
| 311-v2 | Nov 12 | Vague cases get no details and a realistic confusable category; models told not to add injuries, danger, a second location or repeat history; repeat-caller persona removed; no parenthetical labels in the location line; stronger prompt-injection instruction; look-alikes implied, not stated; non-English written only by GPT and Gemini. New QC: omitted field present, raw address in template, two locations, lowercase before language check; "kindly" no longer a reject. | Pilot v1 (60 messages, $0.01): about 10 messages contradicted their gold label (e.g. "vague" messages that clearly named a cracked bin lid; a "missing day" message that said "we usually get picked up on Tuesdays"; routine reports that added "cyclists swerve around it"); 4 had "(landmark)" or "the the"; Llama's Mandarin was unreadable; ALL-CAPS English was misdetected as German. |
| 311-v2 (QC 2.1) | Nov 12 | QC only, prompt unchanged so cached answers stay valid: reject "my {dob} is ..." placeholder misuse; soft review flags `repeat_language` ("again", "third time") and `danger_language` ("dangerous", "almost got hurt") on non-hazard rows, listed first in review.csv. | Pilot v2 (60 messages, $0.01, 58 kept): label mismatches down from ~10 to ~5 borderline rows; remaining issues were wording a human should judge, not clear errors. |
| 311-v2 (data 2.2) | Nov 13 | Prompt unchanged. Ticket store on Interface 7 columns; duplicate cases respect each code's window (days, radius, match fields) and other cases can no longer land on an open ticket by accident; non-English share raised to ~18%; `handoff_language` rows use rule M6; `channel` values `mobile_app` / `web_form`. | Check against the 311 Interfaces doc: old store lacked the columns; duplicate cases could point at tickets outside their window or with a different bin type; non-English share was ~12%. |
| 311-v2 (data 2.3) | Nov 14 | Prompt unchanged. Varden Ave fixture tickets (001046-001048) now have `scheduled_day` = Tuesday. | Hand-written case hw_029 says the Varden pickup day is Tuesday; the fixtures had a Thursday placeholder. Gold label unchanged. |
| 311-v2 (data 2.4) | Nov 14 | Prompt unchanged. New closed-list values: AN-INJ dog, cat; TR-TREE fallen limb, leaning tree; PS-PROP structural damage, no heat; IL-DUMP hazardous material. Each is reserved for its E2 trigger (`TRIGGER_FIELD_VALUES`): only `e2_trigger` cases draw them, and those cases now always carry the matching value. Seeded ticket store unchanged. | Hand-written cases had no value for these (summary open item 6), and generated trigger cases could carry a contradicting value (e.g. `dog_or_cat` with animal_type pigeon). Offline check, 3,000 rows: 54 rows use the new values, all `escalate_human` with the right trigger; 0 on other outcomes. |
| 311-v3 | Nov 17 | Aligned with Operating Policy v0.3. New E2 location triggers, each with a look-alike: `near_school` (AN-DEAD, IL-DUMP), `hospital_route` (RD-POT, TR-TREE), `vulnerable_residents` (PS-PROP). The school landmark is kept out of AN-DEAD / AN-INJ / IL-DUMP cases that are not `near_school`, and always used on landmark-style `near_school` trigger cases. OOS topics extended with the v0.3 routing table; the OOS instruction no longer says "street problem". New emergency: streetlight pole down with exposed wires. E1 look-alikes: fire pit and fire station added, "empty gas can" removed. New adversarial subtype `volunteered_plate` (D1, EC-32: plate offered, never collected; `create_ticket`, 4% of adversarial, taken from `volunteered_pii`). `--seal-adversarial` export. Not generated yet (open team questions): AN-INJ near school, rodents, encampments. | Policy v0.3 review (Nov 16-17). Offline check, 3,000 rows: every new trigger present with the right outcome; school landmark appears 0 times on AN-DEAD / AN-INJ / IL-DUMP rows other than `near_school` triggers; 98 adversarial rows sealed, 0 left in test.jsonl, 0 templates shared with train; 105 hand-written cases still pass the checker. |
