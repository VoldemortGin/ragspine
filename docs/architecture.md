---
covers:
  - src/ragspine/agent/
  - src/ragspine/retrieval/
  - src/ragspine/service/faq/
verified-against: a8c67c288f4d14f00da8a4016c74b4c067b7944d
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
- **Narrative** — hybrid retrieve (index text = chunk text; opt-in heading path via `RAGSPINE_CONTEXTUAL_INDEX`, default `off`; a cross-lingual question adds its LLM translation as an extra retrieval query, `RAGSPINE_QUERY_TRANSLATION`, default `auto`) → [per-page de-dup, `RAGSPINE_PAGE_PARENT`, default `page+child`] → listwise rerank → [opt-in page images for the top-N pages, `RAGSPINE_PAGE_IMAGES=all|tagged` (`tagged` keeps only pages whose ingest-time tags — table / figure / low text — hit the trigger, remove-only, ADR 0025); needs a source PDF linked at ingest] → synthesize with citations (whole-page context unless page-parent is `off`; text + page-image parts when the provider reads images).
- **Composite** — run both, compare, merge.
- **Route fallback (ADR 0023)** — a structured-route question with a missing metric or no `found` fact first tries the narrative channel (`RAGSPINE_NARRATIVE_FALLBACK`, default `on`). The answer is kept only if it's grounded (a number from the snippets, no `NO_ANSWER`); otherwise the structured ask / not-found result stands.
- **Narrative number guard (ADR 0024)** — every narrative answer (narrative route, composite attribution, accepted fallback) may only carry numbers found in the retrieved snippets (`RAGSPINE_NARRATIVE_NUMBER_GUARD`, default `on`; `agent/number_guard.py`). Otherwise it is rewritten deterministically: "not directly given" plus the grounded raw values, or just the offending sentences dropped. The narrative prompt also forbids calculation and order / causal inference.

Optional composition preserves the default request flow: `retrieval.postprocess.make_postprocessing_retriever`
wraps an already-isolated narrative retriever, and `retrieval.fusion.route_fusion.make_fused_retriever`
adds text/visual page RRF only when a visual retriever is explicitly supplied. The fusion keeps original
source fields; with filters, visual hits can only confirm identified pages already returned by the text leg.
