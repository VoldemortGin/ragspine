---
covers:
  - src/ragspine/eval/
verified-against: 11bf4362ea0e8a230f6a9749f16ba0ed5a1662d5
---

# eval — agent contract

Auto-loaded when working under `src/ragspine/eval/`. Keep terse; deep dives go in
`src/ragspine/eval/docs/`.

## What lives here

QA + extraction evaluation harnesses with baseline gates. Golden sets live under
`data/golden/` (force-tracked). `groundedness.py` (W5) holds the narrative-side
groundedness metrics (faithfulness + free-text answer-accuracy); `qa_eval.py` wires them in
as two new ratcheted gates alongside the four命门.

## Invariants

- **Baseline gate ratchets up, never down** — a regression must fail the baseline gate,
  not silently lower it; never weaken a golden / baseline to make a case pass. This is now
  **machine-enforced**, not convention: `scripts/ci.sh` runs `run_qa_eval.py --mode tool`
  **and** `--mode agent` (both baseline-gated against `data/golden/qa_baseline.json`), and
  `tests/eval/test_ci_wires_eval_gate.py` pins that wiring so it can't be silently removed.
  Escape hatch for an intentional, reviewed move: `run_qa_eval.py --mode <m> --update-baseline`.
- **W5 groundedness is additive, never a 4-gate rewrite** — `GATE_METRICS` (numeric / citation /
  refusal / clarification) keep their exact semantics; `GROUNDEDNESS_METRICS`
  (`faithfulness`, `answer_accuracy`) are **new** keys in the same `report.metrics` dict, so they
  fold into the **same** baseline ratchet automatically (`compare_to_baseline` gates every metric
  the baseline lists; `make_baseline_entry` serializes all of `report.metrics`). `ALL_GATE_METRICS`
  = the union. Don't merge groundedness into a 4-gate metric or drop it out of `report.metrics`.
- **Faithfulness measures the narrative answer vs the retrieved context, eval-side only** —
  `CaseOutcome.narrative_answer` / `retrieved_context` are populated by the **runners** (tool-direct
  inline; agent mode by **re-running the retriever** with the same query/filters as `_run_narrative`).
  This is pure bypass observation: `answer_question`'s default answer synthesis is **unchanged**.
  Don't make the agent expose context by mutating the answer path.
- **Default groundedness method is the offline deterministic lexical-overlap entailment** —
  `groundedness.LexicalOverlapJudge`: a claim is entailed iff its content-token coverage by the
  context ≥ `FAITHFULNESS_COVERAGE_THRESHOLD`. It is a **lexical proxy, not a real NLI** (honest
  limitation: blind to paraphrase / negation / numeric reversal). It runs with **no model, no
  network** so `make ci` gates it offline. The real ONNX-NLI judge (`[eval]`) and the LLM-judge
  (`[llm]`) are **opt-in adapters behind the `EntailmentJudge` seam** — follow-ups (see PRD W5),
  default stays `make_entailment_judge("lexical")`.
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
