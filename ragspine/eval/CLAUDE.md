---
covers:
  - ragspine/eval/
verified-against: f37cfd3
---

# eval — agent contract

Auto-loaded when working under `ragspine/eval/`. Keep terse; deep dives go in
`ragspine/eval/docs/`.

## What lives here

QA + extraction evaluation harnesses with baseline gates. Golden sets live under
`data/golden/` (force-tracked).

## Invariants

- **Baseline gate ratchets up, never down** — a regression must fail the baseline gate,
  not silently lower it; never weaken a golden / baseline to make a case pass.
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
