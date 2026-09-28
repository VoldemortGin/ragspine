---
covers:
  - src/ragspine/agent/
  - src/ragspine/retrieval/
  - src/ragspine/service/faq/
verified-against: 241ec9dc1705ab7e9fe39688266505aac5ff9f8b
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
- **LLM output truncation** — every provider call (tool loop, narrative synthesis, translation, rerank, decompose) goes through `chat`, so the provider layer handles a length cut: `agent/truncation.retry_on_truncation` doubles the output budget (cap `RAGSPINE_LLM_TRUNCATION_MAX_TOKENS`, default 16384; at most 2 retries) with reasoning turned off, and raises `TruncatedOutputError` (a `ProviderError`) when the output is still cut, so the existing honest degrade applies — a half answer or half tool-call JSON is never used. `RAGSPINE_LLM_TRUNCATION_RETRY`, default `on`; no truncation ⇒ one call with byte-identical request parameters.
- **Narrative number guard (ADR 0024)** — every narrative answer (narrative route, composite attribution, accepted fallback) may only carry numbers found in the retrieved snippets (`RAGSPINE_NARRATIVE_NUMBER_GUARD`, default `on`; `agent/number_guard.py`). Otherwise it is rewritten deterministically: "not directly given" plus the grounded raw values, or just the offending sentences dropped. The narrative prompt also forbids calculation and order / causal inference.

Optional composition preserves the default request flow: `retrieval.postprocess.make_postprocessing_retriever`
wraps an already-isolated narrative retriever, and `retrieval.fusion.route_fusion.make_fused_retriever`
adds text/visual page RRF only when a visual retriever is explicitly supplied. The fusion keeps original
source fields; with filters, visual hits can only confirm identified pages already returned by the text leg.
