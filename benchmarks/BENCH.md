# RAGSpine performance baseline

Lightweight, **offline, zero-API, deterministic** benchmarks for the RAG hot paths. Every path
runs with `MockProvider` / the offline deterministic embedder / in-memory stores — **no network,
no API key, no real model**. Pure standard library (`time.perf_counter` + `statistics`), so it adds
**no runtime nor dev dependency** (uses `pytest`-free plain `timeit`-style timing).

## Run (always from the package root)

```bash
.venv/bin/python benchmarks/bench_rag.py            # all paths, human-readable
.venv/bin/python benchmarks/bench_rag.py embed vector   # only selected paths
.venv/bin/python benchmarks/bench_rag.py --json     # machine-readable (for archiving)
```

Selectable names: `embed`, `vector`, `chunk`, `retrieve`, `answer`.

Each path is warmed up once, then timed over N repeats; the **median** is the baseline (mean shown
for reference). Synthetic deterministic corpus only — unrelated to `data/golden`, never written to disk.

## What each path measures

| name       | RAG path                              | API exercised |
|------------|---------------------------------------|---------------|
| `embed`    | offline embedding throughput          | `DeterministicEmbeddingBackend.embed_texts` (512 texts/batch, dim=256) |
| `vector`   | in-memory vector store add + top-k    | `InProcessVectorStore.upsert(5000)` + `.query(top-k=10)` (brute-force cosine) |
| `chunk`    | document chunking throughput          | `chunk_document` (~106 K chars, max_chars=480, overlap=80) |
| `retrieve` | build index + hybrid retrieval        | `NarrativeIndex.ingest(200 docs)` + `.retrieve(top-k=10)` — BM25 vs deterministic-vector hybrid |
| `answer`   | end-to-end Q&A latency                | `answer_question` + `MockProvider` + 50-doc narrative index + structured fact (composite route) |

## Baseline numbers

Machine: Apple M1 Pro (macOS, arm64) · Python 3.13.2 · single core, no parallelism.
These are **relative** baselines for regression-spotting on the same machine, not absolute SLAs.

| path                                  | median   | mean     | notes |
|---------------------------------------|----------|----------|-------|
| embed — 512 texts/batch               | 61.5 ms  | 61.7 ms  | ~8.3 K texts/s |
| vector — upsert 5000                   | 0.60 ms  | 0.62 ms  | dict write |
| vector — query top-k=10 over 5000      | 104.5 ms | 104.9 ms | brute-force cosine, ~10 q/s — slowest single op |
| chunk — ~106 K chars                   | 2.49 ms  | 2.49 ms  | ~43 M chars/s, 250 chunks |
| retrieve(bm25) — ingest 200 docs       | 120.6 ms | 131.5 ms | 400 chunks |
| retrieve(bm25) — retrieve top-k=10     | 17.3 ms  | 17.3 ms  | ~58 q/s |
| retrieve(hybrid) — ingest 200 docs     | 221.2 ms | 221.3 ms | + deterministic embed at ingest |
| retrieve(hybrid) — retrieve top-k=10   | 26.8 ms  | 26.8 ms  | ~37 q/s |
| answer — end-to-end (composite route)  | 1.9 ms   | 1.9 ms   | ~520 qps, MockProvider |

### Reading the baseline

- **`vector.query` dominates** at ~104 ms over 5000 records — `InProcessVectorStore` is a pure-Python
  brute-force cosine scan (deterministic by design). It is the natural first target if vector recall
  ever needs to scale; the pluggable `make_vector_store('sqlite_vec'|'qdrant')` adapters exist for that.
- **`answer` is sub-2 ms** because `MockProvider` is deterministic and local — the end-to-end number
  isolates orchestration/retrieval overhead from LLM latency (which a real provider would dominate).
- Hybrid (deterministic-vector) ingest/retrieve costs ~2x BM25-only: the extra is the offline embed
  at ingest plus the vector channel at query.
