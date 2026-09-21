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
   ~~a question of at most `MAX_BM25_ONLY_TOKENS = 5` tokens~~, **or** one that carries a figure
   and at most `MAX_BM25_ONLY_CONTENT_WORDS = 1` content word, takes BM25 alone; everything
   else keeps RRF fusion. A *content word* is a token that is neither a small closed set of
   English function words nor a figure. Only a figure is tested, never a period: every period
   label `processing/periods.py` normalises (`1H26`, `FY24`, `Q1 2025`, `2026年上半年`) carries
   a digit, so the digit test subsumes the period test, and a test pins that.
   **Superseded by Amendment 2** for the struck clause: a short question has to fit
   `MAX_BM25_ONLY_TOKENS = 5` tokens **and** `MAX_BM25_ONLY_SHORT_CONTENT_WORDS = 2` content
   words. The figure clause is untouched.

2. **Both thresholds come from an offline sweep, and ~~are the narrowest pair that reaches
   its ceiling~~.** Every (N, M) in 0…12 × 0…12 was replayed over the probe's per-fact channel
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

   ~~Among the ties at the ceiling we take the pair that **reroutes the fewest queries**, which
   is (5, 1): 119 of the probe's 250 queries, 48%.~~ That is the point of the rule — the probe
   only ever asked label-and-period questions, so a larger N would extrapolate from evidence
   that does not exist onto the narrative questions the vector channel is there for. M = 0
   ties (5, 1) on every measured metric; M = 1 is preferred as the smallest threshold at
   which the figure clause does any work at all. The sweep script was a one-off and is not
   kept in the repo; it is reproducible from `facts.jsonl` and the rule above.

   **Superseded by Amendment 2** for the choice, not for the measurements: every number in
   the table above was reproduced digit for digit and still holds, but the bolded (5, 1) row
   is no longer the shipped rule, and the shipped rule no longer sits at this family's
   ceiling — it recalls 92 of 125 at ten seats, one fact below always-BM25. Note that the
   (5, 2) row here is *not* that rule: M is the figure clause's budget, so (5, 2) still
   admits any five-token question and reroutes 163 of 250 queries, where the shipped rule
   reroutes 100. Amendment 2 sweeps the new budget on its own axis.

3. **A single-channel mode is a fusion with one empty ranking, and skips the channel it does
   not use** (`adapters/hybrid_search.py`). `HybridSearch.search(query, *, top_k, allowed,
   mode="auto")` returns a `SearchOutcome(mode, hits)`; `auto` classifies, any other value
   pins the channels. `bm25_only` never calls `document.search`, so the request makes **one
   embedding call fewer**. Scores stay comparable across modes because every mode ends in the
   same `fuse()`: a lone ranking scores `1/(k + rank)` exactly as RRF would score it against
   an empty partner, and `FusedHit` reports the unused channel's rank and score as `None`.
   `AnswerRequest.fusion_mode` (default `auto`) overrides the classifier;
   `AnswerResult.fusion_mode` and `AnswerEnvelope.fusion_mode` report what actually ran. The
   ~~ADR 0012 guaranteed seat for a citable visual object is untouched — it reads the fused
   ranking, whatever produced it.~~ **Superseded by Amendment 3**: the seat's promotion window
   now also reads each channel's own rank, because fusion is exactly what buries the object
   the seat exists for.

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

5. **The translation reaches the two retrieval channels and nothing else.** ~~Both channels
   score the English~~; the prompt, the period / region pre-filters (ADR 0013) and the
   prose-number gate keep the question the user actually asked. ~~Using the translation for the
   vector channel as well as for BM25 is deliberate but *unmeasured*: the probe is
   English-only, so there is no evidence either way, and scoring one string in both channels
   keeps ADR 0012's "both channels score the same text" property.~~ **Superseded by
   Amendment 3**: it has since been measured against the real embedder, and only the lexical
   channel reads the restatement. `SYSTEM_RULES` gains a rule
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

## Amendment 2 (2026-09-21): the short clause spends a content-word budget too

Decision 1's short clause — "a question of at most `MAX_BM25_ONLY_TOKENS = 5` tokens […] takes
BM25 alone" — and the (N, M) choice of Decision 2 that set it are **superseded**. A short
question now has to fit under two budgets at once: at most `MAX_BM25_ONLY_TOKENS = 5` tokens
**and** at most `MAX_BM25_ONLY_SHORT_CONTENT_WORDS = 2` content words. The separate figure
clause is untouched and still spends the tighter `MAX_BM25_ONLY_CONTENT_WORDS = 1`, because
there the token count is already over.

A token count was only ever a proxy for the shape Decision 2 measured, and it is a leaky one.
All 250 probe queries are a short label plus a period — one or two content words — so five
tokens reads as "one label" *there*. `Agency share of VONB 1H26` is five tokens as well, but
three of them carry content: it is a phrase, not a label. Routed to BM25 alone it drops the
chart it needs from seat 7 under fusion to seat 12, and the same question without its period
(`Agency share of VONB`, four tokens, three content words) loses the seat altogether.

### The sweep, reproduced and extended

The probe's per-fact channel ranks were replayed first to reproduce this ADR's own two tables
digit for digit — vector 48.0% / BM25 74.4% (93 of 125) / RRF 70.4% at ten seats, and the
shipped (N = 5, M = 1) row at 74.4%, MRR 0.476, 119 queries routed to BM25 against 131 to
fusion — and then swept over the new content-word budget K, with N = 5 and M = 1 held fixed:

| K | r@3 | r@5 | **r@10** | r@20 | MRR | queries routed bm25 / rrf |
| ---: | ---: | ---: | ---: | ---: | ---: | --- |
| 0 | 48.8% | 53.6% | 70.4% (88) | 79.2% | 0.417 | 58 / 192 |
| 1 | 48.8% | 53.6% | 70.4% (88) | 79.2% | 0.417 | 58 / 192 |
| **2** | **52.8%** | **56.8%** | **73.6% (92)** | **79.2%** | **0.453** | **100 / 150** |
| 3 | 54.4% | 57.6% | 73.6% (92) | 78.4% | 0.469 | 113 / 137 |
| 4 | 55.2% | 58.4% | 74.4% (93) | 78.4% | 0.476 | 118 / 132 |
| 5 | 55.2% | 58.4% | 74.4% (93) | 78.4% | 0.476 | 119 / 131 |

K = 5 is the old rule restated: five tokens cannot carry six content words, so the budget never
binds and the row is (5, 1) again. **K = 2 is taken, and it costs recall on the probe**:
recall@10 **74.4% → 73.6%**, one fact of 125. recall@20 moves the other way, **78.4% → 79.2%**,
one fact back. Over the full (N, K) grid at M = 1, K = 2 holds 73.6% for every N in 3…5 and
falls to 72.8% from N = 6 up, so N = 5 is still the right N and nothing in the grid recovers
74.4% at K = 2.

The fact that is lost is `p09-a424f3446bd4` (`+25%`), asked as
`Strong Underlying Growth Drivers 1H26` — five tokens, four content words. BM25 ranks it
**first** and fusion eleventh (the vector channel has it at 37 on the other phrasing and
nowhere on this one). It is itself a five-token *phrase* rather than a label, exactly the shape
this amendment reroutes; it kept its seat only because BM25 happened to hit it squarely.

### Why one measured fact is traded for three unmeasurable ones

K = 4 would hold 74.4%, and it is the wrong answer here: `Agency share of VONB 1H26` carries
three content words, so only K ≤ 2 sends it back to fusion. The choice is therefore explicit —
**one probe fact against the frozen gold cases `p07`, `p13` and `p14`**, the two phrasings of
`Agency share of VONB` that the old rule sent to BM25 alone and the new one returns to fusion.
The other frozen questions do not move: `p03`, `p04` and `p08` are long enough that they were
on fusion already, and `p05` (`1H26 Distribution Mix`) and `k01` (`Thailand 1H26 VONB`) are
genuine two-content-word labels that stay on BM25 alone.

The trade is taken because the probe *cannot see the other side of it*. Its 250 queries are
label-and-period by construction; not one of them is a phrase, so the shape this amendment
exists for is unrepresented in the only instrument that can score it. Eight tenths of a
percentage point is a quantity the probe measures; three gold cases asking a question the probe
never asks is a quantity it has no power to measure at all. We take the loss we can name over
the loss we cannot.

The sweep script is a one-off and is not kept in the repo; a copy sits beside the evidence at
`data/validation/generic-chat-2026-09-21/gold-fix/sweep_baseline.py` (`data/` is git-ignored,
so that is a local record only). It is reproducible from `facts.jsonl` and the rule above,
exactly as the Decision 2 sweep is.

## Amendment 3 (2026-09-21): a translation feeds the lexical channel only

Decision 5 above — "both channels score the English" — is **superseded**. It said of itself
that using the translation for the vector channel as well as for BM25 was "deliberate but
*unmeasured*". It has since been measured against the real embedder, and it is wrong.

`HybridSearch.search` takes an optional `lexical_query: str | None`. Left `None` it is the old
behaviour byte for byte: one string scores both channels. Given — which is exactly when a
question was translated — **BM25 and `classify_query` score the restatement, while the vector
channel and the rerank judge score the question as asked**. `AnswerService._plan` carries this
as a fourth field on `_QueryPlan`, whose `query` is now always the user's own question.

**The measurement.** For `p11-diagram-zh`, the corpus's only Diagram (member
`a9b9c1a4d6ae…`, p.6) ranks **12th** on the vector channel for the Chinese question and
**32nd** for its English restatement (Qwen3-Embedding-4B, 2560 dimensions, over the pinned
snapshot). The difference is not language but wording: the restatement writes `agents'` /
`agents` where the index prints `Agency`. A token-matching channel cannot score a question in a
language it has never seen, which is the entire reason a restatement exists; a dense channel
reads the question *as language*, so handing it someone else's paraphrase only trades the
asker's own words away. "Put the question in the index's language" is right for one channel and
wrong for the other.

**Amendment 1 is not superseded by this.** The period / region pre-filters are still derived
from the question *and* from the translation and unioned, for the reason Amendment 1 gives. The
prompt, the prose numeric gate and claim verification still see the original question only.

### Fixing the channels was not enough: the guaranteed seat reads channel ranks

With the vector channel back on the question, `p11-diagram-zh` still failed. The real
translation, `What are the three stages of technology investment by agents?`, shares **no
content word at all** with that member — `agents` is not `agency`, and neither `technology` nor
`investment` appears in its text. So it never entered the lexical channel's
`channel_limit = 50` cut at all (a diagnostic probe with a larger limit puts it around 59th),
and the envelope reports its `lexical_rank` as **`None`**: it carries no lexical contribution
into the fusion whatever. Its fused score is then exactly the single-channel value
`1 / (60 + 12) = 0.01389`, and it fell outside the promotion window of the guaranteed visual
seat, which read `ranked[top_k : 2 * top_k]`.

The recorded prompt shows exactly what that cost: the page_context block **printed the
diagram's text**, but `SYSTEM_RULES` rule 6 forbids citing page_context, so the model could see
the answer and had no citable route to it. It returned `not_in_context`. **The model behaved
correctly; the defect was entirely in the seats.**

Run 1 and run 2 happen to be a clean controlled experiment, because both drew a byte-identical
translation. Their envelopes' `member_ranks` agree on the first **nine** seats, member for
member and fused score for fused score (`a00e8fefd645` 0.03279, `cf096c5aeb90` 0.02921,
`df19e3120212` 0.02837, `8bae5867c514` 0.02788, `76be7fbab1f5` 0.02688, `107d42f42b66`
0.02669, `2f50964777ac` 0.02666, `dfd8f0d6399f` 0.02629, `19658c9322f2` 0.02617). The entire
difference is the tenth seat:

| run | tenth seat | `vector_rank` | `lexical_rank` | `fused_score` | `p11` |
| --- | --- | ---: | ---: | ---: | --- |
| run 1 | `1364d4b9f1ed` | 4 | 39 | 0.02573 | `abstained` / `not_in_context` |
| run 2 | `a9b9c1a4d6ae` (the Diagram) | 12 | **`None`** | 0.01389 | `answered`, 3 claims |

Same question, same translation, same fusion — only the seat rule changed.

So `select_context` (`adapters/answer_service.py`) widens the promotion window: a candidate
qualifies if it falls in the next k fused positions **or** if either channel ranked it inside
`2 * top_k` on that channel's own ranking (`_within_a_channel`). The call site asks
`search` for `2 * request.channel_limit` instead of `2 * request.top_k` — both channels
together rank at most that many members, so it is the exact upper bound on the fused set, and
`select_context` is handed the whole fused ordering rather than its head.

The reason to look at channel ranks is structural, and it is the same reason the seat exists at
all (ADR 0012, generalised by [ADR 0015](0015-diagram-and-formula-retrievable.md)):
**reciprocal rank fusion penalises an object only one channel can see, and it does so by
construction rather than by accident.** At this deployment's constants — RRF *k* = 60 and
`channel_limit` = 50 — a hit only one channel ranks scores at most `1 / (60 + 1) = 0.01639`,
while a hit both channels rank scores at least `2 / (60 + 50) = 0.01818`. The best possible
single-channel hit therefore sorts below the *worst possible* two-channel hit, whatever its
rank on the channel that sees it. The measured ranking matches: every one of run 1's ten seats
had both channels behind it and the lowest of them scored 0.02573, against the Diagram's
0.01389. An object one channel sees well while the other cannot score it at all is precisely
what the guaranteed seat was built for, so judging the window by fused position asks the seat
to survive the very ordering it exists to correct.

Everything else about the seat is unchanged, and still deterministic: it fires only when a
visual kind has no citable block in the head at all; at most one member per kind is promoted;
only the last seat not already holding a citable visual object is given up; a pending or
label-only object never qualifies; candidates are examined in fused order, so the fused window
is always offered first; and nothing outside both windows is promoted.

**This was a regression, not an inherent gap.** `p11` answered in all four real runs that
preceded the translation feature (`093e976`), promoted into a seat from fused position 11 / 12
by exactly this mechanism. It was not caused by `fix/mount-latency`. Four cold runs pin the
repair to the seat rather than to the model:

| run | Diagram `a9b9c1a4d6ae…` seated | `p11` result |
| --- | --- | --- |
| run 1 (before this fix) | no | `abstained` / `not_in_context`, 0 claims |
| run 2 | yes | `answered`, 3 claims (`nodes.node-{foundation,growth,intelligence}.label`) |
| run 3 | yes | `answered`, 3 claims |
| run 4 | yes | `answered`, 2 claims — the model left out `nodes.node-intelligence.label` from a member it was holding |

### A translated question is no longer deterministic in retrieval

This amendment has one consequence worth stating plainly, because it changes what a single run
proves. The restatement is now **the string the lexical channel scores**, and (by Amendment 1)
one of the two strings the pre-filters are derived from — so for a translated question, *part
of the retrieval input is model output*. Retrieval itself contains no randomness; the
non-determinism is inherited from the translation call.

Three cold runs of the same 22 gold questions over the same pinned release, on identical code,
make the split exact: **20 of 22 cases returned byte-identical `member_ids`, in identical
order**. The two that did not are precisely the two questions that are translated,
`p06-donut-zh` and `p11-diagram-zh`. `k02-region-thailand-zh` is the control: it is Chinese but
Decision 4's content-word probe finds `VONB` scoreable and never translates it, and its
`member_ids` are identical across all three runs.

`p11` shows the mechanism directly. Its translation was drawn fresh four times and came back
three different ways — `What are the three stages of technology investment by agents?`
(runs 1 and 2), `…of investment in agent technology?` (run 3), `…of agents' investment in
technology?` (run 4) — and the retrieved set moved with it. None of the three contains
`Agency`, so in each of the three runs that seated the Diagram its `lexical_rank` is `None` and
its fused score is exactly the same single-channel 0.01389. The repair is stable
*across* the translation's variance rather than lucky within it.

That is why `p06`'s `filters_expected` must enumerate its observed filter sets rather than
freeze one (`any_of` in the gold schema), and why no single run may be read as the retrieval
behaviour of a translated question.

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
  something three counts over the question's own tokens already decide, and it would make
  retrieval non-deterministic.
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

- **Offline estimate, before / after** (125 indexed facts, two phrasings, union, pre-rerank),
  as shipped after Amendment 2: recall@10 **70.4% → 73.6%**, recall@3 **46.4% → 52.8%**,
  recall@5 **51.2% → 56.8%**, recall@20 **78.4% → 79.2%**, MRR **0.381 → 0.453**. 40% of the
  probe's queries are rerouted (100 of 250); the rest keep fusion byte-for-byte. The gain is 4
  facts of 125 at ten seats — thin on its own, but the same direction and a much larger margin
  at every tighter cut, which is where the prompt seats are. *(Before Amendment 2 this bullet
  read 74.4% / 55.2% / 58.4% / unchanged 78.4% / 0.476 at 48% rerouted; that rule is the
  bolded (5, 1) row of Decision 2 and is no longer shipped.)*
- **The Chinese path has no offline number, and now has a live one.** The probe is
  English-only, and the offline gold replay cannot exercise the Chinese cases. They are
  measured instead by the live runner against the pinned release `22127d0fad13` /
  `42939d6a4e87`; four cold runs are recorded under
  `data/validation/generic-chat-2026-09-21/gold-fix/`. *(This bullet previously said the
  offline gold group skipped because the store had moved to `4f6ce62fe0b7` while the gold was
  pinned to `231c904c843e`; the gold has since been re-pinned and that follow-up is done.)*
- **The live gold set, four cold runs.** On `main` before this work the set scored 13 passed /
  7 failed / 2 known-gap-moved. After the amendments it scores **19 passed / 1 failed / 2
  known-gap-moved** (run 1, run 3 and run 4; run 2 scored 18 / 2 / 2). Each run fails one case,
  and never the same one for the same reason: run 1's `p11` was the seat bug Amendment 3 fixes;
  run 2 failed `p05` (the model truncated a member id to 57 hex characters and
  `model_output_invalid: unknown member` rejected it) and `p06` (a differently worded
  translation derived a different period union, since fixed in the gold with `any_of`); run 3
  failed `p13` (`model_declined`, detail `ambiguous`, on byte-identical retrieval to the two
  runs that passed it); run 4 failed `p11` by writing two of the diagram's three nodes. Only
  the first is a defect in this code. **A single run of this set is not a deterministic
  result** — read it across runs.
- **The offline gold replay pins `fusion_mode="rrf"`.** It declares its vector channel (it
  returns exactly the members the scripted claims cite), so a BM25-only route would remove the
  guarantee it is built on. It guards seats, budget, strict schema, field-level verification,
  the prose gate and abstention — not recall — and channel routing is guarded instead by
  `answers/test_query_mode.py` and `adapters/test_hybrid_search.py` and measured by the live
  runner.
- **Budget.** A non-English question now costs two live calls, so the process-wide
  `APP_ANSWER_MAX_LIVE_CALLS` (200) buys proportionally fewer of them. A repeat of the same
  question costs none: both calls replay from the immutable cache.
- **`k01` / `k02` are still known gaps, and the gap got worse, not smaller.** Retrieval now
  reaches the page — Amendment 1's whole-word region matching lets `Thailand` match the
  verified value `AIA Thailand` — so both cases moved from a safe refusal to `answered` **with
  a wrong number**. p.13 prints three `VONB ($m)` charts side by side: AIA Thailand **514**
  (`a05e27202ea4…`), AIA Singapore **294** (`36f5b652e8e0…`), AIA Malaysia **232**
  (`3e0a86925a4e…`). Region metadata is page-level, so every member on that page carries all
  four region values and no filter can tell the columns apart; the model is left to guess the
  binding from text that merely co-occurs on the page. Across four cold runs, eight answers,
  it guessed **$294m five times and $232m three times, and $514m not once**. The provenance is
  real — `294` and `$m` are verbatim observations with true bboxes — which makes this strictly
  worse than the refusal the case used to record. The gold expectation therefore stays
  `abstained` / `known_gap: true` and **must not be frozen as `answered`**. The fix is a
  member-level, in-column region binding: the three charts' bboxes are cleanly separable on the
  x axis against the three country headings, so the binding is derivable, and the chart IR
  already admits the weakness in its own confidence note, `the category-to-value association is
  unproven`. Not implemented. *(The earlier wording here — that `k02` was a smaller gap because
  the region filter matched nothing, and that Amendment 1 had addressed it — is no longer
  true.)*
- **Follow-up: the completion's sampling is not pinned.** `adapters/json_completion.py` sets no
  `temperature`, `top_p` or `seed`, so synthesis runs on the provider's defaults, and the
  roughly one flaky case per live run above is the model, not the engine. Pinning them would
  make the gold set repeatable, and would also invalidate the whole completion cache and force
  a full re-run of the live set — a call to be made deliberately, not in passing. Not done.
- `answers/` may import no SDK, so `query_mode.tokenize_query` restates the lexical channel's
  tokenizer instead of importing it; `test_query_mode` pins the two to identical output over
  mixed English / CJK input, because a token budget is meaningless unless it counts the tokens
  BM25 actually scores.
- Offline coverage, as measured after the amendments: `answers/test_query_mode.py` (**45**),
  `adapters/test_query_translation.py` (16), `adapters/test_hybrid_search.py` (**26**, routing
  and `lexical_query`), `answers/test_answer_service.py` (**48**, routing / translation /
  channel-rank promotion), `answers/test_verify_claim_units.py` (**29**, the claim unit split),
  `adapters/test_nl_gold.py` (**38**, including `any_of`), `answers/test_nl_gold.py` (25),
  `adapters/test_chat_http.py` (17), `adapters/test_strict_response_schemas.py` (12). Whole
  package: **1296 passed**, 0 skipped, 0 failed.
  `docs/enterprise-pdf-rag/schemas/rag-chat-v1.json` regenerated with two optional
  fields added and nothing removed.
