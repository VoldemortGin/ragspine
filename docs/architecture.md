---
covers:
  - src/ragspine/agent/
  - src/ragspine/retrieval/
  - src/ragspine/service/faq/
verified-against: 0c3be5c66766bd6fa9b36743a7bf725642838ba1
---

# Architecture — request flow & dual channel

Authoritative expansion of the request flow summarized in `README.md`. Keep the
canonical one-liner diagram in `README.md`; the control-flow detail lives here.

## Request flow

<!-- TODO: expand control flow:
     intent parse → clarification gate → FAQ short-circuit (service edge)
     → route (structured / narrative / composite) → anti-fabrication guard. -->

## Channels

- **Structured** — function-calling over the fact store → `found` / `not_found` / `unrecognized`.
- **Narrative** — hybrid retrieve → [per-page de-dup, `RAGSPINE_PAGE_PARENT`, default `page+child`] → listwise rerank → [opt-in page images for the top-N pages, `RAGSPINE_PAGE_IMAGES`; needs a source PDF linked at ingest] → synthesize with citations (whole-page context unless page-parent is `off`; text + page-image parts when the provider reads images).
- **Composite** — run both, compare, merge.
- **Route fallback (ADR 0023)** — a structured-route question with a missing metric or no `found` fact first tries the narrative channel (`RAGSPINE_NARRATIVE_FALLBACK`, default `on`). The answer is kept only if it's grounded (a number from the snippets, no `NO_ANSWER`); otherwise the structured ask / not-found result stands.
