# Traceable ChartQA stage

This stage narrows ChartQA to one independently reviewed source region while the general figure milestone remains incomplete. It does not change the approved PRD or mark P1–P5 complete.

## Current status

- **P1 — minimal evidence chain: partial.** The selected PDF has source-bound pages, SVGs, text observations and object artifacts. A representative cross-type gold set, frozen SLOs and the original no-real-report-to-external-model gate are not complete.
- **P2 — tables and layout: partial.** Typed table invariants exist, but the page-20 visual matrix has no verified native cell grid and complex-header/backend acceptance is incomplete.
- **P3 — reliable ingestion: partial.** Content addressing, immutable attempts, bounded calls and atomic local publication exist. Distributed leases, fencing, cancellation and stale-worker recovery are outside the current one-machine implementation.
- **P4 — publication, retrieval and TableQA: partial.** A pinned snapshot can embed descriptions, rerank and hydrate the same member closure. Three retrieval modes, TableQA business acceptance, permissions and multi-tenant isolation are incomplete.
- **P5 — figure branches and ChartQA: partial.** All 29 charts on physical pages 1–20 have raw typed and description branches. The active immutable snapshot retains eight label-only chart members and replaces the page-18 donut member with a source-paint-backed numeric qualification; the earlier nine-member label-only snapshot remains readable and refuses numerical answers. General visual completeness, field accuracy, qualified ranking and broad ChartQA acceptance have not passed.

The first source-backed numeric query slice is active for a single donut grammar. Its qualification is based on the independent pdfspine glyph/paint proof and immutable member closure; a model's ChartIR, description or confidence does not supply that proof.

## Frozen gold scope

[`chart-qa-gold-v1.json`](../../data/benchmarks/enterprise-pdf-rag/aia-2026-interim/chart-qa-gold-v1.json) binds the AIA source digest and physical page 18 region `[18, 155, 250, 338]` in top-left PDF points. The assistant independently reviewed the native SVG crop, source text sidecar and deterministic render. This benchmark review is not a production qualification receipt.

The reviewed explicit observations are:

| Metric | Period | Category | Display | Canonical value | Unit |
| --- | --- | --- | --- | ---: | --- |
| VONB | 1H26 | Agency | 72% | 72 | % |
| VONB | 1H26 | Partnerships | 28% | 28 | % |

The positive queries are both lookups and both ordered percentage-point differences: `72 − 28 = 44 percentage_points` and `28 − 72 = -44 percentage_points`. The gold stores the exact source span ID, SVG observation ID, Unicode range and source bbox for series, period, category, number and percent sign separately. The arithmetic sum to 100 is only a consistency check; it did not generate either source value.

Hard negatives cover swapped category/point identity, wrong series, wrong period, wrong unit, wrong member or snapshot, missing branch, estimated or unknown values, a changed numeric artifact and a wrong source. The public request never accepts a caller-supplied value or value kind. Corrupt source closures therefore fail at the source-aware resolver with `409 invalid_evidence`, and a missing immutable branch fails with `503 unavailable_evidence`; the evaluator does not bypass that resolver merely to obtain a `200` refusal. A same-snapshot bar that has only label qualification returns `200 unqualified_member`. Pure-domain tests separately cover `unsupported_grammar` and `unsupported_value_kind`. The ratio operation is rejected at request validation. A test mutation must first be checked against the gold so it is not accidentally another true fact.

## Evaluation contract

`enterprise_pdf_rag.adapters.chart_qa_evaluation.evaluate_chart_qa` reads two byte payloads and performs no I/O:

1. the frozen gold JSON;
2. a `chart-qa-observations-v1` JSON object with one captured result per `case_id`.

For a persisted run, invoke the same adapter with explicit paths; it never searches a source checkout or imports a benchmark by default:

```bash
python -m enterprise_pdf_rag.adapters.chart_qa_evaluation \
  --gold data/benchmarks/enterprise-pdf-rag/aia-2026-interim/chart-qa-gold-v1.json \
  --observations /path/to/captured-observations.json \
  --output-root /path/to/processing-run
```

The command writes `chart-qa-evaluations/<report_sha256>/gold.json`, `observations.json` and `report.json`. Reusing the same report is idempotent; different bytes at an existing content-addressed path are rejected.

Each captured result records the HTTP status and either the exact `chart-qa-v1` response body or a bounded transport error class. An answered response must be `verified` and carry unknown numerical confidence (`score=null`) with the expected deterministic source or calculation method; a model score cannot substitute for either. It must also preserve the ordered input claims. Each input carries separate citations for series, category, period, unit and value. A citation includes the ChartIR, SVG and qualification artifact identities plus the exact source occurrence and source anchor. Difference answers also require the ordered input field references and values in the calculation receipt; comparing only the final number is insufficient.

The deterministic report records the SHA-256 of both inputs, per-case diagnostics and these metrics:

- answer precision;
- positive answer coverage;
- overall refusal rate;
- unanswerable refusal recall;
- answerable over-refusal rate;
- citation exactness and citation coverage;
- hard-negative escape rate;
- transport-error count.

For this narrow scope, answer precision, positive coverage, citation exactness and citation coverage must each equal 1, while hard-negative escape rate must equal 0. Missing thresholds, no positive cases, no correct positive answer, a missing case or an extra case fails the report. All-refusal output therefore cannot pass. Unsupported-scope refusal remains visible and does not establish coverage for the other 28 charts.

The evaluator's field citations compare the response's exact source span, Unicode slice and text-span bbox with the gold. Calculation source references separately bind the PDF revision, physical page and reviewed figure region.

Unit tests build synthetic captured responses to test the evaluator itself. They are not production acceptance. A real pass must capture the source-backed HTTP response from the newly published numerical snapshot, retain its processing/snapshot/member pins and then evaluate those bytes without rewriting them from the gold.

`enterprise_pdf_rag.adapters.chart_qa_capture` accepts the gold, an explicit `chart-qa-capture-targets-v1` mapping and an output path. Every target supplies a loopback HTTP endpoint plus processing, snapshot and member IDs. The runner rejects remote endpoints and target omissions, posts only the public request fields, and records the returned body or bounded error class. Fault targets must be served from independent copied stores; creating a fault by truncating or overwriting a hard link would mutate the original immutable release and is forbidden. The original source, raw artifacts and current pointer digests must be checked before and after fault capture.

## Captured pre-activation result

The source-backed capture used processing ID `a7384f0c2654d4a2d195e6f119e6e441ef08319af35a7655d8b7a9a099caa8d5`, snapshot ID `f59d230869d5dac6981f0545286b4772a1a9770e63597350f1a19b1a14349703` and member ID `3ded2dd7e682c29dbe874ebb5253db08b426a222d3f2d2be36fd2dfd5f24efc4`. It was captured while the processing snapshot was still a draft and was activated only after the evaluation and complete offline gate passed. Temporary loopback services exposed the public `/v1/queries` route. Fault cases ran against copy-on-write stores with atomic replacement after startup. The original source PDF, current source manifest and current processing pointer retained their before-capture SHA-256 digests, and every temporary port was closed after capture.

The passing bundle is stored under the ignored runtime tree at:

```text
data/output/aia-2026-interim/pages-001-020/runs/
  a7384f0c2654d4a2d195e6f119e6e441ef08319af35a7655d8b7a9a099caa8d5/
  chart-qa-evaluations/
  02e9181bc4dd47d77cb5bbeeda627561ed9af1331e731d874fbe5aeb794c3bc2/
```

All 19 captured cases passed: four positive answers, seven business or unsupported-scope refusals, and eight expected pin, evidence, dependency or request faults. Answer precision, positive answer coverage, citation exactness, citation coverage and unanswerable refusal recall are each `1`; hard-negative escape and answerable over-refusal are `0`. Transport errors remain separately visible and are not counted as successful business refusals. The bundle reader rechecked the report directory name and the SHA-256 bindings for the gold and observations.

The first evaluation remains preserved as a failed report. It exposed four manually shortened IEEE-754 bbox tails in the independent gold. The authoritative source sidecar records `62.09472000000002`, `95.13983999999999` and `249.85048000000003`; the API returned those exact values. The corrected gold retains the source JSON values without adding a coordinate tolerance, so source span, text range, page, coordinate frame, transform and exact bbox matching remain strict.

This result qualifies only the two explicit page-18 percentage facts and their ordered percentage-point differences. Activation does not qualify the other 28 charts or establish the full P5 milestone.

## Acceptance sequence

1. Preserve the existing raw ChartIR and description artifacts unchanged.
2. Produce the versioned pdfspine ReplayDevice glyph trace and complete paint-accounting proof.
3. Rebuild the numerical chart and description projections from the pinned source and admit only explicitly qualified percentage fields.
4. Publish a new immutable retrieval snapshot; never mutate the label-only snapshot.
5. Run the frozen positive, hard-negative and unsupported cases through the source-backed API and save the captured observation file outside the repository's committed source tree.
6. Run the offline evaluator and retain its content-addressed report with the processing run.
7. Run the complete offline project quality gate after all production and evaluation code is frozen.

A pass means only that this reviewed donut scope answers and refuses correctly with exact citations. It does not qualify general ChartQA, ranking quality, TableQA, multi-tenant access or the full P5 milestone.
