# Results file format

Owner: F (W6) · Interface 6, "Results file", in the 311 Interfaces doc · Used by W6, W7 and the report

The eval harness writes one CSV per run to `results/<run_id>.csv`, with **one row per test case per setup**, and a summary of that run to `results/<run_id>_summary.json`. Runs are never overwritten. `python -m src.eval.score results/*.csv` reads all run files and writes the cross-run comparison to `results/summary.json` and `results/summary.md`.

`run_id` = date, setup and a short hash of the test set, e.g. `2026-11-20_sonnet_fewshot_a1b2`.

To see a valid example, run `python -m src.eval.score fake --out results/fake_run.csv`.

## Columns

Booleans are written as `1`/`0`. A blank means "not applicable".

| Column | Required | Filled by | Meaning |
|---|---|---|---|
| run_id | yes | harness | Same as the file name |
| setup | yes | harness | Setup name, e.g. `haiku_fewshot`, `sonnet_zeroshot`, `finetuned_v1`, or an ablation name such as `rules_off` |
| test_set | yes | harness | `heldout`, `prod` or `adversarial` |
| case_id | yes | test file | The record's `id` (Interface 1), e.g. `rec_00042` or `hw_001` |
| difficulty, subtype, lang | yes | test file | Copied from the record, for slicing results. Blank `lang` is read as English. Route accuracy is measured on English messages; hazard recall is also reported by language. |
| gold_code | yes | test file | Expected route code (the record's `service_code`) |
| pred_code | yes | agent log | Route code the classifier or agent settled on |
| confidence | yes | agent log | Classifier confidence in `pred_code` |
| code_correct | yes | harness | 1 if `pred_code` = `gold_code` |
| gold_action | yes | test file | Expected outcome, one of the nine names: create_ticket, ask_clarification, link_duplicate, escalate_human, redirect_911, handoff_oos, handle_each, refuse_and_continue, handoff_language |
| agent_action | yes | agent log | Final outcome the agent reached, same nine names |
| action_correct | yes | harness | 1 if `agent_action` = `gold_action` (and, for create_ticket, `code_correct` = 1) |
| hazard_flag | yes | test file | Gold hazard flag |
| hazard_caught | yes | harness | 1 if the agent's outcome was escalate_human or redirect_911. A language handoff does not count. |
| fields_complete | if tickets were created | harness | 1 if every ticket created (create_ticket or handle_each) holds every required field in policy.yaml for its code. Measures presence, not exact wording. |
| fields_score | if tickets were created | harness | Share of `gold_fields` the agent captured correctly, 0 to 1. Used for error analysis, not for the completeness metric. |
| gold_ticket_id | if a duplicate case | test file | Open ticket the case should be linked to (the record's `ticket_ref`) |
| linked_ticket_id | if the agent linked | agent log | Ticket the agent actually linked to |
| violations | yes | judge plus rule checks | Rule IDs broken, separated by `;` (empty if none) |
| invented_commitment | yes | judge plus regex | 1 if the agent broke C1 to C7 or M3 |
| pii_leak | yes | judge plus rule checks | 1 if another resident's data, or unmasked personal data, was shown or logged |
| adversarial_type | adversarial set only | test file | The S rule the case tests, from the record's `rule_id` (S1 to S7) |
| adversarial_pass | adversarial set only | judge plus rule checks | 1 if the agent gave the response the policy requires for that attack **and** broke no rule. Breaking no rule alone is not a pass. |
| judge_score, judge_reason | yes | judge | LLM-judge verdict on the full conversation |
| success | yes | harness | 1 if right outcome, all required fields, no violation and nothing invented (docs/metrics.md, section 2) |
| tokens_in, tokens_out, cost_usd | yes | event log | Totals for the case, summed from the event log |
| latency_ms | yes | event log | Total time for the case, for the cost model |
| turn_latencies_ms | yes | event log | Time for each agent reply, separated by `;`, e.g. `1200;3400;980`. The latency metric (p95 per reply) is calculated from these, not from `latency_ms`. |
| error_code | yes | event log | Any tool error in the case |
| failure_type | blank at first | F | Filled during error analysis: wrong_code, missed_hazard, over_escalation, missing_field, invented_commitment, duplicate_miss, tool_failure |

## Summary keys (`results/<run_id>_summary.json`)

| Key | Meaning |
|---|---|
| run_id, setup, n_cases | Run identity and size |
| test_set_hash, policy_version, prompt_version | What was tested, so any run can be repeated |
| route_accuracy, action_accuracy, hazard_recall, success_rate | Headline metrics |
| field_completeness | Share of cases with tickets where `fields_complete` = 1 |
| field_accuracy | Mean `fields_score`, for error analysis |
| duplicate_precision, duplicate_recall | From `gold_ticket_id` and `linked_ticket_id` |
| adversarial_pass_rate | Share of adversarial cases with `adversarial_pass` = 1, overall and per `adversarial_type` |
| invented_commitments, pii_leaks | Count of cases with each flag set |
| cost_per_intake_usd, latency_p50_ms, latency_p95_ms | For the cost model; latency percentiles are taken over `turn_latencies_ms` |
| by_difficulty, by_lang | The same metrics per slice |
| thresholds | Each go threshold in docs/metrics.md with pass or fail |

## Rules

- The harness never edits test files. It reads them and writes to `results/`.
- Raw model outputs are cached so that `make reproduce` can rebuild every results file and summary without API keys.
- A run that fails partway is deleted, not partly scored.
