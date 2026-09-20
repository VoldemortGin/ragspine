# ADR 0015: A diagram's structure and a formula's tokens are proved before they are retrievable

Status: Accepted, 2026-09-21. Extends [ADR 0006](0006-non-chart-visual-semantics.md) (the
same-SVG two branches for non-chart visuals), [ADR 0012](0012-chart-index-text-and-retrieval-seats.md)
(a member's index text is the deterministic projection of its already-qualified IR) and
[ADR 0013](0013-page-metadata-and-prefilters.md) (the contextual header, policy v4). It raises the
index-text policy to `source-transcription-and-scoped-chart-qualification-v5` — once, for both
kinds. [ADR 0014](0014-ruled-table-grid-proof.md) ships in the same release and is independent of
this one: it proves a table's grid and changes no policy string.

## Context

ADR 0006 gave every non-chart visual object two branches over one pinned SVG crop — a typed IR and
a natural-language description — and then pinned the object's `qualification` stage to
`UNAVAILABLE` with one verbatim diagnostic: *"Visual semantics are source-bound model inferences;
an independent field/relationship verifier is not available for this object."* `eligibility()`
never admitted `DIAGRAM` or `FORMULA`, so neither reached the index, a context block or a citation.

That was the right refusal for an inference, but it also refused things the page itself prints. A
diagram's node labels *are* page spans; a formula's symbols *are* page characters; a fraction bar
*is* a painted line. What was missing was a rule saying which drawn shape proves which node, which
character range proves which token, and what to do when none does — the same gap ADR 0014 closed
for a ruled table's grid.

The rules below were written after measuring the real sample and pdfspine 0.11.0; the measurements,
not the design's convenience, decided several of them.

**The real sample (AIA interim results, pages 1–20)**

- Two `Diagram` objects. Page 6 is a three-stage stacked pathway: three rounded rectangles, three
  node labels each quoting one span verbatim, **no connector and no arrowhead at all**. Its node
  bboxes differ from the real painted shapes by at most 1.76pt (rounded corners, and the shapes
  run wider than the object bbox, so they must be clipped to it before comparing).
- Page 5 is a technology flow added by source review. All five of its node labels are empty
  strings with no cited span, because partition left six spans unowned
  (`Industry-` / `Leading` / `Technology` / `Customer ` / `Super App` / `Agency `). Its arrows are
  single one-piece curved filled polygons, not "line plus triangle", and two of its tiles are
  bitmaps rather than vector rectangles.
- **Zero `Formula` objects**, and across all 71 pages the text carries no `=`, no Greek letter, no
  superscript digit and none of `× ÷ √ ≈`. The formula rule can therefore only be exercised by
  authored fixtures; the real-sample check is the smoke that runs the moment one appears.
- Seven `Image` objects, all logos and icons. They stay out of scope and keep the ADR 0006
  diagnostic word for word.

**pdfspine 0.11.0, as measured**

- A crop's native SVG wraps everything in `<g transform="matrix(1,0,0,-1,0,H)">` and paints in PDF
  bottom-left coordinates; composing the transforms down the tree yields page-top-left — the same
  frame as text spans and IR bboxes. An embedded-font page emits **zero** `<text>`: glyphs arrive
  as `<path>` under a non-unit scale matrix, which is how they can be told apart from drawings.
- pdfspine has no arrowhead concept. A filled triangle is just a closed filled path; which vertex
  is the tip has to be derived.
- A span's `text_matrix` and `origin` differ by exactly the PDF `Ts` operand — but only when `ctm`
  is the identity and `dir == (1, 0)`; rotation lands in `ctm` and degenerates `text_matrix`. The
  `flags & 1` "superscript" bit is a raised-baseline heuristic (a same-size raised run sets it too)
  and there is no subscript bit at all.
- `get_text("rawdict")` gives a bbox per character, and pdfspine merges `"ROE ="` into a single
  span — so a token must be allowed to quote a *substring*, or almost every formula fails closed.
- `get_cdrawings()` is bottom-left with nine documented keys; `get_drawings()` is top-left. A
  fraction bar is a stroked single `'l'`; a drawn radical is three or four `'l'` segments.

## Decision

1. **The proof is a third pure product, not a third model branch.** `visual_semantics.infer` is
   untouched: the same crop still yields one model IR and one model description, and both assets
   stay on disk byte for byte as lineage (`raw_ir` / `raw_description`, plus `model_view`). Beside
   them a pure function produces three new content-addressed stages — `qualified_ir`,
   `qualified_description` and `qualification` — exactly as the chart branch already does. Nothing
   in the proof, the projection, the description template, the replay or `verify.py` calls a model.

2. **Diagram geometry is read from the crop's own paths (`adapters/diagram_geometry.py`).**
   `native_shapes` walks `svg` / `g` / `path`, composes every `transform`, and keeps a `<path>`
   only under a unit-scale matrix, so glyph outlines are excluded. Unlike `source_paint._native_paths`
   it *skips* `defs` / `metadata` / `clipPath` / `image` / `text` / `title` / `desc` instead of
   refusing them — a diagram proof needs the strokes and filled polygons, and the real p5 crop
   contains `<image>`. On top of that: `rectangle_like` (a closed filled or stroked path whose
   flattened area is at least `SHAPE_MIN_FILL_RATIO = 0.85` of its bounding box — a rounded
   rectangle measures 0.997 — never the page backdrop), `straight_lines` (an open stroked `M`/`L`
   polyline; a Bézier connector is refused), `arrowhead` (a closed filled triangle with bounding
   area at most `ARROWHEAD_MAX_AREA = 400` pt²), `tip_and_base` (the vertex farthest from the
   midpoint of the other two, and that midpoint), `touches` and `segment_crosses` (Liang–Barsky).
   The crop is `member.source_svg`, so the whole proof replays byte for byte without the PDF.

3. **`adapters/diagram_qualification.qualify_diagram` is the rule, and it fails closed as a whole.**
   It takes the crop bytes, the page's spans and the model's `DiagramIR`, and raises
   `DiagramQualificationError` whose `str()` *is* the stage diagnostic, `<subject>: <reason>`:

   - **Nodes.** `node_id` matches `NODE_ID_PATTERN` (`[A-Za-z0-9_-]{1,80}`, so `nodes.<id>.label`
     stays an unambiguous path); the node bbox lies inside the object bbox; the label is non-empty
     **and** cites spans **and** equals, whitespace-folded and case-kept, one cited span's text or
     their ordered space-join (`empty_label_without_source_occurrence`,
     `label_is_not_verbatim_source_text`); every cited span exists, lies inside the object bbox and
     inside the node bbox within `SPAN_INSIDE_TOLERANCE = 0.5`; no span is cited twice
     (`span_cited_twice`); exactly one unclaimed `rectangle_like` shape, clipped to the object
     bbox, matches the node bbox on every edge within `NODE_BBOX_TOLERANCE = 2.0` — zero is
     `no_native_shape_matches_bbox`, two is `ambiguous_native_shape`; node bboxes do not overlap.
   - **Edges.** No self-loop, no duplicate pair, both endpoints known; a connector polyline whose
     one end touches the source node (within `CONNECT_TOLERANCE = 2.0`) and not the target; an
     unclaimed arrowhead whose base midpoint sits within `ARROW_JOIN_TOLERANCE = 3.0` of the
     connector's far end and whose derived tip touches the target and not the source; no segment of
     that connector crosses a third node's bbox shrunk by `CONNECT_TOLERANCE`; an edge label is
     either `None` with no cited span or verbatim source text under the node rules.
   - **Coverage.** Every span inside the object bbox must be cited by exactly one node or edge
     label (`object: uncited_source_span:<span_id>`). This is the one deterministic guard against a
     model silently dropping a node: a missed node leaves its span uncited.

   On success the IR is re-emitted with every edge and the IR itself `VERIFIED`, one diagnostic
   appended naming the method, and a `DiagramQualification` receipt records, per node, the cited
   span ids and the matched path (index, flattened points, bounds, clipped bounds) and, per edge,
   the connector, the arrowhead, the tip, the base midpoint and the connector end that fed it.

   **Departure from the written plan.** The plan's rule E2 asked the connector to touch *both*
   nodes. Its own fixture disproves it: the connector ends at `x = 142` and the target node starts
   at `x = 150`, an 8pt gap that the arrowhead spans — a literal E2 would reject the plan's own
   positive case. The implemented rule attaches the connector at the **source** end and lets E3's
   arrowhead prove the **target** end, which is the stronger statement anyway: direction is proved
   by the painted triangle, not by proximity.

4. **A proved diagram's description is a deterministic template (`processing/diagram_description.py`).**
   `describe_diagram` emits `Diagram with <n> nodes[ and <m> edges]: <label>; <label>. <from> -> <to>.`
   in reading order (`y0` quantised to `READING_ROW_QUANTUM = 4.0`, then `x0`, then id), producer
   `deterministic-diagram-description-v1`, `VERIFIED`. Every word is either a template word or a
   label copied unchanged. It is stored as the new `qualified_description` asset — not computed at
   retrieval time — because the description asset is the embedding cache key, the member binding
   and the `description_text` the answer prompt shows; a retrieval-time projection would fix the
   index text and leave an unproven model sentence in the prompt. The model's description is not
   rewritten; it stays as `raw_description` lineage.

5. **A nodes-only diagram is admitted; an unadorned connector is not an edge.** When `edges == ()`
   the object still qualifies (p6 is exactly this case): the labels are verbatim, the frames are
   real, the coverage is complete, and "what is the Growth stage" is answerable. Nothing then
   prints an `edges.` line anywhere — description, index text and context block all stay silent
   about order — and `SYSTEM_RULES` states outright that a diagram's edges are its drawn arrows
   only and that no order or next step may be inferred. Conversely a connector with no filled
   arrowhead proves no direction: the edge fails `no_arrowhead_pointing_to_target` and the whole
   object fails closed, because `DiagramEdge` has no place to carry "undirected".

6. **Formula tokens quote span substrings, and a closure rule keeps the quotation honest**
   (`processing/formula_rules.py`, pure stdlib). `tile_run` cuts a span into maximal same-role runs
   (`operand` / `number` / `greek` merge; `operator` / `relation` / `bracket` / `unit` / `radical`
   are single characters; whitespace is a gap), and `check_tiling` is the invariant: the pieces are
   ordered, non-overlapping, and their concatenation equals the span text with whitespace removed —
   nothing else may be skipped. Each token's bbox is the union of its own `rawdict` character
   boxes, so `len(chars) != len(text)` (ligatures, composed glyphs) is a refusal rather than a
   guess. The same check runs again on replay.

7. **A script is proved from the PDF's own `Ts`, or it is marked derived.** `rise_of` returns
   `(page_height - origin_y) - text_matrix[5]` only when `ctm` is the identity and `dir == (1, 0)`;
   a non-zero rise proves a superscript or subscript verbatim (`script_proof="text_rise"`). When
   the rise is zero — which is what pdfTeX, InDesign and Word exports actually produce, because
   they move the baseline with `Tm`/`Td` — the position is *inferred* from two numbers and marked
   `script_proof="derived"`: the run's size is at most `DERIVED_MAX_SIZE_RATIO = 0.8` of its base's
   and its baseline is raised by at least `DERIVED_MIN_SUPER_SHIFT = 0.15` (or lowered by
   `DERIVED_MIN_SUB_SHIFT = 0.10`) times the base size. `ScriptEvidence` stores all three numbers
   plus pdfspine's `flags & 1`, which is **recorded and never decisive**. A same-size raised run
   stays a base token; a merged same-size `"x2"` becomes two base tokens and reads `x 2`, never
   "x squared".

8. **Structures quote drawn paths, and every path inside the object must be explained.** A
   horizontal rule is a stroked single `'l'` with `|dy| <= RULE_MAX_SLOPE = 0.5` and
   `width <= RULE_MAX_WIDTH = 2.0`, or a thin `'re'`. A fraction needs tokens both above and below
   the rule, horizontally overlapping it within `X_OVERLAP_TOLERANCE = 1.0` and within
   `VERTICAL_REACH = 1.5` token heights, with no token straddling it
   (`formula_fraction_line_unpaired:<path_index>`). A radical is either the `√` glyph plus an
   overline to its upper right (`formula_radical_without_overline:<span_id>`) or a three-to-four
   segment polyline whose last, longest segment is horizontal. Any path in the object's bbox that
   no structure consumed is `formula_unexplained_path:<path_index>` — an unexplained line could be
   an underline or a strikethrough and would change the meaning. A token in two structures and base
   tokens on more than one baseline (`BASELINE_CLUSTER = 0.3` of the size) are refusals too.
   *Departures:* `size_of(token)` is the token bbox height (the plan left it unnamed); a rule that
   already reported `formula_fraction_line_unpaired` is marked addressed and is not re-reported as
   `formula_unexplained_path`; and a readable group is parenthesised only when it contains an
   operator or a relation.

9. **Two proof levels, and only one of them is `VERIFIED`.** With every script proved by `Ts` the
   IR is `VERIFIED` and `proof_level="full"`; with at least one derived script it is `PENDING` and
   `proof_level="literal"` — the transcription is proved, the typographic *relation* is not. Both
   are retrievable, exactly as ADR 0011's literally-transcribed tables are retrievable while
   pinned `PENDING`. `FormulaIR.__post_init__` enforces the pairing (tokens ⇔ `linear` ⇔ `readable`
   ⇔ `proof_level`; `VERIFIED` ⇒ `full`; dense token indices; a script attaches to an existing base
   token; structure members exist), and a model-only `FormulaIR` written before this ADR still
   parses because every new field defaults to empty.

10. **`linear` is a LaTeX subset and `readable` is a fixed connective table; symbols stay Unicode.**
    `linearize` emits `\frac{}{}`, `\sqrt{}`, `^{}`, `_{}` and joins words inside a group with
    `\ `, but never maps `α` to `\alpha` or `×` to `\times` — a mapping would itself be a
    non-verbatim rewrite. `readable_text` walks the same tree with one fixed table
    (`=` → 等于, `÷`/`/` → 除以, fraction → `<num> 除以 <den>`, `√` → `… 的平方根`) and, crucially,
    distinguishes the two proofs in words: a `text_rise` superscript reads `x 的 2 次方`, a derived
    one reads `x 上标 2`. The reader is never told the document said "squared" when the document
    only set smaller type higher.

11. **The stage set, and what a refusal looks like.** Both kinds keep `native_crop`, `source_text`,
    `svg`, `model_render`, `model_view`, `ir(_raw)`, `description(_raw)` unchanged, and add
    `qualified_ir`, `qualified_description` and `qualification`; a formula adds
    `formula_observation` (the quoted pdfspine fields, always written when observation succeeded)
    and `qualification_exclusions` (the informational model-agreement record). A refusal writes no
    `qualified_*` at all and leaves `qualification` `UNAVAILABLE` carrying the verbatim diagnostic,
    which is exactly today's behaviour for an unproven object. `qualified_claim_count` is
    `len(nodes) + len(edges)` for a diagram and `len(tokens)` for a formula. Like `_table`, neither
    proof consults `qualification_policy` — a deterministic check that reads no model has nothing
    to gate on, and the real ingestion entry passes `"none"`. A **diagram** still requires both
    model branches (its IR is what is being proved); a **formula** requires neither, because no
    model byte reaches its qualified products — an object whose model calls were budget-exhausted
    still qualifies, with its lineage reduced to whatever actually succeeded.

12. **Eligibility, policy v5, and one shared gate.** `eligibility` admits `DIAGRAM` and `FORMULA`,
    requires the chart-shaped stage set (`qualified_ir`, `qualified_description`, `qualification`,
    `svg`) for all three qualified kinds, and refuses with a kind-specific string: *"Diagram
    structure is not proven; only geometry-qualified diagrams are retrievable"* and *"Formula
    tokens are not source-proven; only proven formulas are retrievable"*. `member_index_text` gains
    two branches: a proved diagram projects `diagram figure` plus its labels in reading order plus
    one `<from> -> <to>` per edge; a proved formula projects `readable`, `linear`, the word
    `formula` and every distinct token text. Both fall back to the description when the IR carries
    nothing citable — the ADR 0012 rule that citability is a content property, not a `Verification`
    value. The read side is gated by one new frozen set, `VISUAL_PROJECTION_POLICIES = {v5}`, used
    by both kinds; `PROJECTED_CHART_POLICIES` and `CONTEXTUAL_POLICIES` gain the literal v4 string
    so that v4 snapshots keep scoring exactly what they embedded. `_POLICY` is part of the snapshot
    id, so a v4 snapshot keeps its id (frozen in `test_retrieval_snapshot.py`) and stays mountable;
    no code ever refuses a snapshot for its policy.

13. **Citable paths, and one text criterion for all of them.** A diagram block prints
    `diagram nodes=<n> edges=<m>`, then `nodes.<node_id>.label: <label>` and
    `edges.<index>: <from> -> <to>`; a formula block prints `formula proof_level=<level>`,
    `formula.linear: …`, `formula.readable: …` and
    `tokens.<index>: <text>  (role=…, script=…, proof=…)`. `ClaimKind` gains `diagram_node`,
    `diagram_edge` and `formula`; `_PATH_PREFIX` values became tuples because a formula claim owns
    two prefixes (`formula.`, `tokens.`), which `str.startswith` accepts unchanged. All three new
    verifiers compare with `_exact` — whitespace folded, **case kept** — and `_literal` was renamed
    to `_exact` so that the literal-transcription criterion, ADR 0014's header criterion and these
    three are demonstrably the same function. That closes the gap the design notes flagged: the
    verify side is not laxer than the qualification side (which requires a label to equal its span
    verbatim), and `ROE` is not `roe`. Cell text keeps `_norm` (case-folded) because a cell quotes
    content, not a name.

14. **Receipts bind the proof and every resolve replays it.** `DiagramPublicationReceipt`
    (`source-diagram-structure-qualification-v1`) binds the object, the manifest, the three
    qualified refs, the crop and the full raw closure (`raw_ir`, `raw_description`, `view`);
    `validate_diagram_member` re-crops the pinned page, re-parses the raw IR, re-runs
    `qualify_diagram` over the page's own sidecar spans and requires the stored IR, description and
    receipt to compare equal. `FormulaPublicationReceipt` (`source-formula-qualification-v1`)
    additionally pins the observation asset; `validate_formula_member` re-opens the **pinned source
    PDF**, re-observes it, requires the observation to equal the stored one, repeats `check_formula`
    and compares the IR, the description (including producer and confidence) and the four receipt
    counters. A `DiagramQualificationError` is a `ValueError` and simply refuses the mount. Lineage
    closures are exact: three refs for a diagram, observation plus whatever model branches were
    recorded for a formula.

15. **Migration: an already-published snapshot is re-proved from its stored branches.**
    `adapters/visual_requalification.py` (driven by `scripts/enterprise_pdf_rag/requalify_visual_objects.py`)
    re-applies `qualify_diagram` to a saved snapshot's stored `svg` / `ir` / `description` /
    `model_view` plus the page sidecar, copies every other record verbatim, and saves the result as
    a **new content-addressed draft** with `retrieval=None` (the eligible member set changed, so the
    pinned plan no longer describes the snapshot); no pointer moves, no model runs, and `--dry-run`
    writes nothing. It deliberately stays out of `semantic_objects._Writer`'s stage cache and salts
    its own fingerprints with `visual-requalification-stage-v1`, so a re-run re-derives byte-identical
    artifacts and the published store's cache is untouched. The alternative — re-running
    `process-aia-semantics` over the release — was measured and rejected: page 5 collides with the
    stage cache (the bbox-containment tolerance changed since that run) and page 18's donut loses
    its numeric qualification. The Formula branch of the same function is the seat for the same
    migration; the AIA release holds no Formula object, so nothing exercises it there yet.

16. **Offline coverage, and two read-only smokes.** `authored_pdf` grew
    `diagram_page` / `diagram_caption` / `formula_page` / `formula_rule` (mutually exclusive with
    `table_page`, all drawn on the last page): two boxes joined by a real filled triangle, and a
    fraction with a drawn rule plus a derived `x²`. A real `Ts` cannot be authored with pdfspine
    (`insert_text` has no rise parameter), so `tests/…/adapters/formula_fixture.py::rise_formula_pdf`
    writes the `full`-level fixture with reportlab's `setRise`. `formula_observation_fixtures.py`
    builds observations directly for the pure rules. The pure rules, the geometry, the receipts and
    replay, the index text, the context blocks, the claim chain and the end-to-end publications
    (two diagram cases in `test_generic_publication_e2e.py`, five formula cases in
    `test_formula_publication_e2e.py`) are covered offline; the real sample is touched only by two
    read-only smokes
    (`test_diagram_real_samples.py`, `test_formula_aia_smoke.py`) and by the dry-run requalification
    test, none of which writes to `data/`.

## Rejected alternatives

- **A VLM description checked after the fact.** Reusing the model's `description` for
  `qualified_description` and checking it (every node label a verbatim substring, no number outside
  the labels, every `(obs-…)` mapping to this object's spans) is checkable prose but not citable
  structure: `VisualDescriptionDTO` has no claims list, so "A leads to B" in free prose cannot be
  aligned mechanically to `edges.<index>`. Making it alignable means changing the description DTO
  and the prompt, which invalidates the model cache. The model description is kept as lineage, so
  the experiment stays available offline.
- **Recording an unadorned connector as an undirected edge.** `DiagramEdge` has no `directed`
  field, so "undirected" would have to be encoded in prose or in the pair's order — both of which
  the answer chain would read as direction. A line without an arrowhead proves adjacency, not
  direction; until there is a field for it, the object fails closed.
- **Admitting only `full`-level formulas.** Requiring a real `Ts` would make the feature die on
  real documents: pdfTeX, InDesign and Word exports move the baseline with `Tm`/`Td` and leave
  `Ts` at zero. The verbatim invariant still holds for those documents — only the script relation
  is inferred — so they are admitted at `literal` level, `PENDING`, with the three measured numbers
  stored and the readable text saying "上标" rather than "次方".
- **A custom linear syntax.** A private notation would need its own documentation, its own parser
  and its own escaping rules; the LaTeX subset is already what a financial analyst's tooling reads.
  Mapping symbols to LaTeX macros was rejected separately: `\alpha` is not what the page prints.
- **One token per span.** pdfspine merges `"ROE ="` into a single span, so a whole-span token rule
  would refuse nearly every real formula. Substrings are admitted instead, bounded by
  `check_tiling`'s closure rule and by per-character bboxes, so a token can never quote text the
  span does not contain or leave text unaccounted for.
- **Re-running the whole `semantics` stage to migrate a published snapshot.** It re-enters the
  model path for every object in the run, collides with the existing stage cache where a tolerance
  has since changed, and can lose an unrelated object's qualification. The deterministic proof
  needs only bytes the snapshot already stores, so the migration re-proves those and writes a new
  draft instead.

## Consequences and follow-ups

- Snapshots published before this ADR still parse, mount and resolve unchanged: `DIAGRAM` and
  `FORMULA` members simply do not exist in them, every new IR field defaults to absent, and
  `ProcessingStore.load`'s `qualified_*`-before-`*` binding already handles the new stages. To
  *gain* the capability a document must be re-proved — `requalify_visual_objects` (diagrams, from
  stored branches) or a fresh `semantics` run (required for formulas, which need the pinned PDF) —
  and then `index` + `publish`. The `data/ingestion` snapshots and the AIA release still have no
  owner for that rebuild. Re-proving the AIA release changes exactly what the sample predicts:
  page 6 qualifies, page 5 is withheld with its verbatim diagnostic, eligible members go 189 → 190
  and skipped objects 52 → 51 — one diagram's worth of capability, honestly earned.
- `SYSTEM_RULES` changed, so `request_fingerprint` changes and every cached answer misses once.
- Three published contracts changed: `rag-chat-v1` gains `diagram` / `formula` in `BlockKind` and
  `diagram_node` / `diagram_edge` / `formula` in `ClaimKind`; `aia-processing-v1` and
  `document-catalog-v1` gain the new qualification and IR definitions. `check_schema.py` compares
  them for exact equality and has no `--write`, so they were regenerated by hand, once.
- `processing_export` coverage gained two columns, `diagram_structure_qualified` and
  `formula_tokens_qualified`; a proved diagram or formula is no longer counted as
  `source_transcription_qualified`.
- Every `resolve` of a diagram member re-crops its page and replays the geometry proof, and every
  resolve of a formula member re-opens the pinned PDF and re-observes it — on top of ADR 0014's
  per-resolve table re-proof. Visual members are few (at most a handful per page in every sample so
  far) and v1 accepts the cost; if it becomes a bottleneck, the digest comparison can stay at
  resolve while the full proof moves to `build`.
- **Not addressed.** `IMAGE` stays unretrievable with its ADR 0006 diagnostic unchanged. Page 5's
  empty labels are a partition defect (six unowned spans), and the fix belongs in partition, not in
  a looser proof. Bézier connectors, open (two-stroke) arrowheads and one-piece curved arrows are
  refused; so are multi-line formulas, `∑`/`∫` as operators with limits, and any grouping or
  swimlane relation — none of which has a field in the IR today.
- **pdfspine stays pinned at 0.11.0.** The diagram proof compares composed SVG path coordinates and
  the formula proof compares an observation of span matrices and character boxes byte for byte, so
  a change in its output turns a stored proof into a mount-time refusal — loudly, never silently.
  An upgrade is: bump the pin, run the offline suite, run the two read-only smokes, and re-run
  `semantics → index → publish` for any snapshot that no longer replays, rather than relaxing a
  rule.

## Validation

Real-sample validation (the AIA legacy store re-proved through
`requalify_visual_objects`, plus an authored formula PDF through the full
`ingest → qualify → index → publish → chat` chain) is recorded in
[`CLAUDE_HANDOFF.md`](../CLAUDE_HANDOFF.md) under this ADR's section, with the evidence under
`data/validation/generic-chat-2026-09-21/visual-objects/`. The offline gate in this repository
proves the rules; that run proves the release. Outcome in one paragraph: the AIA release moved
from `00d5c714…` to `231c904c…` (190 members, policy v5) with only the two Diagram records
re-proved; the p6 node label `Growth: Data-Driven Lead Generation` is cited as a `diagram_node`
claim by the real model, the "which stage comes after Foundation" question yields no
`diagram_edge` and an honest abstention (nodes-only rule holds), and the ISSUE-2 / ROE control
answers do not regress. Authored PDFs through the full chain cite `edges.0: PLAN -> BUILD`
(`diagram_edge`), a `literal`-level and a `full`-level `formula.linear`. The run also exposed and
fixed a blocking provider bug unrelated to the proofs (strict response schemas must list every
declared property, `a0a0d18`) and left two retrieval follow-ups: diagram members are recalled
only when the question carries a node-label word, and Chinese queries have no lexical channel.

**Follow-up status** (branch `fix/visual-recall-and-gates`, after this ADR's real run). Three
of the defects that run exposed are closed: (1) the diagram-recall gap — ADR 0012's guaranteed
chart seat is generalised in `adapters/answer_service.select_context` to one seat per citable
visual kind (chart with an explicit value / diagram with a labelled node / formula with a linear
form) found within `2 * top_k`, so a proved diagram no longer depends on a node-label word in the
question; (2) the prose number gate counted a list's `1. 2. 3.` enumerators as figures and
abstained a correct numbered answer — line- and sentence-start enumeration markers are now
excluded in `answers/verify.prose_grounded`, numbers inside an item stay gated; (3) the strict
response-schema contract that BUG-A broke now has an offline guard over all nine
`response_model` classes (`tests/enterprise_pdf_rag/adapters/test_strict_response_schemas.py`).
Still open: Chinese queries have no lexical channel (BM25 tokenisation is Latin-only, RRF
degrades to the vector channel alone), and the partition still labels heading text lines as
`DIAGRAM` / `FORMULA` (the proofs reject them correctly; only the kind counts are polluted).
