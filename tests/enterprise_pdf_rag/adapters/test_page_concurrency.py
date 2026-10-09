"""Pages and objects of one document at once (ADR 0045): ``APP_PAGE_CONCURRENCY``.

N = 1 is the serial page loop, byte for byte. N > 1 runs up to N pages' layout calls and
objects' semantic calls at once, under one semaphore of N, and still records every page and
object in order: the same stage entries, the same manifest, the same processing id.
"""

import _thread
import json
import threading
import time
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path

import pytest

from enterprise_pdf_rag.adapters.aia_processing import ProcessingPipeline
from enterprise_pdf_rag.adapters.document_store import LocalDocumentStore
from enterprise_pdf_rag.adapters.folder_pipeline import run_folder_pipeline
from enterprise_pdf_rag.adapters.offline import OfflineDescriptionEmbedder
from enterprise_pdf_rag.adapters.page_partition import ModelPagePartitioner
from enterprise_pdf_rag.adapters.pdf_ingestion import ingest_pdf
from enterprise_pdf_rag.adapters.processing_store import ProcessingStore
from ragspine.common.evidence.configs import get_settings
from ragspine.common.evidence.object_backend import sqlite as sqlite_backend
from ragspine.common.evidence.providers import transient
from ragspine.common.evidence.providers.json_completion import JsonCompletionClient
from ragspine.common.evidence.providers.providers import LLMConfig, ProviderRequestError
from ragspine.extraction.evidence.document.models import (
    DocumentManifest,
    PageRecord,
    RegionRecord,
    TextSidecar,
    TextSpan,
)
from ragspine.extraction.evidence.figures.models import Confidence
from ragspine.extraction.evidence.page.models import (
    LayoutObject,
    ObjectKind,
    ObjectProcessingRecord,
    PageInput,
    PagePartition,
    StageOutcome,
    StageState,
)
from tests.enterprise_pdf_rag.adapters.generic_publication_helpers import PROVIDER_BASE_URL
from tests.enterprise_pdf_rag.adapters.lite_ingest_helpers import (
    FULL_PUBLISHED_ID,
    FULL_REQUESTS_DIGEST,
    FULL_STORE_DIGEST,
    FULL_STORE_FILES,
    lite_env,
    mixed_folder,
    store_digest,
)
from tests.enterprise_pdf_rag.adapters.test_folder_pipeline import _PER_PDF
from tests.enterprise_pdf_rag.adapters.test_parallel_documents import (
    Events,
    _assert_no_claim_left,
    _env,
    _files,
    _folder,
    _logical_tree,
    _Model,
    _run,
    _worker_threads,
)
from tests.enterprise_pdf_rag.adapters.test_pdf_ingestion import authored_pdf

_SPANS = 3


# ---- a synthetic source and fakes that record what overlaps ------------------------------------


def _source(tmp_path: Path, pages: int) -> tuple[LocalDocumentStore, str]:
    sources = LocalDocumentStore(tmp_path / "source")
    pdf = sources.put(b"page-concurrency source", media_type="application/pdf")
    svg = sources.put(b'<svg xmlns="http://www.w3.org/2000/svg"/>', media_type="image/svg+xml")
    records = []
    for index in range(pages):
        text = TextSidecar(
            "source-text-v1",
            pdf.sha256,
            index,
            tuple(
                TextSpan(
                    f"p{index}-s{span}",
                    f"Page {index + 1} line {span}",
                    (1.0, 1.0 + 20 * span, 50.0, 10.0 + 20 * span),
                )
                for span in range(_SPANS)
            ),
        )
        sidecar = sources.put(json.dumps(asdict(text)).encode(), media_type="application/json")
        records.append(PageRecord(index, 960.0, 540.0, 0, svg, sidecar, 1, ()))
    manifest = DocumentManifest(
        "source-ingestion-v1",
        "unit.pdf",
        pdf,
        "test-source",
        tuple(records),
        RegionRecord(0, (0.0, 0.0, 100.0, 100.0), svg, svg, records[0].text, ()),
    )
    return sources, sources.publish(manifest)


class _Tracker:
    """Counts the units of model-bearing work in flight together, across pages and objects."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.in_flight = 0
        self.max_in_flight = 0
        self.partitioned: list[int] = []

    def __enter__(self) -> None:
        with self._lock:
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)

    def __exit__(self, *_exc: object) -> None:
        with self._lock:
            self.in_flight -= 1


class _Partition:
    fingerprint = "concurrency-layout-v1"

    def __init__(self, tracker: _Tracker, delay: float = 0.0) -> None:
        self.tracker = tracker
        self.delay = delay

    def partition(self, page: PageInput) -> PagePartition:
        with self.tracker:
            time.sleep(self.delay)
            with self.tracker._lock:
                self.tracker.partitioned.append(page.page_index)
        objects = tuple(
            LayoutObject(
                f"p{page.page_index}-o{position}",
                ObjectKind.TEXT,
                span.bbox,
                (span.span_id,),
                "test text",
                Confidence(None, "test"),
            )
            for position, span in enumerate(page.text.spans)
        )
        return PagePartition(
            "layout-v1",
            page.source_manifest_id,
            page.source_sha256,
            page.page_index,
            self.fingerprint,
            objects,
            (),
        )


class _Objects:
    """Later objects of a page answer sooner, so only an ordered fill keeps their order."""

    def __init__(self, tracker: _Tracker, delay: float = 0.0) -> None:
        self.tracker = tracker
        self.delay = delay

    def process(self, page: PageInput, item: LayoutObject) -> ObjectProcessingRecord:
        position = int(item.object_id.rsplit("-o", 1)[1])
        with self.tracker:
            time.sleep(self.delay * (_SPANS - position))
        return ObjectProcessingRecord(
            item.object_id,
            item.kind,
            (
                StageOutcome(
                    "ir",
                    "a" * 64,
                    StageState.DEFERRED,
                    "concurrency-objects-v1",
                    diagnostic=f"object {item.object_id} of page {page.page_index}",
                ),
            ),
        )


def _pipeline(
    root: Path,
    sources: LocalDocumentStore,
    tracker: _Tracker,
    *,
    concurrency: int,
    partition_delay: float = 0.0,
    object_delay: float = 0.0,
    objects: bool = True,
) -> ProcessingPipeline:
    return ProcessingPipeline(
        sources,
        ProcessingStore(root),
        _Partition(tracker, partition_delay),
        _Objects(tracker, object_delay) if objects else None,
        normalize_layout=False,
        page_concurrency=concurrency,
    )


def _page_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name.startswith(("ingest-page", "ingest-object"))]


# ---- 1. the same bytes, in the same order --------------------------------------------------------


@pytest.mark.usefixtures("object_backend")
def test_pages_at_once_publish_exactly_what_one_page_at_a_time_publishes(tmp_path: Path) -> None:
    sources, source_id = _source(tmp_path, 8)
    progress: dict[int, list[tuple[int, int]]] = {1: [], 4: []}
    results = {}
    for concurrency in (1, 4):
        tracker = _Tracker()
        pipeline = _pipeline(
            tmp_path / f"processing-{concurrency}",
            sources,
            tracker,
            concurrency=concurrency,
            partition_delay=0.02,
            object_delay=0.01,
        )
        seen = progress[concurrency]

        def report(done: int, total: int, seen: list[tuple[int, int]] = seen) -> None:
            seen.append((done, total))

        results[concurrency] = pipeline.run(
            source_id, selected_page_indices=tuple(range(8)), on_page=report
        )
        if concurrency == 4:
            assert 1 < tracker.max_in_flight <= 4
        else:
            assert tracker.max_in_flight == 1
    assert results[4] == results[1]
    manifest = results[4][1]
    assert [page.page_index for page in manifest.pages] == list(range(8))
    for page in manifest.pages:
        assert [item.object_id for item in page.objects] == [
            f"p{page.page_index}-o{position}" for position in range(_SPANS)
        ]
    assert progress[4] == progress[1] == [(done, 8) for done in range(1, 9)]
    assert not _page_threads()


def test_page_concurrency_must_be_within_its_bounds(tmp_path: Path) -> None:
    sources, _ = _source(tmp_path, 1)
    for bad in (0, -1, 17):
        with pytest.raises(ValueError, match="page_concurrency"):
            _pipeline(tmp_path / "processing", sources, _Tracker(), concurrency=bad)


def test_the_page_concurrency_setting_defaults_to_one(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("APP_PAGE_CONCURRENCY", raising=False)
    get_settings.cache_clear()
    assert get_settings().page_concurrency == 1
    monkeypatch.setenv("APP_PAGE_CONCURRENCY", "4")
    get_settings.cache_clear()
    assert get_settings().page_concurrency == 4
    get_settings.cache_clear()


# ---- 2. overlap and its bound --------------------------------------------------------------------


def test_pages_and_objects_together_never_exceed_the_page_concurrency(tmp_path: Path) -> None:
    sources, source_id = _source(tmp_path, 10)
    tracker = _Tracker()
    pipeline = _pipeline(
        tmp_path / "processing",
        sources,
        tracker,
        concurrency=3,
        partition_delay=0.03,
        object_delay=0.02,
    )
    pipeline.run(source_id, selected_page_indices=tuple(range(10)))
    assert tracker.max_in_flight == 3
    assert sorted(tracker.partitioned) == list(range(10))


def test_four_pages_at_once_take_less_than_half_the_wall_clock(tmp_path: Path) -> None:
    sources, source_id = _source(tmp_path, 8)
    elapsed = {}
    for concurrency in (1, 4):
        pipeline = _pipeline(
            tmp_path / f"processing-{concurrency}",
            sources,
            _Tracker(),
            concurrency=concurrency,
            partition_delay=0.5,
            objects=False,
        )
        started = time.perf_counter()
        pipeline.run(source_id, selected_page_indices=tuple(range(8)))
        elapsed[concurrency] = time.perf_counter() - started
    timing = f"serial {elapsed[1]:.2f}s, pages(4) {elapsed[4]:.2f}s"
    assert elapsed[1] >= 8 * 0.5, timing
    assert elapsed[4] < elapsed[1] / 2, timing


# ---- 3. stopping at a page boundary --------------------------------------------------------------


def test_an_exception_from_the_page_reporter_stops_queued_pages_and_joins_running_ones(
    tmp_path: Path,
) -> None:
    sources, source_id = _source(tmp_path, 12)
    tracker = _Tracker()
    pipeline = _pipeline(
        tmp_path / "processing", sources, tracker, concurrency=2, partition_delay=0.05
    )

    def stop(done: int, _total: int) -> None:
        if done == 1:
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        pipeline.run(source_id, selected_page_indices=tuple(range(12)), on_page=stop)
    assert not _page_threads(), "a page or object thread kept running after the stop"
    assert tracker.in_flight == 0
    # Page 0 was reported; at most the pages already running then were finished, never the rest.
    assert len(tracker.partitioned) <= 1 + 2 + 2
    assert len(tracker.partitioned) < 12


# ---- 4. the real ingest: same store bytes, budget, 429 cooldown, interrupt -----------------------


@pytest.mark.usefixtures("model_cache_backend", "object_backend")
def test_full_mode_store_bytes_are_unchanged_with_pages_at_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lite_env(monkeypatch)
    monkeypatch.setenv("APP_PAGE_CONCURRENCY", "4")
    get_settings.cache_clear()
    result = run_folder_pipeline(
        mixed_folder(tmp_path),
        ingestion_root=tmp_path / "ingestion",
        max_live_calls_per_pdf=200,
        build_tree=False,
        embedder=OfflineDescriptionEmbedder(),
    )

    (document,) = result.documents
    assert document.publication is not None
    assert document.publication.published_processing_id == FULL_PUBLISHED_ID
    logical = _logical_tree(tmp_path / "ingestion")
    digest, count, requests = store_digest(logical)
    nonvolatile = count - sum(1 for _ in logical.rglob("contexts/*.json"))
    assert (digest, nonvolatile, requests) == (
        FULL_STORE_DIGEST,
        FULL_STORE_FILES,
        FULL_REQUESTS_DIGEST,
    )
    assert not _page_threads()


@pytest.mark.usefixtures("model_cache_backend")
def test_documents_and_pages_at_once_write_exactly_what_the_serial_run_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = _env(monkeypatch, _Model(delay=lambda _payload: 0.01))
    folder = _folder(tmp_path)
    serial = _run(tmp_path / "serial", folder, parallel=1)
    monkeypatch.setenv("APP_PAGE_CONCURRENCY", "3")
    get_settings.cache_clear()
    model.max_in_flight = 0
    both = _run(tmp_path / "both", folder, parallel=2)

    assert [item.status for item in both.documents] == ["published"] * 4
    assert both.live_calls == serial.live_calls
    assert _files(tmp_path / "both") == _files(tmp_path / "serial")
    assert model.max_in_flight > 2, "pages of one document never overlapped"
    _assert_no_claim_left(tmp_path / "both")


def _layout_ingest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sender: Callable[..., bytes],
    *,
    pages: int,
    budget: int,
    concurrency: int,
) -> tuple[object, Path]:
    for key, value in {
        "APP_LLM_API_KEY": "offline-secret",
        "APP_LLM_BASE_URL": PROVIDER_BASE_URL,
        "APP_LLM_MODEL": "offline-test",
        "APP_PAGE_CONCURRENCY": str(concurrency),
    }.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    monkeypatch.setattr("ragspine.common.evidence.providers.json_completion._send_once", sender)
    pdf = authored_pdf(
        tmp_path / "layout.pdf", page_count=pages, label="Atlas FY2025 Japan", embedded_font=True
    )
    root = tmp_path / f"ingestion-{concurrency}"
    summary = ingest_pdf(
        pdf=pdf, output_dir=root, pages="all", stage="layout", max_live_calls=budget
    )
    return summary, root


@pytest.mark.usefixtures("model_cache_backend")
def test_a_tight_budget_is_never_overspent_by_pages_at_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = _Model(delay=lambda _payload: 0.05)
    summary, _ = _layout_ingest(tmp_path, monkeypatch, model, pages=8, budget=3, concurrency=4)
    assert len(model.sent) == 3
    assert summary.live_call_count == 3  # type: ignore[attr-defined]
    assert model.max_in_flight > 1


@pytest.mark.usefixtures("model_cache_backend")
def test_a_rate_limit_cooldown_holds_back_every_page_of_every_document(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cooled = threading.Event()
    lock = threading.Lock()
    sleeps: list[float] = []
    sent_before_cooldown: list[int] = []
    inner = _Model()

    def sleep(seconds: float) -> None:
        with lock:
            sleeps.append(seconds)
        cooled.set()

    monkeypatch.setattr(transient, "_sleep", sleep)
    monkeypatch.setattr(transient, "_clock", lambda: 100.0)  # the cooldown never runs out
    monkeypatch.setattr(transient, "_random", lambda: 0.0)

    def sender(url: str, *, api_key: str, payload: bytes, timeout: float) -> bytes:
        with lock:
            first = not inner.sent and not sent_before_cooldown
            if not cooled.is_set():
                sent_before_cooldown.append(1)
        if first:
            raise ProviderRequestError(
                "Provider returned HTTP 429; no retry performed",
                status=429,
                category="http",
                retry_after=7.0,
            )
        return inner(url, api_key=api_key, payload=payload, timeout=timeout)

    _env(monkeypatch, inner)
    monkeypatch.setattr("ragspine.common.evidence.providers.json_completion._send_once", sender)
    monkeypatch.setenv("APP_PAGE_CONCURRENCY", "4")
    get_settings.cache_clear()
    result = _run(
        tmp_path / "ingestion", _folder(tmp_path), parallel=2, max_live_calls_per_pdf=_PER_PDF + 1
    )

    assert result.ok
    attempts = len(inner.sent) + 1
    # Every attempt sent after the 429 waited out its Retry-After first (the retry included);
    # only attempts already on their way when it arrived went without.
    assert sleeps and set(sleeps) == {7.0}
    assert len(sleeps) == attempts - len(sent_before_cooldown)


@pytest.mark.usefixtures("model_cache_backend")
def test_an_interrupt_with_pages_at_once_stops_at_page_boundaries_and_leaves_no_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = _env(monkeypatch, _Model(delay=lambda _payload: 0.05))
    monkeypatch.setenv("APP_PAGE_CONCURRENCY", "2")
    get_settings.cache_clear()
    model.hook = lambda count: _thread.interrupt_main() if count == 3 else None
    folder = _folder(tmp_path)
    root = tmp_path / "ingestion"
    events: Events = []

    with pytest.raises(KeyboardInterrupt):
        _run(root, folder, parallel=2, events=events)

    assert not _worker_threads() and not _page_threads()
    _assert_no_claim_left(root)
    assert len(model.sent) < 2 * _PER_PDF

    model.hook = lambda _count: None
    resumed = _run(root, folder, parallel=2)
    assert [item.status for item in resumed.documents] == ["published"] * 4
    assert len(model.sent) == 4 * _PER_PDF, "a call made before the interrupt was made again"


# ---- 5. the layout page image width ---------------------------------------------------------------


def test_the_layout_png_width_is_configurable_and_960_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = LLMConfig(api_key="k", base_url="https://example.invalid", model="m")  # type: ignore[arg-type]
    sources = LocalDocumentStore(tmp_path / "source")
    monkeypatch.delenv("APP_LAYOUT_PNG_WIDTH", raising=False)
    get_settings.cache_clear()
    default = ModelPagePartitioner(
        JsonCompletionClient(config, cache_dir=tmp_path / "c", max_live_calls=0), sources
    )
    assert get_settings().layout_png_width == 960
    monkeypatch.setenv("APP_LAYOUT_PNG_WIDTH", "1280")
    get_settings.cache_clear()
    wide = ModelPagePartitioner(
        JsonCompletionClient(config, cache_dir=tmp_path / "c", max_live_calls=0), sources
    )
    get_settings.cache_clear()
    svg = (
        b'<svg xmlns="http://www.w3.org/2000/svg" width="200" height="100" viewBox="0 0 200 100"/>'
    )
    assert int.from_bytes(default.renderer(svg)[16:20], "big") == 960
    assert int.from_bytes(wide.renderer(svg)[16:20], "big") == 1280
    # The default keeps its fingerprint (and every cached partition); another width is another
    # partition stage, so changing it re-runs layout instead of reusing 960-px results.
    assert default.fingerprint == "page-layout-mapper-v3:" + default.client.fingerprint
    assert wide.fingerprint != default.fingerprint


# ---- 6. one store written from several page threads ----------------------------------------------


def test_page_threads_taking_the_store_writer_at_once_count_it_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Page threads make a store's first writes together. Each must not count the writer lease
    # again, or ``close()`` would leave this process holding it and lock out every other one.
    real = sqlite_backend._WRITER_LOCK

    class SlowLock:
        def __enter__(self) -> None:
            time.sleep(0.05)  # every thread passes the "already acquired?" check first
            real.acquire()

        def __exit__(self, *_exc: object) -> None:
            real.release()

    monkeypatch.setattr(sqlite_backend, "_WRITER_LOCK", SlowLock())
    backend = sqlite_backend.SqliteBackend(tmp_path)
    barrier = threading.Barrier(4, timeout=5.0)

    def write() -> None:
        barrier.wait()
        with backend.transaction():
            pass

    threads = [threading.Thread(target=write) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    db = tmp_path / "store.sqlite"
    assert sqlite_backend._WRITER_COUNTS.get(db) == 1
    backend.close()
    assert db not in sqlite_backend._WRITER_COUNTS
