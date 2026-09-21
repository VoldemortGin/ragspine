# ADR 0012: Chart index text and the retrieval seats that make a chart answerable

Status: Accepted, 2026-09-20. Resolves the "ISSUE-2, chart recall" follow-up of
[ADR 0011](0011-document-catalog-and-verified-answer-chain.md) and corrects the diagnosis recorded
there. The real rebuild and re-measurement are in [`CLAUDE_HANDOFF.md`](../CLAUDE_HANDOFF.md).

## Context

The 2026-09-20 real-model acceptance (ADR 0011) left one open failure, ISSUE-2: on the AIA
first-twenty-pages release (`df902346791b…`, 189 retrievable members) the question *"In the 1H26
Distribution Mix chart, what percentage of VONB came from Agency?"* was answered from a page-14
text span ("Premier Agency: 55% of VONB") instead of the page-18 donut that holds the number.
ADR 0011 recorded the cause as "chart members embed only a short description, so the channels
favour long text". Measuring it showed that attribution is wrong, and the real mechanism is
specific enough to fix.

What the release actually contains: of its 9 chart members exactly one — the page-18 donut
(title `Distribution Mix`, period `1H26`, `Agency` / `VONB` = 72%, `Partnerships` / `VONB` = 28%)
— is verified and carries citable values. The other 8 bar / donut members are `PENDING`, with zero
points and no title. The failure is about one member, not about charts being systematically
drowned by long text.

Measured mechanism, channel by channel:

- **The index text was the title.** A chart member's embedded text was its description text, and
  for this donut the description *is* its title: `Distribution Mix`, two tokens. Both channels
  scored those two tokens; nothing else about the chart was searchable.
- **The lexical channel was already right.** BM25 ranked the donut **1st** for that question. The
  vector channel ranked it **13–20th** — two title tokens embed nothing like a sentence-shaped
  question.
- **RRF cannot rescue a single-channel hit.** With `k = 60`, a member found by one channel only
  scores at most `1/(60+1) ≈ 0.0164`, while a member found by both at even mediocre ranks scores
  `1/(60+r_v) + 1/(60+r_l)` ≈ 0.025 at ranks (20, 20). A first-place lexical hit therefore loses
  to any ordinary paragraph that both channels find.
- **`channel_limit = 20` sat exactly on the cut.** The donut's vector rank was at or past the
  channel limit, so it frequently did not enter the vector list at all — which is precisely the
  single-channel case above. Its fused rank came out **≥ 7**, and the default `top_k = 6` cut it.
  Byte-checking the cached prompt under `<ingestion_root>/model-cache` confirmed that **zero chart
  blocks** reached the model; the model answered correctly from what it was given.
- **Rerank could not repair it.** `rerank=true` produced the same wrong answer, because the
  listwise judge scored a whitespace-joined concatenation of the same index texts rather than the
  evidence blocks. Two title tokens look irrelevant to a reranker too, and the 8 `PENDING`,
  zero-value charts — whose descriptions do mention charts — were promoted ahead of the one chart
  that can be cited.
- **A query-side fix cannot work.** Asked without the chart's title — *"Agency share of VONB
  1H26"* — the donut's BM25 score is **0**: not one query token occurs in `Distribution Mix`.
  Weighting chart members on the query side multiplies zero. Only changing what is indexed puts
  the donut in reach of that question.

Constraint that shaped the fix: description assets are content-addressed, and they are the
evidence that qualification receipts, context blocks and claim verification all read. Their bytes
may not change.

## Decision

1. **A chart's index text is a projection of its qualified IR (policy v3).** New pure module
   `processing/index_text.py` (stdlib only, reads no store):
   - `has_citable_value(chart)` is true when at least one `ChartPoint` has `value.kind ==
     EXPLICIT` and a non-`None` value.
   - `chart_index_text(chart, *, fallback)` returns a deterministic projection when that holds:
     `<title> <period> <grammar> chart figure`, then per point `<category> <series>
     <value><unit>`. A point whose value is unavailable contributes its labels only; a unit with
     no alphanumeric character is concatenated (`72%`) and a word unit is space-separated
     (`15 US cents`). Otherwise it returns `fallback` — the description text — so a `PENDING`,
     label-only or valueless figure never climbs the ranking on words it cannot cite. The page-18
     donut becomes `Distribution Mix 1H26 donut chart figure Agency VONB 72% Partnerships VONB 28%`.
   - `member_index_text(ir, description_text)` returns the description unchanged for text, list,
     group and table members. **Description assets themselves are not touched.**
   - `adapters/processing_retrieval.py` embeds `member_index_text(checked_ir,
     checked_description.text)`, moves `_POLICY` to
     `source-transcription-and-scoped-chart-qualification-v3`, and changes the embedding cache
     fingerprint from `("description-embedding-v1", description_ref, embedder_fp)` to
     `("index-text-embedding-v1", description_ref, sha256(text), embedder_fp)`, so a member whose
     projection changes cannot replay a stale vector.
   - **Both channels score the same string, on old snapshots too.**
     `PROJECTED_CHART_POLICIES = {v3, "source-transcription-donut-and-displayed-bar-v2"}` and
     `member_text(assets, plan, member)` return the projection only when the mounted plan's
     `qualification_policy` is in that set, and the description text otherwise.
     `MountedDocument.member_texts` (and the test bridge) go through it, so BM25 always scores
     exactly what the vector channel embedded — whether the snapshot predates the projection or
     not. `adapters/chart_qa_bar_promotion.BAR_PUBLICATION_POLICY` moves to
     `source-transcription-donut-and-displayed-bar-v2` for the same reason: displayed-bar
     admission goes through the same `build`, so its new members are projected as well.
   - Policy strings stay informational, as in ADR 0011 Decision 7: no code refuses a snapshot for
     its policy, so existing releases (the AIA `f59d2308…` manifest, policy
     `source-transcription-and-numeric-paint-qualification-v1`) still scan, mount and answer —
     they simply keep their description-only vectors until re-indexed.

2. **Retrieval defaults widen to `top_k = 10`, `channel_limit = 50`.** `AnswerRequest`'s defaults
   move from 6 / 20. The measurement above is the reason: 20 sat on the vector rank of the one
   member that mattered, and 6 cut a member whose fused rank the RRF arithmetic already caps.
   Neither field is exposed on `RagChatRequest`, so the `rag-chat-v1` request shape is unchanged
   and the wire behaviour is the new defaults.

3. **The reranker judges evidence, not index text.** `HybridSearch.search` builds each candidate
   as `build_context_block(document.resolve(hit)).prompt_text()` — the same block the answer model
   would see, with a chart's citable `points.<id>.value` lines and a text member's spans. The
   judge can no longer be fooled by a two-token title, nor by a chart description that names a
   chart it cannot cite. The cost is real and measured: rerank must resolve every fused candidate
   (up to `2 × channel_limit` = 100), and on the AIA release one resolve costs ≈0.8s — `manifest()`
   re-reads and re-validates ~5000 asset digests, and `load_retrieval` parses the 189 × 2560-dim
   index twice — which took the reranked case to **52s**, against ≈13–14s without. Rerank
   therefore stays **off by default** and opt-in per request, as in ADR 0011.

4. **One conditional seat for a citable chart, plus ranks in the envelope.**
   `adapters/answer_service.select_context(document, ranked, top_k)` retrieves `2 * top_k`,
   resolves the first `top_k`, and — only when none of those is a chart block with an explicit
   value — scans `ranked[top_k : 2*top_k]` for the first chart that has one and gives it the last
   seat. The window is scanned cheaply: `member_texts()` supplies each candidate's kind, and only
   `CHART` candidates are resolved. A `PENDING` or label-only chart never takes the seat, nothing
   outside the window is promoted, and when a citable chart is already in the head nothing extra
   is read. `AnswerResult.fused` reports the members that actually entered hydration, promotion
   included. The criterion is **an explicit value, not `ChartIR.verification`**: a displayed-bar IR
   (ADR 0009) is `PENDING` by construction yet carries explicit displayed values, and the index
   projection of Decision 1 uses the same criterion for the same reason.
   For observability, `adapters/http/chat_schemas.MemberRankOut(member_id, fused_score,
   vector_rank, lexical_rank, vector_score, bm25_score)` and the optional
   `AnswerEnvelope.member_ranks` (one entry per member that entered the prompt, in `member_ids`
   order, default `()`) make all of the above readable from a response instead of from a byte-dump
   of the model cache. The contract name `rag-chat-v1` is unchanged;
   `docs/enterprise-pdf-rag/schemas/rag-chat-v1.json` was regenerated and `check_schema.py` passes.

## Rejected alternatives

- **Weighting chart members on the query side** (a fixed boost, a chart-intent classifier, or a
  third chart-only channel). It cannot answer *"Agency share of VONB 1H26"*: there the donut's
  BM25 score is 0 and its vector rank is 24, so there is nothing to boost. It would also lift the
  8 `PENDING`, zero-value charts by exactly the same factor.
- **Rewriting the description assets to carry period / category aliases.** Descriptions are
  content-addressed evidence: changing their bytes changes every description ref and therefore the
  qualification receipts, the context blocks and the claim verification that read them. The
  projection is a retrieval-time view of the already-qualified IR; the evidence stays
  byte-identical.
- **Always reserving a chart seat, or injecting every chart member into the prompt.** That spends
  prompt budget on charts nobody asked about, and on this release it would seat `PENDING`,
  zero-point figures that cannot yield a single citable claim. The seat is conditional: only when
  no citable chart is already in the head, only from the fused window, only for a chart with an
  explicit value.
- **Using `ChartIR.verification` as the "citable" test.** Displayed-bar IRs (ADR 0009) are
  `PENDING` by construction while carrying explicit displayed values, so a verification-based test
  would index and seat exactly the wrong set. An explicit value is the property that decides
  whether a claim can be cited at all.
- **Refusing snapshots whose policy predates v3, or migrating the corpus behind a policy check.**
  ADR 0011 already fixed that policy strings are informational. Gating inside `member_text` gives
  the weaker, sufficient guarantee — both channels score the same string per snapshot — without
  invalidating a published release.

## Consequences and follow-ups

- **Real rebuild, 2026-09-20 (AIA first twenty pages).** `qualify` is unchanged by the projection:
  eligible = 189 / skipped = 52 / charts = 9. `index` with the real `Qwen3-Embedding-4B` (2560
  dims, fingerprint `local-http/Qwen/Qwen3-Embedding-4B`) took 40s and produced processing
  `da1065fc0bd6…` and retrieval snapshot `53e08ad41839…`: 189 members, policy
  `source-transcription-and-scoped-chart-qualification-v3`. `publish` switched `current-processing`
  only (`a7384f0c…` → `da1065fc…`); `current-manifest` stayed `e702bf1c…`, the older snapshot files
  remain on disk and the previous manifest still loads. The 8768 / 3200 preview remounted the new
  snapshot 16s after a restart with the same environment. Evidence:
  `data/validation/generic-chat-2026-09-20/aia-after-issue2/`.
- **Re-measured HTTP cases** (same directory; `summary.json` carries each envelope's ranks). `b`
  (the original question), `b2` (rephrased, naming Partnerships), `n` (title-free *"Agency share
  of VONB 1H26"*) and `b3` (`b` with `rerank=true`) are all **200 / answered**, citing p.18
  `points.point-agency.value = 72%` (`b2` additionally `points.point-partnerships.value = 28%`).
  In the envelope the donut shows `lexical_rank = 1`, `vector_rank = 17` (`b` / `b2` / `b3`) or 24
  (`n`), and **fused rank 5** — inside the new `top_k`, without needing the reserved seat. The text
  control `a` (p.4 quote, record Operating ROE 17.5%) is unchanged. `b` / `b2` / `n` / `a` take
  ≈13–14s each.
- **Rerank costs 52s on this release**, for the reason in Decision 3, and stays off by default.
  Making it affordable means caching the mount's manifest validation and the parsed index per
  snapshot, which is not done here.
- `member_texts()` costs ≈0.58s on the AIA mount, which is why `select_context` calls it lazily and
  at most once per request.
- **Snapshots published before 2026-09-20 do not benefit until re-indexed.** They keep
  description-only chart vectors and, under `member_text`'s policy gate, description-only BM25
  text as well; re-running `index` + `publish` is the whole migration.
- Offline coverage: `tests/enterprise_pdf_rag/processing/test_index_text.py` (4);
  `tests/enterprise_pdf_rag/answers/fake_document.py` (an in-memory `MountedDocument` whose members
  resolve to real `ContextBlock`s, with `donut_chart` / `pending_chart` builders);
  `test_hybrid_search.py` +2 (the judge receives evidence blocks; the AIA mechanism reproduced —
  title-only leaves the chart at vector rank 13, the projection makes it lexical rank 1 and fuses
  it into the top 6); `test_answer_service.py` +4 (the new defaults; promotion; no promotion
  outside the window or for a pending chart; no extra read when a citable chart is already in the
  head); `test_document_catalog.py` (updated assertions plus a label-only regression);
  `test_document_catalog_aia_smoke.py` (189 members, projection branch selected by policy); the
  generic e2e pins the v3 policy string, and `test_chart_publication.py` /
  `test_chart_qa_bar_draft.py` assert the projected embedding text. Whole package: **802 passed**;
  mypy 452 files clean; ruff, the four structural checks and `check_doc_drift.py` pass.
- **The frozen gold set now exists** (ADR 0011 follow-up, 2026-09-20):
  `data/benchmarks/enterprise-pdf-rag/aia-2026-interim/nl-answers-gold-v1.json`. Six of its cases are
  the chart-recall regression this ADR fixed (plain, rephrased, title-only, Chinese, keywords-only
  and with rerank), so a relapse fails the run instead of being re-measured by hand. It still
  covers one document; that is not a quality claim about arbitrary PDFs.
