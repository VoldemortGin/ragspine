"""`layout_policy` 接入 ingest_pdf: 版面模型调用数 = 回退页数, 预算与计数可见."""

import json
from pathlib import Path

import pytest
from beartype.roar import BeartypeCallHintParamViolation
from pydantic import TypeAdapter

from enterprise_pdf_rag.adapters.aia_ingestion import read_text_sidecar
from enterprise_pdf_rag.adapters.document_store import LocalDocumentStore
from enterprise_pdf_rag.adapters.folder_pipeline import run_folder_pipeline
from enterprise_pdf_rag.adapters.offline import OfflineDescriptionEmbedder
from enterprise_pdf_rag.adapters.pdf_ingestion import ingest_pdf
from enterprise_pdf_rag.adapters.processing_store import ProcessingStore
from ragspine.extraction.evidence.objects.typed_ir import ObjectDescription
from tests.enterprise_pdf_rag.adapters.page_metadata_helpers import combined_sender
from tests.enterprise_pdf_rag.adapters.test_deterministic_partition import authored_report

_LLM_ENV = {
    "APP_LLM_API_KEY": "offline-secret",
    "APP_LLM_BASE_URL": "https://provider.invalid",
    "APP_LLM_MODEL": "offline-test",
}


def _model_env(monkeypatch: pytest.MonkeyPatch) -> tuple[list[bytes], list[str]]:
    for key, value in _LLM_ENV.items():
        monkeypatch.setenv(key, value)
    calls: list[bytes] = []
    metadata_calls: list[str] = []
    monkeypatch.setattr(
        "ragspine.common.evidence.providers.json_completion._send_once",
        combined_sender(calls, metadata_calls=metadata_calls),
    )
    return calls, metadata_calls


def _layout_calls(calls: list[bytes]) -> int:
    count = 0
    for payload in calls:
        content = json.loads(payload)["messages"][1]["content"]
        if isinstance(content, list) and "Source text observations:" in content[0]["text"]:
            count += 1
    return count


def test_deterministic_strategy_spends_layout_calls_only_on_fallback_pages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls, metadata_calls = _model_env(monkeypatch)
    pdf = authored_report(tmp_path / "mixed.pdf", page_count=4, bars_page=2, embedded_font=True)
    summary = ingest_pdf(
        pdf=pdf,
        output_dir=tmp_path / "output",
        stage="semantics",
        max_live_calls=50,
        layout_policy="deterministic-text-pages",
    )
    assert _layout_calls(calls) == 1  # 只有柱状图页回退到模型版面
    assert len(metadata_calls) == 4  # 页元数据仍然每页一次(不在本改动范围)
    assert summary.pages_partitioned_deterministically == 3
    assert summary.pages_partition_model_fallback == 1
    assert summary.partition_fallback_reasons == {"residual_graphics": 1}
    assert summary.pages_complete == 4
    assert summary.failed_stage_count == 0

    # 重跑: 全部命中缓存, 零调用, 计数从已存产物重导出而不是归零.
    before = len(calls)
    replay = ingest_pdf(
        pdf=pdf,
        output_dir=tmp_path / "output",
        stage="semantics",
        max_live_calls=50,
        layout_policy="deterministic-text-pages",
    )
    assert len(calls) == before
    assert replay.live_call_count == 0
    assert replay.processing_id == summary.processing_id
    assert replay.pages_partitioned_deterministically == 3
    assert replay.partition_fallback_reasons == {"residual_graphics": 1}


def test_default_strategy_is_unchanged_and_reports_no_partition_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls, _metadata = _model_env(monkeypatch)
    pdf = authored_report(tmp_path / "plain.pdf", page_count=2, embedded_font=True)
    summary = ingest_pdf(
        pdf=pdf, output_dir=tmp_path / "output", stage="semantics", max_live_calls=50
    )
    assert _layout_calls(calls) == 2  # 既有行为: 每页一次模型版面
    assert summary.pages_partitioned_deterministically == 0
    assert summary.pages_partition_model_fallback == 0
    assert summary.partition_fallback_reasons == {}


def test_the_two_strategies_coexist_on_one_document_without_new_live_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls, _metadata = _model_env(monkeypatch)
    pdf = authored_report(tmp_path / "mixed.pdf", page_count=3, bars_page=1, embedded_font=True)
    model_run = ingest_pdf(
        pdf=pdf, output_dir=tmp_path / "output", stage="semantics", max_live_calls=50
    )
    assert _layout_calls(calls) == 3
    # 切到确定性策略: 文本页不再调用, 回退页的模型请求与原先逐字节相同 → 命中模型缓存.
    deterministic_run = ingest_pdf(
        pdf=pdf,
        output_dir=tmp_path / "output",
        stage="semantics",
        max_live_calls=50,
        layout_policy="deterministic-text-pages",
    )
    assert _layout_calls(calls) == 3  # 没有任何新的版面调用
    assert deterministic_run.live_call_count == 0
    assert deterministic_run.processing_id != model_run.processing_id
    outputs = ProcessingStore(Path(deterministic_run.processing_store))
    assert outputs.load(model_run.processing_id) is not None  # 两种产物共存
    # 切回默认策略: 命中原 stage 缓存, 产物与第一次逐字节一致.
    model_replay = ingest_pdf(
        pdf=pdf, output_dir=tmp_path / "output", stage="semantics", max_live_calls=50
    )
    assert model_replay.processing_id == model_run.processing_id
    assert _layout_calls(calls) == 3


def test_folder_pipeline_publishes_and_text_pages_stay_retrievable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls, _metadata = _model_env(monkeypatch)
    folder = tmp_path / "pdfs"
    folder.mkdir()
    authored_report(folder / "mixed.pdf", page_count=4, bars_page=2, embedded_font=True)
    result = run_folder_pipeline(
        folder,
        ingestion_root=tmp_path / "ingestion",
        max_live_calls_per_pdf=20,
        build_tree=False,
        embedder=OfflineDescriptionEmbedder(),
        layout_policy="deterministic-text-pages",
    )
    (document,) = result.documents
    assert document.status == "published"
    assert document.ingestion is not None
    assert document.ingestion.pages_partitioned_deterministically == 3
    assert document.ingestion.partition_fallback_reasons == {"residual_graphics": 1}
    assert _layout_calls(calls) == 1  # 只有柱状图页做了模型版面
    # 确定性页的文本进入已发布的检索单元, 且引用能定位到页与 span.
    outputs = ProcessingStore(Path(document.ingestion.processing_store))
    _current_id, manifest = outputs.load_current()
    assert manifest.retrieval is not None
    plan, _index = outputs.load_retrieval(manifest.retrieval)
    descriptions = {
        member: TypeAdapter(ObjectDescription).validate_json(outputs.assets.get(member.description))
        for member in plan.members
    }
    member, description = next(
        (member, value)
        for member, value in descriptions.items()
        if "Dividend raised by ten percent" in value.text
    )
    assert member.page_index in (0, 1, 3)
    assert description.source.page_index == member.page_index
    assert description.source_span_ids
    sources = LocalDocumentStore(Path(document.ingestion.source_store))
    sidecar = read_text_sidecar(
        sources, sources.load(document.ingestion.source_manifest_id), member.page_index
    )
    observed = {span.span_id: span.text for span in sidecar.spans}
    assert all(span_id in observed for span_id in description.source_span_ids)
    assert "Dividend raised by ten percent" in " ".join(
        observed[span_id] for span_id in description.source_span_ids
    )
    # 重跑: 零调用, 复用已发布索引.
    before = len(calls)
    replay = run_folder_pipeline(
        folder,
        ingestion_root=tmp_path / "ingestion",
        max_live_calls_per_pdf=20,
        build_tree=False,
        embedder=OfflineDescriptionEmbedder(),
        layout_policy="deterministic-text-pages",
    )
    assert len(calls) == before
    assert replay.documents[0].live_calls == 0


def test_an_invalid_strategy_is_rejected_before_any_work(tmp_path: Path) -> None:
    pdf = authored_report(tmp_path / "plain.pdf", page_count=1)
    with pytest.raises((ValueError, BeartypeCallHintParamViolation)):
        ingest_pdf(
            pdf=pdf,
            output_dir=tmp_path / "output",
            layout_policy="deterministic",  # type: ignore[arg-type]
        )


def test_the_report_names_the_layout_and_each_pdfs_partition_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _model_env(monkeypatch)
    folder = tmp_path / "pdfs"
    folder.mkdir()
    authored_report(folder / "mixed.pdf", page_count=4, bars_page=2, embedded_font=True)
    result = run_folder_pipeline(
        folder,
        ingestion_root=tmp_path / "ingestion",
        max_live_calls_per_pdf=20,
        build_tree=False,
        embedder=OfflineDescriptionEmbedder(),
        ingest_mode="lite",
        layout_policy="deterministic-text-pages",
        report_dir=tmp_path / "report",
    )
    assert result.layout_policy == "deterministic-text-pages"
    report = (tmp_path / "report" / "report.md").read_text(encoding="utf-8")
    assert "- layout: **deterministic-text-pages**" in report
    assert "- `mixed.pdf`: deterministic 3, model fallback 1 (residual_graphics 1)" in report
    (document,) = result.documents
    assert document.index is not None
    assert f"embedding requests {document.index.embedding_requests}" in report
