---
covers:
  - src/ragspine/eval/
verified-against: f67fbf2e2c2bae6bfdbb65f915f0705d227d7d82
---

# eval — agent contract

Auto-loaded when working under `src/ragspine/eval/`. Keep terse; deep dives go in
`src/ragspine/eval/docs/`.

## What lives here

QA + extraction evaluation harnesses with baseline gates. Golden sets live under
`data/golden/` (force-tracked). `groundedness.py` (W5) holds the narrative-side
groundedness metrics (faithfulness + free-text answer-accuracy); `qa_eval.py` wires them in
as two new ratcheted gates alongside the four命门.
`nl_gold_ragspine.py` runs an nl-answers-gold file (e.g. the AIA sample) on the ragspine main chain
(`answer_question`) along two routes — `A-ask` (rule intent parser, routed as-is) and `B-narrative`
(`ForcedNarrativeIntentParser` pins the route) — and writes a report; **not a CI gate** (real-model
baseline via `scripts/run_nl_gold_ragspine.py` / `make eval-nl-gold`, reports under
`data/validation/ragspine-nl-gold/`, git-ignored). With `--embedding local-http` the script ingests through the
formal persisted-vector path (`storage.persist_vectors`, embed at ingest) and reads the index back via
`service.config.open_vector_channel` — no eval-side vector backfill. `--page-parent off|dedup|page+child` (default `page+child`) switches the
retriever's page-level parent/child mode; `recall_at_k` reports both `recall` (k = retrieved chunks) and
`page_recall` (k = distinct pages). `--source-pdf` links the original PDF at ingest (else the md's `<stem>.meta.json`
sidecar) and `--page-images on --page-images-top-n N` adds page images to the top-N pages (image+text context);
`CountingProvider` forwards `supports_image_input` so the wrapped provider still receives image parts.
`route_label` reports `fallback` when `AgentResult.fallback` is set (structured miss answered by the narrative
fallback, ADR 0023), and `not_found` for an `ask_first` whose answer is a refusal (missing metric, fallback ungrounded).
**Main scope is the full document**: `--pages all` is the default (`--pages gold` stays as the optional pinned-page
scope), and the default gold is `nl-answers-gold-v2.json`. `--repeat N` (default 1) runs every case N times;
`CaseRun.repeat` tags the run and `repeat_stats` reports per-case pass rate, main-rate mean ± std (sample std) and
the unstable cases — no majority vote; route distribution counts every run. `--rejudge <report dir>` re-scores the
answers recorded in an old `report.json` with the current judge + `--gold` (no ingest, no model call).

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
- **nl-gold is ragspine-side end to end** — `load_nl_gold` is a light reader of the
  `nl-answers-gold-v1` / `-v2` shape (ragspine must not import `enterprise_pdf_rag`, ADR 0022 conformance gate),
  and pass/fail is rewritten here: content (normalized quote/value) and page
  (`@page={page_index+1}#` in a source locator) are counted **separately**, and one `any_of` anchor must
  satisfy both. `--pages all` (default) ingests the whole document; `--pages gold` ingests only the gold's
  `pinned.selected_physical_pages` (`select_di_pages` blanks the rest, page numbers kept).
  Known-gap is run but unscored; adversarial / `offline_only` cases are skipped with a
  reason. Answers go only into report artifacts — never into observability traces; `RecordingRetriever` /
  `CountingProvider` observe locators and call counts only.
- **Gold v1 is frozen; v2 carries its evidence** — `nl-answers-gold-v1.json` is never edited. `-v2` requires a
  non-empty top-level `changelog` (each entry: `case_id`, `change`, `old`, `new`, `evidence`; the loader rejects
  it otherwise) and is registered in the benchmark `manifest.json`. Fix a gold with a new version, not an edit.
- **Judge changes are versioned** — `JUDGE_VERSION` (now `nl-gold-judge-v2`) is written into every report's
  `meta` by `write_report`, next to the script's `gold_version` / `gold_sha256`; scores of different judge or gold
  versions are not directly comparable (use `--rejudge` to compare on the same answers). Judge v2 rules:
  refusal = orchestrator template anywhere (line-start `查不到` / `无法识别参数`, no-material text) **or** a refusal
  phrase in the answer's lead (headings + first sentence, parentheticals dropped), unless the lead is a hedged
  answer (`most likely` / `很可能`…); scaled currency amounts (`5.14 亿美元`, `US$1.168b`) get their exact
  millions appended in `normalize_answer` (only when no zero-padding is needed; `%` untouched) — the
  `normalize_answer` / `contains_normalized` signatures stay fixed (imported elsewhere); the cross-lingual
  fragment rule (≥3 key tokens of a quote inside a 3×-quote-length window) is a **relaxation**, flagged per claim
  (`fragment_hit`) and counted separately in the report.

## Read before editing

- **`qa_eval` is in the `_PROFILE` bound-modules contract** —
  `tests/common/test_company_generalization.py` `_PROFILE_BOUND_MODULES` rebinds
  `qa_eval._PROFILE` alongside intent / query_tools / agent. Keep it bound so the
  fabrication whitelist flips with the active profile (ADR 0004 step 11).

## Deep dives

<!-- none yet -->
