---
covers:
  - src/ragspine/agent/
  - src/ragspine/retrieval/
  - src/ragspine/service/faq/
verified-against: eaf2f3392fbb7840042a4bf68076da35fcb2f943
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
- **Narrative** — hybrid retrieve → [opt-in per-page de-dup, `RAGSPINE_PAGE_PARENT`] → listwise rerank → synthesize with citations (whole-page context when page-parent is on).
- **Composite** — run both, compare, merge.
