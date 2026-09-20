# ADR 0009 — Admit source-displayed Expense Ratio bar labels without deriving values

Status: Accepted for the limited displayed-value bar lookup slice, 2026-09-19.

## Context

The active ChartQA v1 slice answers two explicitly labelled values from the page-18 donut. That contract and its immutable pins remain valid. It does not cover bars whose horizontal labels carry the time role, and extending its donut assumptions would blur a material source distinction.

The next candidate is the Expense Ratio chart on physical page 20, source page index 19, object `layout-object-v1:50365bbe30728f288c8199778fbd66c13f877c6ec61ddeae612d4188c81b3388`, in region `[354, 324, 612, 474]` in top-left PDF points. Independent inspection of the stored source sidecar, native crop, structured SVG and deterministic render establishes only these displayed facts:

- category label `1H24` has an explicit source label `8.2%`;
- category label `1H26` has an explicit source label `6.9%`;
- category label `1H25` has no displayed numeric value;
- the chart also displays `(130) bps` and an arrow, but their relationship is not qualified by this slice.

The raw ChartIR correctly leaves its global period empty and stores `1H24`, `1H25` and `1H26` as point categories. The raw description contains a correct `8.2%` claim, but repeats the same title evidence ID inside that claim. The existing mapper rejects the duplicate and preserves the raw response and diagnostic, so the current typed description omits that claim. The object is not one of the 189 members in the current retrieval index.

The page footnote states that Expense Ratio comparatives and two-year changes are shown on an actual exchange rate basis. It is page context, outside the chart crop. It must remain visible with the answer, but a displayed-value lookup does not establish economic comparability, interpret a half-year date range, or authorize the arrow as a calculation.

The source contains visible strokes: bar outlines, a baseline, an axis break, the arrow and the surrounding panel. pdfspine 0.10 Replay/native SVG currently exposes path, close state, colour, alpha, width, transform and dashes but does not expose line cap, line join or miter limit. Treating SVG defaults as the PDF source state would be an unsupported fidelity claim.

## Decision

Keep ChartQA v1, the donut policy and all existing v1 snapshots unchanged. Add an explicit `chart-qa-v2` request and response branch on the same source-backed query endpoint. Version selection is mandatory; a v2 request cannot be interpreted by the v1 service or silently fall back to it.

ChartQA v2 supports `lookup` only. It retains the pinned processing, snapshot and member IDs and accepts one point selector plus explicit series, requested period and unit. For this policy, `series` must be `Expense Ratio`, `unit` must be `%`, and the requested period and point category must both equal the exact displayed label `1H24` or `1H26`. A difference operation, a ratio, an arrow or basis-points query, and any request to derive a value from bar height are unsupported.

Do not write a synthetic value into `ChartIR.period`. Instead, create an immutable `PointPeriodInterpretation` that binds:

- the raw ChartIR artifact and point ID;
- the literal raw field path, such as `points.point-1h24.category`;
- the exact category occurrence, text range and source anchor;
- a versioned rule stating that this point category supplies the period role only for the selected point lookup;
- verified source occurrence status and unknown numerical confidence.

The category and requested-period roles may cite the same source occurrence. The response must say that one occurrence has two declared roles; it must not fabricate a second occurrence or imply that the chart has a global period. A missing, ambiguous or mismatched interpretation causes refusal.

A successful response declares semantic scope `source_display_only`, returns the exact explicit decimal and source display string, carries no calculation receipt, and includes field-level citations plus the period interpretation. It also cites the exact page-context footnote separately from crop evidence. The footnote remains contextual disclosure and does not turn the lookup into a comparable-rate analysis.

`1H25` remains a known category with an unavailable value and must produce a business refusal. The service must not estimate it from height, neighboring bars or the arrow. The displayed `(130) bps` is not a qualified point or operation. Cross-period subtraction, including the arithmetically tempting `8.2 - 6.9`, is out of scope even if it numerically resembles the displayed annotation.

### Source qualification

Create a separate bar source-proof v3 policy. Do not upgrade or reinterpret the donut v2 proof. Bar geometry and complete source-paint visibility are separate requirements, and both must pass before promotion.

The proof binds the original PDF digest, page, reviewed region, same native and cropped SVGs, structured observations, raw ChartIR and description, and every approved text and vector occurrence. It accepts only finite positive-width solid strokes under a source transform that is a proven similarity transform; the initial chart uses an identity transform. Source and native path, transform, width, colour, alpha and rectangular clips must match exactly.

Because cap, join and miter state are unavailable, record `bounded_unknown_style` instead of filling default values. Every parser state needed to decide the bound must either come from a source-backed port or remain explicit and fail closed; a cap/join envelope alone cannot stand in for otherwise missing graphics state. Visibility is proven with a conservative geometric envelope:

- an open segment uses its half-width line tube and an endpoint radius of `sqrt(2) * half_width`, covering butt, round and square caps;
- a nondegenerate join with interior angle `theta` uses the worst-case miter extent `half_width / sin(theta / 2)`, which also covers round and bevel joins;
- reverse, zero-tangent, nonfinite or otherwise unbounded geometry is rejected;
- a curve used only to prove separation is bounded by its control-point convex hull, the stroke envelope and join/endpoint envelopes; if that bound touches the region of interest, the proof rejects it.

This conservative proof does not repair the native SVG or claim complete stroke-style fidelity. Unknown style remains explicit. A whole surrounding frame cannot be treated as a filled bbox merely because its bbox covers the chart; every segment must be evaluated. An envelope causes rejection when it intersects a protected label or a visibility/association corridor that the qualification depends on; a stroke touching its own bar boundary is not, by itself, a failure. Any unsupported path/style, unknown source state needed by the bound, changed clip, hidden or invisible text, glyph mismatch, later occlusion, altered stroke, or changed source/native digest fails closed. Colour is never an exclusion rule.

The finite grammar may establish only exact label-to-point association and visibility. Values still come from explicit text occurrences, never from bar height, colour, axis interpolation or arrow geometry.

### Description normalization and indexing

Preserve the raw description, existing typed artifact, hashes and diagnostics. A new versioned normalization may remove repeated occurrences of the exact same evidence ID only within one raw claim, preserving first-occurrence order. It must first prove that the evidence pool contains one unambiguous occurrence for that ID and reject empty, unknown, conflicting or differently sourced occurrences. The normalization receipt binds the raw response and new typed output. Text, value, unit, period, category, confidence and every nonduplicate evidence ID remain byte-for-byte semantically unchanged. Normalization does not grant source qualification.

After source qualification, project only the explicit `1H24` and `1H26` claims. Exclude the unavailable `1H25` claim and the `(130) bps` arrow claim from embedding and numerical QA. Since this chart is not in the current 189-member index, promotion adds a 190th member and requires one description embedding with the existing model fingerprint and dimension. Preserve all 189 existing vectors. This one embedding is allowed only after the offline implementation gate passes; it does not authorize another LLM call or reranking call.

## Validation

Implementation begins with tests. Source-proof tests must cover the exact page-20 source plus authored counterexamples for cap/join uncertainty, acute and degenerate joins, curve bounds, outline/baseline/break/arrow strokes, clips, alpha, hidden glyphs, later occlusion and digest changes. Unknown or unsupported geometry must reject rather than approximate.

The source-backed gold is independent of model output and the API. It is transcribed from the stored source sidecar and structured SVG with the exact JSON floating-point coordinates. It contains two positive lookups and hard negatives for the missing `1H25` value, swapped or neighboring labels, comparator/approximate displays, cross-period difference, the arrow, height-derived values, wrong pins and mutated glyph/stroke/clip/occlusion closures. Existing ChartQA v1 gold and its reports remain unchanged and must continue to pass.

The v2 evaluator requires 100% answer precision, positive coverage, citation exactness, point-period interpretation exactness and page-context citation exactness, with zero hard-negative escapes. An all-refusal implementation cannot pass. A passing v2 slice establishes only displayed-value lookup for the two approved labels; it does not qualify bar-height inference, period-over-period calculation, exchange-rate comparability, the other page-20 charts or the broader P5 milestone.
