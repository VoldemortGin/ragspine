# Changelog

All notable changes to RAGSpine are documented here. This project follows Semantic Versioning.

## [0.15.0] - 2026-09-21

### Added

- **A ruled table's grid is proved from the page's own rulings** (`enterprise_pdf_rag`,
  ADR 0014): `TableIR` / `TableCell.verification` were pinned `PENDING` by construction, so a
  cell citation could quote the cell's text but never its row or column. They are now `VERIFIED`
  exactly when a proof exists — every row and column boundary sits on a real axis-aligned ruling
  from `page.get_drawings()` within 0.5pt, every cell edge is continuously ruled (collinear
  pieces stitched), and every merge is proved by the absence of a rule inside the merged cell.
  `processing/geometry.py` gains the ruling vocabulary, `processing/table_grid_proof.py` is the
  pure rule, and `adapters/pdfspine_tables.py` produces the observations (solid strokes, thin
  filled rectangles and stroked-rectangle edges; dashed, curved and over-thick paths are not
  rulings). Header rows / columns are graded: a thick interior rule or a filled band is `proved`
  and citable, a bold face or "first row" is a `heuristic` that never is. The stage receipt binds
  `grid_scope` + `ruling_digest`, and every `resolve` re-derives the whole proof from the pinned
  source PDF. An answer may then cite a cell's `row`, `col` and `header` — only on a verified
  grid, with the header matched verbatim apart from whitespace. Unruled, snapped and
  double-ruled tables stay `PENDING`; they remain retrievable and citable by cell text, and every
  snapshot published earlier still parses, mounts and resolves unchanged.
- **Diagrams and formulas become retrievable once their structure is proved from the source**
  (`enterprise_pdf_rag`, ADR 0015): `DIAGRAM` and `FORMULA` objects used to stop at a
  "no independent verifier" diagnostic (ADR 0006) and never reached the index, a context block or
  a citation. A **model-free, replayable** proof now runs beside the two model branches over the
  same pinned crop and, when it holds, writes `qualified_ir` / `qualified_description` /
  `qualification`. For a diagram (`adapters/diagram_geometry.py` + `diagram_qualification.py`,
  `processing/diagram_models.py` + `diagram_description.py`): every node label must equal its cited
  span verbatim, every node bbox must match a real painted frame within 2pt, every edge needs a
  connector leaving the source node plus a filled arrowhead whose derived tip lands in the target,
  and every span inside the object must be cited — otherwise the whole object fails closed with a
  verbatim diagnostic. A nodes-only diagram is admitted and prints no edge at all. For a formula
  (`processing/formula_models.py` + `formula_rules.py`, `adapters/pdfspine_formula.py` +
  `formula_qualification.py`): tokens quote span substrings under a tiling closure rule with
  per-character bboxes, superscripts and subscripts are proved from the PDF's own `Ts` or marked
  `derived`, fraction bars and radicals quote real `get_cdrawings()` paths, and every path inside
  the object must be explained. A fully proved formula is `VERIFIED` (`proof_level="full"`); one
  with a derived script is retrievable at `literal` level and stays `PENDING`. The description of
  both is a deterministic template, never a second model pass. Index-text policy rises to
  `source-transcription-and-scoped-chart-qualification-v5` (one new gate,
  `VISUAL_PROJECTION_POLICIES`, shared by both kinds); answers may cite `nodes.<id>.label`,
  `edges.<index>`, `formula.linear`, `formula.readable` and `tokens.<index>`, all compared
  whitespace-folded with case kept (`_literal` was renamed `_exact` and now serves every such
  check). `rag-chat-v1` gains `diagram` / `formula` block kinds and `diagram_node` / `diagram_edge`
  / `formula` claim kinds; `processing_export` gains `diagram_structure_qualified` and
  `formula_tokens_qualified` coverage columns. `scripts/enterprise_pdf_rag/requalify_visual_objects.py`
  re-proves an already-published snapshot's diagrams from the branches it already stores (no model,
  no pointer moved, `--dry-run` writes nothing) so a release can gain the capability without
  re-running `semantics`, and `scripts/enterprise_pdf_rag/formula_smoke.py` runs the formula proof
  read-only over a saved processing id. Snapshots published earlier still parse, mount and resolve
  unchanged.

### Fixed

- **A proved diagram or formula gets the same guaranteed prompt seat a chart has**
  (`enterprise_pdf_rag`, ADR 0012 generalised for ADR 0015): the AIA p6 three-stage pathway was
  qualified and indexed but never entered the ten prompt seats for "What are the three stages of
  the agency technology investment?", so the model answered from p5 prose instead.
  `adapters/answer_service.select_context` now keeps one seat per citable visual kind — a chart
  with an explicit value, a diagram with a labelled node, a formula with a linear form — for the
  first such member found within `2 * top_k` but outside `top_k`; seats are given up from the
  last one backward and never from a seat already holding a citable visual object, pending or
  label-only objects are never promoted, and only members of a still-missing kind in the window
  are resolved. The diagram index projection was confirmed to carry every node label.
- **A numbered list no longer trips the prose number gate** (`answers/verify.prose_grounded`):
  `1.` / `2)` / `3、` / `(4)` / `第 5` / `Step 6` at the start of a line or a sentence are
  enumeration markers, not figures, so a list-shaped answer with verified claims is answered
  instead of abstaining on "numbers outside verified claims". A number inside an item's body
  (amount, percentage, year) is gated exactly as before, and a decimal that ends a sentence is
  never mistaken for a marker.
- **Strict response schemas are guarded offline** (`tests/enterprise_pdf_rag/adapters/
  test_strict_response_schemas.py`): the ADR 0015 validation hit a provider-side HTTP 400 on every
  chat because an optional `ModelClaim` field left `required` incomplete (`a0a0d18`), which no
  offline test could see. A parametrised guard now walks the exact schema `_response_schema`
  sends for all nine `response_model` classes used at `complete_json` / `complete_text_json`
  call sites and enforces the strict-mode rules (every property required, `additionalProperties:
  false`, no unsupported composition keywords, `$ref`s local to `$defs`).

## [0.14.0] - 2026-09-21

### Added

- **`enterprise_pdf_rag` ships in the same distribution** (ADR 0021): `pip install rag-spine`
  now provides both `import ragspine` and `import enterprise_pdf_rag` (the traceable
  financial-PDF evidence / QA backend: content-addressed immutable snapshots
  source → processing → retrieval, span/drawing-level evidence chains, source-qualified
  ChartQA), plus a second console script `enterprise-pdf-rag` next to `ragspine`. History was
  preserved via `git subtree`; `ragspine` may only be imported under its `adapters/`, the
  `figures/ documents/ processing/` packages stay pure stdlib (guarded by
  `check_architecture.py`), and its four structural gates (conformance / architecture / schema /
  drift) run as `scripts/ci.sh` step 9. Its `CLAUDE.md` / `AGENTS.md` contract and `resources/`
  ship in the wheel; PRD, ADR 0001–0011 and JSON schemas live under `docs/enterprise-pdf-rag/`.
- **Generic PDF ingestion entry** (enterprise_pdf_rag ADR 0010):
  `enterprise-pdf-rag ingest --pdf <any PDF> --pages all|1-3,5 --stage source|layout|semantics
  --max-live-calls N` turns any PDF into an immutable draft; the default `source` stage makes
  zero model calls, the full source is always retained and page selection only scopes
  downstream work. `qualify → index → publish` stay explicit, with no implicit model call and
  no implicit activation. Runs outside the checkout with `APP_ROOT_DIR` / `APP_DATA_DIR`.
- **Document catalog, mounted documents and verified answer chain** (enterprise_pdf_rag
  ADR 0011): `APP_EXECUTION_MODE=document-catalog` scans published documents under
  `APP_INGESTION_DIR`, mounts each one and re-verifies its pinned manifest before any model
  call (drift / corruption → 409, missing embedder → 503, never a mock fallback). The
  `document-catalog-v1` contract adds `GET /v1/documents`, `GET /v1/documents/{id}`,
  `.../manifest`, `POST .../search` and `POST .../context`; hybrid retrieval pairs the pinned
  cosine vector channel with BM25 over the same description text, reusing `ragspine`'s
  retrieval / rerank pieces. `POST /v1/chat/completions` grows from source-review-only (422 on
  financial questions) into evidence-chain natural-language answering, every claim checked
  against its evidence under the family's anti-fabrication / provenance invariants. `TABLE`
  figures are admitted to the catalog and chart geometry matching gained an explicit tolerance.
- **Open WebUI `document-catalog` profile** for `scripts/enterprise_pdf_rag/webui_preview.py`,
  and `ENTERPRISE_PREVIEW_STATE_DIR` to relocate the preview's logs / PID record so a second
  preview can run beside an already recorded one.
- **`TableStructureRecognizer` seam** (`extraction/tables/`): given an already-detected table
  region plus its text-layer words, produce a cell grid (rows / columns / spans). Motivated by a
  2026-08 measurement on FinTabNet.c (150 pages / 186 gold tables): pdfspine's `strategy="text"`
  table **detection** is already good (79.6% recall, 100% precision — all 148 detections hit a gold
  table), while the **grid reconstruction** is what collapses (GriTS_Top 0.233 even on correctly
  located tables). The seam therefore deliberately does *not* do table detection.
  Cell *text* is never produced by this seam — a digital PDF's text layer is exact, so callers pull
  content from it by cell coordinates rather than letting a model read characters
  (the structure/content split that 2026 SOTA work such as DELTA also adopts).
  Five-part shape matching the family's other seams: Protocol + offline deterministic default
  (`GridStructureRecognizer`, word-centroid clustering, zero third-party deps) +
  `make_table_structure_recognizer` factory + `RAGSPINE_TABLE_STRUCTURE` env selection +
  parameterized conformance. **Default `None` = off**, so the existing extraction path is
  byte-identical; returning `None` means "no opinion" and the caller keeps its own grid — the seam
  never fabricates an empty grid to look like it answered.
- **TATR vision backend** (`extraction/tables/adapters/tatr.py`, new `[tsr]` extra): wraps
  Microsoft's Table Transformer structure-recognition model. Only its TSR half is used — detection
  stays with pdfspine — which saves one model's inference and removes an error source. Pixel
  coordinates are converted back to PDF points and clamped into the caller's region, so its output
  coordinate system matches the deterministic default exactly. torch/transformers/pillow are
  lazy-imported behind the extra with a friendly error when missing. The chosen checkpoint's licence
  must be checked against ADR 0009's ≤Apache-2.0 gate before promoting it to a default path.

- **A conditional prompt seat for a citable chart** (`enterprise_pdf_rag`, ADR 0012): when no
  hit in the top-k is a chart block with an explicit value but one sits within the next k fused
  positions, it replaces the last seat. Pending or label-only charts never qualify and nothing
  outside the window is promoted.
- **`AnswerEnvelope.member_ranks`** in the `rag-chat-v1` response: one optional
  `MemberRankOut(member_id, fused_score, vector_rank, lexical_rank, vector_score, bm25_score)`
  per member that entered the prompt, so retrieval behaviour is readable from a response. The
  contract name is unchanged.

- **`enterprise_pdf_rag` page-level automatic metadata and pre-filters** (enterprise_pdf_rag
  ADR 0013): a `page_metadata` processing stage — one text-only model call per page returns
  title / section / page type / language / periods / regions, each kept only when it quotes the
  page's spans verbatim (dropped with a diagnostic otherwise); periods normalise
  deterministically (`1H26` / `2026年上半年` → `1H2026`, `FY24` → `FY2024`, `Q1 2025` →
  `Q1-2025`, bare year → `Y2026`); document metadata (cover title, report period, years,
  region vocabulary) is a zero-model fold recomputed on load. `enterprise-pdf-rag metadata`
  annotates a saved draft or release; `ingest --stage semantics` runs the stage too and
  `--stage metadata` runs it alone. Index text policy v4 prepends
  `display_title | page_title | section` above the ADR 0012 projection (descriptions and
  evidence unchanged; older snapshots keep scoring what they embedded). `rag-chat-v1` gains
  optional `filters` (`periods` / `regions`, derived from the question when omitted; cover
  and agenda pages never enter the candidates; starved filters are relaxed and reported as
  `filters_applied` / `filters_relaxed`), citations gain `page_title`, `/v1/models` and
  `/v1/documents` show the verified display title, and an unnamed document is routed by
  distinctive cover-title words and years across mounted documents.

### Fixed

- **Prose number gate no longer rejects restated years / periods** (enterprise_pdf_rag
  ISSUE-3, `answers/verify.py::prose_grounded`): "…in 1H 2026 was 17.5%" abstained as
  `claim_not_in_evidence` because `2026` was not inside a verified claim's text. A prose number
  is now grounded when it equals a verified claim's text number or value, appears verbatim in
  the user's question, or equals a number in the evidence text the verified claims cite (span
  quote, table cell text, chart period / category labels and source display). Any other number
  still abstains the whole answer; zero verified claims and the `decide` order are unchanged.
- **`scripts/enterprise_pdf_rag/webui_preview.py` no longer requires `lsof`**: a recorded PID's
  working directory is read from `/proc/<pid>/cwd` on Linux and from `lsof` only where `/proc` is
  absent (macOS); an unreadable directory never matches, so unrelated processes are still refused
  instead of crashing on a runner without `lsof`.

### Changed

- **`enterprise_pdf_rag` indexes charts by a projection of their qualified IR** (ADR 0012).
  A chart member used to embed its description, which is often its title alone, so a question
  naming the chart's categories or values had nothing to match. `processing/index_text.py` now
  projects a chart with at least one explicit point value into
  `<title> <period> <grammar> chart figure` plus `<category> <series> <value><unit>` per point;
  a pending, label-only or valueless chart keeps its description, and text / list / group / table
  members are unchanged. Description assets are untouched. The retrieval policy moves to
  `source-transcription-and-scoped-chart-qualification-v3` (bar publication to
  `source-transcription-donut-and-displayed-bar-v2`) and BM25 is gated on the same policy set, so
  both channels always score the string that was embedded — on old snapshots too. Policy strings
  stay informational: **existing releases keep mounting and answering, but only a re-run of
  `index` + `publish` gives them the projection.**
- **`enterprise_pdf_rag` retrieval defaults widen**: `AnswerRequest.top_k` 6 → 10 and
  `channel_limit` 20 → 50 (neither is exposed on the `rag-chat-v1` request, so the contract is
  unchanged). The opt-in reranker now judges evidence blocks — what the answer model would see —
  instead of a concatenation of index texts; it stays off by default because it must resolve
  every fused candidate.
- **Base dependencies**: `httpx>=0.27` moves into the base install — `enterprise_pdf_rag`'s HTTP
  layer imports it statically, so `enterprise-pdf-rag --help` failed on a plain
  `pip install rag-spine` without `[service]`. A new guard test
  (`tests/enterprise_pdf_rag/test_base_dependencies.py`) asserts that every third-party import
  of `enterprise_pdf_rag` is covered by `[project].dependencies`. Also in base for the sibling
  package: `pydantic-settings[yaml]`, `jinja2`, `fastapi` / `uvicorn`, `pdfspine`, `resvg-py`,
  `fonttools`; `corespine>=0.5.1`, `pdfspine>=0.11.0`, `pydantic>=2.12,<3`.
- **Toolchain**: ruff 0.16 formatting across the repo; `mypy --strict` covers `src/ragspine`, `src/enterprise_pdf_rag`,
  `tests/enterprise_pdf_rag` and `scripts/enterprise_pdf_rag`; `uv` 0.12.17.

## [0.13.0] - 2026-08-03

### Added

- **OpenAI Chat Completions compatibility** (`service/api/openai_public.py`): `POST /v1/chat/completions`
  (blocking + SSE streaming) and `GET /v1/models` clone the official OpenAI shape, so `openai` SDK
  clients, Open WebUI, LangChain, and any OpenAI-compatible provider slot can talk to RAGSpine
  unchanged. Provenance is preserved through a non-standard top-level `ragspine` extension field
  (`route` + `sources` + `request_id`); OpenAI clients ignore unknown fields, so lineage is never
  dropped to fit someone else's signature. Reuses the `/v1/ask` guard chain verbatim and keeps the
  guard-before-stream invariant (the generator replays an already-guarded answer, no provider/store
  access). Client-supplied `system` messages are deliberately ignored — the system prompt stays
  server-controlled.
- **LightRAG-shaped Python adapter** (`ragspine/compat/lightrag.py`): `LightRAG` + `QueryParam`
  clone HKUDS/LightRAG's public surface (`insert` / `ainsert` / `query` / `aquery` /
  `initialize_storages`) so existing LightRAG call sites migrate by changing one import. It is a
  thin signature translation over the `RAGSpine` facade — no retrieval logic is reimplemented.
  Because LightRAG's `query()` returns a bare string and would swallow lineage, an extra
  `query_with_sources()` returns the full `AgentResult`. Inserted raw text is landed as a
  content-addressed `.txt` under the workspace and ingested through the normal pipeline, so it
  gets real `doc_id` + locator provenance instead of becoming an unsourced dangling chunk.
  Semantic gaps (mode mapping, no Leiden hierarchy, ignored LightRAG-only kwargs) are documented
  rather than papered over.
- **Microsoft GraphRAG artifact interop** (`ragspine/compat/graphrag.py`, new `[graphrag-compat]`
  extra): `import_graphrag_artifacts()` loads `entities` / `relationships` / `text_units` parquet
  from a `graphrag index` output directory into any `GraphStore`, and `export_graphrag_artifacts()`
  writes a subgraph back out in that shape. GraphRAG exposes no Python API — its real contract is
  the parquet layout — so interop is done at the artifact layer. Imported records get lineage
  back-traced through `text_units` (never left empty) and are stamped
  `derived=model-derived` + `verified=unverified`; export goes through `GraphStore.subgraph`, so
  RESTRICTED nodes can never leak into files handed to an external tool. pandas/pyarrow are
  lazy-imported behind the extra, keeping the default install unchanged.

## [0.12.1] - 2026-07-30

### Changed

- Relaxed the Python requirement back to `>=3.12` (no upper bound); 0.12.0's 3.14-only floor
  is lifted. Restored the quoted self-referential annotations that 3.12/3.13 need (no PEP 649
  lazy evaluation there); toolchain (ruff/mypy) and CI matrices now target 3.12–3.14.

## [0.12.0] - 2026-07-21

### Added

- High-level `RAGSpine` workspace facade with unified dual-channel ingestion and guarded asking.
- `economy`, `balanced`, and `quality` retrieval presets with explicit typed overrides.
- Installed `ingest`, `doctor`, `config init/show`, and zero-Redis local `serve` CLI paths.
- Effective-configuration provenance and offline dependency, key, model, and filesystem diagnostics.
- Per-file ingestion channel, fact, chunk, review, skipped-page, warning, and remediation feedback.

### Changed

- **Breaking**: RAGSpine now requires Python 3.14 exclusively (`>=3.14,<3.15`). Python 3.11–3.13
  users stay on 0.11.0. Toolchain (ruff/mypy), CI matrices, and Docker images target 3.14.
- The package-root API now exposes the `RAGSpine` facade alongside the four original primitives.
- Installed users can complete ingestion, querying, and local visualization without repository scripts.

[Unreleased]: https://github.com/VoldemortGin/ragspine/compare/v0.15.0...HEAD
[0.15.0]: https://github.com/VoldemortGin/ragspine/compare/v0.14.0...v0.15.0
[0.14.0]: https://github.com/VoldemortGin/ragspine/compare/v0.13.0...v0.14.0
[0.13.0]: https://github.com/VoldemortGin/ragspine/compare/v0.12.1...v0.13.0
[0.12.1]: https://github.com/VoldemortGin/ragspine/compare/v0.12.0...v0.12.1
[0.12.0]: https://github.com/VoldemortGin/ragspine/compare/v0.11.0...v0.12.0
