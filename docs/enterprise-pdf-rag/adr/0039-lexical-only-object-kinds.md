# ADR 0039: Lexical-only object kinds — tables and charts may skip the vector channel

Status: Accepted as an **opt-in** switch, 2026-10-09. Default off (empty set): every snapshot,
index version, cache entry and ranking is byte for byte what it was. Measured below; the
recommended value (`table,chart`) is **not** turned on in any preset or notebook, because the
measurement shows a fusion penalty that has to be addressed first.

## Context

The index stage of one annual report embeds about 3000 index texts, 16 per request, and that
stage is serialized behind the pipeline's global lock; it costs 2–4 minutes per document. Most
of those texts are tables (in lite, a long verbatim-rows table embeds one vector per row unit,
ADR 0027 Amendment 1) and charts. Dropping the model calls altogether and embedding text only
was rejected: annual-report answers mostly live in tables, and a table that vanishes from
retrieval cannot be answered. The compromise: let chosen kinds keep only the lexical (BM25)
channel. They stay members — resolvable, citable, verified, in their page window — but hold no
vector, so the embedding stage never sees them.

## Decision

- **Switch.** `IndexTextOptions.lexical_only_kinds: frozenset[ObjectKind]` (default empty), set
  by `IngestPlan.lexical_only_kinds` / `ingest_plan(lexical_only=...)` and by
  `run_folder_pipeline(lexical_only_kinds=...)`, whose `None` reads the setting
  `APP_INDEX_LEXICAL_ONLY_KINDS` (`Settings.index_lexical_only_kinds`, comma separated, any case;
  `ingest_mode.lexical_only_kinds` parses it, an unknown name is a `ValueError` naming the
  setting). `Text` is refused: a Text member without a vector already means a running line (ADR
  0028 Amendment 1), and the narrative channel is what the vector channel is for. No preset
  sets it. The CLI `index` command keeps the default layout.
- **Index version = fingerprint.** The kinds are named in the index version
  (`immutable-cosine-unit-index-v1:…+lexical-only-v1=Chart,Table`), which is part of the
  retrieval plan and therefore of the snapshot id. `_reusable_index` compares that version, so a
  release indexed under another setting is rebuilt, never reused with stale vectors; switching
  back republishes the earlier snapshot with zero embedding requests (every other text replays
  its cached vector).
- **Build.** `ProcessingRetrieval.build` validates every member as before, then embeds none of a
  lexical-only kind: the member gets the vector-less `RetrievalUnitEmbeddings` artifact a
  running line gets and no index entry. If nothing else in the document would carry a vector
  dimension, everything is embedded after all (the existing running-line fallback).
  `DraftIndex.lexical_only_members` counts them; `row_unit_tables` / `row_units` count row-unit
  tables whichever channel scores them. `_check_unit_index` admits a vector-less member only
  under `drop_running_lines` or for a lexical-only kind.
- **What BM25 scores.** A lexical-only table keeps its index text and, under row units, its row
  units (`member_units` returns them without the vector-count check). A lexical-only **chart**
  scores its PDF text layer instead of its description / IR projection
  (`processing/lexical/chart_text_layer.py`, called from `processing_retrieval.lexical_member_text`
  when the catalog builds `MemberText`): every non-blank text span of the pinned source page whose
  centre lies inside the chart's qualified rectangle, grouped into printed lines top to bottom and
  left to right, under the page's contextual header. Each line keeps its span ids (the locator).
  Nothing comes from a model, no number is rendered from a `Decimal`, nothing is inferred from bar
  heights, colours or axes (ADR 0009): `1,168` keeps its separator, a value the PDF does not print
  is absent. A chart printing no text scores its header alone. When the fallback above gave a
  lexical-only chart a vector, it scores the same string it was embedded with, as before.
- **Unchanged.** Hydration, claim verification, citations, page windows and the answer prompt
  read the same evidence; `query_mode` routing is untouched (a vector-only question cannot reach a
  lexical-only member — by construction).

## Measurement (offline; counts only)

Vectors per index build, mock embedder counting cache misses:

| Corpus | Default | `table,chart` lexical-only |
|---|---:|---:|
| Synthetic 13-page lite report (32-row statement as row units, fixture) | 45 | 13 (−71%) |
| AIA interim, physical pages 1–20, full-mode release (190 members: 163 Text, 11 List, 6 Group, 9 Chart, 1 Diagram, 0 Table) | 190 | 181 (−4.7%) |
| Small local ingests under `data/ingestion` (3 pages, 1 table each) | 8 / 3 | 7 / 2 |

The "more than half" expectation holds only where tables are split into row units (lite,
ADR 0027). The local AIA release has no verified table and few charts, so it barely moves; the
71-page AIA sample in lite was not available offline and was not measured.

Retrieval proxy on the same AIA pages 1–20 release, NL gold set v1 (16 cases with a required
page; seat of the first member of the expected page and kind, channel limit 50). Both arms use
the offline token-hash embedder, so the vector channel here is itself near-lexical; the real
Qwen3 channel was not available:

| Mode | Arm | r@1 | r@3 | r@10 | r@50 | MRR |
|---|---|---:|---:|---:|---:|---:|
| auto (ADR 0018 routing) | default | 7 | 8 | 9 | 13 | 0.486 |
| auto | lexical-only | 3 | 3 | 3 | 13 | 0.210 |
| rrf | default | 7 | 8 | 9 | 13 | 0.486 |
| rrf | lexical-only | 2 | 2 | 2 | 13 | 0.153 |
| bm25_only | default | 6 | 6 | 8 | 13 | 0.409 |
| bm25_only | lexical-only | 6 | 6 | 8 | 14 | 0.410 |

Every loss is a chart question that `auto` routes to fusion (the p.17 donut: seat 1 → 24–29,
p.13 / p.14 filters: 8 → 35, 1 → 29). BM25 alone ranks the same chart at the same seat in both
arms, and an ablation that kept the IR projection as the lexical text lost exactly the same
seats, so the **text-layer projection costs nothing** (it gained the Chinese donut question a
BM25 seat, 41 vs none). The cause is fusion: RRF adds one term per channel, so a member present in
one channel only (BM25 seat 1: 1/61) sorts below any member both channels rank moderately (seat
10 in both: 2/70). Under fusion a lexical-only object is structurally demoted. The QA eval
ratchet of `scripts/ci.sh` (ACME, `scripts/run_qa_eval.py`) does not read this index and is
unchanged in both settings.

## Consequences

- Off by default: nothing changes. On, the index stage sends far fewer texts where tables are
  row units, and BM25-routed questions (short label + period, ADR 0018) keep their seats.
- **Not yet recommended for production** while the default `auto` routing sends chart / table
  questions to fusion. Follow-ups, each its own decision: let RRF score a lexical-only member's
  missing vector term as its BM25 term (channel-aware fusion), or route a question whose best
  BM25 hits are lexical-only members to `bm25_only`; re-measure with the real embedder and a
  lite 71-page release before turning the recommended value on.

## Amendment 1 (2026-10-09): channel-aware fusion for lexical-only members

**Decision.** `MemberText.lexical_only` marks a member its snapshot indexed lexical-only (its kind
is in the index version's `lexical_only_kinds` and it holds no vector —
`processing_retrieval.is_lexical_only`, set by the catalog). `HybridSearch` collects those ids from
its lexical index and passes them to `fuse(..., lexical_only=...)`. When the fusion has a vector
ranking, a flagged member that BM25 ranked and the vector channel did not earns its BM25 term a
second time in place of the vector term it can never earn: `2/(k + bm25 rank)`, as if both
channels agreed. Nothing else moves:

- **Default is byte for byte.** No member is flagged unless `APP_INDEX_LEXICAL_ONLY_KINDS` is set
  (and a snapshot indexed under it), and with an empty set `fuse` is plain RRF; pinned by
  `test_an_empty_lexical_only_set_fuses_exactly_as_before`, and the default arm of the gold
  measurement below reproduces every seat and every resolved mode of the original run.
- **Only fusion.** With no vector ranking (`bm25_only`, or a vector channel that returned
  nothing) every member is single-channel already, so no member is lifted and `bm25_only` ranks
  exactly as before; `vector_only` has no BM25 term to copy. Non-flagged members score exactly
  their RRF terms. `FusedHit.vector_rank` / `vector_score` stay `None` — no rank is invented.
- **Ordering only.** Hydration, claim verification, the anti-fabrication checks and the
  RESTRICTED exit filters read the same evidence; a lifted member is still resolved and verified
  from its own stored evidence like any other hit.

**Why this and not routing.** The alternative — route a question whose best BM25 hits are
lexical-only to `bm25_only` — changes ADR 0018's classifier and drops the vector channel for
every member of that question; on this set its seats are the `bm25_only` column below (p.17
donut 7, period filter 19), worse than fusion with the amendment (3, 14). The amendment is one
additive term behind an explicit flag, and the two are not stacked.

**Measurement** (same release, gold set, offline token-hash embedder and channel limit as above;
seat of the first member of the expected page and kind):

| Mode | Arm | r@1 | r@3 | r@10 | r@50 | MRR |
|---|---|---:|---:|---:|---:|---:|
| auto | default | 7 | 8 | 9 | 13 | 0.486 |
| auto | lexical-only, plain RRF | 3 | 3 | 3 | 13 | 0.210 |
| auto | lexical-only, Amendment 1 | 6 | 8 | 9 | 14 | 0.439 |
| rrf | default | 7 | 8 | 9 | 13 | 0.486 |
| rrf | lexical-only, plain RRF | 2 | 2 | 2 | 13 | 0.153 |
| rrf | lexical-only, Amendment 1 | 6 | 8 | 9 | 14 | 0.439 |
| bm25_only | default | 6 | 6 | 8 | 13 | 0.409 |
| bm25_only | lexical-only (either) | 6 | 6 | 8 | 14 | 0.410 |

The p.17 donut questions return to seat 1 (agency share, both shares, rerank: 24–27 → 1), and the
Chinese donut question gains a fused seat (none → 7). Still below the default: the three
"Agency share of VONB" phrasings (p07 / p14: 1 → 3, p13: 8 → 14) and two diagram questions at the
tail (p10: 23 → 24, p11: 43 → 48). In the default arm the vector channel ranked the p.17 donut
**first** (its IR projection reads "Agency VONB 72%") while BM25 ranked it 7th; lexical-only, that
vector signal is gone, so the best fusion can do is BM25 seat 7 counted twice (2/67), and a p.10
lexical-only chart that BM25 seats 1st for the same words now sorts above it at 2/61. That is
information the vector channel no longer has, not a fusion penalty, and no rank-based rule can
recover it.

**Consequence.** r@10 matches the default in all three modes, but `auto` / `rrf` MRR is 0.439
against 0.486, so by the bar "no mode below the baseline in r@10 or MRR" `table,chart` is **still
not the recommended value**; `.env.example` says so and no preset or notebook turns it on. Re-measure
with the real embedder (where the vector channel is semantic rather than near-lexical) and a lite
71-page release before deciding again.
