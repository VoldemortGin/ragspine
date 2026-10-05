# ADR 00NN: Description embeddings are sent in batches, cached one object at a time

Status: Proposed, 2026-10-05 (number assigned at integration). Changes only the transport of
the index-build embeddings behind [ADR 0012](0012-chart-index-text-and-retrieval-seats.md)'s
`index-text-embedding-v1` stage cache. It changes **no cache key, no cache entry, no vector,
no snapshot id and no single-text request**: an ingestion directory indexed before this ADR is
re-indexed with zero embedding requests.

## Context

`ProcessingRetrieval.build` embedded each eligible member with one `embed_description` call,
and `LocalEmbeddingAdapter` sent one `/v1/embeddings` request per call. A 300-page report has
about 3000 indexable objects, so a first index cost about 3000 sequential HTTPS round trips
(about 25–50 minutes at 0.5–1 s each), although every OpenAI-compatible endpoint, Azure OpenAI
included, takes `input` as an array and answers a `data` list carrying each input's `index`.

## Decision

- **Optional port.** `BatchEmbeddingPort` (`figures/ports.py`) extends `EmbeddingPort` with
  `embed_descriptions(texts)` and `request_count`; element `i` must equal
  `embed_description(texts[i])`. `EmbeddingPort` and the offline embedder are unchanged; an
  embedder without the capability is called once per object, as before.
- **Index build.** `build` validates every member first, then hands the texts missing from the
  stage cache (deduplicated by cache key) to a batch embedder in slices of 256 and stores each
  vector under the unchanged per-object key and `RetrievalEmbedding` entry before the next slice
  is sent, so a failure keeps the slices already paid for. The member loop then reads them as
  cache hits. `DraftIndex.embedding_requests` / `embedded_objects` report what was sent (counts
  only).
- **Adapter.** `LocalEmbeddingAdapter.embed_descriptions` packs at most
  `EMBEDDING_BATCH_MAX_ITEMS = 16` inputs and `EMBEDDING_BATCH_MAX_CHARS = 48 000` characters per
  request (constructor arguments, no new setting). 16 is the per-request input cap of Azure
  OpenAI embedding deployments on older API versions, the strictest common limit (OpenAI allows
  2048 inputs / 300k tokens); 48 000 characters stays far below any per-request token cap even
  for CJK text. A text over the character cap goes alone. A one-text batch is the unchanged
  string-`input` request with its 30 s timeout; a batch waits 60 s and accepts a reply of up to
  1 MiB per input.
- **Validation.** A batch reply must hold exactly one item per input, its `index` set exactly
  `0..n-1`, every vector non-empty and finite, all of one dimension; anything else is a failed
  request. Vectors are aligned by `index`, never by position.
- **Degradation.** A failed batch (any HTTP status, timeout, connection error, size limit or
  invalid reply) is halved recursively; a single input fails exactly as `embed_description`
  does and the build raises as before. HTTP 401 / 403 / 404 are raised at once. A 400 on a pair
  whose two singles then answer, before any array ever succeeded on this adapter, marks arrays
  unsupported; past `EMBEDDING_BATCH_MAX_FAILURES = 8` failed batches the adapter also stops
  batching. Either way it sends single inputs for the rest of its life, so a degraded endpoint
  costs at most a handful of extra requests per run, never one per batch.

## Consequences

- 3000 uncached objects: 3000 requests → 188 (16 per request), roughly 25–50 min → 2–4 min of
  round trips at the same per-request latency (an estimate; a batch request itself is somewhat
  slower than a single one).
- Default on for every mode; identical disk state either way (tested byte for byte), so no
  switch is needed. Real Azure limits cannot be verified offline; the adapter falls back on its
  own if a deployment refuses the defaults.
- The query embedding (`embed_query`, preflight) stays a single request. ragspine's narrative
  `SingleTextEmbeddingBackend` still loops single texts; it can adopt `embed_descriptions` later.
- What is embedded is unchanged: every eligible member's index text, with no sensitivity
  filter in this chain (gateway-mode embedding sends it to the LLM gateway, as before).
