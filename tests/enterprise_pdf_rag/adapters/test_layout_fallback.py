"""版面回退策略 ``layout_fallback``: 路由切分器拿不准的页不再回退 VLM 版面, 且绝不丢页.

``"model"``(默认)保持原样; ``"onnx-accept"`` 接受低置信 ONNX 结果, ONNX 完全没框时按文字层
切块; ``"text-only"`` 一律不调 VLM 版面, 有 ONNX 用 ONNX, 其余页按文字层切块.
"""

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pdfspine
import pytest
from pdfspine.geometry import Rect
from pydantic import TypeAdapter

import enterprise_pdf_rag.adapters.onnx_partition as onnx_partition
import enterprise_pdf_rag.adapters.pdfspine_tsr as pdfspine_tsr
from enterprise_pdf_rag.adapters.deterministic_partition import partition_counts
from enterprise_pdf_rag.adapters.folder_pipeline import run_folder_pipeline
from enterprise_pdf_rag.adapters.ingest_mode import (
    LAYOUT_FALLBACKS,
    IngestPlan,
    check_layout_fallback,
    choose_layout_policy,
    choose_unverified_table_structure,
    ingest_plan,
    make_partitioner,
)
from enterprise_pdf_rag.adapters.offline import OfflineDescriptionEmbedder
from enterprise_pdf_rag.adapters.onnx_partition import (
    ONNX_ACCEPTED_LOW_CONFIDENCE,
    ONNX_LAYOUT_MODEL_FILE,
    ONNX_LAYOUT_MODEL_URL,
    ONNX_MODELS_ENV,
    ONNX_PRODUCER_PREFIX,
    OnnxPagePartitioner,
    ensure_onnx_layout_weights,
    onnx_layout_status,
    onnx_layout_unavailable,
    resolve_onnx_layout_model,
)
from enterprise_pdf_rag.adapters.pdf_ingestion import ingest_pdf
from enterprise_pdf_rag.adapters.processing_store import ProcessingStore
from enterprise_pdf_rag.adapters.text_block_partition import (
    TEXT_BLOCK_MARK,
    TEXT_BLOCK_PRODUCER_PREFIX,
    TextBlockPartitioner,
)
from ragspine.common.evidence.configs import get_settings
from ragspine.extraction.evidence.page.models import (
    ObjectKind,
    PageInput,
    PagePartition,
    StageState,
)
from ragspine.extraction.evidence.page.service import validate_partition
from tests.enterprise_pdf_rag.adapters.lite_ingest_helpers import (
    CHART_REGION,
    claim_script,
    lite_env,
    mixed_folder,
)
from tests.enterprise_pdf_rag.adapters.test_deterministic_partition import (
    StubModelPartitioner,
    _page_input,
    _snapshot,
    authored_report,
)
from tests.enterprise_pdf_rag.adapters.test_onnx_partition import BANDS
from tests.enterprise_pdf_rag.adapters.test_onnx_partition_ingest import _onnx_env, _stub_blocks
from tests.enterprise_pdf_rag.answers.fake_llm import scripted_client

PRODUCER = f"{ONNX_PRODUCER_PREFIX}:pdfspine/{pdfspine.__version__}:000000000000"


def _block(
    bbox: tuple[float, float, float, float], label: str, raw: str = "", score: float = 0.9
) -> pdfspine.LayoutBlock:
    return pdfspine.LayoutBlock(Rect(*bbox), label, score, raw or label)


def _report_page(tmp_path: Path, index: int = 0, **kwargs: object) -> tuple[object, PageInput]:
    pdf = authored_report(tmp_path / "report.pdf", **kwargs)  # type: ignore[arg-type]
    sources, snapshot = _snapshot(tmp_path, pdf)
    return snapshot, _page_input(sources, snapshot, index)


# ---- 配置: 名字 / 默认 / 环境变量 ------------------------------------------------------


def test_the_default_fallback_is_the_model_everywhere() -> None:
    assert LAYOUT_FALLBACKS == ("model", "onnx-accept", "text-only")
    assert IngestPlan("lite").layout_fallback == "model"
    assert ingest_plan("lite").layout_fallback == "model"
    assert ingest_plan("full").layout_fallback == "model"
    assert get_settings().layout_fallback == "model"


def test_the_fallback_is_an_override_and_unknown_values_are_refused() -> None:
    assert ingest_plan("lite", layout_fallback="text-only").layout_fallback == "text-only"
    assert check_layout_fallback("onnx-accept") == "onnx-accept"
    with pytest.raises(ValueError, match="layout_fallback"):
        check_layout_fallback("vlm")


def test_the_fallback_is_read_from_app_layout_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_LAYOUT_FALLBACK", "text-only")
    get_settings.cache_clear()
    assert get_settings().layout_fallback == "text-only"


# ---- 纯文本切块: 每个 span 有归属, 零模型调用, 产物带 code -----------------------------


def test_text_blocks_cover_every_span_without_the_model(tmp_path: Path) -> None:
    snapshot, page = _report_page(tmp_path)
    partition = TextBlockPartitioner(snapshot).partition(page)  # type: ignore[arg-type]
    validate_partition(page, partition)
    assert partition.producer.startswith(TEXT_BLOCK_PRODUCER_PREFIX)
    assert TEXT_BLOCK_MARK in partition.diagnostics
    assert partition.objects and partition.unassigned_span_ids == ()
    assert {item.kind for item in partition.objects} == {ObjectKind.TEXT}
    owned = [span_id for item in partition.objects for span_id in item.source_span_ids]
    assert sorted(owned) == sorted(span.span_id for span in page.text.spans)
    texts = {span.span_id: span.text for span in page.text.spans}
    # 诊断只记 code / 计数, 绝不记正文.
    for diagnostic in partition.diagnostics:
        assert not any(text.strip() and text.strip() in diagnostic for text in texts.values())


def test_text_blocks_split_on_vertical_gaps(tmp_path: Path) -> None:
    snapshot, page = _report_page(tmp_path)
    partition = TextBlockPartitioner(snapshot).partition(page)  # type: ignore[arg-type]
    # 页眉 / 正文 / 页码之间有大的竖直空白: 至少三块, 而不是一整页一块.
    assert len(partition.objects) >= 3


def test_a_page_without_text_still_gets_a_partition(tmp_path: Path) -> None:
    snapshot, page = _report_page(tmp_path)
    empty = replace(page, text=replace(page.text, spans=()))
    partition = TextBlockPartitioner(snapshot).partition(empty)  # type: ignore[arg-type]
    validate_partition(empty, partition)
    assert partition.objects == () and TEXT_BLOCK_MARK in partition.diagnostics


def test_text_blocks_replay_deterministically(tmp_path: Path) -> None:
    snapshot, page = _report_page(tmp_path)
    first = TextBlockPartitioner(snapshot).partition(page)  # type: ignore[arg-type]
    assert TextBlockPartitioner(snapshot).partition(page) == first  # type: ignore[arg-type]


# ---- onnx-accept: 低置信接受, 无框退文本 ------------------------------------------------


def _onnx(
    snapshot: object, blocks: list[pdfspine.LayoutBlock], *, accept: bool
) -> tuple[StubModelPartitioner, OnnxPagePartitioner]:
    stub = StubModelPartitioner()
    return stub, OnnxPagePartitioner(
        stub,
        snapshot,  # type: ignore[arg-type]
        producer=PRODUCER,
        blocks_for=lambda _page: blocks,
        accept_low_confidence=accept,
    )


def test_onnx_accept_keeps_a_suspect_chart_instead_of_calling_the_model(tmp_path: Path) -> None:
    snapshot, page = _report_page(tmp_path)
    suspect = _block((450.0, 300.0, 560.0, 400.0), "figure", "chart", score=0.4)
    stub, partitioner = _onnx(snapshot, [*BANDS, suspect], accept=True)
    partition = partitioner.partition(page)
    assert stub.pages == []
    assert partition.producer == PRODUCER
    validate_partition(page, partition)
    # 低分图表框不再被丢: 它成了 Chart 对象, 图表分支照常取 IR.
    assert ObjectKind.CHART in {item.kind for item in partition.objects}
    assert ONNX_ACCEPTED_LOW_CONFIDENCE in partition.diagnostics


def test_onnx_accept_takes_all_low_score_blocks_when_none_passes_the_threshold(
    tmp_path: Path,
) -> None:
    snapshot, page = _report_page(tmp_path)
    weak = [
        _block(tuple(block.bbox), str(block.label), str(block.raw_label), score=0.35)  # type: ignore[arg-type]
        for block in BANDS
    ]
    stub, partitioner = _onnx(snapshot, weak, accept=True)
    partition = partitioner.partition(page)
    assert stub.pages == [] and partition.producer == PRODUCER
    assert len(partition.objects) == 3
    assert ONNX_ACCEPTED_LOW_CONFIDENCE in partition.diagnostics


def test_without_accept_the_same_page_still_falls_back(tmp_path: Path) -> None:
    snapshot, page = _report_page(tmp_path)
    suspect = _block((450.0, 300.0, 560.0, 400.0), "figure", "chart", score=0.4)
    stub, partitioner = _onnx(snapshot, [*BANDS, suspect], accept=False)
    partition = partitioner.partition(page)
    assert stub.pages == [0]
    assert ONNX_ACCEPTED_LOW_CONFIDENCE not in partition.diagnostics
    assert "accept" not in partitioner.fingerprint


def test_accepting_changes_the_router_fingerprint(tmp_path: Path) -> None:
    snapshot, _page = _report_page(tmp_path)
    _stub, strict = _onnx(snapshot, BANDS, accept=False)
    _stub, accepting = _onnx(snapshot, BANDS, accept=True)
    assert strict.fingerprint != accepting.fingerprint


# ---- make_partitioner: 回退位换成文本切块 ------------------------------------------------


@pytest.mark.parametrize("fallback", ["onnx-accept", "text-only"])
def test_a_no_model_fallback_never_reaches_the_model_on_a_figure_page(
    tmp_path: Path, fallback: str
) -> None:
    pdf = authored_report(tmp_path / "report.pdf", image_page=1)
    sources, snapshot = _snapshot(tmp_path, pdf)
    page = _page_input(sources, snapshot, 1)
    plan = ingest_plan("lite", layout_policy="deterministic-text-pages", layout_fallback=fallback)  # type: ignore[arg-type]
    client, prompts = scripted_client(tmp_path / "cache", claim_script, max_live_calls=0)
    partitioner = make_partitioner(plan, client, sources, snapshot)
    partition = partitioner.partition(page)
    validate_partition(page, partition)
    assert partition.producer.startswith(TEXT_BLOCK_PRODUCER_PREFIX)
    assert prompts == [] and client.live_call_count == 0


def test_the_model_fallback_keeps_the_model_partitioner(tmp_path: Path) -> None:
    pdf = authored_report(tmp_path / "report.pdf")
    sources, snapshot = _snapshot(tmp_path, pdf)
    client, _prompts = scripted_client(tmp_path / "cache", claim_script, max_live_calls=0)
    default = make_partitioner(
        ingest_plan("lite", layout_policy="deterministic-text-pages"), client, sources, snapshot
    )
    explicit = make_partitioner(
        ingest_plan("lite", layout_policy="deterministic-text-pages", layout_fallback="model"),
        client,
        sources,
        snapshot,
    )
    assert default.fingerprint == explicit.fingerprint
    assert TEXT_BLOCK_PRODUCER_PREFIX not in default.fingerprint


# ---- 整条 ingest: 零版面调用, 一页不丢, 计数如实 ------------------------------------------


def _ingest(tmp_path: Path, *, budget: int, **options: object) -> object:
    folder = mixed_folder(tmp_path)
    return ingest_pdf(
        pdf=folder / "mixed.pdf",
        output_dir=tmp_path / "output",
        stage="semantics",
        max_live_calls=budget,
        ingest_mode="lite",
        **options,  # type: ignore[arg-type]
    )


def test_the_default_with_a_zero_budget_leaves_figure_pages_without_a_layout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 现状(默认 "model")钉住: 预算 0 时回退页停在"需要模型", 这正是不能拿预算当开关的原因.
    tasks = lite_env(monkeypatch)
    summary = _ingest(tmp_path, budget=0, layout_policy="deterministic-text-pages")
    assert summary.layout_succeeded_pages == 4  # type: ignore[attr-defined]
    assert tasks["page-layout"] == 0


@pytest.mark.parametrize("fallback", ["onnx-accept", "text-only"])
def test_a_no_model_fallback_partitions_every_page_with_zero_live_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fallback: str
) -> None:
    tasks = lite_env(monkeypatch)
    summary = _ingest(
        tmp_path, budget=0, layout_policy="deterministic-text-pages", layout_fallback=fallback
    )
    assert summary.layout_succeeded_pages == 7  # type: ignore[attr-defined]
    assert summary.live_call_count == 0  # type: ignore[attr-defined]
    assert tasks["page-layout"] == 0
    assert summary.pages_partitioned_deterministically == 4  # type: ignore[attr-defined]
    assert summary.pages_partition_model_fallback == 0  # type: ignore[attr-defined]
    assert summary.partition_fallback_reasons == {}  # type: ignore[attr-defined]
    assert summary.pages_partition_text_fallback == 3  # type: ignore[attr-defined]
    reasons = summary.partition_text_fallback_reasons  # type: ignore[attr-defined]
    assert sum(reasons.values()) == 3 and set(reasons) <= {"residual_graphics", "has_image"}


def test_the_fallback_comes_from_settings_when_not_passed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tasks = lite_env(monkeypatch)
    monkeypatch.setenv("APP_LAYOUT_FALLBACK", "text-only")
    get_settings.cache_clear()
    summary = _ingest(tmp_path, budget=0, layout_policy="deterministic-text-pages")
    assert summary.layout_succeeded_pages == 7  # type: ignore[attr-defined]
    assert summary.pages_partition_text_fallback == 3  # type: ignore[attr-defined]
    assert tasks["page-layout"] == 0


def test_the_model_fallback_with_a_budget_still_spends_one_call_per_fallback_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tasks = lite_env(monkeypatch)
    summary = _ingest(
        tmp_path, budget=50, layout_policy="deterministic-text-pages", layout_fallback="model"
    )
    assert summary.layout_succeeded_pages == 7  # type: ignore[attr-defined]
    assert tasks["page-layout"] == 3
    assert summary.pages_partition_model_fallback == 3  # type: ignore[attr-defined]
    assert summary.pages_partition_text_fallback == 0  # type: ignore[attr-defined]


def test_text_fallback_pages_carry_their_code_in_the_saved_partition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lite_env(monkeypatch)
    summary = _ingest(
        tmp_path, budget=0, layout_policy="deterministic-text-pages", layout_fallback="text-only"
    )
    outputs = ProcessingStore(Path(summary.processing_store))  # type: ignore[attr-defined]
    manifest = outputs.load(summary.processing_id)  # type: ignore[attr-defined]
    text_pages = []
    for record in manifest.pages:
        assert record.partition.state is StageState.SUCCEEDED
        assert record.partition.artifact is not None
        partition = TypeAdapter(PagePartition).validate_json(
            outputs.assets.get(record.partition.artifact)
        )
        if partition.producer.startswith(TEXT_BLOCK_PRODUCER_PREFIX):
            assert TEXT_BLOCK_MARK in partition.diagnostics
            text_pages.append(record.page_index)
    assert len(text_pages) == 3
    counts = partition_counts(outputs, manifest)
    assert counts.text_fallback_pages == 3 and counts.model_fallback_pages == 0


def test_onnx_layout_with_onnx_accept_spends_no_layout_call_on_any_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tasks = lite_env(monkeypatch)
    _onnx_env(tmp_path, monkeypatch)

    def blocks(
        document: pdfspine.Document, page: PageInput, options: object
    ) -> list[pdfspine.LayoutBlock]:
        if page.page_index == 3:
            # 图表页: 正文框够分, 图表框只有 0.4 分 -> 接受(Chart 仍进划分, IR 仍由模型取).
            return [
                block if block.label != "figure" else _block(CHART_REGION, "figure", "chart", 0.4)
                for block in _stub_blocks(document, page, options)
            ]
        if page.page_index == 4:
            return []  # 完全没框 -> 文本切块
        return _stub_blocks(document, page, options)

    monkeypatch.setattr(onnx_partition, "layout_blocks", blocks)
    mixed_folder(tmp_path)
    monkeypatch.setenv("APP_LAYOUT_FALLBACK", "onnx-accept")
    get_settings.cache_clear()
    result = run_folder_pipeline(
        tmp_path / "pdfs",
        ingestion_root=tmp_path / "ingestion-accept",
        max_live_calls_per_pdf=200,
        embedder=OfflineDescriptionEmbedder(),
        ingest_mode="lite",
        layout_policy="onnx-layout",
        report_dir=tmp_path / "report-accept",
    )
    (document,) = result.documents
    assert document.status == "published" and document.ingestion is not None
    ingested = document.ingestion
    assert tasks["page-layout"] == 0
    assert ingested.layout_succeeded_pages == 7
    assert ingested.pages_partitioned_deterministically == 4
    assert ingested.pages_partitioned_onnx == 2
    assert ingested.pages_onnx_low_confidence_accepted == 1
    assert ingested.pages_partition_model_fallback == 0
    assert ingested.pages_partition_text_fallback == 1
    assert ingested.partition_text_fallback_reasons == {"onnx_low_confidence": 1}
    assert tasks["chart-ir"] == 1  # 接受的低分图表框照样走模型 IR 分支
    report = (tmp_path / "report-accept" / "report.md").read_text(encoding="utf-8")
    assert (
        "- `mixed.pdf`: deterministic 4, onnx 2, model fallback 0, "
        "text fallback 1 (onnx_low_confidence 1)" in report
    )


def test_text_only_progress_reports_the_text_fallback_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lite_env(monkeypatch)
    monkeypatch.setenv("APP_LAYOUT_FALLBACK", "text-only")
    get_settings.cache_clear()
    mixed_folder(tmp_path)
    events: list[tuple[str, dict[str, object]]] = []
    run_folder_pipeline(
        tmp_path / "pdfs",
        ingestion_root=tmp_path / "ingestion",
        max_live_calls_per_pdf=0,
        embedder=OfflineDescriptionEmbedder(),
        ingest_mode="lite",
        layout_policy="deterministic-text-pages",
        progress=lambda event, payload: events.append((event, dict(payload))),
    )
    (done,) = [payload for event, payload in events if event == "document_done"]
    assert done["pages"] == "7/7"
    assert done["pages_partition_text_fallback"] == 3
    assert sum(done["partition_text_fallback_reasons"].values()) == 3  # type: ignore[attr-defined]


# ---- Databricks 安装步骤: 权重下载与自检行 -----------------------------------------------


def test_weights_are_downloaded_into_the_configured_directory(tmp_path: Path) -> None:
    fetched: list[tuple[str, Path]] = []

    def download(url: str, target: Path) -> None:
        fetched.append((url, target))
        target.write_bytes(b"weights")

    models = tmp_path / "models"
    message = ensure_onnx_layout_weights(str(models), download=download)
    # 版面权重与表格结构权重(SLANet-plus, ADR 0031)放同一目录.
    assert [url for url, _target in fetched] == [ONNX_LAYOUT_MODEL_URL, pdfspine_tsr.MODEL_URL]
    assert (models / ONNX_LAYOUT_MODEL_FILE).read_bytes() == b"weights"
    assert (models / pdfspine_tsr.MODEL_FILE).read_bytes() == b"weights"
    assert str(models / ONNX_LAYOUT_MODEL_FILE) in message
    assert str(models / pdfspine_tsr.MODEL_FILE) in message
    # 已经在了: 不再下载.
    ensure_onnx_layout_weights(str(models), download=download)
    assert len(fetched) == 2


def test_a_configured_file_path_is_downloaded_as_that_file(tmp_path: Path) -> None:
    target = tmp_path / "custom" / "layout.onnx"

    def download(url: str, path: Path) -> None:
        path.write_bytes(b"weights")

    ensure_onnx_layout_weights(str(target), download=download)
    assert target.read_bytes() == b"weights"


def test_without_a_configured_path_weights_go_to_the_default_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Databricks 上 pull 之后什么都不配也能用: 权重落到 <项目根>/data/models/pdfspine-onnx/.
    monkeypatch.delenv(ONNX_MODELS_ENV, raising=False)
    default = tmp_path / "default-models"
    monkeypatch.setattr(onnx_partition, "DEFAULT_ONNX_MODELS_DIR", default)

    def download(url: str, path: Path) -> None:
        path.write_bytes(b"weights")

    message = ensure_onnx_layout_weights(None, download=download)
    assert (default / ONNX_LAYOUT_MODEL_FILE).read_bytes() == b"weights"
    assert (default / pdfspine_tsr.MODEL_FILE).read_bytes() == b"weights"
    assert str(default / ONNX_LAYOUT_MODEL_FILE) in message


@pytest.mark.usefixtures("onnx_runtime_present")
def test_table_structure_weights_in_the_default_directory_are_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(ONNX_MODELS_ENV, raising=False)
    monkeypatch.setattr(
        pdfspine_tsr, "get_settings", lambda: SimpleNamespace(onnx_layout_model=None)
    )
    default = tmp_path / "default-models"
    default.mkdir()
    monkeypatch.setattr(onnx_partition, "DEFAULT_ONNX_MODELS_DIR", default)
    assert pdfspine_tsr.table_structure_unavailable() is not None
    (default / pdfspine_tsr.MODEL_FILE).write_bytes(b"structure-weights")
    assert pdfspine_tsr.table_structure_unavailable() is None
    structure, _reason = choose_unverified_table_structure("auto", ingest_mode="lite")
    assert structure == "tsr"


@pytest.mark.usefixtures("onnx_runtime_present")
def test_weights_in_the_default_directory_are_found_without_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(ONNX_MODELS_ENV, raising=False)
    default = tmp_path / "default-models"
    default.mkdir()
    (default / ONNX_LAYOUT_MODEL_FILE).write_bytes(b"weights")
    monkeypatch.setattr(onnx_partition, "DEFAULT_ONNX_MODELS_DIR", default)
    assert resolve_onnx_layout_model(None) == default / ONNX_LAYOUT_MODEL_FILE
    assert onnx_layout_unavailable(None) is None
    policy, _reason = choose_layout_policy("auto", ingest_mode="lite", onnx_layout_model=None)
    assert policy == "onnx-layout"


def test_a_failed_download_names_the_url_instead_of_raising(tmp_path: Path) -> None:
    def download(url: str, path: Path) -> None:
        raise OSError("network unreachable")

    message = ensure_onnx_layout_weights(str(tmp_path / "models"), download=download)
    assert ONNX_LAYOUT_MODEL_URL in message and pdfspine_tsr.MODEL_URL in message
    assert "OSError" in message
    assert not (tmp_path / "models" / ONNX_LAYOUT_MODEL_FILE).exists()
    assert not (tmp_path / "models" / pdfspine_tsr.MODEL_FILE).exists()


@pytest.mark.usefixtures("onnx_runtime_present")
def test_the_status_line_names_the_weights_when_available(tmp_path: Path) -> None:
    model = tmp_path / ONNX_LAYOUT_MODEL_FILE
    model.write_bytes(b"weights")
    line = onnx_layout_status(str(model))
    assert "可用=是" in line and str(model) in line


def test_the_status_line_says_how_to_install_when_onnxruntime_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = tmp_path / ONNX_LAYOUT_MODEL_FILE
    model.write_bytes(b"weights")
    monkeypatch.setattr(
        onnx_partition, "_find_spec", lambda name: None if name == "onnxruntime" else object()
    )
    line = onnx_layout_status(str(model))
    assert "可用=否" in line and "pdfspine[onnx]" in line and str(model) in line


def test_the_status_line_says_unconfigured_without_weights(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(ONNX_MODELS_ENV, raising=False)
    line = onnx_layout_status(None)
    assert "可用=否" in line and "未配置" in line and "APP_ONNX_LAYOUT_MODEL" in line
