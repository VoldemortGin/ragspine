---
covers:
  - src/ragspine/agent/
  - src/ragspine/retrieval/
  - src/ragspine/service/faq/
verified-against: 08c27176f17abf97df5629c081ca9720d2dbfecb
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
- **Narrative** — hybrid retrieve → [opt-in per-page de-dup, `RAGSPINE_PAGE_PARENT`] → listwise rerank → [opt-in page images for the top-N pages, `RAGSPINE_PAGE_IMAGES`; needs a source PDF linked at ingest] → synthesize with citations (whole-page context when page-parent is on; text + page-image parts when the provider reads images).
- **Composite** — run both, compare, merge.
