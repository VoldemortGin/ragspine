"""The one-call folder pipeline: discovery, every stage in order, budgets, resume and eval."""

import asyncio
import io
import json
import shutil
from collections.abc import Callable
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from typing import Never

import pytest

from enterprise_pdf_rag import cli
from enterprise_pdf_rag.adapters import folder_pipeline
from enterprise_pdf_rag.adapters.document_store import LocalDocumentStore
from enterprise_pdf_rag.adapters.folder_pipeline import (
    FolderPipelineResult,
    PreflightError,
    discover_pdfs,
    run_folder_pipeline,
)
from enterprise_pdf_rag.adapters.offline import OfflineDescriptionEmbedder
from enterprise_pdf_rag.adapters.processing_store import ProcessingStore
from enterprise_pdf_rag.adapters.visual_requalification import RequalificationSummary
from ragspine.common.evidence.configs import get_settings
from ragspine.common.evidence.providers.json_completion import JsonCompletionClient
from ragspine.common.evidence.providers.providers import LocalModelConfig, ProviderRequestError
from ragspine.extraction.evidence.figures.ports import EmbeddingPort
from tests.enterprise_pdf_rag.adapters.generic_publication_helpers import (
    PROVIDER_BASE_URL,
    text_partition_sender,
)
from tests.enterprise_pdf_rag.adapters.page_metadata_helpers import combined_sender
from tests.enterprise_pdf_rag.adapters.test_chat_metadata_http import _quote_page
from tests.enterprise_pdf_rag.adapters.test_pdf_ingestion import authored_pdf
from tests.enterprise_pdf_rag.answers.fake_llm import scripted_client

_OFFLINE = OfflineDescriptionEmbedder()
_LLM_ENV = {
    "APP_LLM_API_KEY": "offline-secret",
    "APP_LLM_BASE_URL": PROVIDER_BASE_URL,
    "APP_LLM_MODEL": "offline-test",
}
_EMBEDDING_ENV = {
    "APP_EMBEDDING_BASE_URL": "http://127.0.0.1:9/v1",
    "APP_EMBEDDING_MODEL": "offline-embedding",
    "APP_EMBEDDING_API_KEY": "offline-embedding-secret",
}
# Three pages: one layout and one page-metadata call each.
_PAGES = 3
_PER_PDF = 2 * _PAGES
_MERIDIAN = "Meridian 1H26 Hong Kong"
_ORION = "Orion FY2024 Thailand"


def _model_env(monkeypatch: pytest.MonkeyPatch, *, diagram_page: bool = False) -> list[bytes]:
    """Configure the answer model and stub its transport for layout (and metadata) calls."""
    for key, value in _LLM_ENV.items():
        monkeypatch.setenv(key, value)
    calls: list[bytes] = []
    sender = (
        text_partition_sender(calls, diagram_page=True)
        if diagram_page
        else combined_sender(calls, metadata_calls=[])
    )
    monkeypatch.setattr("ragspine.common.evidence.providers.json_completion._send_once", sender)
    return calls


def _pdf(path: Path, label: str, **layout: bool) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    return authored_pdf(path, page_count=_PAGES, label=label, embedded_font=True, **layout)


def _folder(tmp_path: Path, *labels: tuple[str, str]) -> Path:
    folder = tmp_path / "pdfs"
    for name, label in labels:
        _pdf(folder / name, label)
    return folder


def _run(
    tmp_path: Path,
    folder: Path,
    *,
    questions: Path | None = None,
    max_live_calls_per_pdf: int = _PER_PDF,
    max_live_calls_total: int | None = None,
    requalify: bool = True,
    build_tree: bool = False,
    tree_max_live_calls: int = 50,
    continue_on_error: bool = True,
    report_dir: Path | None = None,
    embedder: EmbeddingPort | None = _OFFLINE,
    answer_llm: JsonCompletionClient | None = None,
) -> FolderPipelineResult:
    return run_folder_pipeline(
        folder,
        questions=questions,
        ingestion_root=tmp_path / "ingestion",
        max_live_calls_per_pdf=max_live_calls_per_pdf,
        max_live_calls_total=max_live_calls_total,
        requalify=requalify,
        build_tree=build_tree,
        tree_max_live_calls=tree_max_live_calls,
        continue_on_error=continue_on_error,
        report_dir=report_dir,
        embedder=embedder,
        answer_llm=answer_llm,
    )


def _spy(monkeypatch: pytest.MonkeyPatch, name: str, record: list[dict[str, object]]) -> None:
    real = getattr(folder_pipeline, name)

    def spy(*args: object, **kwargs: object) -> object:
        record.append(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(folder_pipeline, name, spy)


def _forbid(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    def refuse(*_args: object, **_kwargs: object) -> Never:
        raise AssertionError(f"{name} must not be called")

    monkeypatch.setattr(folder_pipeline, name, refuse)


# ---- 1. discovery and deduplication -------------------------------------------------------


def test_discovery_walks_nested_folders_any_case_and_skips_hidden_and_other_files(
    tmp_path: Path,
) -> None:
    folder = tmp_path / "pdfs"
    for relative in ("b.pdf", "sub/a.PDF", "sub/deeper/c.Pdf", ".hidden.pdf", ".cache/d.pdf"):
        (folder / relative).parent.mkdir(parents=True, exist_ok=True)
        (folder / relative).write_bytes(b"%PDF-1.7\n")
    (folder / "notes.txt").write_text("not a pdf")
    (folder / "sub" / "folder.pdf").mkdir()
    assert discover_pdfs(folder) == (
        folder / "b.pdf",
        folder / "sub/a.PDF",
        folder / "sub/deeper/c.Pdf",
    )


def test_same_content_under_two_names_is_ingested_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _model_env(monkeypatch)
    folder = _folder(tmp_path, ("a/meridian.pdf", _MERIDIAN))
    shutil.copyfile(folder / "a/meridian.pdf", folder / "b-copy.PDF")
    ingested: list[dict[str, object]] = []
    _spy(monkeypatch, "ingest_pdf", ingested)

    result = _run(tmp_path, folder)

    first, copy = result.documents
    assert (Path(first.pdf_path).name, first.status) == ("meridian.pdf", "published")
    assert (copy.status, copy.duplicate_of, copy.sha256) == (
        "duplicate_of",
        first.pdf_path,
        first.sha256,
    )
    assert [Path(str(call["pdf"])).name for call in ingested] == ["meridian.pdf"]
    assert result.ok


# ---- 2. one PDF end to end ---------------------------------------------------------------


def test_one_pdf_runs_every_stage_and_moves_the_discovery_pointer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _model_env(monkeypatch)
    folder = _folder(tmp_path, ("meridian.pdf", _MERIDIAN))

    result = _run(tmp_path, folder, report_dir=tmp_path / "report")

    (document,) = result.documents
    assert document.status == "published" and document.error is None
    assert document.ingestion is not None and document.publication is not None
    assert document.index is not None and not document.index_reused
    assert document.qualification is not None
    assert document.qualification.eligible_member_count == _PAGES
    assert document.live_calls == _PER_PDF == result.live_calls.ingest == result.live_calls.total
    processing = Path(document.ingestion.processing_store)
    assert (processing / "current-processing").read_text().strip() == (
        document.publication.published_processing_id
    )
    assert document.publication.source_activated
    assert result.eval is None and result.ok and not result.budget_exhausted
    saved = FolderPipelineResult.model_validate_json(
        (tmp_path / "report" / "report.json").read_bytes()
    )
    assert saved.documents[0].sha256 == document.sha256
    assert "| `meridian.pdf` |" in (tmp_path / "report" / "report.md").read_text()


# ---- 3. resume ---------------------------------------------------------------------------


def test_a_second_run_replays_everything_and_reuses_the_published_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _model_env(monkeypatch)
    folder = _folder(tmp_path, ("meridian.pdf", _MERIDIAN), ("orion.pdf", _ORION))
    first = _run(tmp_path, folder)
    indexed: list[dict[str, object]] = []
    _spy(monkeypatch, "index_draft", indexed)

    second = _run(tmp_path, folder)

    assert first.live_calls.total == 2 * _PER_PDF
    assert second.live_calls.total == 0
    assert all(item.index_reused and item.index is None for item in second.documents)
    assert indexed == []
    assert [
        item.publication.published_processing_id for item in second.documents if item.publication
    ] == [item.publication.published_processing_id for item in first.documents if item.publication]


def test_another_embedder_reindexes_instead_of_reusing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Renamed(OfflineDescriptionEmbedder):
        @property
        def fingerprint(self) -> str:
            return "offline-demo/token-hash-64-v1-renamed"

    _model_env(monkeypatch)
    folder = _folder(tmp_path, ("meridian.pdf", _MERIDIAN))
    _run(tmp_path, folder)

    (document,) = _run(tmp_path, folder, embedder=Renamed()).documents

    assert not document.index_reused and document.index is not None
    assert document.index.embedding_fingerprint == "offline-demo/token-hash-64-v1-renamed"


# ---- 4. requalification wiring -----------------------------------------------------------


def test_the_requalified_draft_is_what_gets_qualified_and_indexed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _model_env(monkeypatch)
    folder = _folder(tmp_path, ("meridian.pdf", _MERIDIAN))
    drafts: list[str] = []

    def requalify(
        sources: LocalDocumentStore, outputs: ProcessingStore, *, processing_id: str
    ) -> RequalificationSummary:
        manifest = outputs.load(processing_id)
        draft = outputs.save_draft(replace(manifest, producer="test-requalified"), sources=sources)
        drafts.append(draft)
        return RequalificationSummary(processing_id, draft, ())

    monkeypatch.setattr(folder_pipeline, "requalify_visual_objects", requalify)

    (document,) = _run(tmp_path, folder).documents

    assert document.ingestion is not None and document.index is not None
    assert drafts and drafts[0] != document.ingestion.processing_id
    assert document.requalification is not None
    assert document.requalification.draft_processing_id == drafts[0]
    assert document.qualification is not None
    assert document.qualification.processing_id == drafts[0]
    assert document.index.processing_id == drafts[0]


def test_a_real_requalification_without_a_new_draft_falls_back_to_the_ingested_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A diagram is already proven at ingest, so the real pass changes nothing."""
    _model_env(monkeypatch, diagram_page=True)
    folder = tmp_path / "pdfs"
    _pdf(folder / "flow.pdf", "Flow", diagram_page=True)

    # Layout per page plus the diagram's two visual branches; metadata is left deferred.
    (document,) = _run(tmp_path, folder, max_live_calls_per_pdf=_PAGES + 2).documents

    assert document.status == "published", document.error
    assert document.ingestion is not None and document.qualification is not None
    assert document.requalification is not None
    assert document.requalification.draft_processing_id is None
    assert document.requalification.outcomes == {"Diagram:unchanged": 1}
    assert document.qualification.processing_id == document.ingestion.processing_id
    assert document.qualification.kinds.get("Diagram") == 1


def test_requalify_off_never_calls_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _model_env(monkeypatch)
    folder = _folder(tmp_path, ("meridian.pdf", _MERIDIAN))
    _forbid(monkeypatch, "requalify_visual_objects")

    (document,) = _run(tmp_path, folder, requalify=False).documents

    assert document.status == "published" and document.requalification is None


# ---- 5. budgets --------------------------------------------------------------------------


def test_a_shared_total_is_handed_out_in_order_and_then_starves_the_rest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _model_env(monkeypatch)
    folder = _folder(tmp_path, ("a.pdf", _MERIDIAN), ("b.pdf", _ORION), ("c.pdf", "Atlas FY2025"))
    ingested: list[dict[str, object]] = []
    _spy(monkeypatch, "ingest_pdf", ingested)

    result = _run(tmp_path, folder, max_live_calls_total=_PER_PDF + 4)

    assert [call["max_live_calls"] for call in ingested] == [_PER_PDF, 4, 0]
    assert [item.live_call_budget for item in result.documents] == [_PER_PDF, 4, 0]
    assert [item.status for item in result.documents] == [
        "published",
        "budget_starved",
        "budget_starved",
    ]
    assert result.budget_exhausted and not result.ok
    assert result.live_calls.ingest == _PER_PDF + 4

    # A rerun with budget finishes the starved ones from where the cache left them.
    resumed = _run(tmp_path, folder)
    assert [item.status for item in resumed.documents] == ["published"] * 3
    assert [item.live_calls for item in resumed.documents] == [0, 2, _PER_PDF]
    assert not resumed.budget_exhausted


def test_budgets_out_of_range_fail_before_any_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _model_env(monkeypatch)
    folder = _folder(tmp_path, ("meridian.pdf", _MERIDIAN))
    _forbid(monkeypatch, "ingest_pdf")
    with pytest.raises(ValueError, match="max_live_calls_per_pdf"):
        _run(tmp_path, folder, max_live_calls_per_pdf=201)
    with pytest.raises(ValueError, match="max_live_calls_per_pdf"):
        _run(tmp_path, folder, max_live_calls_per_pdf=-1)
    with pytest.raises(ValueError, match="max_live_calls_total"):
        _run(tmp_path, folder, max_live_calls_total=-1)
    with pytest.raises(ValueError, match="tree_max_live_calls"):
        _run(tmp_path, folder, tree_max_live_calls=500)
    with pytest.raises(FileNotFoundError):
        _run(tmp_path, tmp_path / "missing")


# ---- 6. failure isolation ----------------------------------------------------------------


def test_one_broken_pdf_is_recorded_and_the_others_still_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _model_env(monkeypatch)
    folder = _folder(tmp_path, ("meridian.pdf", _MERIDIAN))
    (folder / "broken.pdf").write_text("not a pdf")

    result = _run(tmp_path, folder)

    broken, meridian = result.documents
    assert (broken.status, broken.failed_stage) == ("failed", "ingest")
    assert broken.error is not None and "%PDF-" in broken.error
    assert meridian.status == "published"
    assert not result.ok

    with pytest.raises(ValueError, match="%PDF-"):
        _run(tmp_path, folder, continue_on_error=False)


# ---- 7. tree -----------------------------------------------------------------------------


def test_the_tree_stage_is_skipped_on_request_and_reports_its_saved_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _model_env(monkeypatch)
    folder = _folder(tmp_path, ("meridian.pdf", _MERIDIAN))
    with monkeypatch.context() as patched:
        _forbid(patched, "annotate_document_tree")
        (skipped,) = _run(tmp_path, folder, build_tree=False).documents
    assert skipped.tree is None

    (document,) = _run(tmp_path, folder, build_tree=True).documents

    assert document.tree is not None and document.publication is not None
    published = document.publication.published_processing_id
    assert document.tree.processing_id == published
    record = ProcessingStore(Path(document.publication.processing_store)).document_tree_record(
        published
    )
    assert record is not None and record.state is document.tree.state


# ---- 8. in-process evaluation ------------------------------------------------------------


def _two_documents(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    _model_env(monkeypatch)
    return _folder(tmp_path, ("meridian.pdf", _MERIDIAN), ("orion.pdf", _ORION))


def _answer_llm(tmp_path: Path) -> tuple[JsonCompletionClient, list[str]]:
    return scripted_client(tmp_path / "answer-cache", _quote_page("page 2"), max_live_calls=5)


def test_a_light_question_set_is_answered_in_process_inside_a_running_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = _two_documents(tmp_path, monkeypatch)
    questions = tmp_path / "questions.jsonl"
    rows: list[dict[str, object]] = [
        {"id": "named", "question": "What does page 2 say?", "doc": "meridian.pdf", "pages": "2"},
        {"id": "routed", "question": "What does Orion say on page 2?", "expected": "page 2"},
        {"id": "unroutable", "question": "What does page 2 say?"},
        {"id": "elsewhere", "question": "What does page 2 say?", "doc": "absent.pdf"},
    ]
    questions.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    llm, prompts = _answer_llm(tmp_path)

    async def notebook_cell() -> FolderPipelineResult:
        # A notebook already runs a loop; the pipeline must still be callable from it.
        return _run(tmp_path, folder, questions=questions, answer_llm=llm)

    result = asyncio.run(notebook_cell())

    assert result.eval is not None and result.eval.format == "questions"
    cases = {case.case_id: case for case in result.eval.cases}
    meridian, orion = (item.sha256 for item in result.documents)
    assert (cases["named"].verdict, cases["named"].document_id) == ("answered", meridian)
    assert cases["named"].cited_pages == (2,) and cases["named"].failures == ()
    assert cases["named"].page_rank is not None
    assert (cases["routed"].verdict, cases["routed"].document_id) == ("answered", orion)
    assert cases["routed"].failures == ()
    assert cases["unroutable"].verdict == "routing_failed"
    assert "Document selection required" in cases["unroutable"].failures[0]
    assert cases["elsewhere"].verdict == "routing_failed"
    assert "no published document" in cases["elsewhere"].failures[0]
    # Only the two routed questions ever reached the model.
    assert len(prompts) == 2
    assert result.live_calls.answer == 2
    assert result.eval.totals["answered"] == 2 and result.eval.totals["routing_failed"] == 2
    assert result.eval.metrics["judged"] == 1
    assert not result.ok


def _gold(meridian: str) -> dict[str, object]:
    return {
        "schema_version": "nl-answers-gold-v1",
        "corpus": "folder-pipeline-test",
        "review": ["offline fixture"],
        "pinned": {
            "document_sha256": meridian,
            "processing_id": "0" * 64,
            "snapshot_id": "1" * 64,
            "selected_physical_pages": [1, 2, 3],
            "member_count": 3,
            "embedding_fingerprint": "offline-demo/token-hash-64-v1",
        },
        "minimum_positive_cases": 1,
        "cases": [
            {
                "case_id": "page-two",
                "case_class": "positive",
                "question": {"en": "What does Meridian say on page 2?"},
                "document_sha256": meridian,
                "expected": {
                    "status": "answered",
                    "min_claims": 1,
                    "required_claims": [
                        {"kind": "quote", "page_index": 1, "field_path_prefix": "fragments."}
                    ],
                },
                "rationale": "The page-2 line is quoted verbatim.",
            },
            {
                "case_id": "other-document",
                "case_class": "abstain",
                "question": {"en": "What does the other report say?"},
                "document_sha256": "a" * 64,
                "expected": {"status": "abstained"},
                "rationale": "Its document is not part of this run.",
            },
        ],
    }


def test_a_frozen_gold_set_is_judged_and_a_case_outside_the_run_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = _two_documents(tmp_path, monkeypatch)
    first = _run(tmp_path, folder)
    gold = tmp_path / "gold.json"
    gold.write_text(json.dumps(_gold(first.documents[0].sha256)))
    llm, prompts = _answer_llm(tmp_path)

    result = _run(tmp_path, folder, questions=gold, answer_llm=llm)

    assert result.eval is not None and result.eval.format == "nl-answers-gold-v1"
    passed, outside = result.eval.cases
    assert (passed.case_id, passed.verdict, passed.failures) == ("page-two", "pass", ())
    assert passed.cited_pages == (2,) and passed.page_rank is not None
    assert passed.envelope["status"] == "answered"
    assert (outside.case_id, outside.verdict) == ("other-document", "routing_failed")
    assert "not among this run" in outside.failures[0]
    assert len(prompts) == 1
    assert result.eval.totals["passed"] == 1 and result.eval.totals["routing_failed"] == 1
    assert result.live_calls == result.live_calls.model_copy(
        update={"ingest": 0, "tree": 0, "answer": 1, "total": 1}
    )


# ---- 9. preflight ------------------------------------------------------------------------


def test_an_unconfigured_llm_fails_before_any_ingest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = _folder(tmp_path, ("meridian.pdf", _MERIDIAN))
    for key in _LLM_ENV:
        monkeypatch.delenv(key, raising=False)
    _forbid(monkeypatch, "ingest_pdf")

    with pytest.raises(PreflightError, match="APP_LLM_API_KEY") as raised:
        _run(tmp_path, folder)
    assert ".env" in str(raised.value)


def test_an_unreachable_embedding_service_fails_before_any_ingest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _model_env(monkeypatch)
    for key, value in _EMBEDDING_ENV.items():
        monkeypatch.setenv(key, value)
    folder = _folder(tmp_path, ("meridian.pdf", _MERIDIAN))
    probes: list[str] = []

    class Down(OfflineDescriptionEmbedder):
        def __init__(self, _config: object) -> None:
            pass

        def embed_query(self, text: str) -> tuple[float, ...]:
            probes.append(text)
            raise ProviderRequestError("embedding transport failed", category="connection")

    monkeypatch.setattr(folder_pipeline, "LocalEmbeddingAdapter", Down)
    ingested: list[dict[str, object]] = []
    _spy(monkeypatch, "ingest_pdf", ingested)

    with pytest.raises(PreflightError, match=r"local_model_tunnel\.py start"):
        _run(tmp_path, folder, embedder=None)
    assert probes == ["preflight"] and ingested == []


def test_the_llm_gateway_alone_configures_embedding_for_the_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _model_env(monkeypatch)
    for key in _EMBEDDING_ENV:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("OPENAI_EMBEDDING_MODEL", "gateway-embedding")
    seen: list[object] = []

    class Probe(OfflineDescriptionEmbedder):
        def __init__(self, config: object) -> None:
            super().__init__()
            seen.append(config)

        def embed_query(self, text: str) -> tuple[float, ...]:
            return _OFFLINE.embed_query(text)

    monkeypatch.setattr(folder_pipeline, "LocalEmbeddingAdapter", Probe)
    embedder, reranker = folder_pipeline._preflight(
        embedder=None, reranker=None, needs_rerank=False
    )
    assert isinstance(embedder, Probe) and reranker is None
    (config,) = seen
    assert isinstance(config, LocalModelConfig)
    assert (config.base_url, config.model) == (PROVIDER_BASE_URL.rstrip("/"), "gateway-embedding")
    assert config.api_key.get_secret_value() == _LLM_ENV["APP_LLM_API_KEY"]


def test_a_missing_embedding_model_names_the_gateway_setting_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _model_env(monkeypatch)
    for key in _EMBEDDING_ENV:
        monkeypatch.delenv(key, raising=False)
    folder = _folder(tmp_path, ("meridian.pdf", _MERIDIAN))
    _forbid(monkeypatch, "ingest_pdf")

    with pytest.raises(PreflightError, match="OPENAI_EMBEDDING_MODEL") as raised:
        _run(tmp_path, folder, embedder=None)
    message = str(raised.value)
    assert message.index("simplest") < message.index("APP_EMBEDDING_BASE_URL")


# ---- 10. CLI -----------------------------------------------------------------------------


def _cli(arguments: list[str]) -> tuple[int, str]:
    stdout = io.StringIO()
    with redirect_stdout(stdout):
        code = cli.main(arguments)
    return code, stdout.getvalue()


def test_cli_prints_a_parseable_result_and_rejects_bad_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _model_env(monkeypatch)
    for key, value in _EMBEDDING_ENV.items():
        monkeypatch.setenv(key, value)
    factory: Callable[[object], OfflineDescriptionEmbedder] = lambda _config: _OFFLINE  # noqa: E731
    monkeypatch.setattr(folder_pipeline, "LocalEmbeddingAdapter", factory)
    folder = _folder(tmp_path, ("meridian.pdf", _MERIDIAN))
    base = ["run-folder", "--folder", str(folder), "--output-dir", str(tmp_path / "ingestion")]

    code, printed = _cli([*base, "--max-live-calls-per-pdf", str(_PER_PDF), "--no-tree"])

    assert code == 0
    result = FolderPipelineResult.model_validate_json(printed)
    assert [item.status for item in result.documents] == ["published"]

    for bad in (
        [*base, "--max-live-calls-per-pdf", "500"],
        ["run-folder", "--folder", str(tmp_path / "missing"), "--max-live-calls-per-pdf", "0"],
    ):
        code, printed = _cli(bad)
        assert code == 1
        assert "error" in json.loads(printed)


# ---- 11. NB_* defaults ---------------------------------------------------------------------


def _offline_embedding(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in _EMBEDDING_ENV.items():
        monkeypatch.setenv(key, value)
    factory: Callable[[object], OfflineDescriptionEmbedder] = lambda _config: _OFFLINE  # noqa: E731
    monkeypatch.setattr(folder_pipeline, "LocalEmbeddingAdapter", factory)


def _notebook_settings(monkeypatch: pytest.MonkeyPatch, **values: Path) -> None:
    for name, value in values.items():
        monkeypatch.setenv(name, str(value))
    get_settings.cache_clear()


def _questions_file(tmp_path: Path) -> Path:
    questions = tmp_path / "questions.jsonl"
    row = {"id": "named", "question": "What does page 2 say?", "doc": "meridian.pdf", "pages": "2"}
    questions.write_text(json.dumps(row) + "\n")
    return questions


def test_folder_questions_and_report_dir_default_to_the_notebook_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = _two_documents(tmp_path, monkeypatch)
    questions = _questions_file(tmp_path)
    llm, _prompts = _answer_llm(tmp_path)
    _notebook_settings(
        monkeypatch,
        NB_PDF_DIR=folder,
        NB_QUESTIONS_PATH=questions,
        NB_REPORT_DIR=tmp_path / "report",
    )

    result = run_folder_pipeline(
        ingestion_root=tmp_path / "ingestion",
        max_live_calls_per_pdf=_PER_PDF,
        build_tree=False,
        embedder=_OFFLINE,
        answer_llm=llm,
    )

    assert result.folder == str(folder.resolve())
    assert [item.status for item in result.documents] == ["published", "published"]
    assert result.eval is not None and [case.case_id for case in result.eval.cases] == ["named"]
    assert result.report_dir == str((tmp_path / "report").resolve())
    assert (tmp_path / "report" / "report.json").is_file()


def test_explicit_arguments_beat_the_notebook_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = _two_documents(tmp_path, monkeypatch)
    llm, _prompts = _answer_llm(tmp_path)
    _notebook_settings(
        monkeypatch,
        NB_PDF_DIR=tmp_path / "missing",
        NB_QUESTIONS_PATH=tmp_path / "missing.jsonl",
        NB_REPORT_DIR=tmp_path / "configured-report",
    )

    result = run_folder_pipeline(
        folder,
        questions=_questions_file(tmp_path),
        report_dir=tmp_path / "explicit-report",
        ingestion_root=tmp_path / "ingestion",
        max_live_calls_per_pdf=_PER_PDF,
        build_tree=False,
        embedder=_OFFLINE,
        answer_llm=llm,
    )

    assert result.eval is not None and result.folder == str(folder.resolve())
    assert (tmp_path / "explicit-report" / "report.json").is_file()
    assert not (tmp_path / "configured-report").exists()


def test_unset_questions_and_report_dir_keep_the_old_behaviour(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = _folder(tmp_path, ("meridian.pdf", _MERIDIAN))
    _model_env(monkeypatch)
    _notebook_settings(monkeypatch, NB_PDF_DIR=folder)

    result = run_folder_pipeline(
        ingestion_root=tmp_path / "ingestion",
        max_live_calls_per_pdf=_PER_PDF,
        build_tree=False,
        embedder=_OFFLINE,
    )

    assert result.eval is None and result.report_dir is None


def test_no_folder_from_either_place_is_a_clear_error_before_any_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _model_env(monkeypatch)
    _forbid(monkeypatch, "ingest_pdf")
    with pytest.raises(ValueError, match=r"pass folder or set NB_PDF_DIR"):
        run_folder_pipeline(
            ingestion_root=tmp_path / "ingestion", max_live_calls_per_pdf=0, embedder=_OFFLINE
        )


def test_cli_folder_defaults_to_nb_pdf_dir_and_errors_without_either(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _model_env(monkeypatch)
    _offline_embedding(monkeypatch)
    folder = _folder(tmp_path, ("meridian.pdf", _MERIDIAN))
    arguments = [
        "run-folder",
        "--output-dir",
        str(tmp_path / "ingestion"),
        "--max-live-calls-per-pdf",
        str(_PER_PDF),
        "--no-tree",
    ]

    code, printed = _cli(arguments)
    assert code == 1
    assert "NB_PDF_DIR" in json.loads(printed)["error"]

    _notebook_settings(monkeypatch, NB_PDF_DIR=folder)
    code, printed = _cli(arguments)
    assert code == 0
    result = FolderPipelineResult.model_validate_json(printed)
    assert result.folder == str(folder.resolve())
