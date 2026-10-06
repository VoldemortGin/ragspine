"""ONNX 版面切分器: 标签映射 / span 归属 / 原因码回退 / producer 含模型摘要(桩测试, 零真模型)."""

from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pdfspine
import pytest
from pdfspine.geometry import Rect

import enterprise_pdf_rag.adapters.onnx_partition as onnx_partition
from enterprise_pdf_rag.adapters.document_store import LocalDocumentStore
from enterprise_pdf_rag.adapters.onnx_partition import (
    ONNX_LAYOUT_MODEL_FILE,
    ONNX_MARK,
    ONNX_MODELS_ENV,
    ONNX_PRODUCER_PREFIX,
    OnnxPagePartitioner,
    make_onnx_page_partitioner,
    resolve_onnx_layout_model,
)
from ragspine.extraction.evidence.document.models import DocumentSnapshot
from ragspine.extraction.evidence.page.models import ObjectKind, PageInput, PagePartition
from ragspine.extraction.evidence.page.service import validate_partition
from tests.enterprise_pdf_rag.adapters.test_deterministic_partition import (
    HEADER_TEXT,
    PAGE_H,
    PAGE_W,
    StubModelPartitioner,
    _page_input,
    _snapshot,
    authored_report,
)

PRODUCER = f"{ONNX_PRODUCER_PREFIX}:pdfspine/{pdfspine.__version__}:000000000000"


def _block(
    bbox: tuple[float, float, float, float], label: str, raw: str = "", score: float = 0.9
) -> object:
    return pdfspine.LayoutBlock(Rect(*bbox), label, score, raw or label)


def _partitioner(
    snapshot: DocumentSnapshot, blocks: list[object] | Exception
) -> tuple[StubModelPartitioner, OnnxPagePartitioner]:
    stub = StubModelPartitioner()

    def blocks_for(page: PageInput) -> list[object]:
        if isinstance(blocks, Exception):
            raise blocks
        return blocks

    return stub, OnnxPagePartitioner(stub, snapshot, producer=PRODUCER, blocks_for=blocks_for)


def _report_page(tmp_path: Path) -> tuple[LocalDocumentStore, DocumentSnapshot, PageInput]:
    pdf = authored_report(tmp_path / "report.pdf")
    sources, snapshot = _snapshot(tmp_path, pdf)
    return sources, snapshot, _page_input(sources, snapshot, 0)


def _fallback_reason(partition: PagePartition) -> str | None:
    for diagnostic in partition.diagnostics:
        if diagnostic.startswith("onnx-partition: model fallback ("):
            return diagnostic.removeprefix("onnx-partition: model fallback (")[:-1]
    return None


# 三个不重叠横带盖满 authored_report 的页: 页眉带 / 正文带 / 页脚带.
HEADER_BAND = (0.0, 0.0, PAGE_W, 60.0)
BODY_BAND = (0.0, 60.0, PAGE_W, 700.0)
FOOTER_BAND = (0.0, 700.0, PAGE_W, PAGE_H)
BANDS = [
    _block(HEADER_BAND, "abandon", "header"),
    _block(BODY_BAND, "plain text", "text"),
    _block(FOOTER_BAND, "abandon", "number"),
]


def test_onnx_blocks_map_to_text_objects_and_cover_every_span(tmp_path: Path) -> None:
    _sources, snapshot, page = _report_page(tmp_path)
    stub, partitioner = _partitioner(snapshot, BANDS)
    partition = partitioner.partition(page)

    assert stub.pages == []
    assert partition.producer == PRODUCER
    assert partition.schema_version == "layout-enrichment-v2"
    assert partition.unassigned_span_ids == ()
    assert ONNX_MARK in partition.diagnostics
    validate_partition(page, partition)
    assert [item.kind for item in partition.objects] == [ObjectKind.TEXT] * 3
    texts = {span.span_id: span.text for span in page.text.spans}
    header, body, footer = partition.objects
    assert "Running page header" in header.interpretation
    assert "Page number" in footer.interpretation
    assert " ".join(texts[s] for s in header.source_span_ids) == HEADER_TEXT
    assert "Results overview" in " ".join(texts[s] for s in body.source_span_ids)


def test_visual_labels_map_to_table_chart_image_and_formula(tmp_path: Path) -> None:
    _sources, snapshot, page = _report_page(tmp_path)
    blocks = [
        *BANDS,
        _block((300.0, 300.0, 400.0, 350.0), "table", "table"),
        _block((300.0, 360.0, 400.0, 410.0), "figure", "chart"),
        _block((300.0, 420.0, 400.0, 470.0), "figure", "image"),
        _block((300.0, 480.0, 400.0, 530.0), "isolate_formula", "display_formula"),
    ]
    _stub, partitioner = _partitioner(snapshot, blocks)
    partition = partitioner.partition(page)
    validate_partition(page, partition)
    kinds = [item.kind for item in partition.objects[3:]]
    assert kinds == [ObjectKind.TABLE, ObjectKind.CHART, ObjectKind.IMAGE, ObjectKind.FORMULA]


def test_an_unknown_figure_class_is_conservatively_a_chart(tmp_path: Path) -> None:
    # 拿不准的 figure 交给模型图表分支判定(错标成 Image 的代价是图表数字永久不可答).
    _sources, snapshot, page = _report_page(tmp_path)
    blocks = [*BANDS, _block((300.0, 300.0, 400.0, 350.0), "figure", "hologram")]
    _stub, partitioner = _partitioner(snapshot, blocks)
    partition = partitioner.partition(page)
    assert partition.objects[3].kind is ObjectKind.CHART


def test_a_span_inside_nested_blocks_goes_to_the_innermost(tmp_path: Path) -> None:
    _sources, snapshot, page = _report_page(tmp_path)
    texts = {span.span_id: span.text for span in page.text.spans}
    heading = next(span for span in page.text.spans if texts[span.span_id] == "Results overview")
    inner = (
        heading.bbox[0] - 5.0,
        heading.bbox[1] - 5.0,
        heading.bbox[2] + 5.0,
        heading.bbox[3] + 5.0,
    )
    _stub, partitioner = _partitioner(
        snapshot, [*BANDS, _block(inner, "title", "paragraph_title")]
    )
    partition = partitioner.partition(page)
    validate_partition(page, partition)
    title = partition.objects[3]
    assert "Heading" in title.interpretation
    assert heading.span_id in title.source_span_ids
    assert heading.span_id not in partition.objects[1].source_span_ids


def test_partially_overlapping_blocks_fall_back_to_the_model(tmp_path: Path) -> None:
    _sources, snapshot, page = _report_page(tmp_path)
    texts = {span.span_id: span.text for span in page.text.spans}
    heading = next(span for span in page.text.spans if texts[span.span_id] == "Results overview")
    x, y = (heading.bbox[0] + heading.bbox[2]) / 2, (heading.bbox[1] + heading.bbox[3]) / 2
    # 两个都含 span 中心但互不包含的框: 模型版面自相矛盾, 整页回退.
    overlapping = [
        _block(HEADER_BAND, "abandon", "header"),
        _block(FOOTER_BAND, "abandon", "number"),
        _block((0.0, 60.0, x + 10.0, 700.0), "plain text", "text"),
        _block((x - 10.0, y - 5.0, PAGE_W, 700.0), "plain text", "text"),
    ]
    stub, partitioner = _partitioner(snapshot, overlapping)
    partition = partitioner.partition(page)
    assert stub.pages == [0]
    assert partition.producer == StubModelPartitioner.fingerprint
    assert _fallback_reason(partition) == "onnx_overlapping_blocks"


def test_a_stray_span_merges_into_the_nearest_text_block(tmp_path: Path) -> None:
    _sources, snapshot, page = _report_page(tmp_path)
    # 没有页脚带: 页码 span 落空, 并入最近的 Text 块并把它的 bbox 扩到并集.
    _stub, partitioner = _partitioner(snapshot, BANDS[:2])
    partition = partitioner.partition(page)
    validate_partition(page, partition)
    assert partition.unassigned_span_ids == ()
    texts = {span.span_id: span.text for span in page.text.spans}
    number = next(span for span in page.text.spans if texts[span.span_id] == "1")
    body = partition.objects[1]
    assert number.span_id in body.source_span_ids
    assert body.bbox[3] >= number.bbox[3]
    assert any(d == "merged_spans=1" for d in partition.diagnostics)


def test_mostly_uncovered_pages_fall_back_as_unassigned_spans(tmp_path: Path) -> None:
    _sources, snapshot, page = _report_page(tmp_path)
    stub, partitioner = _partitioner(snapshot, [_block(HEADER_BAND, "abandon", "header")])
    partition = partitioner.partition(page)
    assert stub.pages == [0]
    assert _fallback_reason(partition) == "onnx_unassigned_spans"


def test_stray_spans_with_no_text_block_fall_back(tmp_path: Path) -> None:
    _sources, snapshot, page = _report_page(tmp_path)
    # 只有页码 span 落空(占比低于回退阈值), 但页上没有任何 Text 块可并: 仍然回退不猜.
    blocks = [
        _block(BODY_BAND, "figure", "image"),
        _block(HEADER_BAND, "figure", "image"),
    ]
    stub, partitioner = _partitioner(snapshot, blocks)
    partition = partitioner.partition(page)
    assert stub.pages == [0]
    assert _fallback_reason(partition) == "onnx_unassigned_spans"


def test_a_suspect_visual_block_nobody_explains_falls_back(tmp_path: Path) -> None:
    """[0.3, 0.5) 的图表框不进划分, 但它的存在说明这页可能有图被漏掉: 回退模型, 不静默丢图.

    真实对照里钉住的失败形态(AIA p18): 右半页的柱状图只有 0.389 分, 阈值 0.5 下整页被
    "干净地"切完, 图表数字静默不可答; 这个守卫把它变成一次模型版面调用.
    """
    _sources, snapshot, page = _report_page(tmp_path)
    suspect = _block((450.0, 300.0, 560.0, 400.0), "figure", "chart", score=0.4)
    stub, partitioner = _partitioner(snapshot, [*BANDS, suspect])
    partition = partitioner.partition(page)
    assert stub.pages == [0]
    assert _fallback_reason(partition) == "onnx_low_confidence"


def test_a_suspect_visual_block_inside_an_accepted_one_is_explained(tmp_path: Path) -> None:
    _sources, snapshot, page = _report_page(tmp_path)
    accepted = _block((300.0, 300.0, 500.0, 450.0), "figure", "chart", score=0.7)
    duplicate = _block((320.0, 310.0, 480.0, 440.0), "figure", "chart", score=0.4)
    _stub, partitioner = _partitioner(snapshot, [*BANDS, accepted, duplicate])
    partition = partitioner.partition(page)
    assert partition.producer == PRODUCER
    assert ObjectKind.CHART in {item.kind for item in partition.objects}
    assert sum(item.kind is ObjectKind.CHART for item in partition.objects) == 1


def test_a_low_score_text_block_is_simply_ignored(tmp_path: Path) -> None:
    # 低分文本框不是守卫对象: 文本永不静默丢失(span 覆盖规则兜底), 回退只为视觉对象设.
    _sources, snapshot, page = _report_page(tmp_path)
    weak_text = _block((300.0, 300.0, 400.0, 350.0), "plain text", "text", score=0.4)
    _stub, partitioner = _partitioner(snapshot, [*BANDS, weak_text])
    partition = partitioner.partition(page)
    assert partition.producer == PRODUCER
    assert len(partition.objects) == 3


def test_an_empty_detection_falls_back_as_low_confidence(tmp_path: Path) -> None:
    _sources, snapshot, page = _report_page(tmp_path)
    stub, partitioner = _partitioner(snapshot, [])
    partition = partitioner.partition(page)
    assert stub.pages == [0]
    assert _fallback_reason(partition) == "onnx_low_confidence"


def test_an_inference_error_falls_back_as_unavailable(tmp_path: Path) -> None:
    _sources, snapshot, page = _report_page(tmp_path)
    stub, partitioner = _partitioner(snapshot, RuntimeError("onnxruntime exploded"))
    partition = partitioner.partition(page)
    assert stub.pages == [0]
    assert _fallback_reason(partition) == "onnx_unavailable"


def test_duplicate_object_identities_fall_back_as_partition_invalid(tmp_path: Path) -> None:
    _sources, snapshot, page = _report_page(tmp_path)
    duplicate = _block((300.0, 300.0, 400.0, 350.0), "figure", "image")
    stub, partitioner = _partitioner(snapshot, [*BANDS, duplicate, duplicate])
    partition = partitioner.partition(page)
    assert stub.pages == [0]
    assert _fallback_reason(partition) == "onnx_partition_invalid"


def test_replay_is_deterministic(tmp_path: Path) -> None:
    _sources, snapshot, page = _report_page(tmp_path)
    _stub, partitioner = _partitioner(snapshot, BANDS)
    assert partitioner.partition(page) == partitioner.partition(page)
    _stub2, second = _partitioner(snapshot, BANDS)
    assert second.partition(page) == partitioner.partition(page)


def test_a_page_from_another_source_is_refused(tmp_path: Path) -> None:
    _sources, _own_snapshot, page = _report_page(tmp_path)
    other = authored_report(tmp_path / "other.pdf", page_count=1)
    _o_sources, other_snapshot = _snapshot(tmp_path / "other", other)
    _stub, partitioner = _partitioner(other_snapshot, BANDS)
    with pytest.raises(ValueError, match="another source"):
        partitioner.partition(page)


# ---- 模型文件解析 / 可用性预检 / producer ------------------------------------------------


def test_resolve_accepts_a_file_or_its_directory(tmp_path: Path) -> None:
    model = tmp_path / ONNX_LAYOUT_MODEL_FILE
    model.write_bytes(b"weights")
    assert resolve_onnx_layout_model(str(model)) == model
    assert resolve_onnx_layout_model(str(tmp_path)) == model


def test_resolve_falls_back_to_the_pdfspine_models_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = tmp_path / ONNX_LAYOUT_MODEL_FILE
    model.write_bytes(b"weights")
    monkeypatch.setenv(ONNX_MODELS_ENV, str(tmp_path))
    assert resolve_onnx_layout_model(None) == model


def test_a_missing_model_file_names_the_setting(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="APP_ONNX_LAYOUT_MODEL"):
        resolve_onnx_layout_model(str(tmp_path / "missing.onnx"))


def test_an_unconfigured_model_path_names_both_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(ONNX_MODELS_ENV, raising=False)
    with pytest.raises(ValueError) as excinfo:
        resolve_onnx_layout_model(None)
    assert "APP_ONNX_LAYOUT_MODEL" in str(excinfo.value)
    assert ONNX_MODELS_ENV in str(excinfo.value)


def test_the_producer_carries_the_model_digest(tmp_path: Path) -> None:
    _sources, snapshot, _page = _report_page(tmp_path)
    model = tmp_path / ONNX_LAYOUT_MODEL_FILE
    model.write_bytes(b"fake-weights")
    sources = LocalDocumentStore(tmp_path / "unused")
    partitioner = make_onnx_page_partitioner(
        StubModelPartitioner(), sources, snapshot, layout_model=str(model)
    )
    digest = sha256(b"fake-weights").hexdigest()[:12]
    expected = f"{ONNX_PRODUCER_PREFIX}:pdfspine/{pdfspine.__version__}:{digest}"
    assert isinstance(partitioner, OnnxPagePartitioner)
    assert partitioner.producer == expected
    assert expected in partitioner.fingerprint
    assert StubModelPartitioner.fingerprint in partitioner.fingerprint


def test_the_model_path_is_read_from_settings_when_not_passed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _sources, snapshot, _page = _report_page(tmp_path)
    model = tmp_path / ONNX_LAYOUT_MODEL_FILE
    model.write_bytes(b"fake-weights")
    monkeypatch.setattr(
        onnx_partition, "get_settings", lambda: SimpleNamespace(onnx_layout_model=str(model))
    )
    partitioner = make_onnx_page_partitioner(
        StubModelPartitioner(), LocalDocumentStore(tmp_path / "unused"), snapshot
    )
    assert isinstance(partitioner, OnnxPagePartitioner)
    assert sha256(b"fake-weights").hexdigest()[:12] in partitioner.producer


_REAL_WEIGHTS = Path.home() / "models" / "pdfspine-onnx" / ONNX_LAYOUT_MODEL_FILE


@pytest.mark.onnx
@pytest.mark.skipif(not _REAL_WEIGHTS.is_file(), reason="需要本地 PP-DocLayoutV3 权重")
def test_the_real_model_partitions_an_authored_text_page(tmp_path: Path) -> None:
    """真模型集成: 钉住 pdfspine ``find_layout`` 的调用方式(参数名传错曾被桩测试盖住)."""
    pdf = authored_report(tmp_path / "report.pdf")
    sources, snapshot = _snapshot(tmp_path, pdf)
    page = _page_input(sources, snapshot, 0)
    partitioner = make_onnx_page_partitioner(
        StubModelPartitioner(), sources, snapshot, layout_model=str(_REAL_WEIGHTS)
    )
    partition = partitioner.partition(page)
    # 推理路径必须机械可用: 绝不允许 onnx_unavailable(那是调用方式坏了, 不是版面难).
    assert _fallback_reason(partition) != "onnx_unavailable"
    if partition.producer.startswith(ONNX_PRODUCER_PREFIX):
        validate_partition(page, partition)
        assert partition.unassigned_span_ids == ()


def test_a_missing_onnxruntime_is_a_clear_error_before_any_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _sources, snapshot, _page = _report_page(tmp_path)
    model = tmp_path / ONNX_LAYOUT_MODEL_FILE
    model.write_bytes(b"fake-weights")
    monkeypatch.setattr(
        onnx_partition,
        "_find_spec",
        lambda name: None if name == "onnxruntime" else object(),
    )
    with pytest.raises(ValueError, match=r"pdfspine\[onnx\]"):
        make_onnx_page_partitioner(
            StubModelPartitioner(), LocalDocumentStore(tmp_path / "unused"), snapshot,
            layout_model=str(model),
        )
