# Test-set lock

Locked 2026-11-23 17:01 UTC · Owner: F (W6 Evaluation) · Seed 894

Route codes and outcomes checked against: tests/fixtures/config/policy.yaml, version v1.0.3

These files are frozen. Only the eval harness reads them. Nothing is tuned on them. The data files stay off GitHub; only this file is committed. Check any shared copy against the hashes below (`shasum -a 256 <file>`).

## Locked files

| File | Cases | SHA-256 |
|---|---|---|
| test_heldout.jsonl | 774 | `3a522a9897b3236903a36f32b92bfd1fc8fe9a96cebfea0a1f5cf5c4b6bfb932` |
| test_prod.jsonl | 340 | `9951b65f3856c4a4d26564852bf9a2faedabc13a420b075c80af6864434b0c11` |
| test_adversarial.jsonl | 118 | `e19bdcf2c0626a4086236097708500f8682ab896919d75567b68a086144d851a` |

## Inputs

| File | SHA-256 |
|---|---|
| data/synthetic/train.jsonl | `20113454a970affa4d225426b706ea1ff4c8fe947debdada7dd8a3029a2984e4` |
| data/synthetic/test.jsonl | `44947c4865fda15116960b8ae7abf48ff4a112ba7dfeb21c7e0cce7e552b3251` |
| data/protected/labels.csv | `17b71baec0939bd991a038a0ea79b5d3136307f70352baeb970ccf962721ec9c` |
| data/protected/john.txt | `c1e7304f2bf7e7a158d07070271b1c678a535ea26fbcaaf192430b6f918e2ba2` |
| data/protected/abi.txt | `7e1a05f0ec545b7f539a8871695d333b3c4247fe79515ca3bee08ddf30a1c8fa` |
| data/protected/saif.txt | `b8ba8500784b07276d915cafb26495d469847057bf0b0e59d6fb51e56a1e083b` |
| data/protected/edge_cases.jsonl | `fc13e87ab132a906122c21ac039805d057f173c0cae4d3b90032174825d328e4` |
| data/protected/adversarial_sealed.jsonl | `c5d31acd1f8eac69f416a6a58fa34e1a7e2881cb30ce3cc8627d72ac6c716283` |

## Leakage checks (train vs every test file)

- [x] No shared or repeated IDs
- [x] No scenario template in both train and test
- [x] No identical message in train and test
- [x] No near-identical message (similarity >= 0.9)
- 0 generated test cases were near-identical to a train message and were dropped (listed in near_duplicates_dropped.csv). No hand-written case was near-identical.

## What is in each set

- **Held-out:** 585 generated test cases, 105 hand-written cases and 84 policy edge cases.
- **Production-like:** drawn from the held-out set, 306 routine (create_ticket, no hazard) and 34 other, about 90% routine.
- **Adversarial:** 118 cases from adversarial_sealed.jsonl.
- **Hazard cases in held-out (G1):** 160. If every one is caught, the 95% Wilson lower bound is 97.7%; one miss gives recall 99.4%.

### Trigger coverage in held-out (all cases / generated only)

| Trigger | Must escalate | Look-alike |
|---|---|---|
| SW-BIN/bin_injury | 13 / 9 | 6 / 5 |
| AN-DEAD/private_property | 6 / 3 | 7 / 6 |
| AN-DEAD/near_school | 11 / 9 | 5 / 4 |
| AN-INJ/aggressive | 4 / 3 | 4 / 3 |
| AN-INJ/in_traffic | 5 / 2 | 7 / 6 |
| AN-INJ/dog_or_cat | 7 / 3 | 4 / 3 |
| RD-POT/swerving | 4 / 1 | 4 / 3 |
| RD-POT/vehicle_damage | 7 / 3 | 4 / 3 |
| RD-POT/cyclists_at_risk | 7 / 6 | 4 / 3 |
| RD-POT/deep | 4 / 3 | 4 / 3 |
| RD-POT/hospital_route | 4 / 3 | 4 / 3 |
| TR-TREE/limb_down | 8 / 5 | 6 / 5 |
| TR-TREE/leaning_home | 4 / 2 | 4 / 3 |
| TR-TREE/hospital_route | 4 / 3 | 7 / 6 |
| PS-PROP/tenant_dispute | 5 / 3 | 4 / 3 |
| PS-PROP/no_heat_or_water | 6 / 3 | 4 / 3 |
| PS-PROP/structural_danger | 7 / 5 | 3 / 2 |
| PS-PROP/vulnerable_residents | 3 / 2 | 9 / 8 |
| IL-DUMP/hazardous | 8 / 3 | 4 / 3 |
| IL-DUMP/near_school | 4 / 3 | 3 / 2 |

### Held-out by route code

| Code | Cases |
|---|---|
| AN-DEAD | 93 |
| AN-INJ | 74 |
| EMERG | 39 |
| IL-DUMP | 47 |
| OOS | 67 |
| PS-PROP | 88 |
| RD-POT | 93 |
| SW-BIN | 120 |
| SW-MISS | 81 |
| TR-TREE | 72 |

### Held-out by expected outcome

| Outcome | Cases |
|---|---|
| ask_clarification | 78 |
| create_ticket | 314 |
| escalate_human | 138 |
| handle_each | 25 |
| handoff_language | 98 |
| handoff_oos | 48 |
| link_duplicate | 12 |
| redirect_911 | 39 |
| refuse_and_continue | 11 |
| report_status | 11 |

### Held-out by language

| Language | Cases |
|---|---|
| en | 652 |
| es | 16 |
| fr | 46 |
| franglais | 20 |
| pa | 20 |
| zh | 20 |
