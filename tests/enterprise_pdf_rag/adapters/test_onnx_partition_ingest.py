"""``"onnx-layout"`` 接入 ingest: 版面 LLM 调用数 = 回退页数, 图表 IR 仍由模型生成, 可答可发布."""

from pathlib import Path
from types import SimpleNamespace

import pdfspine
import pytest
from pdfspine.geometry import Rect

import enterprise_pdf_rag.adapters.onnx_partition as onnx_partition
from enterprise_pdf_rag.adapters.folder_pipeline import run_folder_pipeline
from enterprise_pdf_rag.adapters.offline import OfflineDescriptionEmbedder
from enterprise_pdf_rag.adapters.onnx_partition import ONNX_LAYOUT_MODEL_FILE, ONNX_MODELS_ENV
from enterprise_pdf_rag.adapters.pdf_ingestion import ingest_pdf
from ragspine.extraction.evidence.page.models import PageInput
from tests.enterprise_pdf_rag.adapters.lite_ingest_helpers import (
    CHART_REGION,
    IMAGE_RECT,
    claim_script,
    lite_env,
    mixed_folder,
    write_questions,
)
from tests.enterprise_pdf_rag.adapters.test_pdf_ingestion import DIAGRAM_REGION
from tests.enterprise_pdf_rag.answers.fake_llm import scripted_client

# mixed_pdf 里会落到 ONNX 切分器的三页(纯文字 / 划线表 / 无线表 / 公式页由确定性分诊处理):
# 图表页出 Chart(IR 仍由模型取), 图片页出 Image, 流程图页按已知限制被 ONNX 认作 Image.
_SPECIAL = {
    3: ("figure", "chart", CHART_REGION),
    4: ("figure", "image", IMAGE_RECT),
    6: ("figure", "image", DIAGRAM_REGION),
}


def _center_inside(bbox: tuple[float, ...], region: tuple[float, ...]) -> bool:
    x, y = (bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2
    return region[0] <= x <= region[2] and region[1] <= y <= region[3]


def _stub_blocks(
    document: pdfspine.Document, page: PageInput, options: object
) -> list[pdfspine.LayoutBlock]:
    label, raw, region = _SPECIAL[page.page_index]
    rest = [span.bbox for span in page.text.spans if not _center_inside(span.bbox, region)]
    blocks = [
        pdfspine.LayoutBlock(
            Rect(
                min(b[0] for b in rest),
                min(b[1] for b in rest),
                max(b[2] for b in rest),
                max(b[3] for b in rest),
            ),
            "plain text",
            0.9,
            "text",
        ),
        pdfspine.LayoutBlock(Rect(*region), label, 0.9, raw),
    ]
    return blocks


def _onnx_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    models = tmp_path / "models"
    models.mkdir(exist_ok=True)
    (models / ONNX_LAYOUT_MODEL_FILE).write_bytes(b"stub-weights")
    monkeypatch.setenv(ONNX_MODELS_ENV, str(models))
    monkeypatch.setattr(onnx_partition, "layout_blocks", _stub_blocks)


def test_onnx_layout_spends_no_layout_calls_and_keeps_chart_ir_on_the_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tasks = lite_env(monkeypatch)
    _onnx_env(tmp_path, monkeypatch)
    mixed_folder(tmp_path)
    llm, _ = scripted_client(tmp_path / "answers", claim_script, max_live_calls=20)
    result = run_folder_pipeline(
        tmp_path / "pdfs",
        questions=write_questions(tmp_path),
        ingestion_root=tmp_path / "ingestion",
        max_live_calls_per_pdf=200,
        embedder=OfflineDescriptionEmbedder(),
        answer_llm=llm,
        ingest_mode="lite",
        layout_policy="onnx-layout",
    )
    (document,) = result.documents
    assert document.status == "published" and document.ingestion is not None
    # 文本 / 划线表 / 无线表 / 公式页确定性处理, 图表 / 图片 / 流程图页由 ONNX 切分: 零模型版面.
    assert document.ingestion.pages_partitioned_deterministically == 4
    assert document.ingestion.pages_partitioned_onnx == 3
    assert document.ingestion.pages_partition_model_fallback == 0
    assert document.ingestion.partition_fallback_reasons == {}
    assert tasks["page-layout"] == 0
    # 图表 IR 仍只来自模型(用户决定); lite 的描述从 IR 派生, 不再单发调用.
    assert tasks["chart-ir"] == 1
    assert tasks["chart-description"] == 0
    assert result.eval is not None
    verdicts = {case.case_id: case.verdict for case in result.eval.cases}
    # 图表数字可答, 文字 / 表格可答; 公式页由确定性分诊处理, 与 ADR 0028 相同的已知限制.
    assert verdicts == {
        "chart": "answered",
        "unruled": "answered",
        "ruled": "answered",
        "period": "answered",
        "formula": "abstained",
    }
    # 重跑: 全部命中缓存, 零调用, 计数从已存产物重导出.
    tasks.clear()
    replay = run_folder_pipeline(
        tmp_path / "pdfs",
        ingestion_root=tmp_path / "ingestion",
        max_live_calls_per_pdf=200,
        embedder=OfflineDescriptionEmbedder(),
        ingest_mode="lite",
        layout_policy="onnx-layout",
    )
    assert dict(tasks) == {} and replay.documents[0].live_calls == 0
    ingested = replay.documents[0].ingestion
    assert ingested is not None and ingested.pages_partitioned_onnx == 3


def test_an_onnx_fallback_page_spends_exactly_one_layout_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tasks = lite_env(monkeypatch)
    _onnx_env(tmp_path, monkeypatch)

    def flaky_blocks(
        document: pdfspine.Document, page: PageInput, options: object
    ) -> list[pdfspine.LayoutBlock]:
        if page.page_index == 3:
            return []  # 图表页没有任何达到阈值的框: onnx_low_confidence 回退模型版面.
        return _stub_blocks(document, page, options)

    monkeypatch.setattr(onnx_partition, "layout_blocks", flaky_blocks)
    mixed_folder(tmp_path)
    result = run_folder_pipeline(
        tmp_path / "pdfs",
        ingestion_root=tmp_path / "ingestion",
        max_live_calls_per_pdf=200,
        embedder=OfflineDescriptionEmbedder(),
        ingest_mode="lite",
        layout_policy="onnx-layout",
        report_dir=tmp_path / "report",
    )
    (document,) = result.documents
    assert document.status == "published" and document.ingestion is not None
    assert tasks["page-layout"] == 1  # 版面 LLM 调用数 = 回退页数
    assert document.ingestion.pages_partitioned_onnx == 2
    assert document.ingestion.pages_partition_model_fallback == 1
    assert document.ingestion.partition_fallback_reasons == {"onnx_low_confidence": 1}
    assert tasks["chart-ir"] == 1  # 回退页的模型版面照样提出 Chart, IR 分支不变.
    report = (tmp_path / "report" / "report.md").read_text(encoding="utf-8")
    assert "- layout: **onnx-layout**" in report
    assert (
        "- `mixed.pdf`: deterministic 4, onnx 2, model fallback 1 (onnx_low_confidence 1)"
        in report
    )


def test_switching_to_onnx_layout_on_an_ingested_document_sends_no_new_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tasks = lite_env(monkeypatch)
    _onnx_env(tmp_path, monkeypatch)
    # ONNX 对三个含图页全部放弃: 回退页的模型请求与默认策略逐字节相同 → 全部命中模型缓存.
    monkeypatch.setattr(onnx_partition, "layout_blocks", lambda document, page, options: [])
    mixed_folder(tmp_path)
    folder = tmp_path / "pdfs"
    first = run_folder_pipeline(
        folder,
        ingestion_root=tmp_path / "ingestion",
        max_live_calls_per_pdf=200,
        embedder=OfflineDescriptionEmbedder(),
        ingest_mode="lite",
    )
    assert first.documents[0].status == "published"
    assert tasks["page-layout"] == 7  # 预设 layout="model": 每页一次
    tasks.clear()
    switched = run_folder_pipeline(
        folder,
        ingestion_root=tmp_path / "ingestion",
        max_live_calls_per_pdf=200,
        embedder=OfflineDescriptionEmbedder(),
        ingest_mode="lite",
        layout_policy="onnx-layout",
    )
    (document,) = switched.documents
    assert document.status == "published" and document.ingestion is not None
    assert tasks["page-layout"] == 0  # 回退页命中模型缓存, 零新增 live call
    assert document.live_calls == 0
    assert document.ingestion.pages_partition_model_fallback == 3
    assert document.ingestion.partition_fallback_reasons == {"onnx_low_confidence": 3}


def test_a_missing_model_file_stops_the_ingest_before_any_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lite_env(monkeypatch)
    monkeypatch.delenv(ONNX_MODELS_ENV, raising=False)
    monkeypatch.setattr(
        onnx_partition, "get_settings", lambda: SimpleNamespace(onnx_layout_model=None)
    )
    folder = mixed_folder(tmp_path)
    with pytest.raises(ValueError, match="APP_ONNX_LAYOUT_MODEL"):
        ingest_pdf(
            pdf=folder / "mixed.pdf",
            output_dir=tmp_path / "output",
            stage="semantics",
            max_live_calls=50,
            layout_policy="onnx-layout",
        )
