---
covers:
  - src/ragspine/eval/
verified-against: cab40fe
---

# eval — agent contract

Auto-loaded when working under `src/ragspine/eval/`. Keep terse; deep dives go in
`src/ragspine/eval/docs/`.

## What lives here

QA + extraction evaluation harnesses with baseline gates. Golden sets live under
`data/golden/` (force-tracked).

## Invariants

- **Baseline gate ratchets up, never down** — a regression must fail the baseline gate,
  not silently lower it; never weaken a golden / baseline to make a case pass. This is now
  **machine-enforced**, not convention: `scripts/ci.sh` runs `run_qa_eval.py --mode tool`
  **and** `--mode agent` (both baseline-gated against `data/golden/qa_baseline.json`), and
  `tests/eval/test_ci_wires_eval_gate.py` pins that wiring so it can't be silently removed.
  Escape hatch for an intentional, reviewed move: `run_qa_eval.py --mode <m> --update-baseline`.
- **Anti-fabrication whitelist is profile-sourced** — `detect_fabricated_numbers` strips
  only the active profile's temporal-dim `fabrication_whitelist_regex` (read from
  `qa_eval._PROFILE` **at call time**) and strips **nothing** when no such dim exists, so a
  non-temporal domain flags every digit. The period regex is an explicit verbatim literal
  (byte-pinned against `_PERIOD_TOKEN_RE`) — never derived from synonyms / grain, or the
  `(?:19\|20)` year anchor could vanish and whitelist any 4-digit number.

## Read before editing

- **`qa_eval` is in the `_PROFILE` bound-modules contract** —
  `tests/common/test_company_generalization.py` `_PROFILE_BOUND_MODULES` rebinds
  `qa_eval._PROFILE` alongside intent / query_tools / agent. Keep it bound so the
  fabrication whitelist flips with the active profile (ADR 0004 step 11).

## Deep dives

<!-- none yet -->
