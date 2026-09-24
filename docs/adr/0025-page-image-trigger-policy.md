---
status: accepted
date: 2026-09-24
---

# ADR 0025 — Page image trigger policy (attach page images on demand)

> Immutable record. Exempt from drift tracking (no `covers`). Supersede, don't edit.
> One exception is declared up front: the **"Evaluation results"** section is a placeholder that is
> filled in exactly once, when the pre-registered evaluation below has run. That fill-in may not change
> anything else in this ADR; a different decision needs a new ADR.

Extends the page-image exit (`retrieval/page_images/`, [invariants](../invariants.md) "Page images are a
new exit, screened at the door"). Interacts with [0024](0024-narrative-number-guard.md): a number that
appears only in a page image is still rewritten as ungrounded. Ported from SuperIndex
`superindex/page_images.py` (design note 02).

## Context

`RAGSPINE_PAGE_IMAGES=on` attached an image to each of the top `top_n` (3) retrieved pages, by rank
alone, whatever the page held. On the nl-gold gold pages with `page+child`, turning it on made answers
worse and slower: route A 12 → 10 / 22, route B 15 → 14 / 22, total time 441 → 634 s (+44 %); on the
full document A 12 → 11, B 16 → 14, 528 → 675 s (+28 %). Only one image question flipped to correct.
When the text is good, whole-page images every time cost more than they return.

That text was exported from a PDF with a text layer, so chart numbers were already in the markdown.
A scanned or outlined deck goes through OCR, which loses tables and figures; that is where an image
should help. Any decision has to be measured under both text conditions.

## Decision

### 1. Modes

`RAGSPINE_PAGE_IMAGES` / `ServiceConfig.page_images` / `RetrievalPreset.page_images`:

- `off` (**default, unchanged**): the base retriever is returned as is; prompt byte-identical.
- `all`: the old behavior, image on each of the top `top_n` pages. **`on` stays accepted as an alias
  of `all`.** With no `page_images_max`, the factory returns the plain `PageImageRetriever`, so `on` and
  `all` are byte-identical to the pre-ADR `on` (frozen digest in
  `tests/retrieval/page_images/trigger/test_trigger.py`).
- `tagged`: of the top `top_n` pages, only those whose page tags hit the trigger get an image.

New settings (service, facade preset, `ragspine batch`, `scripts/run_nl_gold_ragspine.py`; env names
follow `RAGSPINE_<FIELD>`; the pydantic `RAGSpineConfig` does not expose them, like `page_parent`):

| Setting | Default | Meaning |
|---|---|---|
| `page_images_top_n` | 3 | candidate window (unchanged semantics) |
| `page_images_trigger` | `has_table,low_text` | tags that trigger in `tagged` mode; comma list, OR; `any` = all three |
| `page_images_max` | unset = `top_n` | at most this many images per retrieval call (each sub-question of a decomposed question is its own call) |
| `page_images_low_text_chars` | 300 | `low_text` threshold |
| `page_images_figure_min_chars` | 10 | `has_figure` threshold (small-figure filter) |

Invalid values (unknown mode or tag, empty trigger, negative numbers) raise `ValueError` when the
retriever is assembled; `ragspine batch` reports them as exit 2.

### 2. Page tags

Computed from `parse_di_markdown`'s `DiPage.blocks` (`extraction/di_markdown/page_tags.py`), not from
chunk text: chunks already linearize tables into `row | col: value` lines, so a table is no longer
recognizable at query time.

- `has_table` — the page has a `Table` block, or a `Paragraph` holding a pipe-table separator row
  (SuperIndex's regex, plus "the row contains a `|`", so a lone `---` rule is not a table).
- `has_figure` — the page has a `Figure` block **and** its largest figure has at least
  `figure_min_chars` characters of text (figure text + caption, whitespace removed). DI marks logos and
  decoration as figures; they carry no or almost no text.
- `low_text` — all block text on the page (headings, paragraphs, table anchor cells, figure text and
  captions) has fewer than `low_text_chars` characters after removing whitespace and `|`. Page headers,
  footers, page numbers and comments are already stripped by the parser, as in SuperIndex.

**Only raw measures are stored** (`has_table`, `n_figures`, `figure_max_chars`, `text_chars`); tags are
derived at query time from the current thresholds, so a threshold change needs no re-ingest.

**Threshold calibration (one sample only).** Both defaults were set on a single document, the AIA
2026 interim results deck (71 pages, pdfspine stand-in for DI markdown), and must be revisited on other
corpora:

- `low_text_chars = 300`: kept from SuperIndex. On the sample, 16 / 71 pages are `low_text` in both the
  text-layer and the OCR markdown.
- `figure_min_chars = 10`: every figure block in the sample (48 pages) is a real chart, KPI tile or
  diagram, and the smallest carries 12 characters (`$3.2b 17% CAGR`, page 5); the stand-in emits no
  text-less image figures. A logo or wordmark figure carries 0–8 characters (`AIA` = 3). 10 sits below
  the smallest real chart and above typical wordmarks, so on this sample the filter removes nothing
  (has_figure 48 pages) and would drop DI's logo figures. Characters are used rather than lines because
  a one-line KPI (`Customer Agency`, 14) is still content.

Sample distribution with default thresholds (71 pages): text-layer markdown `has_table` 11,
`has_figure` 48, `low_text` 16, any tag 62 (87 %), default trigger `has_table,low_text` 26 (37 %);
OCR markdown (PP-OCRv6 on the outlined PDF, tables and figures not reconstructed) `has_table` 0,
`has_figure` 0, `low_text` 16, default trigger 16. "Any tag" is nearly "every page" on a deck, which is
why the default trigger leaves `has_figure` out.

### 3. Storage and ingest

Table `page_tag(doc_id, page, has_table, n_figures, figure_max_chars, text_chars, md_sha256,
tags_version, PK(doc_id, page))` in the chunk db, created on first write only (a db with no linked PDF
gains no table). Written in `ingestion/page_images/index.py:sync_ingested_page_images`, the one point
that the facade, CLI and worker ingest paths all pass through, for every `.md` that has a source PDF
(idempotently skipped files included, so re-running ingest backfills an old db). Signature =
`md_sha256` + `tags_version`; unchanged ⇒ skipped. A `.md` re-ingested without a PDF loses its tags
together with its images. Page = physical page order, the locator's `page=N`.

**Old db, lazy and read-only:** when a doc has no `page_tag` rows, the trigger reads
`narrative_doc.source_path` / `file_hash`; if the file is still there with the same hash it is parsed
and the result cached in process (key `(doc_id, file_hash)`), never written back — the query path stays
read-only because the service db belongs to the worker. Otherwise the doc is **untagged**: in `tagged`
mode it gets no image (reason `untagged`), as SuperIndex does when `load_tags` returns `{}`.

### 4. Selection — the trigger only removes (invariant)

`PageImageTriggerRetriever` wraps `PageImageRetriever` (outermost, `attach.py` unchanged). In rank
order over results that carry a `page_image`: a repeated `(doc_id, page)` or `image_sha256` is dropped
(`dup`); in `tagged` mode a page without a matching tag is dropped (`not_tagged` / `untagged`); the
rest is cut to `max` (`over_max`).

**It only ever deletes a `page_image` key. It never adds a reference and never touches any other key.**
So the RESTRICTED screening at the image exit (`attach.py` re-checks the chunk store) is inherited
unchanged; the conformance test `tests/conformance/test_page_image_isolation.py` runs `tagged` next to
`on`, with its reverse-proof. The trace `op=narrative.page_image_trigger` carries mode, trigger names,
counts, drop reason codes and tag-source counts only.

### 5. Out of scope for v1

- **Per-route triggering.** The retriever sees only the query and filters; the route (and whether this
  is a structured fallback) lives in `agent/`. Results are reported per route instead; if only fallback
  or numeric questions benefit, a separate change will touch the agent.
- **A `get_page_image` tool** (model asks for an image). The narrative path is one provider call with
  no tool loop; adding one changes the agent's spine and adds a round trip.
- **"Screenshot wins over markdown" prompt rule.** It conflicts with 0024 (numbers seen only in an
  image are rewritten) and needs an `agent/` prompt change. Revisit with an ADR superseding part of 0024
  if the evaluation shows `tagged` gains that the number guard cancels — the batch run reports the
  guard's rewrite count for exactly this reason.
- **Crops instead of whole pages** (v2): feasible for tables via pdfspine `find_tables` bboxes aligned
  to DI tables; vector charts need drawing clustering.

## Evaluation protocol and decision rule (pre-registered)

**Two text conditions**, each its own workspace:

1. *Text layer* — the existing AIA DI markdown (exported from the PDF's text layer), source PDF linked.
2. *OCR* — the outlined (text-less) PDF, PP-OCRv6 lines turned into DI-style markdown by
   `scripts/examples/ocr_to_di_markdown.py` (paragraphs in bbox reading order, tables degraded to
   paragraphs), with its sidecar linking the outlined PDF. This is the realistic case for images.

**Quality** — `scripts/run_nl_gold_ragspine.py`, nl-gold v2, full document, `--repeat 3`, claude-cli +
local-http embedding / reranker, routes A and B, en + zh; modes `off`, `tagged`, `all` (`all` is an upper
reference, never a default candidate). Judged on each run's mean ± std and the unstable-case list, **not
a majority vote**.

**Cost** — `ragspine batch` (ask mode) on the same workspace and settings, `--concurrency 1`: per-question
images sent, latency p50 / p95, token usage from the request trace, number-guard rewrites. With
claude-cli the images are read through its Read tool; whether those tokens are fully counted in the CLI's
reported usage is unverified. When no token usage is reported, only images and latency are compared and
the report says so.

**Rule.** Report each text condition separately. On-demand images are **worth it** only if, in some text
condition, `tagged` versus `off`:

- raises the **positive pass rate mean by ≥ 9 percentage points**;
- does **not increase the number of unstable cases**;
- does **not lower the abstain pass rate**;
- raises **p50 latency by ≤ 25 %**; and
- raises **tokens by ≤ 30 %** (skipped, and stated, when tokens are unavailable).

Meeting the rule in the OCR condition alone is sufficient to conclude that on-demand images have
value. The default for each text condition is decided from these results and recorded once in
"Evaluation results" below; until then the default is `off` everywhere and `tagged` is opt-in. If the
rule is met nowhere, that stays so. Results go to `data/validation/ragspine-nl-gold/<date>-page-image-*`
and the CHANGELOG.

## Evaluation results

*Pending — to be filled in once, after the runs above (see the note at the top).*

| Text condition | Mode | Positive mean ± std | Abstain | Unstable | p50 / p95 s | Tokens in / out | Images / q | Guard rewrites |
|---|---|---|---|---|---|---|---|---|
| text layer | off | | | | | | | |
| text layer | tagged | | | | | | | |
| text layer | all | | | | | | | |
| OCR | off | | | | | | | |
| OCR | tagged | | | | | | | |
| OCR | all | | | | | | | |

Conclusion per condition and resulting default: *pending*.

## Consequences

- **Default `off`, byte-identical**; `on` / `all` byte-identical to the pre-ADR `on`.
- The trigger inherits RESTRICTED isolation by construction (remove-only) and adds a conformance
  parametrization rather than a new exit.
- Tags cost one markdown parse per linked `.md` at ingest; no model, no network.
- A threshold change is a query-time setting; a change to how raw measures are computed bumps
  `PAGE_TAGS_VERSION`, and re-running ingest rewrites the rows.
- Frozen by `tests/extraction/di_markdown/test_page_tags.py`, `tests/retrieval/page_images/trigger/`,
  `tests/service/test_page_images_switch.py`, `tests/conformance/test_page_image_isolation.py`,
  `tests/cli/test_batch.py` and the unchanged off snapshot
  `tests/retrieval/page_images/test_page_images_off_snapshot.py`.

## Alternatives considered

- **Any tag triggers (SuperIndex default).** On a slide deck DI tags nearly every page as a figure; the
  sample reaches 87 %, i.e. almost `all`.
- **Store the derived tags.** Freezes the thresholds into the index; storing raw measures keeps
  calibration a query-time knob.
- **Write tags from the three ingest callers.** Three call sites instead of one shared function.
- **Change `attach.py`.** A wrapper keeps the audited isolation code untouched and makes "only removes"
  checkable as a subset property.
