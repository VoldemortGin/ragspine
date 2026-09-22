# ADR 0019: A document's own outline is a third retrieval channel that never testifies

Status: Accepted, 2026-09-21. Extends [ADR 0013](0013-page-metadata-and-prefilters.md) (verbatim
page-level metadata, the only material the structure is folded from), [ADR
0012](0012-chart-index-text-and-retrieval-seats.md) (retrieval seats and the guaranteed visual seat
whose promotion window this widens), [ADR 0017](0017-page-context-window.md) (the page as the unit a
hit is read in, and its reading order) and [ADR 0018](0018-query-classification-and-translation.md)
(a question chooses its channels, and the restatement that reaches the lexical one). It changes
nothing a published release already carries: **no index is rebuilt, no snapshot id moves, no policy
string changes, no qualification is re-run, no evidence asset is rewritten.** The tree is folded
from metadata the release already stores and saved beside it, so a release published yesterday gains
a tree without being re-qualified, re-indexed or re-published — and a deployment that never builds
one answers byte for byte as it did before this ADR. A deployment that *does* build one answers
byte for byte too, until a caller asks to route: on the sample this was measured against the
channel changed no verdict and no citation, so it ships **opt-in** (`ROUTE_BY_DEFAULT = False`).
Decision 11 says how to ask for it and Validation says why it is off. **Amended by Amendment 1
(2026-09-22): the default is now `True`** — the "off" in Decision 11, in the closing clause of
Decision 12 and in Validation's *The decision that follows* reads as the record of what shipped on
2026-09-21, not as the current default.

## Context

Both channels this package has score fragments. BM25 scores a member's index text, the vector
channel scores its embedding, and ADR 0018 measured which of the two to trust for which shape of
question. Neither of them knows that a document has *parts*. A question aimed at a named section of
a long report competes, member by member, with every similarly worded fragment printed anywhere else
in it — and the document's own structure, the thing a human reads first, is not an input to
retrieval at all.

PageIndex (VectifyAI, <https://github.com/VectifyAI/PageIndex>) is the published form of the other
approach: rather than embed a long document, build a hierarchical table of contents for it once, let
an LLM reason over that tree to choose the sections a question belongs to, and read those pages.
What is borrowed here is exactly that shape — an outline built once at ingestion, one reasoning call
per question, and a **page set** as the whole of its output.

What is deliberately **not** borrowed is who writes the structure. PageIndex has a model produce the
hierarchy. A hierarchy a model asserted is a hierarchy nobody can check, and the standing discipline
of this package is that structure is *proved*, not asserted: a table's grid is bound to real rulings
(ADR 0014), a chart's points to source occurrences (ADR 0016), a page's metadata to the span that
prints it (ADR 0013). So here the structure is folded deterministically from that already-verified
page metadata — every node's title is a page's own words, carrying the `MetadataEvidence` that
proved them — and the only thing a model writes is a per-branch **routing note**, which never
becomes evidence, is never indexed and can never be cited. The pages the router picks are then read
by the same verbatim-evidence path as any other channel; the map is a map, and the territory is
unchanged.

What was actually measured, rather than assumed, is the shape of a real document's outline. The
`tree` stage over the pinned AIA release (`processing_id` `22127d0f…`, source `df902346…`, **20**
selected pages) took the contents page: `origin = agenda`, **22** nodes, **20** leaves and **2**
branches, from **2** live model calls in **12.5 s** cold. Replayed with `--max-live-calls 0` it
costs **0** live calls and **1.1 s**, summaries intact, because the structure owes the model nothing
and the notes replay from the stage cache. Its first level is exactly what the deck's own agenda
page prints:

```text
n0001 p1-15 OVERVIEW AND BUSINESS HIGHLIGHTS
n0017 p16-20 FINANCIAL PERFORMANCE
```

and `n0017`'s routing note reads, verbatim: *"Pages 16–20 present key financial performance metrics,
including VONB, OPAT, operating ROE/ROEV, EV equity, shareholder returns, and capital measures. They
also show new business mix and profitability, EV equity movements, in-force portfolio composition,
operating variances, and sensitivities to interest rates and equity prices."* That is a map — it
names where to look and states no figure as a fact — and it is also the honest measure of this
sample: a twenty-page deck folds into two branches, which is the shallowest structure this design
can have and still have one.

The second measured fact is a property of the fusion itself, and it decides two of the decisions
below. RRF ranks are 1-based, so at this deployment's constants (*k* = 60, `channel_limit` = 50) a
member only one channel ranks scores at most `1 / (60 + 1) = 0.0164`, while a member two channels
rank scores at least `2 / (60 + 50) = 0.0182`: the best possible single-channel hit sorts below the
worst possible two-channel one. Agreement beats depth by construction — which is precisely why the
member this channel exists to find, the one neither BM25 nor the vectors could score, cannot be
judged by its fused position alone.

The corollary is the one this ADR had to learn from a bad measurement rather than from arithmetic
(see Validation). A third ranking added at the *same* *k* is not a third opinion, it is a third
vote: its rank-1 term is `1 / 61 = 0.01639`, which outweighs a member only BM25 could score at rank
2 (`1 / 62 = 0.01613`) and outweighs the whole spread of a real top ten. But a page set is not a
relevance ranking. The router says *where to look*, at section granularity, and the order of pages
inside a section is reading order — so scoring it like a similarity hands reading order a vote over
two channels that actually read the text. A ranking that cannot measure relevance must not be
weighted as if it could.

## Decision

1. **The structure is folded from verified page metadata by two cut rules, with no model at all**
   (`processing/document_tree.py`, new, pure and stdlib-only). `build_document_tree(pages, *,
   source_sha256, display_title=None)` returns a `DocumentTree(schema_version, source_sha256,
   origin, roots, diagnostics)` over `TreePage`s, each of which is one page's ADR 0013
   `PageMetadata` beside the canonical spans that metadata quotes. The first level is cut by
   whichever rule fires first. **(a) A contents page**: the first page typed `AGENDA` has each of
   its short lines read as an entry by `agenda_entries` — between `MIN_ENTRY_CHARS` (**3**) and
   `MAX_ENTRY_CHARS` (**80**) characters after whitespace folding, carrying at least one letter, not
   the page's own heading and not a repeat — and each entry names a node starting at the first
   *later* page whose `title` or `section` prints it. Matching is equality, widened to substring
   containment only from `MIN_SUBSTRING_ENTRY_CHARS` (**5**) characters up, so a two-letter entry
   like `EV` cannot bind to every page whose title contains it; the search cursor only moves
   forward, so ranges can never cross; and fewer than `MIN_AGENDA_MATCHES` (**2**) matches means the
   page was not a contents page after all and the rule yields nothing. **(b) Dividers and section
   changes**: otherwise a node starts wherever the running `section` header changes, or at a divider
   page — one carrying a title and at most `MAX_DIVIDER_SPANS` (**8**) spans, never a `CHART` or
   `TABLE` page however few spans it has. `origin` records which rule produced the level, `agenda`
   or `sections`. Below the first level, consecutive pages sharing one title form a second-level
   node, and a second-level node covering more than one page prints one leaf per page. Node ids are
   assigned in document order, `n0001`, `n0002`, ….

2. **Every title is a page's own words, and it carries the evidence that proved them.** A
   `TreeNode(node_id, title, level, pages, children, evidence, summary, key_topics)` takes its
   `title` from a `MetadataValue` — an ADR 0013 verbatim page title or section — and its `evidence`
   is that value's `MetadataEvidence`, so the title is traceable to the span that prints it. No
   title is composed, abbreviated or paraphrased. A run of pages that prints no title of its own
   falls back to the enclosing node's title, and past that to the document's `display_title`, which
   is itself a verbatim ADR 0013 fold; a document where nothing at all prints a title yields no tree
   and says so in `diagnostics` rather than inventing one.

3. **The tiling is an invariant of the type, not a property of the builder.**
   `TreeNode.__post_init__` refuses a node with no id, no title, a level below one or no page;
   refuses pages that do not ascend without repeating; and, when a node has children, refuses them
   unless the concatenation of their pages is **exactly** the parent's pages in order and every
   child sits one level below it. `DocumentTree.__post_init__` refuses an empty tree, a root below
   level one, roots that overlap or descend, and duplicate node ids. So every selected page belongs
   to exactly one leaf and the leaves tile the document in order — and that holds for any tree that
   exists at all, including one a test, a replay or a future builder constructs. A page cannot be
   silently lost or silently counted twice, because such a tree cannot be instantiated.

4. **The summary is the one thing a model writes, and it routes rather than testifies**
   (`adapters/document_tree_extraction.py`, the `document_tree` ingestion stage). Exactly one
   bounded text-only `complete_text_json` call per non-leaf node — `summary_targets(tree)`, which is
   every node with children, because a leaf is a single page the other two channels already read and
   a page window already prints — under the task salt `document-tree-summary-v1`, its own cache
   namespace beside every other stage. The prompt is the node's title, its printed page range, its
   child titles and the **text of its own pages** and nothing else: never an image, never a figure
   asset, bounded at `_MAX_PAGE_CHARS` (**1 200**) per page and `_MAX_BODY_CHARS` (**12 000**)
   overall so it cannot approach the client's 24 000-character refusal. `TreeSummaryDTO` is strict
   and small: one `summary` of at most **600** characters and at most **8** `key_topics`.
   `with_summaries` attaches them to a copy of the tree. **A summary is routing-only: it is never
   evidence, never indexed, never citable, and never appears in an answer prompt's evidence
   blocks.** That is enforced by where it is not: it never enters `processing/index_text.py`, so no
   channel scores it; it never enters `processing/context_builder.py`, so no prompt prints it; it
   carries no member id and no field path, so a claim cannot name it and `verify_claims` rejects any
   attempt as `MODEL_OUTPUT_INVALID` / unknown member, exactly as ADR 0017 already pinned for page
   context. Rule 4 of `_SUMMARY_RULES` tells the writing model the same thing, but the rule is a
   courtesy; the structure is the guarantee.

5. **Running out of budget defers the note; it never costs the structure.** A node whose call raises
   a `JsonCompletionError` with a code in `_DEFERRED_CODES` — `call_budget_exhausted` or
   `cache_miss`, the two that mean *not yet* rather than *wrong* — leaves that node's summary empty
   and marks the stage `DEFERRED`; any other code marks it `FAILED`; no configured client defers
   every node as `no_model_configured`. **In all three cases the tree is still folded and still
   saved**, because its structure owes the model nothing, and the diagnostic names the first
   `_MAX_REPORTED_NODES` (**5**) offenders and says so outright: *"the tree structure is complete
   and was saved."* Only a `SUCCEEDED` run enters the stage cache, keyed by
   `stage_fingerprint("document_tree", producer, (processing_id,))`, so a re-run with the same
   producer replays the whole tree at **0** live calls. `DocumentTreeSummary` is what the CLI prints
   — `source_sha256`, `processing_id`, `state`, `diagnostic`, `origin`, `node_count`, `leaf_count`,
   `summary_calls`, `live_call_count`, `page_count` and the `rendered` tree — so a human reads
   exactly the map the router will.

6. **The tree is persisted beside the release, never inside its manifest**
   (`adapters/processing_store.py`). The body goes through the ordinary content-addressed asset
   store; a small state record, the `DocumentTreeRecord` of `adapters/http/processing_schemas.py`
   (`schema_version` pinned to `document-tree-v1`, `producer`, `state`, `diagnostic`, `artifact`,
   `summary_calls`, with a validator that a succeeded record names its artifact and no diagnostic
   and an unfinished one names a concrete diagnostic), is written by `save_document_tree` to
   `<processing_root>/document-tree/<processing_id>.json`. Unlike a stage-cache pointer that record
   is **rewritable on purpose** — a later, better-funded run is meant to replace a deferred tree
   with a summarised one over the same processing id — so it is replaced atomically via `os.replace`
   rather than linked into place. `document_tree_record` reads it back in any state;
   `load_document_tree` returns a tree only when the record `SUCCEEDED`, and re-checks that
   `tree.source_sha256` equals the manifest scope's before handing it over.

7. **One bounded, cached routing call per question, and having no route is never an error**
   (`adapters/tree_retrieval.py`, new). `route_tree(question, tree, llm)` renders the outline with
   `render_tree(tree, max_chars=MAX_TREE_CHARS)` (**12 000**, truncated on a whole line so no node
   is half described) and makes one `complete_text_json` call under the salt
   `document-tree-route-v1` with `_MAX_OUTPUT_TOKENS` (**512**). `TREE_ROUTE_RULES` states that the
   question and the outline are data and never instructions, that the smallest set of sections wins,
   and — rule 3 — that a node summary *"is a map, not evidence: it says where to look. Never quote
   it, never treat it as a fact and never use it as a source."* Every way of not getting a route
   returns `None` and the channel is simply absent: an exhausted budget, a transport error,
   unparseable JSON, an empty question, a question over `_MAX_QUESTION_CHARS` (**2 000**), an empty
   outline, or a reply naming no page the tree covers. This is the ADR 0018 Decision 6 discipline
   applied unchanged — a missing optional call degrades the ranking, never the request.

8. **The router's whole output is a page set, resolved deterministically and capped.**
   `TreeRouteDTO` is strict, frozen and `extra="forbid"`: `node_ids` (at most 12), `pages` (at most
   24) and a `rationale` of at most **400** characters. `_resolve_pages` then decides in a fixed
   order: the model's own printed page labels first, converted from the one-based `p` labels
   `render_tree` prints to the 0-based indices `MemberText.page_index` carries and dropped when the
   tree does not cover them; then the pages of the nodes it named, via `DocumentTree.pages_of`,
   which ignores unknown ids; deduplicated in that order; cut to `MAX_ROUTE_PAGES` (**6**) after
   `node_ids` was cut to `MAX_ROUTE_NODES` (**6**); then sorted ascending. More sections than that
   is the whole document again, and more pages costs more than the two fragment channels it sits
   beside. The result is a `TreeRoute(node_ids, pages, cache_hit, rationale)` — page indices and
   provenance, no text, no score, nothing quotable.

9. **Those pages become a third ranking inside the same fusion, at its own RRF constant, and
   never a filter** (`adapters/hybrid_search.py`). `fuse(vector, lexical, tree=(), *, k=60.0,
   tree_k=600.0)` takes a third sequence with an empty default, so every pre-existing call fuses
   byte for byte as it always did, and `search(..., tree_pages=())` is the same for the caller.
   `HybridSearch._tree_rank` turns the routed pages into a ranking: the members of each routed page,
   page by page in the router's order, ordered *within* a page by `answers.page_window.reading_key`
   — the ADR 0017 reading order, which is itself the ADR 0015 quantised-row rule — respecting
   `allowed` and cut at `channel_limit`. Its scores are synthetic and strictly descending (`1.0 /
   (len(hits) + 1)`) because only the order is real: a page set is not a similarity. `FusedHit`
   therefore gains `tree_rank: int | None` and **no score field**, and the channel participates
   whatever `QueryMode` the question resolved to, since it is the caller that decides whether to
   route at all.

   **The two scoring channels share `k`; the tree ranking is fused at `tree_k`, an order of
   magnitude larger.** The property that buys is exactly one, and it is stated this narrowly
   because the measurement below showed the wide version to be false: *a member **only** the tree
   reached sorts below every member a scoring channel reached.* The best a routed member can earn
   is `1 / (tree_k + 1)`; the least a member one scoring channel ranked inside the channel limit
   earns is `1 / (k + channel_limit)`; the first must stay strictly below the second, which is
   exactly `tree_k + 1 > k + channel_limit`. `HybridSearch.__init__` validates that inequality over
   its own `tree_rrf_k`, `rrf_k` and `channel_limit` and refuses to construct otherwise, so the
   ordering is a precondition of the object rather than a property of three lucky defaults. At the
   constants the service runs (`tree_rrf_k` **600**, `rrf_k` **60**, `channel_limit` **50**) it
   reads `1 / 601 = 0.00166` against `1 / 110 = 0.00909` — a 5.5x margin. The tree therefore adds
   recall at the tail instead of competing for the head: **a routed page may lift a member no
   channel reached; it may not put one above a member a channel scored.** The term is still
   *additive*, so two members a channel did score can still move relative to each other; the
   guarantee is about unscored pages only, and Validation measures what that reordering actually
   cost.

10. **The guaranteed-seat window learned to read the third rank** (`adapters/answer_service.py`).
    `_within_a_channel(hit, limit)` now tests `tree_rank` beside `vector_rank` and `lexical_rank`.
    This is not tidiness, and after Decision 9 it is the **only** way the channel can seat a member
    the other two missed: a member only the tree reached scores `1 / (600 + rank)` — `0.00164` at
    rank 11 — which by construction sorts below every scored member, while any member two channels
    rank scores at least **0.0182**. Judging such a member by fused position alone would bury it
    whatever its tree rank, and a member neither BM25 nor the vectors could score is exactly the
    member this channel exists to find. ADR 0018 Amendment 3 established the same argument for a
    two-channel fusion. The path is bounded: `select_context` admits a hit with `tree_rank <= 2 *
    top_k`, and seats it only to fill a citable visual kind (chart / diagram / formula) the top-k
    lacks, at most one seat per missing kind and never over a seat already holding one.

11. **Routing is off by default; a caller asks for it, and the short-label rule is what applies
    when it is on** (`answers/query_mode.py`, `adapters/answer_service.py`). ~~`ROUTE_BY_DEFAULT:
    Final = False`. With a tree mounted and `AnswerRequest.tree_route` left `None`, the question is
    **not** routed, and the Validation below is the whole of the reason: on this sample the channel
    is provably safe and changes not one verdict and not one cited page, so routing by default
    would spend a live call and seconds of latency per question to buy nothing measurable. A caller
    asks for it with `AnswerRequest.tree_route=True`.~~ **Superseded by Amendment 1** for the
    value alone: `ROUTE_BY_DEFAULT: Final = True`, and a caller turns it *off* per request with
    `AnswerRequest.tree_route=False`. `_route` decides in order: no tree for this document, no
    route; otherwise `AnswerRequest.tree_route` when it is set; otherwise `ROUTE_BY_DEFAULT and
    not is_label_query(question)`.

    That last clause is written out rather than folded away, because it is the rule that applies
    **the day the default flips** — which it has, so it is live now. `is_label_query(question)`
    states the ADR 0018 short-label rule once — at most `MAX_BM25_ONLY_TOKENS` (**5**) tokens
    *and* `MAX_BM25_ONLY_SHORT_CONTENT_WORDS`
    (**2**) content words, or a figure with at most `MAX_BM25_ONLY_CONTENT_WORDS` (**1**) — where
    `classify_query` used to spell it out inline, and both the channel decision and the routing
    decision now read that one predicate. An exact label lookup already knows what it is looking
    for: BM25 matches a printed label wherever it appears, so a map of the document buys nothing
    for a live call. `AnswerService(trees=…)` takes the mounted trees keyed by `source_sha256`, so
    a service given none has no third channel at all — which is what leaves the frozen offline gold
    replay untouched. The question routed is ADR 0018's English restatement when there is one,
    because the outline is written in the index's language. The whole of it sits between the filter
    re-narrowing and `search.search(...)`, and its only effect on retrieval is the `tree_pages` it
    hands over.

12. **The envelope reports the route; the request body gains no knob**
    (`adapters/http/chat_schemas.py`, `adapters/document_catalog.py`). `AnswerEnvelope.tree_route`
    carries a `TreeRouteOut` (`node_ids`, `pages`, `cache_hit`, `rationale`) when the question was
    routed and `null` otherwise, and every `MemberRankOut` gains `tree_rank`, so a reader can see
    which seats the third channel earned. `CatalogEntry.tree_available` reports whether a mounted
    document has a tree, `MountedDocument.document_tree()` reads it once and remembers its absence
    too, and `catalog_trees(catalog)` is what the app factory hands `AnswerService`.
    **`RagChatRequest` gains nothing**, on the ADR 0018 Decision 3 precedent that set it for
    `fusion_mode`: which channels answer a question is an engine decision, readable afterwards,
    never a caller's knob. `llm_live_calls` counts the routing call honestly, because the snapshot
    that measures it already spans the request. Both contracts gain optional fields only, so an
    older client reads `rag-chat-v1` and `document-catalog-v1` unchanged. Together with Decision 11
    that has a consequence worth naming outright rather than discovering: ~~**the HTTP path has no
    way to ask for a route, and with `ROUTE_BY_DEFAULT` false it does not route**, so `tree_route`
    is `null` on every wire response this release can produce. The channel is reachable in-process
    through `AnswerRequest`, which is where it was measured and where it waits for a document long
    enough to earn the default.~~ **Superseded by Amendment 1**: the HTTP path still has no way to
    ask, and now has no need to — with the default on, a wire question against a mounted tree is
    routed and its envelope carries the route. `tree_route` is `null` only where no tree is mounted
    or the shape rule spared the question.

## Rejected alternatives

- **Fuse the tree as a peer ranking, at the same *k* as the two scoring channels.** This is what
  shipped first, and it is the only alternative here that was rejected by measurement rather than
  by argument. It is the obvious reading of "a third channel", it needs no new constant, and on the
  frozen gold set it took retrieval from **21/22 to 17/22** while improving nothing: five cases
  went pass → FAIL, none of the five structural questions written for this feature got better, and
  routed members held **117 of 220** prompt seats. `p07` / `p14` are the case to remember — asked
  *"Agency share of VONB 1H26"*, the routed run answered **68.3% (ex-Thailand)** citing two real
  verbatim spans, instead of the **72%** the p18 donut states. Provenance intact, answer wrong. The
  reason is the arithmetic in Context: at `k = 60` a tree rank-1 term outweighs a member only BM25
  could score at rank 2, so reading order got a vote over two channels that had actually read the
  text. The fix was not to route better but to weigh the route correctly — Decision 9's `tree_k`,
  with the full before-and-after in Validation.
- **Let a model build the hierarchy, as PageIndex does.** It is the single most tempting
  simplification and it is the one thing this package cannot do. A model-asserted hierarchy is
  unverifiable: there is no span to read it back from, no evidence to attach, and no way to tell a
  correct outline from a plausible one. The standing rule here is that structure is *proved* — a
  ruling for a grid (ADR 0014), a source occurrence for a chart point (ADR 0016), a span for a page
  title (ADR 0013) — and an outline is structure. Folding it from metadata that is already verbatim
  costs one pure module and buys a tree whose every title can be pointed at on a page.
- **Use the tree as a hard pre-filter that narrows the candidate set.** This is the obvious way to
  get the most out of a good route, and it is exactly why it is refused: a filter can only *remove*,
  so a single bad route hides the answer outright and no later stage can recover it. As a ranking a
  bad route costs **rank, not recall** — the other two channels still score every member, and the
  worst a wrong page set can do is lend a few irrelevant members a third-channel rank that the
  fusion then weighs against two channels' disagreement. The period / region pre-filters (ADR 0013)
  are allowed to narrow precisely because they are derived from verified vocabulary and relaxed when
  they starve the ranking; a model's page choice has neither property.
- **Put the tree in `ProcessingManifest`.** It is where a reader first looks for it, and it would
  break two things. `document_metadata` is recomputed on every load and refused on drift, so a tree
  stored there would have to be recomputed — which a model-written summary cannot be,
  deterministically or for free. And a new manifest draft drops `retrieval`, so gaining the tree
  would force every already-published release to be re-indexed for a map that changes no index text
  and no embedding. A content-addressed body plus a rewritable state record keeps the release
  immutable and the tree upgradeable, which is the pair of properties actually needed.
- **Put the summaries in the index, or in the answer prompt.** Both are one step from free: the
  notes are already written, they are about the right pages, and they read well. Both are refused
  for the same reason. Indexed, model prose becomes text a channel scores and a member can be seated
  for; printed in a prompt's evidence blocks, it becomes text a model can quote and a claim can be
  built from. Either way a sentence nobody can trace to a span turns quotable, which the
  anti-fabrication invariant forbids outright. ADR 0017 already drew this line one notch narrower
  for page context — informative, uncitable by construction — and a routing note does not even get
  that far: it never reaches the prompt at all.
- **Expose a per-request routing knob on `RagChatRequest`.** Like `top_k` and `channel_limit` (ADR
  0012) and `fusion_mode` (ADR 0018 Decision 3), whether a question is worth a routing call is an
  engine decision measured against a corpus, not something a caller can be expected to judge per
  question. It stays on `AnswerRequest` for tests and future tooling, and read-only in the envelope.
- **Summarise every node, leaves included.** A leaf is one page, which both fragment channels
  already score and ADR 0017's page window already prints in full; a note about it would add nothing
  and would multiply the stage's live calls by the page count. `summary_targets` therefore returns
  non-leaf nodes only — on the AIA release, **2** calls instead of **22**.

## Consequences and follow-ups

- **A routed question's retrieval input is now partly model output, and that is a widening of a
  known cost.** ADR 0018 Amendment 3 recorded that a *translated* question is no longer
  deterministic in retrieval, because the restatement the lexical channel scores is a model's
  string; three cold runs there showed 20 of 22 gold cases byte-identical and the two that moved
  were precisely the two translated ones. This ADR extends that property from translated questions
  to **routed** ones: the page set is chosen by a model, so the same question may draw a different
  route and therefore a different third ranking. Retrieval itself still contains no randomness — the
  fold is deterministic, `_tree_rank` is deterministic, `fuse` is deterministic — and the
  non-determinism is inherited wholly from the call. As with a translation, the immutable completion
  cache is the only real repeatability guarantee, and no single run may be read as *the* retrieval
  behaviour of a routed question.
- **One extra live call, and seconds of it, per routed question.** A routed answer costs two
  calls, a routed *and* translated one three, against the process-wide `APP_ANSWER_MAX_LIVE_CALLS`
  budget of 200. Measured over the gold set that is **39** live calls against **22**, and **215.1
  s** of wall clock against **147.2 s** — about 46% more; measured per question on the five
  structural questions it is **+3.8 s** to **+23.0 s**. `ROUTE_BY_DEFAULT` being false is what
  keeps that off every question today; `is_label_query` is what would keep it off the cheap ones
  the day the default flips. A repeat of the same question costs nothing, because every call
  replays from the immutable cache. The ingestion side is cheap by construction — **2** calls for
  the whole AIA deck, once.
- **This sample under-tests the structure.** The AIA deck's tree is **2** branches over **20**
  pages; `_branch`, `_titled_runs` and the three-level leaf rule are exercised by unit tests over
  constructed pages rather than by a real deep document. A hundred-page annual report with a real
  multi-level contents page is where the agenda rule, the substring matching and the truncation
  budget would actually be tested, and nothing here has seen one. Read the measured numbers as proof
  that the pipeline runs end to end on a real release, not as proof that the cut rules generalise.
- **A deployment with no tree is unaffected, and so is the offline gate.** `AnswerService` without
  `trees=` has no third channel, `fuse` with an empty third sequence is the old function, and
  `search` with no `tree_pages` takes the old path — each pinned by a test that constructs both and
  compares. The frozen offline gold replay builds its service without trees and is untouched by this
  ADR.
- **The router is not reproducible, and that is now nearly harmless rather than harmless.** Asked
  the same question twice against cold caches, it returned different `node_ids` and different
  pages: `p13-explicit-period-filter-en` routed to `[5]` on one call and `[5, 17]` on the next, and
  the `rationale` prose differed on **every** call of every probe. That injects run-to-run
  randomness into a system whose every other channel is a deterministic read of a pinned snapshot,
  and it was the first measurement's compounding factor — an unstable route reordering the head of
  the ranking moved verdicts. At `tree_rrf_k` = 600 an unstable route can no longer reorder the
  scored seats, so the same probe now moves no verdict and no citation. The instability is real and
  unfixed; only its blast radius shrank.
- **A leaf carries no summary, so leaf-level routing degenerates to title matching.**
  `summary_targets` deliberately returns non-leaf nodes only (see Rejected alternatives), which is
  right for cost on this sample — **2** calls instead of **22** — but it means the router chooses
  among a document's individual pages on their verbatim titles alone, with prose to reason over
  only at branch level. On a twenty-page deck whose branches are two, that is most of the choice it
  is asked to make. On a deep document the branches would carry the reasoning and this would matter
  less; on a flat one it will matter more, and nothing here measures which.
- **The guaranteed visual seat is a path around the fusion, and it is asserted on its own.**
  Decision 9 bounds what a routed page can do *inside* `fuse`, but `select_context`'s ADR 0012
  guaranteed-seat rule calls `_within_a_channel(hit, 2 * top_k)`, which now counts `tree_rank`. A
  routed-only member with `tree_rank <= 20` can therefore take a seat **regardless of its fused
  score** — which is exactly how all **4** tree-only seats in the re-measurement were won, each at
  a fused score of `0.001637` and a fused position past 70. The path is bounded (at most one seat
  per missing citable visual kind, never over a seat that already holds one) and cost nothing in
  this measurement, but it is a second door into the prompt, so it is now asserted directly rather
  than covered incidentally by the gold set:
  `test_a_tree_only_visual_below_every_scored_member_still_wins_its_guaranteed_seat` seats a donut
  carrying nothing but `tree_rank = 1`, at the lowest fused score in a ranking of thirty and a
  fused position past `2 * top_k`, and fails the moment that rank is taken away.
- **Re-measure on a long document before flipping the default.** `ROUTE_BY_DEFAULT` is `False`
  because on this sample routing is free of harm and free of benefit, and a twenty-page deck whose
  two scoring channels already reach every page is not a document a map can help with. PageIndex's
  premise is documents far longer than this. **This sample cannot show the benefit**, and the
  number to beat should be re-measured on a document of hundreds of pages — a real multi-level
  contents page, more pages than a single retrieval pass can cover — before the default flips. Two
  things should be measured there and neither has been: whether routing raises recall on questions
  aimed at a named section, and whether `tree_rrf_k` = 600 is still the right weight when the tree
  has genuine depth.
- **Not addressed.** The router is never reranked or second-guessed: its page set is taken as
  returned, cut and sorted, with no confidence and no fallback to a wider set. There is no
  cross-document routing (each document's tree is consulted only for questions already routed to
  that document), no incremental re-fold when a release gains pages, and no diagnostic that reports
  *why* a page was dropped from a route beyond its absence from `tree.pages`.

## Validation

### Offline

After this branch's commits, from the repository root: `pytest tests/enterprise_pdf_rag -q`
reports **1431 passed**, mypy `--strict` is clean over **521** source files, ruff `check` and
`format --check` are clean over **790** files, and all four conformance / architecture / schema /
drift checks pass. That total includes the cases pinning `tree_rrf_k + 1 > rrf_k + channel_limit`
and the ordering it buys, and the two that pin the default from both sides — a mounted tree
changing nothing field for field until a request asks, and the short-label rule still holding when
`ROUTE_BY_DEFAULT` is patched on, so Decision 11's shape rule stays under test while it is
dormant. The new cases sit in `processing/test_document_tree.py` (the fold, both cut rules, the tiling invariant and the render
budget), `adapters/test_document_tree_extraction.py` (the stage, its `DEFERRED` / `FAILED` split
and its cache replay), `adapters/test_tree_retrieval.py` (the route's resolution order, its caps
and every degrade-to-`None` path), `adapters/test_hybrid_search.py` (the third ranking, the
inequality `HybridSearch.__init__` refuses to violate, and that an empty third ranking fuses
byte-identically to the two-channel call), `answers/test_query_mode.py` (`is_label_query`),
`answers/test_answer_service.py` (the gate, the channel-rank promotion and the envelope) and
`adapters/test_chat_http.py` (`tree_route` and `tree_rank` on the wire).

None of that is evidence that routing improves an answer. It proves only that the channel is wired
correctly and degrades safely — which is exactly the distinction the live measurement went on to
make expensive.

The ingestion stage was run for real once, against the pinned AIA release, and is reported in
Context: `origin = agenda`, **22** nodes, **20** leaves, **2** branches, **2** live calls in **12.5
s** cold, **0** live calls in **1.1 s** replayed with `--max-live-calls 0`.

### How the live arms were built

Everything below was measured on 2026-09-21, entirely in-process in the worktree `ragspine-tree`:
nothing was served on a port, the live service was never contacted or restarted, and no `current-*`
pointer moved. The release is pinned — source `df902346791b`, processing `22127d0fad13`, retrieval
snapshot `42939d6a4e87`, **210** members, the tree replayed from cache and never rebuilt. Answer
model `gpt-5.6-luna`; embeddings `Qwen3-Embedding-4B` and reranker `Qwen3-Reranker-4B` over local
tunnels. Because `RagChatRequest` carries no routing knob (Decision 12), the two arms differ in
exactly one argument: **OFF** is `AnswerService(..., trees=None)`, the pre-ADR-0019 service, and
**ON** is the same mount with `trees=catalog_trees(mounted)`.

Evidence: `data/validation/generic-chat-2026-09-22/document-tree/` (`data/` is git-ignored, so
these are local artefacts, as for every earlier ADR). `README.md` records how each arm was built
and what was spent. The **first** measurement is `gold-comparison.md` (+ `.json`),
`gold-report.md` / `gold-report-tree-off.md`, the fresh-cache confirmation reruns
`gold-report-tree-{on,off}-rerun.md`, and `structural-questions.md` (+ `.json`). The
**re-measurement** is `comparison-treek600.md`, `gold-report-treek600-{on,off}.md` and
`structural-questions-treek600.md` (+ `.json`). `tree-rendered.txt` and `tree-stage-run.json` are
the tree build itself.

### The first measurement: the tree as a peer ranking (`tree_rrf_k` = `rrf_k` = 60)

The frozen NL gold set, 22 runnable cases (3 adversarial cases are offline-only):

| arm | passed | failed |
| --- | ---: | ---: |
| tree **OFF** | **21** | 1 |
| tree **ON** | **17** | **5** |

Six verdicts moved and the prompt member set changed in **17** of 22 cases:

| case | OFF | ON | OFF cited | ON cited | `tree_route.pages` (printed) | tree-ranked seats |
| --- | --- | --- | ---: | ---: | --- | ---: |
| `p01-roe-quote-en` | pass | **FAIL** | 8 | 8 | 8 | 5/10 |
| `p06-donut-zh` | pass | **FAIL** | 18 | — | 6, 7 | **10/10** |
| `p07-donut-keywords-en` | pass | **FAIL** | 18 | 6 | 4, 6 | 5/10 |
| `p13-explicit-period-filter-en` | **FAIL** | pass | — | 18 | 6, 18 | 6/10 |
| `p14-relaxed-region-filter-en` | pass | **FAIL** | 18 | 6 | 4, 6 | 5/10 |
| `p15-cache-repeat-en` | pass | **FAIL** | 8 | 8 | 8 | 5/10 |

`p13` is the one FAIL → pass, and it is a known flake: it failed OFF on run 1, passed OFF on the
fresh-cache rerun, and passes in both arms in the re-measurement. Read it as answer-model variance,
not as the channel earning a case. Routed members held **117 of 220** prompt seats across the 22
cases — 5 of every 10 on average, and **10 of 10** on `p06`, where the ON arm then abstained.

The five structural questions written for this feature, one arm each through
`AnswerRequest(tree_route=False|True)`:

| # | question | OFF | ON | seats |
| --- | --- | --- | --- | ---: |
| 1 | Summarise the Growth Engines section for Hong Kong | answered, p11 | answered, p11 | 10/10 |
| 2 | What does the EV Results section say about new business profile? | answered, p18 | answered, p18 | 10/10 |
| 3 | Which section discusses agency technology investment? | answered, p6 | answered, p6 | 10/10 |
| 4 | What are the key messages of the first half results overview? | answered, p4 | **abstained** (`claim_not_in_evidence`) | 10/10 |
| 5 | 香港业务在增长引擎一节里讲了什么 | answered, p11 | answered, p11 | 10/10 |

OFF answered **5/5**, ON answered **4/5**, and **not one of the five improved**. These are the
questions the channel was built for; on them it lost one and gained nothing.

#### The worst case, and the sentence this ADR exists for

`p07-donut-keywords-en` and `p14-relaxed-region-filter-en` both ask *"Agency share of VONB 1H26"*.
The document answers it on p18, in a donut whose Agency slice reads **72%**.

| arm | route | answer | cited |
| --- | --- | --- | --- |
| OFF | — | *"Agency share of VONB in 1H26 was 72%."* | p18 `points.point-agency.value` = 72% |
| ON | `n0005`, `n0007` → p4, p6 | *"68.3% (ex-Thailand)."* | p6, two verbatim spans: "68.3%" and "ex-Thailand" |

Both ON claims verified. Both spans really are printed on p6, under the page's own title *"Taking
Agency Performance to the Next Level"*, and both were quoted exactly. The citation is sound, the
lineage is sound, the number is not the number that was asked for. **Provenance intact, answer
wrong** — that is the failure mode this package exists to prevent, and the channel that caused it
was the one added to improve retrieval.

The router was also **not reproducible**. Asked the same question against cold caches it returned
different `node_ids` on different runs (`p07` routed to p4, p6 on one run and p6 alone on the
next), so the third ranking itself varied run to run — randomness injected into a system whose
every other channel is a deterministic read of a pinned snapshot.

### The mechanism

Arithmetic, not judgement. `_tree_rank` returns every member of the routed pages in reading order,
and `fuse` scored that ranking at the same `k = 60` the scoring channels use, so a tree rank-1
member earned `1 / 61 = 0.01639`:

| term | value |
| --- | ---: |
| tree rank 1 at `k` = 60 | **0.01639** |
| a member only BM25 could score, at rank 2 | 0.01613 |
| the real top ten of `p06-donut-zh`, whole spread | 0.0044 |

The tree bonus was several times the whole decision it was added to. A page set is not a relevance
ranking. The router says *where to look* at section granularity, and
page order inside a section is reading order — so at a shared `k` reading order outvoted two
channels that had read the text.

### The fix, and the property it buys

The tree ranking gets its own constant. The best a routed member can earn, `1 / (tree_k + 1)`, must
stay strictly below the least a member one scoring channel ranked inside the channel limit earns,
`1 / (k + channel_limit)` — exactly `tree_k + 1 > k + channel_limit`, validated in
`HybridSearch.__init__`:

| term | at the service's constants | value |
| --- | --- | ---: |
| best tree term | `1 / (600 + 1)` | **0.00166** |
| weakest scored term | `1 / (60 + 50)` | **0.00909** |

A retrieval-only probe over the real index confirms the ordering end to end:

| query | route | fused hits | positions held by scored members | positions held by routed-only members |
| --- | --- | ---: | ---: | ---: |
| `Agency share of VONB 1H26` | `[5]` | 85 | 1–72 | **73–85** |
| `2026 上半年 分销渠道 占比` | `[5, 6]` | 75 | 1–54 | **55–75** |

So a routed page may **lift** a member no channel reached; it may not put one **above** a member a
channel scored. The term is additive, so two scored members can still move relative to each other —
the probe saw a single-channel rank-11 member rise to fused 3rd, displacing that channel's rank-10
from the top ten. It moved no verdict and no citation here, but the guarantee is about unscored
pages only, and this ADR states it no wider than that.

### The re-measurement, same day, same release (`tree_rrf_k` = 600)

| arm | `tree_rrf_k` | passed | failed | verdicts moved |
| --- | ---: | ---: | ---: | ---: |
| first, tree OFF | — | 21 | 1 | — |
| first, tree ON | 60 | **17** | **5** | 6 |
| re-measured, tree OFF | — | **22** | 0 | — |
| re-measured, tree ON | 600 | **22** | 0 | **0** |

Gold ON now equals gold OFF: 22/22 in both arms, **not one case moved**, and every case cites
exactly the same pages in both arms. All five regressions (`p01`, `p06`, `p07`, `p14`, `p15`) are
gone, and `p13` passes in both arms. The prompts are not identical — **19 of 220** seats across 17
cases hold a member the OFF arm did not seat — but no verdict and no citation followed from that.

The five structural questions, re-run the same way:

| # | question | OFF | ON | cited OFF | cited ON | seats | tree-only |
| --- | --- | --- | --- | ---: | ---: | ---: | ---: |
| 1 | Summarise the Growth Engines section for Hong Kong | answered | answered | 11 | 11 | 10/10 | 0 |
| 2 | What does the EV Results section say about new business profile? | answered | answered | 18 | 18 | 10/10 | 0 |
| 3 | Which section discusses agency technology investment? | answered | answered | 6 | 6 | 8/10 | 0 |
| 4 | What are the key messages of the first half results overview? | answered | answered | 4, 17 | 4, 17 | 4/10 | 1 |
| 5 | 香港业务在增长引擎一节里讲了什么 | answered | answered | 11 | 11 | 9/10 | 0 |

**5/5 answered in both arms, citing exactly the same pages.** Question 4, the one the peer ranking
lost, answers again. Question 5 confirms the ADR 0018 path unchanged: the router receives the
English restatement *"What was said about the Hong Kong business in the growth engine section?"*,
never the Chinese question.

Seat occupancy fell by a third, and almost nothing is now seated by the tree alone:

| | first measurement | re-measurement |
| --- | ---: | ---: |
| prompt seats carrying a `tree_rank` | 117 / 220 | **78 / 220** |
| seats no scoring channel reached | not recorded | **4 / 220** |
| `p06-donut-zh` | 10 / 10 | 7 / 10 |
| `p07-donut-keywords-en` | 5 / 10 | 1 / 10 |

Each of those four tree-only seats carries the fused score `1 / (600 + 11) = 0.001637` and sits
past fused position 70; each reached the prompt through `select_context`'s guaranteed visual seat,
not through the fusion (see Consequences). All four cases pass and cite the OFF arm's pages.

The router is still not reproducible — `p13` routed to `[5]` on one cold call and `[5, 17]` on the
next, and the `rationale` prose differs on every call — but at `tree_rrf_k` = 600 an unstable route
can no longer reorder the scored seats, so it no longer moves a verdict:

| question | run 1 pages | run 2 pages | same pages | same `node_ids` | same rationale |
| --- | --- | --- | --- | --- | --- |
| `p07-donut-keywords-en` | `[5]` | `[5]` | yes | yes | **no** |
| `p13-explicit-period-filter-en` | `[5]` | `[5, 17]` | **no** | **no** | **no** |
| `p06-donut-zh` (English restatement) | `[5, 6]` | `[5, 6]` | yes | yes | **no** |

### What routing costs

| arm | live calls | wall clock |
| --- | ---: | ---: |
| gold set, tree OFF | 22 | 147.2 s |
| gold set, tree ON | **39** | **215.1 s** |

| # | OFF | ON | delta |
| --- | ---: | ---: | ---: |
| 1 | 28.8 s | 33.1 s | **+4.3 s** |
| 2 | 31.6 s | 43.6 s | **+12.0 s** |
| 3 | 8.5 s | 12.2 s | **+3.8 s** |
| 4 | 22.3 s | 31.2 s | **+9.0 s** |
| 5 | 31.7 s | 54.6 s | **+23.0 s** |

### The decision that follows

*(Superseded by Amendment 1, which flipped the default **on** this section's evidence rather
than against it. Everything here is the record of what was decided on 2026-09-21.)*

`ROUTE_BY_DEFAULT = False`. The channel is provably safe and completely wired — the inequality is a
precondition of `HybridSearch`, the fold is deterministic, the notes cannot be cited, no route is
never an error — and on a twenty-page deck whose two scoring channels already reach every page it
buys nothing: 22/22 either way, the same citations either way, for one extra live call and **+3.8 s
to +23.0 s** a question. A feature that changes no answer should not spend a call per question, so
it does not, and a caller asks for it with `AnswerRequest.tree_route=True`.

That is a statement about this sample, not about the idea. PageIndex's premise is documents far
longer than this one, where no single retrieval pass covers every page and a map is the only way to
find the right section. **This sample cannot show the benefit.** The number to beat should be
re-measured on a document of hundreds of pages with a real multi-level contents page before the
default flips.

## Amendment 1 (2026-09-22): routing is on by default

`ROUTE_BY_DEFAULT: Final = True`. Decision 11's value is superseded, and with it the closing
clause of Decision 12 and the verdict of *The decision that follows*. **Not one measurement
changes** — no number above is restated, no arm is re-run, and nothing else in this ADR moves:
`tree_rrf_k` stays **600**, the inequality `tree_rrf_k + 1 > rrf_k + channel_limit` is still a
precondition of `HybridSearch.__init__`, the routing note is still never citable, and the fold is
still zero-model.

What changed is the question being answered. *The decision that follows* asked "does this sample
show the channel earning its live call?" and answered no. This amendment asks the question the
sample can answer: "**can leaving it on cost an answer?**" — and the same measurement says no, from
both sides. On the pinned 20-page release the frozen gold set scored **22/22 with the channel off
and 22/22 with it on**, not one verdict moved and every case cited exactly the same pages; the five
structural questions were answered **5/5 in both arms**, citing the same pages; and the weighting
makes the strong case structural rather than empirical — a member only the tree reached scores
`1 / 601` and cannot outrank a member any scoring channel reached at `1 / (60 + 50)`. A default
that cannot change an answer, on a sample built to catch exactly that, is a default that should be
on for the documents the channel was built for: the long report where a single retrieval pass does
not cover every page is the case this sample cannot represent, and it is also the case a caller
cannot reach through HTTP, because `RagChatRequest` still carries no knob (Decision 12).

The price is unchanged and is stated plainly: **one extra live call per routed question**, measured
at **+3.8 s to +23.0 s** (gold set: 39 calls / 215.1 s on, against 22 / 147.2 s off). Three things
bound it. A document with no tree has no third channel at all, so a deployment that never runs the
`tree` stage answers byte for byte as before. A short label query is never routed — Decision 11's
`is_label_query` clause, written for this day and now live. And any caller may decline per request
with `AnswerRequest.tree_route=False`, which `test_tree_route_false_answers_field_for_field_as_if_no_tree_existed`
pins to be field for field — prompt included — the answer of a service with no tree mounted.

The follow-up in Consequences stands exactly as written: this sample still **cannot show the
benefit**, and whether routing raises recall on section-aimed questions, and whether 600 is the
right weight once a tree has genuine depth, must still be measured on a document of hundreds of
pages with a real multi-level contents page. That measurement now decides whether to keep the
default, not whether to reach it.

### Offline

`pytest tests/enterprise_pdf_rag -q` reports **1431 passed**, unchanged in count: the two cases that
pinned the default from both sides were re-pointed rather than removed.
`test_a_mounted_tree_routes_nothing_and_changes_nothing_until_a_request_asks_for_it` became
`test_tree_route_false_answers_field_for_field_as_if_no_tree_existed` — the same field-for-field
assertion, now on the explicit off switch — and
`test_with_routing_on_by_default_a_narrative_question_routes_and_a_label_query_does_not` dropped its
`monkeypatch` to become `test_by_default_a_narrative_question_routes_and_a_label_query_does_not`,
so the shipped default is what it reads. `adapters/test_chat_http.py` likewise dropped its
monkeypatch: the envelope's `tree_route` / `tree_rank` trace is now what the wire really produces.
