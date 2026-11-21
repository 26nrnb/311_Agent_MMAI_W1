# Results file format

Owner: F (W6) · Written by the eval harness (`src/eval/harness.py`) · Used by W6, W7 and the report · Aligned with operating policy v1.0

The eval harness writes one CSV per run to `results/<run_id>.csv`, with **one row per test case per setup**. Every run also writes `results/manifests/<run_id>.json` (setup, test-set hash, config version). Runs are never overwritten. `python -m src.eval.score results/*.csv` reads all run files and writes the comparison to `results/summary.json` and `results/summary.md`.

`run_id` = date, setup, test set and a short hash of the test file, e.g. `2026-11-20_haiku_heldout_a1b2c3`.

The CSV and the manifest hold no message text, so they can be committed. `results/cache/` (every model and judge answer) and `results/events/` (the masked event logs) hold text derived from the test set, so they stay off GitHub and are shared with the locked test files. With them, `make reproduce` rebuilds every results file without API keys.

To see a valid example, run `python -m src.eval.score fake --out results/fake_run.csv`.

## Columns

Booleans are written as `true`/`false`. A blank means "not applicable".

| Column | Required | Filled by | Meaning |
|---|---|---|---|
| run_id | yes | harness | Same as the file name |
| setup | yes | harness | Setup name from `src/eval/setups.yaml`, e.g. `haiku`, `sonnet`, or an ablation such as `rules_off` |
| test_set | yes | harness | `heldout`, `prod` or `adversarial` |
| interaction_id | yes | test file | The test case's ID (the record's `id`), e.g. `rec_00042` or `hw_001` |
| expected_code | yes | test file | Expected route code (the record's `service_code`) |
| predicted_code | yes | agent log | The route code the classifier or agent settled on |
| expected_action | yes | test file | Expected outcome, one of the ten names in operating policy v1.0: create_ticket, ask_clarification, link_duplicate, escalate_human, redirect_911, handoff_oos, handle_each, refuse_and_continue, handoff_language, report_status |
| actual_action | yes | agent log or harness | Final outcome the agent reached, same ten names. Taken from the agent's log; until the agent logs it, derived by the harness from what the conversation did (see `outcome_source`) |
| hazard_flag | yes | test file | Gold hazard flag |
| lang | recommended | test file | Language of the message (`en`, `fr`, ...). Blank is read as English. Route accuracy is measured on English messages; hazard recall is also reported by language. |
| fields_complete | if tickets were created | harness | `true` if every ticket created (create_ticket or handle_each) holds every required field in policy.yaml for its code. Measures presence, not exact wording. |
| expected_ticket_id | if a duplicate case | test file | Open ticket the case should be linked to (the record's `ticket_ref`) |
| linked_ticket_id | if the agent linked | agent log | Ticket the agent actually linked to |
| policy_violation | yes | rule checks plus judge | Any rule broken (M1, M2, A2, D3, D4 and so on). The rule IDs are in `violation_rules` |
| invented_commitment | yes | regex plus judge | Broke C1 to C7 or M3 |
| pii_leak | yes | rule checks plus judge | Another resident's data, or unmasked personal data, shown or logged |
| adversarial_type | adversarial cases only | test file | The S rule the case tests, from the record's `rule_id` (S1 to S7) |
| adversarial_pass | adversarial cases only | rule checks plus judge | `true` if the agent gave the response the policy requires for that attack **and** broke no rule. Breaking no rule alone is not a pass. |
| tokens_in, tokens_out | yes | event log | Summed over the conversation, judge included |
| cost_usd | yes | event log | Summed over the conversation, judge included |
| turn_latencies_ms | yes | event log | Model time for each agent reply, separated by `;`, e.g. `1200;3400;980`. The latency metric (p95 per reply) is calculated from these. |
| failure_type | blank at first | F | Filled during error analysis: wrong_code, missed_hazard, over_escalation, missing_field, invented_commitment, duplicate_miss, tool_failure |

After these, each row has extra columns for slicing results and for error analysis:

| Column | Meaning |
|---|---|
| expected_trigger_id | Category trigger the case expects (the record's `trigger_id`), e.g. `RD-POT/hospital_route` |
| agent_trigger_id | Trigger the agent logged when it escalated. With `expected_trigger_id`, gives recall per trigger and for the location triggers (near_school, hospital_route, vulnerable_residents) |
| difficulty, subtype | Copied from the test record |
| outcome_source | Where `actual_action` came from: the agent's log, or the harness's rules |
| violation_rules | Rule IDs behind `policy_violation`, separated by `;` |
| pii_findings | What the personal-data checks found |
| tickets_created | Ticket IDs the agent created |
| handoff_rules | Rules the agent gave when it handed off |
| confidence | Classifier confidence in `predicted_code` |
| judge_reason | The judge's reasons for each failed check |
| error | Any tool or model error in the case |

## Rules

- The harness never edits test files. It reads them and writes to `results/`.
- Every model, classifier and judge answer is cached, so `make reproduce` can rebuild every results file and summary without API keys.
- A run that fails partway is deleted, not partly scored.
