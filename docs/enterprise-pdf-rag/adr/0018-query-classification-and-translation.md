# ADR 0018: Query classification, channel selection and query translation

Status: Accepted, 2026-09-21. Corrects one assumption of
[ADR 0011](0011-document-catalog-and-verified-answer-chain.md) Decision 4 (every question is
answered by RRF fusion) and one of Decision 5 (one model call per answer). Extends
[ADR 0012](0012-chart-index-text-and-retrieval-seats.md) (index projection, 10 / 50 defaults)
and closes known gap (b) of [ADR 0013](0013-page-metadata-and-prefilters.md) on the retrieval
side. The measurement behind it is `data/validation/coverage-2026-09-21/` (`summary.md`,
`metrics.json`, `facts.jsonl`); the run is recorded in [`CLAUDE_HANDOFF.md`](../CLAUDE_HANDOFF.md).

## Context

ADR 0011 made hybrid retrieval unconditional: every question is scored by the vector channel
and by BM25 and the two rankings are fused with RRF. Nobody had measured whether fusion is
actually better than either channel on this corpus.

The 2026-09-21 coverage probe measured it. It extracted 280 atomic candidate facts from the
AIA first-twenty-pages release, kept the 125 whose value is genuinely present in an indexed
member's text, and asked each one in two phrasings — `"{label} {period}"` and
`"What was {label} in {period}?"` — against all three channels over the pinned snapshot
(190 members, vector channel via `POST /v1/documents/<sha>/search`, BM25 via
`ragspine.retrieval.lexical.retrieval.bm25_scores`, fusion via RRF *k* = 60). Recall is the
union over the two phrasings, before any reranking:

| channel | r@3 | r@5 | **r@10** | r@20 | MRR |
| --- | ---: | ---: | ---: | ---: | ---: |
| vector alone | 31.2% | 34.4% | 48.0% | 66.4% | 0.262 |
| **BM25 alone** | **54.4%** | **59.2%** | **74.4%** | 78.4% | **0.482** |
| RRF fusion | 46.4% | 51.2% | 70.4% | 78.4% | 0.381 |

Fusion is *worse than its own stronger channel* at every cut, and the gap widens the further
up the ranking you look — exactly where the ten prompt seats are. The mechanism is plain:
RRF weights both rankings equally, so for these questions the weaker channel dilutes the
stronger one. The union of the two channels' @10 hits is 95 of 125 while fusion returns 88,
so fusion is also losing hits both channels could supply.

Two limits of that evidence decide how far the change may go. Every probe query has the same
shape — a short label plus a period — so the probe says nothing about long, genuinely
narrative questions, where the vector channel is supposed to earn its place. And the probe is
English-only: it cannot measure a Chinese question at all.

The Chinese case is its own problem, and ADR 0013 already recorded it as known gap (b). The
lexical channel scores the document's own words, so a Chinese question over this English deck
scores nothing in it — recall there falls back to the vector channel alone, which the table
above shows is the weakest of the three. Worse, it does so invisibly: `泰国 1H26 VONB` still
matches the token `1h26`, so the lexical ranking is not empty, it is *noise*, and fusion
promotes that noise.

## Decision

1. **A question picks its channels, deterministically and without a model**
   (`answers/query_mode.py`, stdlib only). `classify_query(question, *, lexical_hits)`
   returns `bm25_only`, `rrf` or `vector_only`:
   a question the lexical channel cannot score at all (`lexical_hits == 0`) takes the vector
   channel alone, because fusing an empty ranking with one ranking is that one ranking;
   a question of at most `MAX_BM25_ONLY_TOKENS = 5` tokens, **or** one that carries a figure
   and at most `MAX_BM25_ONLY_CONTENT_WORDS = 1` content word, takes BM25 alone; everything
   else keeps RRF fusion. A *content word* is a token that is neither a small closed set of
   English function words nor a figure. Only a figure is tested, never a period: every period
   label `processing/periods.py` normalises (`1H26`, `FY24`, `Q1 2025`, `2026年上半年`) carries
   a digit, so the digit test subsumes the period test, and a test pins that.

2. **Both thresholds come from an offline sweep, and are the narrowest pair that reaches its
   ceiling.** Every (N, M) in 0…12 × 0…12 was replayed over the probe's per-fact channel
   ranks. The rule family's ceiling is exactly the pure-BM25 result, 93 of 125 at ten seats —
   **no rule in this family beats always-BM25 on this evidence**:

   | N | M | r@3 | r@5 | **r@10** | MRR | queries routed bm25 / rrf |
   | ---: | ---: | ---: | ---: | ---: | ---: | --- |
   | 0 | 0 | 46.4% | 51.2% | 70.4% | 0.382 | 18 / 232 |
   | 3 | 1 | 50.4% | 54.4% | 73.6% | 0.428 | 89 / 161 |
   | 4 | 1 | 54.4% | 57.6% | 73.6% | 0.469 | 110 / 140 |
   | **5** | **1** | **55.2%** | **58.4%** | **74.4%** | 0.476 | **119 / 131** |
   | 5 | 2 | 55.2% | 58.4% | 73.6% | 0.476 | 163 / 87 |
   | 7 | 1 | 56.0% | 59.2% | 74.4% | 0.476 | 182 / 68 |
   | 9 | 0 | 55.2% | 59.2% | 74.4% | 0.482 | 205 / 45 |
   | 12 | 12 | 54.4% | 59.2% | 74.4% | 0.482 | 248 / 2 |

   Among the ties at the ceiling we take the pair that **reroutes the fewest queries**, which
   is (5, 1): 119 of the probe's 250 queries, 48%. That is the point of the rule — the probe
   only ever asked label-and-period questions, so a larger N would extrapolate from evidence
   that does not exist onto the narrative questions the vector channel is there for. M = 0
   ties (5, 1) on every measured metric; M = 1 is preferred as the smallest threshold at
   which the figure clause does any work at all. The sweep script was a one-off and is not
   kept in the repo; it is reproducible from `facts.jsonl` and the rule above.

3. **A single-channel mode is a fusion with one empty ranking, and skips the channel it does
   not use** (`adapters/hybrid_search.py`). `HybridSearch.search(query, *, top_k, allowed,
   mode="auto")` returns a `SearchOutcome(mode, hits)`; `auto` classifies, any other value
   pins the channels. `bm25_only` never calls `document.search`, so the request makes **one
   embedding call fewer**. Scores stay comparable across modes because every mode ends in the
   same `fuse()`: a lone ranking scores `1/(k + rank)` exactly as RRF would score it against
   an empty partner, and `FusedHit` reports the unused channel's rank and score as `None`.
   `AnswerRequest.fusion_mode` (default `auto`) overrides the classifier;
   `AnswerResult.fusion_mode` and `AnswerEnvelope.fusion_mode` report what actually ran. The
   ADR 0012 guaranteed seat for a citable visual object is untouched — it reads the fused
   ranking, whatever produced it.

4. **A question outside the index's language is restated in it by one bounded, cached call**
   (`adapters/query_translation.py`). The trigger is *not* "the lexical channel returned
   nothing": it is that the question carries non-Latin letters **and its content words score
   nothing lexically**, where the probe query is the question minus its function words and
   minus its figures. Dropping figures is the whole point — every financial deck prints years,
   so `2026 上半年 分销渠道 占比` matches `2026` and would otherwise look scoreable while nothing
   else in it is in the index's vocabulary. `translate_query` then makes one
   `complete_text_json` call with task salt `query-translation-v1` (its own cache namespace,
   same budget and cache directory as synthesis), a strict two-field schema
   (`english_query`, `source_language`) and rules that forbid answering, forbid adding
   information, and require figures, period labels and proper names to survive verbatim.

5. **The translation reaches the two retrieval channels and nothing else.** Both channels
   score the English; the prompt, the period / region pre-filters (ADR 0013) and the
   prose-number gate keep the question the user actually asked. Using the translation for the
   vector channel as well as for BM25 is deliberate but *unmeasured*: the probe is
   English-only, so there is no evidence either way, and scoring one string in both channels
   keeps ADR 0012's "both channels score the same text" property. `SYSTEM_RULES` gains a rule
   6 — answer in the language of the question, but a claim's `text` is the evidence's own
   wording, copied verbatim and never translated — so verification is unchanged and still
   compares byte-for-byte against the document.

6. **No translation is never an error.** No budget, no transport, unusable output or
   `translate_query=False` all leave the original question in place, and such a question then
   takes the **vector channel alone** rather than fusing a ranking built from a stray year.
   The plan depends on the question alone and never on how much budget is left, so the same
   question always replays from the same cache entries. `AnswerResult.llm_live_calls` counts
   both calls honestly, and `AnswerEnvelope.query_translation` is `{english, source_language,
   cache_hit}` or `null`, so a reader can always tell which happened.

7. **The query embedder becomes a dependency of the requests that use it, not of the route.**
   A question routed to BM25 alone is answered on a mount with no query embedder, because it
   never reads the vector channel; a question that needs that channel is still 503 with no
   substitute. This is the rule the opt-in reranker already follows, and the answer returned
   is byte-identical to the one a fully configured deployment gives for that same question —
   it is not a degraded substitute for a different answer. `fusion_mode` in the envelope says
   which channels ran.

## Amendment 1 (2026-09-21): the pre-filters are derived from the translation too

Decision 5 above ("the translation reaches the two retrieval channels and nothing else") and
the rejected alternative below it are **superseded** for the pre-filters alone. A Chinese
question never names a value of the document's verified English region vocabulary, so no
region filter was derived at all and `k02-region-thailand-zh` abstained for a second, avoidable
reason. The translation is now also run through `derive_filters` and the result is **unioned**
with what the original question derived (original values first, new ones appended,
deduplicated); `applied` / `allowed` / `relaxed` are recomputed from the union. The ordering of
the pipeline is unchanged — pre-filters → channel choice / translation → retrieval → seats →
page window → prompt → verification — so the first derivation still happens before `_plan`,
whose lexical probe reads `allowed`, and the union lands between the plan and the search. An
**explicitly supplied** `filters` is never widened: the union runs only when the caller left
the filters to be derived. The prompt, the prose numeric gate and claim verification still see
the original question only, so nothing model-generated can reach an answer's wording; a filter
is a recall aid that is relaxed when it starves the ranking, which bounds what a bad
translation can cost. The two frozen gold cases this touches (`k01` / `k02`) are re-decided
with a live re-test, not here.

Region matching itself also stopped being equality in the same change: a filter value matches
a page's region value when every word of the filter appears as a whole word in it (`Thailand`
also matches `AIA Thailand`, `Hong Kong` also matches `Hong Kong Special Administrative
Region`), except that a value which *excludes* a place (`ex-Thailand`, `Asia ex-Japan`) never
matches it — that is the opposite claim, not a narrower one.

## Rejected alternatives

- **Dropping the vector channel entirely.** The probe reaches its ceiling at always-BM25, so
  on *this* evidence the vector channel adds nothing. But every probe query is a short label
  plus a period; the deck's own narrative questions are unrepresented, and the vector channel
  is the only one that can answer a question that shares no words with the page. Removing it
  would be extrapolating a measurement far past what it measured.
- **Tuning the RRF weights instead of choosing a channel.** A weight is one more number to
  fit on 125 facts, and at the extreme it degenerates into this decision anyway. Choosing a
  channel is also legible in the envelope; a weight would not be.
- **Classifying the query with a model.** It would cost a call on every question to decide
  something two token counts already decide, and it would make retrieval non-deterministic.
- **Translating on the strict `lexical_hits == 0` test.** It reads well and is wrong: it never
  fires for the very cases it targets, because a Chinese question that names `1H26` or `2026`
  matches that token. The content-word probe is the version that works.
- **Deriving the period / region pre-filters from the translation too.** ~~That would plausibly
  close ADR 0013's known gap (b) — `泰国` → `Thailand` — but it changes an abstention into an
  answer on a frozen gold case, and it makes a verified-vocabulary filter depend on model
  output. Left as a follow-up to be decided with evidence.~~ **Superseded by Amendment 1**: it
  is now the behaviour.
- **Reserving the last budget call for synthesis so a translation can never starve it.** It
  makes the retrieval plan depend on mutable budget state, so the same question stops
  replaying from the same cache entry. A translated question simply costs two live calls.
- **Exposing `fusion_mode` on `RagChatRequest`.** Like `top_k` and `channel_limit` in
  ADR 0012, channel selection is an engine decision, not a caller's. It stays on
  `AnswerRequest` for tests and future tools, and read-only in the envelope.

## Consequences and follow-ups

- **Offline estimate, before / after** (125 indexed facts, two phrasings, union, pre-rerank):
  recall@10 **70.4% → 74.4%**, recall@3 **46.4% → 55.2%**, recall@5 **51.2% → 58.4%**,
  MRR **0.381 → 0.476**; recall@20 is unchanged at 78.4%. 48% of the probe's queries are
  rerouted; the rest keep fusion byte-for-byte. The gain is 5 facts of 125 at ten seats — thin
  on its own, but the same direction and a much larger margin at every tighter cut, which is
  where the prompt seats are.
- **Not measured, deliberately.** The Chinese path has no offline number: the probe is
  English-only, and the frozen NL gold set's three Chinese cases (`p06`, `p11`, `k02`) cannot
  be replayed here — the local processing store has moved to `4f6ce62fe0b7` while the gold is
  pinned to `231c904c843e`, so the whole offline gold group skips. Re-freezing the gold
  against the current release, then re-running both runners, is the first follow-up.
- **The offline gold replay pins `fusion_mode="rrf"`.** It declares its vector channel (it
  returns exactly the members the scripted claims cite), so a BM25-only route would remove the
  guarantee it is built on. It guards seats, budget, strict schema, field-level verification,
  the prose gate and abstention — not recall — and channel routing is guarded instead by
  `answers/test_query_mode.py` and `adapters/test_hybrid_search.py` and measured by the live
  runner.
- **Budget.** A non-English question now costs two live calls, so the process-wide
  `APP_ANSWER_MAX_LIVE_CALLS` (200) buys proportionally fewer of them. A repeat of the same
  question costs none: both calls replay from the immutable cache.
- **`k02-region-thailand-zh` is still a known gap, for a smaller reason.** Retrieval is now
  translated, but the region pre-filter is still derived from the Chinese question and still
  matches nothing in the document's verified English vocabulary. The ADR 0013 gap (b) wording
  should be narrowed to the filter when that case is re-frozen. *(Addressed by Amendment 1;
  both Thailand cases are re-frozen against a live re-test.)*
- `answers/` may import no SDK, so `query_mode.tokenize_query` restates the lexical channel's
  tokenizer instead of importing it; `test_query_mode` pins the two to identical output over
  mixed English / CJK input, because a token budget is meaningless unless it counts the tokens
  BM25 actually scores.
- Offline coverage: `answers/test_query_mode.py` (37), `adapters/test_query_translation.py`
  (16), `adapters/test_hybrid_search.py` (+6 routing), `answers/test_answer_service.py`
  (+8 routing / translation), `adapters/test_chat_http.py` (+1 embedder-free answer),
  `adapters/test_strict_response_schemas.py` (the new DTO). Whole package: **1106 passed**,
  25 skipped (the gold group, release drift), 1 failing for an unrelated pre-existing reason
  (`test_document_catalog_aia_smoke`, "Unsupported chart qualification scope", already red on
  `main`). `docs/enterprise-pdf-rag/schemas/rag-chat-v1.json` regenerated with two optional
  fields added and nothing removed.
