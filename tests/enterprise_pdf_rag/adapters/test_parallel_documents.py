"""``run_folder_pipeline(max_parallel_documents=N)``: documents ingested at once (ADR 0033).

Every document still runs alone in its own ``<root>/<sha>/`` directory with its own stores,
clients and pdfspine documents; what is shared — the budget total, the progress callback, the
embedder, the sampling-refusal memory — is pinned here to stay exact under threads.
"""

import _thread
import hashlib
import json
import threading
import time
from collections.abc import Callable
from contextlib import nullcontext
from pathlib import Path

import pytest
from pydantic import BaseModel, SecretStr

from enterprise_pdf_rag import cli
from enterprise_pdf_rag.adapters import folder_pipeline
from enterprise_pdf_rag.adapters.folder_pipeline import (
    FolderPipelineResult,
    _Budget,
    run_folder_pipeline,
)
from enterprise_pdf_rag.adapters.offline import OfflineDescriptionEmbedder
from enterprise_pdf_rag.adapters.pdf_ingestion import ingest_pdf
from ragspine.common.evidence.providers.json_completion import (
    JsonCompletionClient,
    forget_unsupported_sampling_parameters,
    one_sampling_probe,
)
from ragspine.common.evidence.providers.providers import LLMConfig, ProviderRequestError
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
from tests.enterprise_pdf_rag.adapters.no_hard_link_helpers import (
    fail_directory_fsync,
    forbid_hard_links,
)
from tests.enterprise_pdf_rag.adapters.page_metadata_helpers import combined_sender
from tests.enterprise_pdf_rag.adapters.test_folder_pipeline import (
    _LLM_ENV,
    _PER_PDF,
    _azure_like,
    _pdf,
)

_LABELS = (
    ("a.pdf", "Atlas FY2025 Japan"),
    ("b.pdf", "Boreal 1H26 Korea"),
    ("c.pdf", "Cedar FY2024 Taiwan"),
    ("d.pdf", "Delta 1H25 Vietnam"),
)
type Events = list[tuple[str, dict[str, object]]]


class _Model:
    """The offline layout + page-metadata endpoint, thread-safe, with an optional delay."""

    def __init__(self, delay: Callable[[bytes], float] = lambda _payload: 0.0) -> None:
        self._inner = combined_sender([], metadata_calls=[])
        self._delay = delay
        self._lock = threading.Lock()
        self.sent: list[bytes] = []
        self.in_flight = 0
        self.max_in_flight = 0
        self.hook: Callable[[int], None] = lambda _count: None

    def __call__(self, url: str, *, api_key: str, payload: bytes, timeout: float) -> bytes:
        with self._lock:
            self.sent.append(payload)
            count = len(self.sent)
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            self.hook(count)
            time.sleep(self._delay(payload))
            return self._inner(url, api_key=api_key, payload=payload, timeout=timeout)
        finally:
            with self._lock:
                self.in_flight -= 1


def _env(monkeypatch: pytest.MonkeyPatch, model: _Model) -> _Model:
    for key, value in _LLM_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr("ragspine.common.evidence.providers.json_completion._send_once", model)
    # Model-cache records keep a rounded wall time; a fixed clock keeps them byte-stable.
    monkeypatch.setattr("ragspine.common.evidence.providers.json_completion.monotonic", lambda: 0.0)
    return model


def _folder(tmp_path: Path, labels: tuple[tuple[str, str], ...] = _LABELS) -> Path:
    folder = tmp_path / "pdfs"
    for name, label in labels:
        _pdf(folder / name, label)
    return folder


def _run(
    root: Path,
    folder: Path,
    *,
    parallel: int,
    events: Events | None = None,
    **options: object,
) -> FolderPipelineResult:
    settings: dict[str, object] = {
        "max_live_calls_per_pdf": _PER_PDF,
        "build_tree": False,
        "embedder": OfflineDescriptionEmbedder(),
        **options,
    }
    return run_folder_pipeline(
        folder,
        ingestion_root=root,
        max_parallel_documents=parallel,
        progress=None if events is None else lambda event, payload: events.append((event, payload)),
        **settings,  # type: ignore[arg-type]
    )


def _files(root: Path) -> dict[str, str]:
    """Every file under ``root`` by relative path → sha256 (model-cache contexts carry a wall
    clock ``created_at`` and are kept by name only)."""
    return {
        path.relative_to(root).as_posix(): ""
        if "/model-cache/contexts/" in path.as_posix()
        else hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _comparable(result: FolderPipelineResult, root: Path) -> list[dict[str, object]]:
    """Each document as reported, with its timing dropped and its ingestion root neutral."""
    return [
        json.loads(
            item.model_copy(update={"elapsed_s": 0.0})
            .model_dump_json()
            .replace(str(root), "<root>")
        )
        for item in result.documents
    ]


# ---- 1. same bytes as the serial run -------------------------------------------------------


def _assert_parallel_matches_serial(tmp_path: Path, model: _Model) -> None:
    folder = _folder(tmp_path)
    serial_root, parallel_root = tmp_path / "serial", tmp_path / "parallel"

    serial = _run(serial_root, folder, parallel=1)
    serial_calls = len(model.sent)
    parallel = _run(parallel_root, folder, parallel=4)

    assert [item.status for item in parallel.documents] == ["published"] * 4
    assert [Path(item.pdf_path).name for item in parallel.documents] == [n for n, _ in _LABELS]
    assert _comparable(parallel, parallel_root) == _comparable(serial, serial_root)
    assert parallel.live_calls == serial.live_calls
    assert len(model.sent) == 2 * serial_calls == 2 * 4 * _PER_PDF
    # Every file, in every document's own directory, is the serial run's file: no request,
    # stage-cache entry, claim or pointer of one document lands in another's.
    assert _files(parallel_root) == _files(serial_root)
    assert not [path for path in parallel_root.rglob("*.claim*")]
    for item in parallel.documents:
        assert item.publication is not None and item.ingestion is not None
        processing = Path(item.ingestion.processing_store)
        assert processing.parent.name == item.sha256
        assert (processing / "current-processing").read_text().strip() == (
            item.publication.published_processing_id
        )


def test_four_documents_at_once_write_exactly_what_the_serial_run_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = _env(monkeypatch, _Model(delay=lambda _payload: 0.01))

    _assert_parallel_matches_serial(tmp_path, model)

    assert model.max_in_flight > 1, "the documents never actually overlapped"


def test_four_documents_at_once_without_hard_links_match_the_serial_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = _env(monkeypatch, _Model(delay=lambda _payload: 0.01))
    forbid_hard_links(monkeypatch)
    fail_directory_fsync(monkeypatch)

    _assert_parallel_matches_serial(tmp_path, model)

    again = _run(tmp_path / "parallel", tmp_path / "pdfs", parallel=4)
    assert again.ok and again.live_calls.total == 0
    assert all(item.index_reused for item in again.documents)
    assert not [p for p in (tmp_path / "parallel").rglob("tmp*") if p.is_file()]


def test_full_mode_store_bytes_are_unchanged_with_parallel_documents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lite_env(monkeypatch)
    result = run_folder_pipeline(
        mixed_folder(tmp_path),
        ingestion_root=tmp_path / "ingestion",
        max_live_calls_per_pdf=200,
        build_tree=False,
        embedder=OfflineDescriptionEmbedder(),
        max_parallel_documents=4,
    )

    (document,) = result.documents
    assert document.publication is not None
    assert document.publication.published_processing_id == FULL_PUBLISHED_ID
    digest, count, requests = store_digest(tmp_path / "ingestion")
    nonvolatile = count - sum(1 for _ in (tmp_path / "ingestion").rglob("contexts/*.json"))
    assert (digest, nonvolatile, requests) == (
        FULL_STORE_DIGEST,
        FULL_STORE_FILES,
        FULL_REQUESTS_DIGEST,
    )


# ---- 2. order, progress and slots -----------------------------------------------------------


def test_results_keep_discovery_order_and_progress_is_complete_per_document(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The first PDF's calls are the slowest, so it finishes last.
    slow = _LABELS[0][1].encode()
    _env(monkeypatch, _Model(delay=lambda payload: 0.08 if slow in payload else 0.01))
    folder = _folder(tmp_path)
    events: Events = []
    inside = threading.Event()
    overlaps: list[str] = []

    def progress(event: str, payload: dict[str, object]) -> None:
        if inside.is_set():
            overlaps.append(event)
        inside.set()
        time.sleep(0.002)  # long enough for another thread to walk in, were it allowed to
        events.append((event, payload))
        inside.clear()

    result = run_folder_pipeline(
        folder,
        ingestion_root=tmp_path / "ingestion",
        max_live_calls_per_pdf=_PER_PDF,
        build_tree=False,
        embedder=OfflineDescriptionEmbedder(),
        max_parallel_documents=4,
        progress=progress,
    )

    assert overlaps == [], "the progress callback was entered by two threads at once"
    assert [Path(item.pdf_path).name for item in result.documents] == [n for n, _ in _LABELS]
    finished = [Path(str(p["pdf"])).name for e, p in events if e == "document_done"]
    assert sorted(finished) == [n for n, _ in _LABELS] and finished[-1] == "a.pdf"
    assert events[0][0] == "discovered" and events[-1][0] == "done"
    assert "slot" not in events[0][1] and "slot" not in events[-1][1]
    for name, _ in _LABELS:
        own = [(e, p) for e, p in events if Path(str(p.get("pdf", ""))).name == name]
        kinds = [e for e, _ in own]
        assert kinds[0] == "document_start" and kinds[-1] == "document_done"
        assert kinds.count("document_start") == kinds.count("document_done") == 1
        stages = [p["stage"] for e, p in own if e == "document_progress" and "pages_done" not in p]
        assert stages == ["requalify", "qualify", "index", "publish"]
        pages = [
            int(str(p["pages_done"]))
            for _, p in own
            if "pages_done" in p and p["stage"] == "layout"
        ]
        assert pages == sorted(pages) and pages[-1] == 3
        slots = {p["slot"] for _, p in own}
        assert len(slots) == 1 and slots <= {1, 2, 3, 4}


def test_one_document_at_a_time_emits_no_slot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _env(monkeypatch, _Model())
    events: Events = []

    _run(tmp_path / "ingestion", _folder(tmp_path, _LABELS[:2]), parallel=1, events=events)

    assert all("slot" not in payload for _, payload in events)


# ---- 3. the shared total ---------------------------------------------------------------------


def test_a_shared_total_is_never_overspent_by_documents_running_at_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = _env(monkeypatch, _Model(delay=lambda _payload: 0.01))
    folder = _folder(tmp_path)
    total = 2 * _PER_PDF + 3

    result = _run(tmp_path / "ingestion", folder, parallel=4, max_live_calls_total=total)

    assert sum(item.live_call_budget for item in result.documents) <= total
    assert result.live_calls.ingest == len(model.sent) <= total
    assert result.budget_exhausted and not result.ok
    statuses = [item.status for item in result.documents]
    assert statuses.count("published") >= 2
    assert set(statuses) <= {"published", "budget_starved"}

    # A rerun with budget finishes the starved ones from the cache: nothing is called twice.
    resumed = _run(tmp_path / "ingestion", folder, parallel=4)
    assert [item.status for item in resumed.documents] == ["published"] * 4
    assert len(model.sent) == 4 * _PER_PDF


def test_the_budget_hands_out_a_total_atomically() -> None:
    budget = _Budget(100)
    start = threading.Barrier(16)
    granted: list[int] = []

    def take() -> None:
        start.wait()
        for _ in range(20):
            got = budget.allot(7)
            granted.append(got)
            budget.spend(got // 2, got)

    threads = [threading.Thread(target=take) for _ in range(16)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert budget.used == sum(got // 2 for got in granted) <= 100
    assert budget.reserved == 0 and budget.exhausted


def test_one_at_a_time_the_budget_grants_what_the_serial_run_granted() -> None:
    budget = _Budget(10)
    assert budget.allot(6) == 6
    budget.spend(5, 6)
    assert budget.allot(6) == 5 and budget.exhausted
    budget.spend(0, 5)
    assert budget.allot(3) == 3
    unlimited = _Budget(None)
    assert unlimited.allot(10_000) == 10_000
    unlimited.spend(3, 10_000)
    assert unlimited.used == 3 and not unlimited.exhausted


# ---- 4. failures ------------------------------------------------------------------------------


def test_a_failing_document_never_stops_the_others(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _env(monkeypatch, _Model(delay=lambda _payload: 0.01))
    folder = _folder(tmp_path, _LABELS[:3])
    (folder / "broken.pdf").write_text("not a pdf")
    real = ingest_pdf

    def crash_on_c(**kwargs: object) -> object:
        if Path(str(kwargs["pdf"])).name == "c.pdf":
            raise RuntimeError("worker crashed")
        return real(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(folder_pipeline, "ingest_pdf", crash_on_c)
    events: Events = []

    result = _run(tmp_path / "ingestion", folder, parallel=4, events=events)

    by_name = {Path(item.pdf_path).name: item for item in result.documents}
    assert [Path(item.pdf_path).name for item in result.documents] == [
        "a.pdf",
        "b.pdf",
        "broken.pdf",
        "c.pdf",
    ]
    assert (by_name["broken.pdf"].status, by_name["broken.pdf"].failed_stage) == (
        "failed",
        "ingest",
    )
    assert (by_name["c.pdf"].status, by_name["c.pdf"].failed_stage) == ("failed", "ingest")
    assert by_name["c.pdf"].error == "RuntimeError: worker crashed"
    assert by_name["a.pdf"].status == by_name["b.pdf"].status == "published"
    done = {Path(str(p["pdf"])).name: p for e, p in events if e == "document_done"}
    assert done["c.pdf"]["error"] == "RuntimeError: worker crashed"
    assert not result.ok

    monkeypatch.setattr(folder_pipeline, "ingest_pdf", real)
    with pytest.raises(ValueError, match="%PDF-"):
        _run(tmp_path / "again", folder, parallel=4, continue_on_error=False)
    assert not _worker_threads()


def _worker_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name.startswith("run-folder")]


def test_an_interrupt_stops_at_page_boundaries_and_leaves_no_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = _env(monkeypatch, _Model(delay=lambda _payload: 0.05))
    model.hook = lambda count: _thread.interrupt_main() if count == 3 else None
    folder = _folder(tmp_path)
    root = tmp_path / "ingestion"
    events: Events = []

    with pytest.raises(KeyboardInterrupt):
        _run(root, folder, parallel=2, events=events)

    assert not _worker_threads(), "a worker kept running after the interrupt"
    assert not [path for path in root.rglob("*.claim*")]
    # Only the two documents that had started were touched; the queued ones never began.
    started = {Path(str(p["pdf"])).name for e, p in events if e == "document_start"}
    assert started == {"a.pdf", "b.pdf"}
    assert ("stopping", {"reason": "KeyboardInterrupt", "running": 2}) in events
    interrupted = len(model.sent)
    assert interrupted < 2 * _PER_PDF  # each stopped at its next page boundary

    model.hook = lambda _count: None
    resumed = _run(root, folder, parallel=2)
    assert [item.status for item in resumed.documents] == ["published"] * 4
    assert len(model.sent) == 4 * _PER_PDF, "a call made before the interrupt was made again"


def test_max_parallel_documents_out_of_range_fails_before_any_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = _env(monkeypatch, _Model())
    folder = _folder(tmp_path, _LABELS[:1])
    for bad in (0, -1, 17):
        with pytest.raises(ValueError, match="max_parallel_documents"):
            _run(tmp_path / "ingestion", folder, parallel=bad)
    assert model.sent == [] and not (tmp_path / "ingestion").exists()


# ---- 5. the sampling-refusal memory -------------------------------------------------------------


def test_a_refused_temperature_is_probed_once_across_parallel_documents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    forget_unsupported_sampling_parameters()
    endpoint = _azure_like(monkeypatch)
    folder = _folder(tmp_path)

    result = _run(tmp_path / "ingestion", folder, parallel=4, max_live_calls_per_pdf=_PER_PDF + 1)

    assert [item.status for item in result.documents] == ["published"] * 4
    assert endpoint.with_temperature() == 1
    assert result.live_calls.total == len(endpoint.sent) == 1 + 4 * _PER_PDF
    assert result.sampling_parameters_dropped == ("temperature",)
    forget_unsupported_sampling_parameters()


@pytest.mark.parametrize(("scoped", "probes"), [(True, 1), (False, 6)])
def test_first_calls_from_many_threads_wait_for_one_probe_only_inside_the_scope(
    tmp_path: Path, scoped: bool, probes: int
) -> None:
    """Inside ``one_sampling_probe()`` six clients racing their first call send one probe;
    outside it nothing waits, exactly as before (every racing first call probes)."""
    forget_unsupported_sampling_parameters()
    lock = threading.Lock()
    bodies: list[dict[str, object]] = []

    def sender(url: str, *, api_key: str, payload: bytes, timeout: float) -> bytes:
        body = json.loads(payload)
        with lock:
            bodies.append(body)
        time.sleep(0.05)
        if "temperature" in body:
            raise ProviderRequestError(
                "Provider returned HTTP 400; no retry performed",
                status=400,
                param="temperature",
                error_code="unsupported_value",
            )
        return json.dumps(
            {"choices": [{"message": {"content": '{"ok": true}'}, "finish_reason": "stop"}]}
        ).encode()

    class _Ok(BaseModel):
        ok: bool

    config = LLMConfig(
        base_url=PROVIDER_BASE_URL, model="offline-test", api_key=SecretStr("offline-secret")
    )
    start = threading.Barrier(6)
    errors: list[BaseException] = []

    def call(index: int) -> None:
        client = JsonCompletionClient(
            config, cache_dir=tmp_path / f"cache-{index}", max_live_calls=3, sender=sender
        )
        start.wait()
        try:
            client.complete_text_json(task="probe", prompt=f"question {index}", response_model=_Ok)
        except BaseException as error:  # noqa: BLE001 - reported below
            errors.append(error)

    threads = [threading.Thread(target=call, args=(index,)) for index in range(6)]
    with one_sampling_probe() if scoped else nullcontext():
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

    assert errors == []
    assert sum("temperature" in body for body in bodies) == probes
    assert len(bodies) == probes + 6
    forget_unsupported_sampling_parameters()


# ---- 6. embedder counts and wall clock ------------------------------------------------------------


class _CountingEmbedder(OfflineDescriptionEmbedder):
    """A batch embedder whose shared ``request_count`` is read around each batch."""

    def __init__(self) -> None:
        super().__init__()
        self._requests = 0

    @property
    def request_count(self) -> int:
        return self._requests

    def embed_descriptions(self, texts: object) -> tuple[tuple[float, ...], ...]:
        assert isinstance(texts, list)
        self._requests += 1
        time.sleep(0.01)
        return tuple(self.embed_description(text) for text in texts)


def test_each_document_reports_only_its_own_embedding_requests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _env(monkeypatch, _Model(delay=lambda _payload: 0.01))
    folder = _folder(tmp_path)

    serial = _run(tmp_path / "serial", folder, parallel=1, embedder=_CountingEmbedder())
    parallel = _run(tmp_path / "parallel", folder, parallel=4, embedder=_CountingEmbedder())

    def requests(result: FolderPipelineResult) -> list[int]:
        return [item.index.embedding_requests for item in result.documents if item.index]

    assert requests(parallel) == requests(serial) == [1, 1, 1, 1]


def test_four_documents_at_once_take_much_less_wall_clock_than_one_at_a_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _env(monkeypatch, _Model(delay=lambda _payload: 0.1))
    folder = _folder(tmp_path)

    started = time.perf_counter()
    serial = _run(tmp_path / "serial", folder, parallel=1)
    serial_s = time.perf_counter() - started
    started = time.perf_counter()
    parallel = _run(tmp_path / "parallel", folder, parallel=4)
    parallel_s = time.perf_counter() - started

    assert serial.ok and parallel.ok
    timing = f"serial {serial_s:.2f}s, parallel(4) {parallel_s:.2f}s"
    waiting = 4 * _PER_PDF * 0.1
    assert serial_s >= waiting, timing
    # Four at once can hide at most three quarters of the model wait; the offline pdfspine work
    # is CPU under one interpreter lock and does not overlap, so only the wait is judged.
    assert serial_s - parallel_s >= 0.5 * (waiting * 3 / 4), timing


# ---- 7. CLI ------------------------------------------------------------------------------------


def test_cli_passes_max_parallel_documents(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[object] = []

    def fake(*_args: object, **kwargs: object) -> object:
        seen.append(kwargs["max_parallel_documents"])
        raise ValueError("stop here")

    monkeypatch.setattr(cli, "run_folder_pipeline", fake)
    base = ["run-folder", "--folder", str(tmp_path), "--max-live-calls-per-pdf", "0"]
    assert cli.main(base) == 1
    assert cli.main([*base, "--max-parallel-documents", "4"]) == 1
    assert seen == [1, 4]
