"""Indexing sends uncached index texts in batches and stores exactly the single-path cache."""

from pathlib import Path

import pytest

from enterprise_pdf_rag.adapters.document_store import LocalDocumentStore
from enterprise_pdf_rag.adapters.draft_publication import (
    DraftIndex,
    index_draft,
    publish_draft,
    qualify_draft,
)
from enterprise_pdf_rag.adapters.offline import OfflineDescriptionEmbedder
from enterprise_pdf_rag.adapters.processing_retrieval import ProcessingRetrieval
from enterprise_pdf_rag.adapters.processing_store import ProcessingStore
from ragspine.common.evidence.file_placement import stored_names, stored_path
from ragspine.common.evidence.providers.local_models import LocalEmbeddingAdapter
from tests.enterprise_pdf_rag.adapters.generic_publication_helpers import (
    ingest_generic_semantics,
)
from tests.enterprise_pdf_rag.adapters.test_embedding_batches import _adapter, _Endpoint

_QUERY = "Metric Value Margin revenue"


class _SingleOnly:
    """The same endpoint through the port alone: what indexing did before batching."""

    def __init__(self, adapter: LocalEmbeddingAdapter) -> None:
        self._adapter = adapter

    @property
    def fingerprint(self) -> str:
        return self._adapter.fingerprint

    def embed_description(self, text: str) -> tuple[float, ...]:
        return self._adapter.embed_description(text)

    def embed_query(self, text: str) -> tuple[float, ...]:
        return self._adapter.embed_query(text)


def _draft(root: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, str]:
    root.mkdir(parents=True, exist_ok=True)
    ingest, _ = ingest_generic_semantics(root, monkeypatch, page_count=5, table_page=True)
    source_store, processing_store = Path(ingest.source_store), Path(ingest.processing_store)
    qualify_draft(
        source_store=source_store,
        processing_store=processing_store,
        processing_id=ingest.processing_id,
    )
    return source_store, processing_store, ingest.processing_id


def _index(draft: tuple[Path, Path, str], embedder: object) -> DraftIndex:
    source_store, processing_store, processing_id = draft
    return index_draft(
        source_store=source_store,
        processing_store=processing_store,
        processing_id=processing_id,
        embedder=embedder,  # type: ignore[arg-type]
    )


def _stage_cache(processing_store: Path) -> dict[str, bytes]:
    flat = processing_store / "stage-cache"
    return {name: _pointer(flat, name).read_bytes() for name in stored_names(flat)}


def _pointer(flat: Path, name: str) -> Path:
    path = stored_path(flat, name)
    assert path is not None
    return path


def _cached_artifacts(processing_store: Path, names: set[str]) -> dict[str, bytes]:
    store = ProcessingStore(processing_store)
    artifacts: dict[str, bytes] = {}
    for name in sorted(names):
        outcome = store.cached(name)
        assert outcome is not None and outcome.artifact is not None
        artifacts[name] = store.assets.get(outcome.artifact)
    return artifacts


def _ranking(draft: tuple[Path, Path, str], indexed: DraftIndex) -> tuple[tuple[str, float], ...]:
    source_store, processing_store, _ = draft
    publish_draft(
        source_store=source_store,
        processing_store=processing_store,
        processing_id=indexed.indexed_processing_id,
    )
    outputs = ProcessingStore(processing_store)
    _, manifest = outputs.load_current()
    assert manifest.retrieval is not None
    retrieval = ProcessingRetrieval(
        LocalDocumentStore(source_store), outputs, _SingleOnly(_adapter(_Endpoint()))
    )
    hits = retrieval.search(manifest.retrieval, _QUERY, limit=10)
    return tuple((hit.member_id, hit.score) for hit in hits)


def test_batched_indexing_sends_fewer_requests_and_stores_the_single_path_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    single_draft = _draft(tmp_path / "single", monkeypatch)
    batch_draft = _draft(tmp_path / "batch", monkeypatch)
    before = set(_stage_cache(batch_draft[1]))

    single_endpoint = _Endpoint()
    single = _index(single_draft, _SingleOnly(_adapter(single_endpoint)))
    batch_endpoint = _Endpoint()
    batched = _index(batch_draft, _adapter(batch_endpoint, batch_max_items=4))

    objects = single.member_count
    assert objects >= 6
    assert single_endpoint.sizes == [1] * objects
    assert (single.embedding_requests, single.embedded_objects) == (objects, objects)
    assert batch_endpoint.sizes == [4] * (objects // 4) + ([objects % 4] if objects % 4 else [])
    assert batched.embedding_requests == -(-objects // 4) < objects
    assert batched.embedded_objects == objects
    # Same snapshot, same index, and the stage cache byte for byte.
    assert batched.retrieval_snapshot_id == single.retrieval_snapshot_id
    assert batched.indexed_processing_id == single.indexed_processing_id
    embedded = set(_stage_cache(batch_draft[1])) - before
    assert len(embedded) == objects
    assert _stage_cache(batch_draft[1]) == _stage_cache(single_draft[1])
    assert _cached_artifacts(batch_draft[1], embedded) == _cached_artifacts(
        single_draft[1], embedded
    )
    assert _ranking(batch_draft, batched) == _ranking(single_draft, single)


def test_a_reindex_sends_nothing_even_over_a_cache_the_single_path_wrote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    draft = _draft(tmp_path, monkeypatch)
    first = _index(draft, _SingleOnly(_adapter(_Endpoint())))

    endpoint = _Endpoint()
    again = _index(draft, _adapter(endpoint))

    assert endpoint.inputs == []
    assert (again.embedding_requests, again.embedded_objects) == (0, 0)
    assert again.retrieval_snapshot_id == first.retrieval_snapshot_id


def test_only_uncached_index_texts_are_sent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    draft = _draft(tmp_path, monkeypatch)
    before = set(_stage_cache(draft[1]))
    first = _index(draft, _adapter(_Endpoint()))
    embedded = sorted(set(_stage_cache(draft[1])) - before)
    for name in embedded[:2]:
        _pointer(draft[1] / "stage-cache", name).unlink()

    endpoint = _Endpoint()
    again = _index(draft, _adapter(endpoint))

    assert endpoint.sizes == [2]
    assert (again.embedding_requests, again.embedded_objects) == (1, 2)
    assert again.retrieval_snapshot_id == first.retrieval_snapshot_id


def test_an_embedder_without_batches_is_called_once_per_object(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    indexed = _index(_draft(tmp_path, monkeypatch), OfflineDescriptionEmbedder())
    assert indexed.embedding_requests == indexed.embedded_objects == indexed.member_count


def test_a_failed_batch_index_keeps_what_earlier_slices_stored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ragspine.common.evidence.providers.providers import ProviderRequestError

    draft = _draft(tmp_path, monkeypatch)
    before = set(_stage_cache(draft[1]))
    calls = {"count": 0}

    def fault(inputs: list[str] | str) -> ProviderRequestError | None:
        calls["count"] += 1
        return (
            ProviderRequestError("Local model returned HTTP 401", status=401, category="http")
            if calls["count"] > 1
            else None
        )

    monkeypatch.setattr("enterprise_pdf_rag.adapters.processing_retrieval._EMBED_SLICE", 2)
    with pytest.raises(ProviderRequestError):
        _index(draft, _adapter(_Endpoint(fault=fault), batch_max_items=2))
    # The first slice answered and is cached; the run that resumes sends only the rest.
    assert len(set(_stage_cache(draft[1])) - before) == 2
