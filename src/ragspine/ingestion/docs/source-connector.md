---
covers:
  - src/ragspine/ingestion/source/connector.py
  - src/ragspine/ingestion/source/memory.py
  - src/ragspine/ingestion/source/remote.py
  - src/ragspine/ingestion/source/bridge.py
verified-against: e6d36368639903633ceb4d142684324cc12bce57
---

# SourceConnector seam — where raw documents enter ingestion

Deep dive behind the `SourceConnector` row of
[`docs/prd-breadth-via-adapters.md`](../../../../docs/prd-breadth-via-adapters.md) (P1, 🔧 commodity).
The PRD is the originating spec; this is the live contract.

## The seam

`SourceConnector` owns exactly one concern: **answer "where do raw documents enter ingestion."** It
enumerates raw documents (local disk today; S3 / Drive / Notion / HTTP later) and yields them as a
stream of `RawDoc`. It does *not* extract, chunk, or persist — those stay downstream in `ingestion/`.
Binding the seam here is what lets the **provenance** invariant be enforced at the *point of entry*,
not bolted on after extraction.

```python
@runtime_checkable
class SourceConnector(Protocol):
    def iter_documents(self) -> Iterable[RawDoc]: ...

@dataclass(frozen=True)
class RawDoc:
    source_doc_id: str                 # blood-lineage root, = filename (same as ingestion today)
    locator: str                       # where it came from: a path / s3:// URI / URL
    content: bytes                     # binary-safe raw bytes (pptx/pdf are binary; text encodes)
    content_type: str = ""             # format hint for downstream routing (".pdf" / ".pptx")
    metadata: Mapping[str, str] = ...  # extra (e.g. file_hash for version lineage)
```

`RawDoc` is **frozen** — once a document enters the stream its lineage can't be mutated in place.
It is *pre-extraction* (raw bytes entering the pipeline), deliberately distinct from `NarrativeDoc`
(post-extraction text + locators) and `StyledGrid` (post-extraction IR): the connector is the input
to extraction, not its output, so it is its own type rather than a reuse of either.

## The offline default

`FilesystemConnector` is the zero-dependency default: a `pathlib` recursive walk over a `root`
directory.

- **`source_doc_id = path.name`** — byte-identical to how `narrative_ingest` identifies a file today
  (`doc_id = path.name`), so the lineage root is the same whether a file is fed directly or through
  the connector. Same convention as `StyledGrid.source_doc_id` / `fact.source_doc_id`.
- **`locator = str(path)`**, **`content_type = path.suffix.lower()`**, and
  **`metadata["file_hash"]` = sha256 of the bytes** (same hash as `extraction.compute_file_hash`,
  computed from the bytes already read — no second read).
- **Ignore rule** matches `narrative_ingest._resolve_inputs`: hidden files (`.` prefix) and Office
  temp files (`~$` prefix) are skipped. An optional `suffixes` filter (case-insensitive) narrows
  formats; `None` means all.
- **Deterministic order** — files are sorted by their **relative-to-root POSIX path** before yielding,
  so the enumeration is stable across platforms and independent of the OS walk order. A missing `root`
  raises `NotADirectoryError` lazily (at iteration, not construction).

## More built-in connectors

Beyond the filesystem default, three connectors ship built-in, each still yielding `RawDoc`s with a
non-empty `source_doc_id` + `locator` (so they inherit the same provenance guarantee):

- **`InMemoryConnector(docs)`** (`memory.py`) — zero-dependency, deterministic. Wraps an in-memory
  `Sequence[RawDoc]` (test fixtures, a read-only external library, upstream artifacts) and yields them
  in **given order** (order is the contract; snapshotted to a tuple at construction, so it is stable
  across calls and immune to later mutation of the source sequence). This is the connector the
  provenance conformance pack registers a second time (alongside `filesystem`).
- **`HttpConnector(urls, *, headers=None, client=None)`** (`remote.py`) — generic HTTP pull. GETs each
  URL in given order and yields `RawDoc(source_doc_id=`URL path last segment (a stable slug of the URL
  if empty)`, locator=`the URL`, content=`response bytes`, content_type=`Content-Type header or URL
  suffix`, metadata={"file_hash": sha256})`. `httpx` is **lazy-imported inside the method**, never at
  module top; a `client` can be injected (a fake `httpx.Client` exposing `.get(url, headers=...)`) for
  offline determinism. Gated behind the `[connectors]` extra.
- **`NotionConnector(*, token, page_ids, base_url="https://api.notion.com/v1", client=None)`**
  (`remote.py`) — Notion REST pull. For each `page_id`, GETs `{base_url}/pages/{page_id}` +
  `{base_url}/blocks/{page_id}/children` with the Notion auth headers (`Authorization: Bearer {token}`,
  `Notion-Version: 2022-06-28`), serializes the merged `{"page": …, "blocks": …}` JSON to bytes as
  `content` (`content_type="application/json"`). `source_doc_id = page_id`, `locator =` the page `url`
  field (or `notion://page/{page_id}`), `metadata["file_hash"] = sha256`. Same lazy-`httpx` /
  injectable-`client` discipline as `HttpConnector`.

## The factory

`make_source_connector(spec, **kwargs)` mirrors `make_vector_store`: it turns "which connector" from
a code change into one spec string / env var.

- `None` / `"none"` → `None` (no connector configured). Reads `RAGSPINE_SOURCE_CONNECTOR` when `spec`
  is omitted.
- `"filesystem"` / `"fs"` / `"local"` (case-/whitespace-insensitive) → `FilesystemConnector` (pass
  `root=` as a kwarg).
- `"in_memory"` / `"memory"` / `"fixture"` → `InMemoryConnector` (pass `docs=`).
- `"http"` / `"https"` / `"url"` → `HttpConnector` (pass `urls=`). The `http`/`notion` loaders
  lazy-import `remote.py`, which in turn lazy-imports `httpx` inside its methods — so resolving these
  names never pulls `httpx` onto the import path of the offline defaults.
- `"notion"` → `NotionConnector` (pass `token=` + `page_ids=`).
- any other name → **entry-point auto-discovery** over the `ragspine.source_connectors` group, so a
  third-party `ragspine-foo` registers a connector by name with **no core PR**; an unknown name raises
  a `ValueError` listing the built-ins and discovered names. Core imports zero SDKs; a remote adapter
  lazy-imports its client inside its own methods, gated behind an extra.

## The lineage → fact_store bridge

`bridge.py`'s `ingest_from_connector(connector, store, registry, queue, *, dry_run=False,
manifest=None, batch_id=None, valid_as_of=None) -> list[IngestReport]` is the thin seam that carries
**lineage end-to-end into the `FactStore` without dropping it**. For each `RawDoc`, it materializes the
bytes to a temp file whose **filename equals `raw.source_doc_id`** (inside a `TemporaryDirectory`,
cross-platform via `pathlib`) and delegates to `structured.ingestion.ingest_file`. Because `ingest_file`
sets `source_doc_id = path.name` and recomputes the file_hash from the bytes, every produced `Fact`
inherits `raw.source_doc_id` as `Fact.source_doc_id` and `raw.metadata["file_hash"]` as
`Fact.source_file_hash` **automatically** — the bridge adds *no* new lineage logic, it just
materializes and delegates. A `RawDoc` whose `content_type` isn't a supported structured format simply
produces a failed/empty `IngestReport` from `ingest_file`; the bridge does not special-case it. This is
bound by `tests/conformance/test_source_lineage_e2e.py`: a real-fixture `RawDoc` proves lineage
survives, and a `RawDoc` given a *different* `source_doc_id` proves the connector's id wins over the
temp path (the temp dir never leaks into `Fact.source_doc_id`/`source_file_hash`).

## Invariant binding (the conformance pack)

`tests/conformance/test_source_connector_provenance.py` is parametrized over every connector
registered in `conftest.SOURCE_CONNECTOR_IMPLS` (today: `filesystem` + `in_memory`). For each, over a
fixture tree,
it asserts the **provenance** invariant: every emitted `RawDoc` carries a **non-null `source_doc_id` +
`locator`**, lineage survives the entry transform. The assertion lives in one shared judgment core
(`_assert_provenance_complete`); a deliberately lineage-dropping stub connector is fed the *same* core
and **must fail** it — proving the pack is non-vacuous, the same honest-counter-proof discipline the
`VectorStore` invariant pack uses for its filter. Any future connector inherits the whole pack by
registering one line. The offline fanout covers the zero-dependency connectors (`filesystem`,
`in_memory`); the network-bound `HttpConnector` / `NotionConnector` are covered instead by
injected-client unit tests (`tests/ingestion/source/test_remote_connectors.py`), so the offline
conformance run stays hermetic.

## Status / wiring

Shipped **seam-first** (as `VectorStore` was). The `InMemoryConnector` / `HttpConnector` /
`NotionConnector` connectors + the `ingest_from_connector` bridge into the structured `FactStore` are
landed and conformance-bound, but the seam is *not* yet wired into the existing narrative ingest path —
`narrative_ingest` still reads local files directly and its behavior and tests are unchanged. Wiring
`FilesystemConnector` into that path (behavior-identical) and a narrative-side bridge are the remaining
P1 follow-ups tracked in the breadth PRD.
