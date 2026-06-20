---
covers:
  - src/ragspine/ingestion/source/connector.py
verified-against: 443885a09c4377f83dcd1394d77a64962da5f0fe
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

## The factory

`make_source_connector(spec, **kwargs)` mirrors `make_vector_store`: it turns "which connector" from
a code change into one spec string / env var.

- `None` / `"none"` → `None` (no connector configured). Reads `RAGSPINE_SOURCE_CONNECTOR` when `spec`
  is omitted.
- `"filesystem"` / `"fs"` / `"local"` (case-/whitespace-insensitive) → `FilesystemConnector` (pass
  `root=` as a kwarg).
- any other name → **entry-point auto-discovery** over the `ragspine.source_connectors` group, so a
  third-party `ragspine-foo` registers a connector by name with **no core PR**; an unknown name raises
  a `ValueError` listing the built-ins and discovered names. Core imports zero SDKs; a remote adapter
  would lazy-import its client inside its own `__init__`, gated behind an extra.

## Invariant binding (the conformance pack)

`tests/conformance/test_source_connector_provenance.py` is parametrized over every connector
registered in `conftest.SOURCE_CONNECTOR_IMPLS` (today: `filesystem`). For each, over a fixture tree,
it asserts the **provenance** invariant: every emitted `RawDoc` carries a **non-null `source_doc_id` +
`locator`**, lineage survives the entry transform. The assertion lives in one shared judgment core
(`_assert_provenance_complete`); a deliberately lineage-dropping stub connector is fed the *same* core
and **must fail** it — proving the pack is non-vacuous, the same honest-counter-proof discipline the
`VectorStore` invariant pack uses for its filter. Any future connector (S3 / Drive / Notion) inherits
the whole pack by registering one line.

## Status / wiring

Shipped **seam-first and standalone** (as `VectorStore` was), *not* yet wired into the existing
narrative ingest path — `narrative_ingest` still reads local files directly and its behavior and tests
are unchanged. Wiring `FilesystemConnector` into that path (behavior-identical) and adding the first
remote adapters (S3/HTTP) are the P1 follow-ups tracked in the breadth PRD.
