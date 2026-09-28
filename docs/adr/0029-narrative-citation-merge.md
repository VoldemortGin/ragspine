---
status: accepted
date: 2026-09-28
---

# ADR 0029 — Merge the narrative citation suffix per document (default on)

> Immutable record. Exempt from drift tracking (no `covers`). Supersede, don't edit.

Changes only how the narrative lineage suffix is written. Provenance (`AgentResult.sources`), anti-fabrication
([0023](0023-structured-miss-narrative-fallback.md)) and the narrative number guard
([0024](0024-narrative-number-guard.md)) keep their contracts.

## Context

When the model's prose does not name a source document, `_run_narrative` appends `\n（资料来源：…）` built from
every retrieved snippet as `{doc} {locator}`, joined by `；`, with no de-duplication. Retrieval returns up to 50
snippets, a markdown locator already repeats the file name (`{doc}@page=N#paraA-B`), and most snippets come from
one or two documents. Across the 608 suffixed answers in `data/validation/ragspine-nl-gold/*` the suffix had a
median of 10 entries and 1,106 characters, up to 50 entries and 5,543 characters. The same document name appears
dozens of times. The API / SSE `answer`, the openai-compatible extension and CLI `ask` all show this text, and
Studio shows it above a `Sources (N)` list that already lists every snippet.

## Decision

New pure function `agent/citations.merge_citation(missing) -> str` (no retrieval import), used for the suffix only:

- Group by `doc` in first-seen (retrieval-rank) order; documents are still joined by `；`.
- A document with **one distinct locator** prints `{doc} {locator}`, byte-identical to before.
- With several: page numbers parsed by `(?:^|@)(page|slide)=(\d+)(?=$|[#,])` (covers `X.md@page=18#para1-19`,
  `X.pptx@slide=3,frame=2#para1` and bare `slide=12`) are deduped, sorted, and consecutive runs (≥ 2 pages) become
  `a-b` with an ASCII hyphen: `deck.md page=5-6, 14, 18`, `X.pptx slide=2, 4`. `page=` / `slide=` match the
  locator vocabulary. Locators without a page are deduped and kept verbatim. Order inside a document: the page
  group, the slide group, then verbatim items in first-seen order, all separated by `, `.

Switch `RAGSPINE_CITATION_MERGE=on|off`, **default `on`**; any other value raises `ValueError`. `answer_question`
resolves it next to `resolve_number_guard` and passes it to `_run_narrative` (and the fallback path) as a keyword. No
new public parameter. Off ⇒ the suffix is byte-identical to before, duplicates included.

The suffix is still appended **after** the number guard, so page numbers are never checked or rewritten as answer
numbers. Every number in a merged suffix is either inside a verbatim `doc` / `locator` (exempt as a source ref) or
eaten whole by the guard's page-reference pattern (`page=3, 5-7`, `slide=2, 4`). One guard change was needed for
that: `_Grounding.build` no longer keeps a source ref that the page-reference pattern already matches in full (a bare
`slide=2` / `page=1` locator). Replaced first as a whole string, such a ref split `slide=2, 4` into a stray `, 4`
(or `page=12-13` into `2-13`), which the guard would report. The page-reference pattern still exempts those refs
wherever they are not glued to a preceding letter, which is how a locator appears in an answer.

That change widened an old gap in the page-reference pattern, shared with `@page` locators: its page list also ate a
number that followed with a unit, so with a bare `page=77` ref, `收入 100（page=77, 44 亿美元）` reported `44` before
and nothing after (likewise `（page=77、44%）`, `page=3, 44%`, `d.md@page=77, 44%`). The page list now stops before a
number carrying `%`, a decimal part or a magnitude word (`亿`, `million`, `m`, `bn`, …): `44`, `44%` and `4.5%` (from
`slide=2, 4.5%`) are checked again. A plain `.` is not a stop, so a sentence-final `see page 18.` stays exempt.
Merged suffixes never have a unit after a page, so they are unaffected.

### Known limits (display only, not fixed)

None of these touches `sources`, `answer_plain` or the guard; they only make a merged suffix read ambiguously.

- A verbatim item (a locator without a page) that starts with a digit, e.g. `12`, prints as `page=3, 12`, which
  reads like a third page.
- `slide=2,notes` and `slide=2,frame=1` both parse as slide 2 and merge into one `slide=2`; the difference is lost.
- An empty locator is dropped silently from a merged document's suffix.
- `Page=3` (capital P) is not parsed as a page; it is kept as a verbatim item.

### Why default on, against the "opt-in, default off, flipped by evaluation" convention

That convention ([prd-quality-depth](../prd-quality-depth.md)) is for batches that change retrieval or answer
content, where the eval decides. This one does not fit it:

- **Display only.** `answer_plain`, `sources` (per snippet, full locators, same order), retrieval, prompts and the
  guards are unchanged. The only change is the suffix text.
- **Revertible.** `RAGSPINE_CITATION_MERGE=off` restores the exact old bytes.
- **The eval cannot see it.** nl-gold scores `answer_plain` and the `sources` locators, `is_refusal` looks at the
  template and the lead, batch uses `answer_plain or answer` and per-locator page hits, and the QA ratchet's
  `MockProvider` echoes the sources so no suffix is appended. Default off would never produce evidence to flip it.

## Consequences

- On the 608 nl-gold answers the suffix drops from 899,471 to 50,959 characters in total (median 1,106 → 85, max
  144). The 50-entry `p07` suffix becomes
  `aia-group-2026-interim-results-presentation.md page=4-8, 10-15, 18-19, 23-25, 29, 32-35, 37, 39-41, 47, 49, 54, 62, 66, 70`.
  All 608 merged suffixes pass `ungrounded_numbers` with the agent's refs.
- `#paraA-B` is dropped from a merged document's suffix; the page stays. Full paragraph lineage is still in
  `sources`, so the provenance invariant holds. Groundedness strips the suffix with the same regex (no `）` inside).
- Structured-channel `（来源：…）` per fact, decomposition sub-answer suffixes and client-side grouping (Studio
  `Sources`, CLI lineage lines) are unchanged; a `sources_by_doc` field is not added (clients can group `sources`).
- Frozen by `tests/agent/test_citation_merge.py`: grouping / ranges / slide forms / verbatim and mixed ordering,
  one-locator-per-doc byte-identical to the old join, off byte-identical with duplicates, `sources` and
  `answer_plain` equal on and off, guard-before-suffix order, merged suffixes through `ungrounded_numbers` and
  `guard_narrative_answer` (orchestrator output and a model echoing the merged form), and the counter-case where
  source numbers do not excuse body numbers, a page list that does not swallow a trailing unit number (bare `page=` /
  `slide=` and `@page`), and a sentence-final page ref that stays exempt. `test_answer_plain.py` and `test_narrative_number_guard.py` pass
  unchanged.
