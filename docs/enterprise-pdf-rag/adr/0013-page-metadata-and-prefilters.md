# ADR 0013: Page-level automatic metadata, contextual index text and period / region pre-filters

Status: Accepted, 2026-09-21. Extends [ADR 0011](0011-document-catalog-and-verified-answer-chain.md)
(document catalog / answer chain) and [ADR 0012](0012-chart-index-text-and-retrieval-seats.md)
(index text). The real rebuild and re-measurement are in [`CLAUDE_HANDOFF.md`](../CLAUDE_HANDOFF.md)
and `data/validation/generic-chat-2026-09-21/page-metadata/`.

## Context

Every input is a slide deck printed to PDF: one page is one unit. Ingestion must be fully
automatic — a model may run at build time, nobody annotates anything by hand. Until now a
document had a sha256 and a filename and nothing else: no "which company, which report,
which period, what is this page about". Three consequences were measured on the AIA
first-twenty-pages release:

- Retrieval could not be narrowed by period or market; a question about 1H26 competed with
  FY2024 members on words alone.
- With several documents mounted the caller had to pick one by sha256; `/v1/models` showed
  filenames.
- A page's headline never entered its members' index text. ADR 0012 fixed the chart
  projection, but a text member on a page titled *"Strong 1H 2026 Results"* still embedded
  only its own span.

The `ragspine` sibling already has the shape we want: equality pre-filters that are dropped
again when they starve the ranking (`retrieval/lexical/retrieval.py`), and a *contextual*
index mode that prepends title / entity / period / heading to the text that is embedded
without touching the stored text (`retrieval/contextual.py`). Sensitivity there is an exit
gate, not a filter — and there is no sensitivity dimension in this package.

## Decision

1. **A page metadata stage, one text-only model call per page, every value verbatim.**
   `processing/page_metadata.py` (stdlib) defines `PageMetadata(page_type, language, title,
   section, periods, regions)` where `page_type ∈ {cover, agenda, chart, table, text,
   appendix, other}` and every string value is a `MetadataValue(text, evidence)`;
   `MetadataEvidence(span_ids, text)` names the consecutive page spans the value was copied
   from. `verify_page_metadata` keeps a value only when, whitespace-folded, it is a substring
   of its cited span or of that span joined with at most two following spans in page order
   (a title wrapped over lines); anything else — paraphrase, translation, a joined phrase
   from elsewhere, an unknown span — is dropped and recorded in `diagnostics`. Nothing is
   corrected. `language` and `page_type` are classifications, not quotes.
   `adapters/page_metadata_extraction.py` runs it: the model sees the page's spans
   (`{id, text}`) and returns a strict DTO citing a span per value; the call goes through
   `complete_text_json` with task `page-metadata-v1`, the same budget and cache as layout and
   semantics, and the verified record is an immutable content-addressed asset with its own
   stage-cache fingerprint (producer `page-metadata-v1.2:<client fingerprint>`). No budget →
   the page is `deferred` with a diagnostic; bad model output → `failed`; never skipped.
   `ingest --stage semantics` runs it after the page pipeline, `--stage metadata` runs it
   alone over the source stage, and `enterprise-pdf-rag metadata` adds it to an existing
   draft or published release (a new un-indexed draft; no pointer moves).

2. **Periods normalise deterministically or not at all.** `processing/periods.py` maps
   `1H26` / `1H 2026` / `H1'26` / `2026年上半年` → `1H2026`, `FY24` / `2024财年` → `FY2024`,
   `Q1 2025` / `1Q25` / `2025年第一季度` → `Q1-2025`, a bare year → `Y2026`; a label the rules
   do not know keeps its verbatim text with `normalized = None`. A year-only query matches
   every period of that year; a finer query matches only its exact canonical form.

3. **Document metadata is a deterministic fold, zero model calls.**
   `processing/document_metadata.py`: the cover page (`page_type == cover`, else the first
   selected page) lends its `title` as `display_title`; `report_period` is the normalised
   period printed on the most pages (ties: earliest page; none normalised: the cover's raw
   period); `regions` is the document's own vocabulary in order of first appearance;
   `years` the sorted years of all normalised periods; `language` the page mode. It is
   stored inline on `ProcessingManifest.document_metadata` and **recomputed on every load** —
   a manifest whose inline summary differs from its page stages is refused. `CatalogEntry`
   carries `display_title / report_period / language / years / regions`; `/v1/models` and
   the catalog name a document by `display_title`, falling back to the filename.
   There is no `company` field: the trimmed page schema extracts no entities, so a company
   name is only what the cover title happens to print (the AIA cover prints none).

4. **Index text policy v4: a contextual header above the projection.** `member_text` /
   `build` embed `"<display_title> | <page_title> | <section>\n<ADR 0012 projection>"`, absent
   parts omitted. Only the string both channels score changes; descriptions and quoted
   evidence are byte-identical. `_POLICY` becomes
   `source-transcription-and-scoped-chart-qualification-v4`; the header is applied only for
   `CONTEXTUAL_POLICIES = {v4}`, so a v1–v3 snapshot keeps scoring exactly what it embedded
   and stays mountable. `MemberText` carries `page_title / section / page_type / periods /
   regions` for the filters below.

5. **Only two dimensions filter: period (by year) and region.** `AnswerRequest.filters:
   MemberFilters(periods, regions)`; `None` derives them from the question
   (`answers/query_filters.py`: the same period rules; regions matched case-insensitively and
   verbatim **only against the document's own region vocabulary** — no gazetteer, no
   hardcoded company; a short all-caps region such as `US` must keep its case so the pronoun
   *us* does not match), an explicit empty object disables them. `answers/member_filter.py`
   narrows the candidates before either channel scores: a member matches a period filter
   when one of its page's normalised periods matches (year → any period of that year), a
   region filter by folded equality with one of its page's region strings; a page without
   metadata for a requested dimension is not a hit. Cover and agenda pages never enter the
   candidates (built-in, not user-facing). When fewer candidates than `top_k` remain the
   narrowing is dropped — a filter can only narrow, never cause an abstention — and the
   result reports `filters_applied` and `filters_relaxed` (`AnswerEnvelope`, `rag-chat-v1`
   optional fields). The vector channel is read over the whole corpus when a filter is on and
   cut to `channel_limit` after filtering, so the filter never starves it. `page_type`,
   `title`, `section` and `language` are **not** filter dimensions: they take part as text
   (the header) or as display / routing only. Claim citations carry `page_title`.

6. **Multi-document routing by title words and years.** `/v1/chat/completions` without
   `document` or a model id, with several documents mounted, routes the question by the
   verified cover titles — a distinctive title token (Latin word not in a small generic list,
   or a CJK bigram) that no other mounted title shares — intersected with the years the
   document prints when the question names a year. Exactly one survivor is selected;
   otherwise 422 listing every candidate's display name. An explicit `document` always wins.

## Rejected alternatives

- **Filtering on entities, page type or metrics.** Entities and metrics were dropped from
  the page schema to keep model output small and verifiable; page type is a classification
  the user should not have to reason about. Year and region are the two questions a
  financial-deck reader actually asks with ("1H26", "Thailand"); everything else is text.
- **Accepting a paraphrased or translated title.** A value that is not printed cannot be
  cited; the rule is the same one the answer chain applies to claims (ADR 0011).
- **Storing document metadata as its own model call.** Folding pages is deterministic and
  free; a second model call would add nothing verifiable and could not be recomputed on load.
- **Changing description assets to carry the page header.** As in ADR 0012: descriptions are
  content-addressed evidence; the header is a retrieval-time view.
- **Refusing snapshots whose policy predates v4.** Policy strings stay informational
  (ADR 0011 Decision 7, ADR 0012); `member_text`'s gate is sufficient.
- **A hard filter that may abstain.** A pre-filter is a recall aid; when it leaves nothing
  the right answer is the unfiltered ranking, flagged as relaxed, not "not found".

## Consequences and follow-ups

- **Real run, 2026-09-21 (AIA first twenty pages, `da1065fc…`).** 20 pages → 20 live calls
  (budget 25); every page `succeeded`; 5 dropped values (`Sri Lanka`, `Taiwan (China)`,
  `Macau` not verbatim in the cited span, `same six-month period in the prior year` as a
  period, and — before the span-window rule — the cover title split over two spans).
  Document metadata: `display_title = "INTERIM RESULTS PRESENTATION"` (cover spans
  `INTERIM RESULTS ` + `PRESENTATION`), `report_period = 1H2026`, `years = 2022–2026`,
  34 regions. `qualify` unchanged at 189 / 52 / 9 charts; `index` (Qwen3-Embedding-4B, 2560
  dims, 41 s) → processing `00d5c714…`, snapshot `99f47f48…`, policy v4; `publish` switched
  `current-processing` `da1065fc…` → `00d5c714…`, `current-manifest` unchanged. The page-18
  donut now indexes as `INTERIM RESULTS PRESENTATION | Attractive New Business Profile | EV
  RESULTS\nDistribution Mix 1H26 donut chart figure …`.
- **Re-measured over HTTP** (same directory, `summary.json`): the ISSUE-2 cases `b` / `b2` /
  `n` and the ROE control `a` stay answered with the same citations; the donut is now fused
  rank 1 for `b`. New: `1H26 Distribution Mix` (no "VONB") and the Chinese
  `2026 上半年 分销渠道 占比` are both answered from p.18 (`72% / 28%`) — the Chinese question
  works because the period filter `1H2026` narrows the corpus and the header carries the page
  context. Explicit `filters: {"periods": ["1H26"]}` is applied, not relaxed; `{"regions":
  ["Mars"]}` is relaxed (`filters_relaxed = true`) and yields the unfiltered answer.
  `What was the VONB growth in 2024?` filters to `Y2024` pages (5–8, 20) and answers `+11%`
  from p.6.
- **Known gaps.** (a) ~~Region equality is exact: `Thailand 1H26 VONB` filters to the pages
  tagged `Thailand` (4, 5) and not to p.13, which prints `AIA Thailand`, so the model
  declines; containment would also admit `ex-Thailand`.~~ **Closed by
  [ADR 0018](0018-query-classification-and-translation.md) Amendment 1**: matching is whole-word
  containment — every word of the filter value must appear as a whole word in the page's value,
  so `Thailand` also matches `AIA Thailand` and `Hong Kong` also matches `Hong Kong Special
  Administrative Region` — and the worry above is answered rather than accepted: a value that
  *excludes* a place (`ex-Thailand`, `Asia ex-Japan`) never matches it, because that is the
  opposite claim, not a narrower one. (b) ~~The vocabulary is verbatim and English on this deck:
  `泰国 1H26 VONB` derives no region filter (cross-language gap) and the model declines.~~
  **Closed as a mechanism by the same Amendment**, which runs `derive_filters` over the
  translation too and unions the result with what the question itself derived — but not for this
  sentence's own example: `泰国 1H26 VONB` is never translated at all, because ADR 0018's
  content-word probe finds `VONB` scoreable on its own and therefore reads the question as one the
  lexical channel can score. Its `regions` is still empty; what changed is that it now reaches
  p.13 regardless. (c) A disclaimer page contributes legal-text regions (`United States`,
  `the Philippines`, …) to the vocabulary. (d) Routing needs a distinctive title word; this
  cover prints no company name, so it routes by year only.
- **A new gap, and a worse one: region metadata is page-level, so it cannot tell two objects on
  one page apart (2026-09-21).** p.13 of the AIA deck (`ASEAN: 32% of VONB; Strengthening Growth
  Momentum in 2Q`, section `GROWTH ENGINES`) prints **three `VONB ($m)` bar charts side by side** —
  AIA Thailand **514** (member `a05e27202ea4…`), AIA Singapore **294** (`36f5b652e8e0…`), AIA
  Malaysia **232** (`3e0a86925a4e…`), with each column's bullet text naming its own partner
  (Bangkok Bank / Citibank · IFA & Broker / Public Bank). The three cross-check against the page's
  own headline: 514 + 294 + 232 = 1040 ≈ 32% of group VONB $3.2b. But **every member on that page
  carries the same `regions` tuple**, `('ASEAN', 'AIA Thailand', 'AIA Singapore', 'AIA Malaysia')`,
  so the three charts are indistinguishable to any filter. Whole-word matching now admits
  `Thailand` → `AIA Thailand`, the pre-filter therefore passes *all* of them through, and the model
  is left to guess the column-to-country binding from text that merely co-occurs on the page.
  This is strictly worse than the gap it replaces. The old behaviour was a safe refusal; the new
  one is a **wrong number carried by real provenance** — `294` and `$m` are verbatim observations
  on that page and the bounding box is genuine. Across four real cold runs the two Thailand
  questions were answered eight times and **not once correctly**: `$294m` (Singapore) five times,
  `$232m` (Malaysia) three times, `$514m` (Thailand) **zero** — the binding is effectively drawn at
  random. The gold cases `k01-region-thailand-en` / `k02-region-thailand-zh` therefore keep
  `expected: abstained` with `known_gap: true`, and must not be frozen as `answered` while this
  holds. The chart IR already admits the weaker half of this in its own confidence note (`the
  category-to-value association is unproven`), but nothing in the model expresses *which column
  belongs to which country*. The fix, not implemented here, is a **member-level region binding
  inside the column or card**: the three charts' bounding boxes and the three country headings'
  bounding boxes are fully separable on the x axis, so the binding is derivable rather than
  guessed.
- Snapshots published before 2026-09-21 keep their policy and text until re-indexed;
  `metadata` → `index` → `publish` is the whole migration.
- Offline coverage: `tests/enterprise_pdf_rag/processing/test_periods.py`,
  `test_page_metadata.py`; `adapters/test_page_metadata_extraction.py` (stage, cache
  replay, deferred / failed, CLI, catalog fields, v4 header), `test_chat_metadata_http.py`
  (names, routing, filters, page titles); `answers/test_query_filters.py`,
  `test_member_filter.py`, `test_answer_service.py` (+3), `adapters/test_hybrid_search.py`
  (+1). Whole package: **854 passed**.
