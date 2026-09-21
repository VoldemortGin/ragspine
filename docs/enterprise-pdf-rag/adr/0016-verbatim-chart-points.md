# ADR 0016: A chart point is retrievable when every one of its strings is printed in the figure

Status: Accepted, 2026-09-21. Amends the admission rule of
[ADR 0008](0008-traceable-chart-qa.md) by adding a **second, weaker and separately named**
chart scope beside `explicit-distribution-shares`, and supersedes the label projection of
[ADR 0006](0006-non-chart-visual-semantics.md) that
`figure-source-labels-only-v1` implemented. The geometry + source-paint proof of ADR 0008 and
the displayed-bar proof of [ADR 0009](0009-source-qualified-expense-ratio-bar-lookup.md) are
untouched; this ADR does not widen either of them. The real rebuild and its measurements are in
[`CLAUDE_HANDOFF.md`](../CLAUDE_HANDOFF.md) and
`data/validation/generic-chat-2026-09-21/chart-labels/`.

## Context

`figure-source-labels-only-v1` (`adapters/figure_label_qualification.py`) was the only
qualification policy the first-twenty-pages run could apply to a chart that is not the one
donut ADR 0008 proves geometrically. It projects the model's *description* claims, keeps a
claim only when its text equals **exactly one** cited source observation, rejects any claim
that carries a value or merely contains a digit, and then blanks the chart itself —
`replace(chart, axes=(), points=(), title=None, marks=())`.

A coverage measurement over the pinned AIA release
(`data/validation/coverage-2026-09-21/`, 280 atomic facts, index snapshot `2f35ca97171a`)
showed what that costs:

- **Chart facts reached 2.6 % index coverage — 4 of 156.** Overall strict coverage was 44.6 %.
- 20 of the 29 chart objects were rejected whole, every one of them with the single
  diagnostic `no_exact_source_labels`, taking 126 atomic facts with them. 18 of those 20 had
  no pure-label claim at all: every claim the model produced was a numeric sentence, so
  `numeric_claim_not_a_label` / `numeric_text_not_a_label` fired on all of them.
- The 9 charts that did qualify contributed a further 29 unreachable facts, because their
  points were blanked after the projection.

Comparing all 281 chart IR fields verbatim against the figure's own source spans showed the
text is almost always there: 105 already matched exactly, 79 needed the value and its unit
read as one printed run (`33` + `%` is the single span `33%`), 30 needed a trailing
parenthetical dropped, 18 needed a label that wraps over two lines, 26 needed the unit that is
printed only in an axis title. **Three** fields out of 281 were genuinely absent from the page.

So the rejections were not protecting against fabrication. They were rejecting text the PDF
prints, because the matcher could only compare against one whole span at a time.

## Decision

1. **A new scope, `source-labels-and-verbatim-points-v1`.** `figure-source-labels-only-v1`
   is frozen byte-for-byte so the already-published members keep replaying; every gate that
   named it now accepts both, and `chart_publication.resolve_chart_member` re-derives a member
   with the scope its own receipt declares.

2. **A label may be printed by up to three adjacent source occurrences.**
   `figures/source_label_match.py` is the rule: the window is the cited occurrences in page
   reading order, one to three wide, each a `SOURCE_TEXT_OBSERVATION` with a span id, each
   geometrically adjacent to the next (same printed line, or wrapped onto the following one),
   and its whitespace-folded concatenation must **equal** the label. Case is preserved, the
   narrowest window wins, and anything unresolved, duplicated, non-adjacent or merely similar
   fails closed. It is the chart sibling of the ADR 0013 page-metadata evidence window, and it
   keeps the *source* text, never the model's string.

3. **A number and its unit are one printed run.** A point's value qualifies when a window
   prints `33%`, or `33` and `%` in two adjacent occurrences, or the value alone with the unit
   printed in the figure's own axis title. Thousands separators are kept verbatim.

4. **"Contains a digit" stops meaning "not a label".** The only label gate left is
   `claim.value is not None`, so `1H26`, `+9%` and `VONB ($m)` qualify — they still have to
   print verbatim, so nothing unsourced enters.

5. **Points survive, one at a time.** The projected `ChartIR` keeps a point when its category
   **and** its value (with unit) each print verbatim inside the figure region, and drops the
   point otherwise. A chart with one good point out of four qualifies with that one point:
   fail-closed is per point, never per figure. `title`, `period` and axis labels survive on
   the same terms. `marks` stay empty — they are model geometry hypotheses.

6. **What this scope proves, and what it does not.** Every string a kept point carries is a
   verbatim occurrence printed inside that figure's own region, cited at field level with its
   span id, page and bounding box. The **association** between a category and its value
   remains the model's assertion: this scope does not prove that `33%` is the share belonging
   to `Traditional Protection` rather than to a neighbouring sector. ADR 0008 says a readable
   label or proximity may not promote a number, and that judgement still holds *for its own
   scope* — `explicit-distribution-shares` keeps requiring the native sector geometry and the
   complete source-paint accounting, and keeps its own receipt type. The two scopes are named
   apart, stored apart and never merged, so a reader of a receipt can always tell which of the
   two guarantees a number carries.

## Rejected alternatives

- **Relaxing ADR 0008's own scope instead of adding one.** That would have quietly weakened
  the one chart fact in the release that *is* geometrically proved. A weaker guarantee has to
  be a different, visibly different name.
- **Extending the donut geometry to the page-18 Product Mix chart.** Its percentages sit
  inside the sectors and its category labels sit around the ring with no leader lines, so the
  only available category-to-sector link is angular proximity — exactly what ADR 0008 forbids.
  The chart cannot be geometrically proved; it can only be read verbatim.
- **Fuzzy or case-insensitive label matching.** Every widening here is a deterministic rule
  with a stated bound: at most three adjacent occurrences, one trailing balanced parenthetical,
  the figure's own axis title for a unit. Nothing is repaired, nothing is normalised beyond
  whitespace folding.
- **Widening a chart's bounding box** to recover the 9 fields the layout partition cut off.
  That changes what the model saw, which would invalidate the pinned view. Left as follow-up.

## Consequences and follow-ups

- **Real run, 2026-09-21 (AIA first twenty pages, `231c904c843e` → `22127d0fad13`).**
  `requalify_visual_objects` re-projected 28 of the 29 Chart objects from their own stored
  branches — no model, no network — and left the 29th untouched because it already carries the
  ADR 0008 geometry + source-paint proof. **All 29 charts now qualify**, against 9 before: the
  other 20 had each been rejected whole with the single diagnostic `no_exact_source_labels`, over
  a claim census of `numeric_claim_not_a_label` 61, `numeric_text_not_a_label` 10 and
  `claim_is_not_one_exact_source_occurrence` 2. **53 chart points survive, against 2** — the
  page-18 Product Mix donut keeps 4 (`Traditional Protection` 33 %, `Participating` 53 %,
  `Unit-linked` 10 %, `Others` 4 %), the page-20 Expense Ratio chart 2, the page-8 OPAT chart 12.
  `qualify`: eligible members 190 → **210**, skipped objects 51 → **31**, chart members 9 → **29**,
  `required qualification stages are incomplete` 43 → **23**. `index` (2560 dims,
  `local-http/Qwen/Qwen3-Embedding-4B`) turned draft `8321a7de0c89` into processing
  `22127d0fad13`, snapshot `42939d6a4e87`, 210 members; `publish --no-activate-source` moved
  `current-processing` `231c904c843e…` → `22127d0fad13…` (pointer file sha1 `19ac8170a983…` →
  `bf8ce33af56d…`), `current-manifest` unchanged at `e702bf1c…`.
- **What it bought, measured the same way as the "before".** Strict index coverage over the
  280-fact atomic set (`data/validation/coverage-2026-09-21/`) went **44.6 % (125/280) →
  81.4 % (228/280)**, chart facts **3.7 % (6/161) → 67.7 % (109/161)** (the Context quotes `summary.md`'s own
  2.6 % / 4-of-156 figure; the before/after pair here is one instrument run twice, which buckets
  a fact by its owning object's kind slightly differently), and no fact is left with
  an unindexed owner object (126 → 0). The frozen natural-language gold set, run against the live
  service, is **20 pass / 0 fail**, exit 0: `k01-region-thailand-en` holds as a known gap, and
  `k02-region-thailand-zh` **moved** — it still abstains, but with `abstain_detail: ambiguous`
  instead of `not_in_context`, because the Thai region question now reaches more retrievable
  members. Evidence: `data/validation/generic-chat-2026-09-21/chart-labels/`; the gold set's
  `pinned` block is re-pinned to the new release (member count 190 → 210).
- **A snapshot published under the new scope needs a build that has it.**
  `chart_publication.resolve_chart_member` refuses an unknown `semantic_scope` with
  `Unsupported chart qualification scope`, so a runtime that predates this ADR cannot mount
  `22127d0fad13`. Rolling the release back is writing `231c904c843e…` into
  `data/output/aia-2026-interim/pages-001-020/current-processing` and restarting. Snapshots
  published under `figure-source-labels-only-v1` keep mounting and replaying under either build.
- **Follow-up:** 9 IR fields are unmatchable because the layout partition cut the chart bbox
  too tight; abbreviation labels whose expansion is printed outside the figure crop still fail,
  because `qualify_source_labels` is given only the figure's own observations.
- **Offline coverage:** `tests/enterprise_pdf_rag/figures/test_source_label_match.py` and the
  new cases in `tests/enterprise_pdf_rag/adapters/test_figure_label_qualification.py`.
