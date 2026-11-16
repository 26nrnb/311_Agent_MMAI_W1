# Results file format

Owner: F (W6) · Interface: "Results file" in docs/interfaces.md · Used by W6, W7 and the report

The eval harness (E) writes one CSV per run to `results/<run_id>.csv`. There is **one row per test case per setup**. `python -m src.eval.score results/*.csv` reads these files and writes `results/summary.json` and `results/summary.md`.

To see a valid example, run `python -m src.eval.score fake --out results/fake_run.csv`.

## Columns

Booleans are written as `true`/`false`. A blank means "not applicable".

| Column                  | Required                | Filled by              | Meaning                                                                                                                                                                                                      |
| ----------------------- | ----------------------- | ---------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| run\_id                 | yes                     | harness                | Same as the file name                                                                                                                                                                                        |
| setup                   | yes                     | harness                | e.g. `fine_tuned`, `prompted_small`, `prompted_large`, or an ablation name such as `rules_off`                                                                                                               |
| test\_set               | yes                     | harness                | `heldout`, `prod` or `adversarial`                                                                                                                                                                           |
| interaction\_id         | yes                     | test file              | From the interaction record                                                                                                                                                                                  |
| expected\_code          | yes                     | test file              | <br />                                                                                                                                                                                                       |
| predicted\_code         | yes                     | agent log              | The code the classifier or agent settled on                                                                                                                                                                  |
| expected\_action        | yes                     | test file              | One of the nine outcomes in operating policy v0.3: create\_ticket, ask\_clarification, link\_duplicate, escalate\_human, redirect\_911, handoff\_oos, handle\_each, refuse\_and\_continue, handoff\_language |
| actual\_action          | yes                     | agent log              | Final outcome the agent reached, same nine names                                                                                                                                                             |
| hazard\_flag            | yes                     | test file              | <br />                                                                                                                                                                                                       |
| lang                    | recommended             | test file              | Language of the message (`en`, `fr`, ...). Blank is read as English. Route accuracy is gated on English messages, and hazard recall is also reported by language.                                            |
| fields\_complete        | if a ticket was created | harness                | Ticket holds every required field in policy.yaml for its code (create\_ticket or handle\_each)                                                                                                               |
| expected\_ticket\_id    | if a duplicate case     | test file              | Open ticket the case should match (A's `ticket_ref` field)                                                                                                                                                   |
| linked\_ticket\_id      | if the agent linked     | agent log              | Ticket the agent linked to                                                                                                                                                                                   |
| policy\_violation       | yes                     | judge plus rule checks | Any rule broken (M1, M2, A2, D3, D4 and so on)                                                                                                                                                               |
| invented\_commitment    | yes                     | judge plus regex       | Broke C1 to C7 or M3                                                                                                                                                                                         |
| pii\_leak               | yes                     | judge plus rule checks | Another resident's data, or unmasked personal data, shown or logged                                                                                                                                          |
| adversarial\_type       | adversarial set only    | test file              | The S rule the case tests, from A's `rule_id`: S1 injection, S2 impersonation, S3 commitment pressure, S4 abuse of the system, S5 indirect injection, S6 out of scope, S7 abuse                              |
| adversarial\_pass       | adversarial set only    | judge plus rule checks | Agent showed the behaviour required by S1 to S7                                                                                                                                                              |
| tokens\_in, tokens\_out | yes                     | event log              | Summed over the conversation                                                                                                                                                                                 |
| cost\_usd               | yes                     | event log              | Summed over the conversation                                                                                                                                                                                 |
| turn\_latencies\_ms     | yes                     | event log              | One value per agent turn, separated by semicolons, e.g. `1200;3400;980`                                                                                                                                      |
| failure\_type           | blank at first          | F                      | Filled during error analysis: wrong\_code, missed\_hazard, over\_escalation, missing\_field, invented\_commitment, duplicate\_miss, tool\_failure                                                            |

## Rules

* The harness never edits test files. It reads them and writes to `results/`.
* Raw model outputs are cached so that `make reproduce` can rebuild every results file and summary without API keys.
* A run that fails partway is deleted, not partly scored.

