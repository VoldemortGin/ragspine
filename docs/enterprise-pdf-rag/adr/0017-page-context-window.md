# ADR 0017: A hit is read beside the rest of its page, which is never citable

Status: Accepted, 2026-09-21. Extends
[ADR 0011](0011-document-catalog-and-verified-answer-chain.md) (evidence blocks, field-level
claim verification and the prose numeric gate),
[ADR 0012](0012-chart-index-text-and-retrieval-seats.md) (retrieval seats and the deterministic
index-text projection), [ADR 0013](0013-page-metadata-and-prefilters.md) (the contextual index
header and verbatim page-level metadata) and
[ADR 0015](0015-diagram-and-formula-retrievable.md) (citable paths and `eligibility`). It changes
nothing before retrieval: **no policy string moves, no snapshot id changes, no index is rebuilt.**
Everything here happens after the ranked hits are chosen — in prompt assembly and in the verifier —
so every published snapshot's bytes are untouched and a release published yesterday answers under
this ADR without being re-qualified, re-indexed or re-published.

## Context

Retrieval scores one member at a time. `select_context` fuses per-member ranks and hands the answer
service one `ContextBlock` per hit, and ADR 0011's block renders that one member field by field —
exactly what field-level verification needs, and exactly what a reader does not have. The hit
reaches the prompt stripped of the page that explains it: a chart with no caption, a heading with no
body, a bullet with no section. The evidence is complete; the context is shredded.

ragspine already solved the same shape for narrative chunks, and it is the template followed here.
`src/ragspine/retrieval/link/narrative_link.py` carries a parent `window_text` beside each child
chunk: the window is written into a **separate** `prompt_text` key and widens only what the model
may read, while `text`, `source_locator` and `chunk_id` stay pinned to the fine child, and the
expansion happens **after** the RESTRICTED double-exit filtering, so a parent window can never
smuggle in a child that the exits removed. That is the small-to-big pattern, and its discipline —
widen generation, never widen citation, and expand only what already passed the gates — is the
whole of this design.

The page is the right parent granularity, and it costs nothing to obtain. Every retrieval member
already stores its `page_index`; page titles and sections are already verbatim page-level facts
(ADR 0013); a page boundary is a fact the layout itself asserts rather than a threshold someone
picked. And the retrieval index is already restricted to verified members (Decision 8), so a page's
neighbours are the same evidence class as its hits — the only difference is that they are printed
without a path.

## Decision

1. **Page context is a second block type, and it prints no path.** `processing/context_builder.py`
   gains `PageContextMember` (member id, `BlockKind`, one line of text), `PageContextBlock`, the
   union `type PromptBlock = ContextBlock | PageContextBlock`, and `build_page_context_block(...)`.
   A page block renders as the head `[page_context page_index=N] title=… section=…`, then the fixed
   line `(page context: understanding only; it carries no citable path)`, then one
   `- (<kind>) <text>` line per member in reading order, and `[truncated]` last when members were
   dropped. It deliberately prints **no field path and no member id**. That is what makes it
   uncitable by construction rather than by rule: a claim names a member and a field path, and this
   block offers neither.

2. **One block per page, inserted after that page's first hit** (`answers/page_window.py`, new,
   pure and stdlib-only). `with_page_context` keeps the hit blocks in their fused order and weaves
   in at most one page block per page, immediately after the first hit on that page. A member that
   already has its own block is excluded from its page's context — its full evidence is printed
   above. A kind with no corresponding `BlockKind` (an `IMAGE`) is skipped. The page title and
   section come from that page's first member's ADR 0013 metadata, because those are page-wide
   facts, not per-member ones. A page with nothing left to say yields no block at all.

3. **Reading order is the criterion a proved diagram already uses.** `reading_key` is
   `(round(y0 / READING_ROW_QUANTUM), x0, member_id)`, with `READING_ROW_QUANTUM = 4.0` imported
   from `processing/diagram_models.py` — the same quantised-row rule that ADR 0015 already proved
   sufficient for reading a diagram's nodes, reused rather than reinvented. A member whose stored
   evidence carries no rectangle cannot be placed on the page, so it sorts after every located one,
   by id: the order stays total and deterministic either way.

4. **`MemberText` carries the geometry and the header it was already parsing.**
   `answers/ports.MemberText` gains `header` (the ADR 0013 contextual header prefixed to the index
   text) and `bbox`, plus a `body` property that is `text` with that header stripped.
   `adapters/processing_retrieval.member_anchor` reads the rectangle from
   `ObjectDescription.source.bbox` for every kind but `CHART` — `member_text` already parsed that
   exact asset and was simply discarding the geometry — and, for a chart, whose description is bound
   to its SVG and therefore has no source anchor, from the **qualification receipt**
   (`parse_chart_receipt` / `parse_displayed_bar_receipt`, never a bare `FigureQualification`), at
   `.qualification.source.bbox`. Measured on the AIA release: **190/190** indexed members obtain a
   bbox, and **190/190** satisfy `text == header + "\n" + body`. `header` is filled only when
   `plan.qualification_policy in CONTEXTUAL_POLICIES`; otherwise it would report a string that was
   never spliced into `text`. The block therefore prints the header once, each member prints only
   its `body`, and a member whose body is exactly the page title or the section is dropped as saying
   nothing new (AIA page 7 really contains such a duplicate).

5. **The verification boundary: page context informs, it never grounds a citation.**

   - **A claim aimed at page context is rejected by code that already existed.** `verify_claims`
     resolves `claim.member_id` against `blocks`; a page context member is not in that mapping, so
     the claim falls into the existing `MODEL_OUTPUT_INVALID` / `"unknown member"` branch.
     **No code was added for this** — only tests that pin it. `answer_service` builds its
     `by_member` mapping from `ContextBlock`s alone, so nothing downstream can even observe a page
     block.
   - **`prose_grounded` gains `context_texts`.** A number printed in the page context counts as
     grounded, so a correct answer is no longer abstained merely because the model repeated a figure
     the prompt showed it. This relaxation widens what the prose may **repeat**, never what it may
     **cite**: a claim must still name a member block, so a figure read off the page context can be
     stated but carries no citation.
   - **Only the members' own text is admitted, never the block's rendering.** Were the rendering
     admitted, the head's `page_index=N` would turn an arbitrary small integer into "evidence".
   - **This is ADR 0011's ISSUE-3 relaxation one notch wider, and the notch is named.** ISSUE-3
     admitted a number appearing in the evidence text that a *verified claim cites*; this admits a
     number appearing in evidence the *prompt printed*, cited or not. The cost is recorded under
     Consequences, together with the three adversarial controls that still refuse.

6. **The budget gives up context before it gives up evidence.** `budget_blocks` became generic,
   `budget_blocks[BlockT: PromptBlock](...)`. When the whole sequence overruns `max_chars`, page
   blocks are dropped **whole, from the last page backward**, until it fits; whatever remains then
   follows the pre-existing rule unchanged (a block that does not fit is skipped, and a later,
   smaller one may still enter). With no page block present the function is byte-for-byte the old
   one. A hit's own evidence is therefore never surrendered to its neighbours' context. Within one
   page, `page_window_budget_chars` caps the block: members are dropped **whole** from the end of
   reading order and the block then says `[truncated]`.

7. **Off by settings or by request, and every block that lands is reported.**
   `AnswerSettings.page_window: bool = True` and `page_window_budget_chars: int = 6000` (per page);
   the total prompt budget stays `prompt_budget_chars = 18000`.
   `AnswerRequest.page_window: bool | None = None` takes the setting when `None` and overrides it
   for one request otherwise, and HTTP's `RagChatRequest.page_window` exposes the same switch.
   `AnswerResult.page_windows: tuple[PageWindowStat, ...]` records
   `(page_index, member_count, chars, truncated)` for every page block that reached the prompt, and
   the envelope carries them as `PageWindowOut`. `rag-chat-v1.json` gains 88 lines and deletes or
   changes none: every new field is optional, so an older client reads the contract unchanged.

8. **No verification filter is needed, because the index already is one.**
   `adapters/processing_retrieval.eligibility()` admits a record only when its four stages —
   `qualified_ir` / `qualified_description` / `qualification` / `svg`, or the unqualified
   `ir` / `description` / … equivalents — all reached `SUCCEEDED` (ADR 0015 decision 12), and the
   build re-checks it member by member. An unverified description leaves the `qualification` stage
   `UNAVAILABLE`, which the predicate refuses. Measured: across six documents **209/209** indexed
   members carry a `verified` description, and AIA indexes **190** of its 241 objects. A page's
   indexed members therefore *are* its verified ones and page context needs no second filter.
   **Trap, recorded so nobody repeats it:** `processing/models.LayoutObject.verification` is a
   different field — layout's own inference, measured `pending` everywhere — and must never be used
   as this criterion.

## Rejected alternatives

- **Make page context citable too — one more member block per neighbour.** It would turn
  projected, uncitable text into a claim target and break the rule that every claim is re-read from
  stored evidence field by field (ADR 0011): the page line is a fold of a member's index text, not
  the member's typed IR, so there is no field to read back. Widening the seat count instead of the
  window is the honest version of that idea, and it already exists as `top_k`.
- **Splice the whole page's text into the hit block's `description_text`.** A description asset is
  content-addressed evidence; changing its bytes changes the description ref, which changes the
  qualification receipt that binds it, which changes the embedding cache fingerprint. ADR 0012
  rejected rewriting description assets for exactly this reason and kept the projection at the
  index-text seam. Page context is assembled in the prompt, where nothing is content-addressed.
- **Store an explicit parent member (a `parent_id` on each retrieval member).** It needs a snapshot
  schema change, a new qualification policy string and a full re-index of every published release,
  in exchange for a parent relation that `page_index` already provides for free on every snapshot
  ever published — including the ones this ADR deliberately does not touch.
- **Take the window by bbox proximity instead of by whole page.** Any radius or gap threshold is a
  number nobody can prove, and it would have to be re-tuned per layout. A page boundary is a fact
  the document itself asserts; it needs no tolerance and no defence.
- **Truncate a member's body instead of dropping the member.** `budget_blocks` has never truncated a
  block — half a sentence of evidence reads as a complete one, and ADR 0011's context builder says
  outright that blocks are dropped whole. Page context obeys the same discipline: whole members
  leave, from the end of reading order, and the block admits it with `[truncated]`.

## Consequences and follow-ups

- **The prose gate is measurably wider, and the widening is one-directional.** A number the prompt
  printed as page context can now appear in the prose without abstaining the answer, while still
  carrying no citation — so a user may read a stated figure that has no `[n]` beside it. That is a
  deliberate trade: the alternative was abstaining answers whose every claim verified, because the
  model repeated a neighbouring figure it had been shown. The three adversarial gold cases still
  refuse unchanged: `x01-derived-number-in-prose`, `x02-fabricated-chart-value` and
  `x03-unknown-span-citation`. `x01`'s escaping figure (`44`, derived by the model as 72 − 28) is
  refused because that number is printed nowhere on the page.
- **Residual risk, named.** If a page happens to print, literally, the number a model derived, an
  `x01`-shaped case would pass the prose gate on a coincidence. The available tightening — not
  implemented — is to admit a page context number only when the page also carries a verified claim,
  which would keep the relaxation but bind it to something the answer actually proved. It is
  deliberately deferred until a real case demands it, since it would re-narrow answers that today
  verify cleanly.
- **Page context spends the shared prompt budget.** With `page_window_budget_chars = 6000` against a
  total of 18000, a multi-page answer can be pushed into the give-up-context path. The order is
  fixed and safe (last page first, whole blocks only, evidence never surrendered), and the envelope
  reports exactly which blocks survived, but a question that spans many pages sees less context per
  page than a single-page one.
- **Two-column pages interleave.** `reading_key` quantises rows before ordering by `x0`, so on a
  two-column layout the left and right columns alternate line by line rather than reading down one
  column and then the other. The lines themselves are correct and complete; only their order is. A
  column-aware reading order needs a column detection the layout does not currently assert, so it is
  not attempted here.
- **A snapshot published before this ADR gains the capability immediately.** Nothing is re-proved,
  re-indexed or re-published; the block is built from `MemberText`, which the mount already
  produces. The only per-snapshot caveat is that a member whose stored evidence carries no anchor
  sorts to the end of its page (Decision 3), and a snapshot indexed under a non-contextual policy
  simply has an empty `header` and prints its whole index text as the body.
- **The prompt changed, so cached answers miss once.** Page context is part of `build_prompt`'s
  input and `SYSTEM_RULES` gained rule 6, and the completion cache keys on both, so every question
  answered before this change re-enters the model path the first time it is asked again. Rules 1–5
  are byte-identical, so nothing already verified is re-judged under a different instruction.
- **Not addressed.** Page context is never reranked or trimmed by relevance — a page contributes
  all of its remaining members or, past the budget, a truncated tail of them. There is no
  cross-page window (a table continued on the next page is still two unrelated pages), and no
  document-level parent.

## Validation

The offline gate in this repository proves the rules: `pytest tests/enterprise_pdf_rag -q` moved
from **1077** to **1097 passed**, the 20 new cases being `answers/test_page_window.py` (11),
`answers/test_answer_service.py` (6), `answers/test_verify.py` (2) and
`adapters/test_chat_http.py` (1), on top of the preparatory `processing/test_context_builder.py`
(7), `answers/test_ports.py` (5) and `adapters/test_document_catalog.py` (2). The uncitability of
page context is pinned by a test that scripts a claim naming a page context member and asserts the
existing `MODEL_OUTPUT_INVALID` / `"unknown member"` rejection; the budget order is pinned by a case
where the total overruns and the last page's block is the first thing dropped. mypy `--strict` over
500 files reports zero errors, ruff is clean, and all four conformance / architecture / schema /
drift checks pass.

Exactly one pre-existing test changed: `test_answer_service.py::_metadata_document` gave its five
members `page_index = 0`, which under a page window makes each of them the others' context. They
now occupy one page each — which is what they always represented (cover, Hong Kong, Thailand, Group
overview …), page metadata being a page-level fact. No assertion was weakened.

Real-model re-validation on the pinned AIA release is recorded in
[`CLAUDE_HANDOFF.md`](../CLAUDE_HANDOFF.md) under this round's section at the top of that file.
