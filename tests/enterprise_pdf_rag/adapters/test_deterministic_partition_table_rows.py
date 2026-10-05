"""确定性版面与无框线表格(ADR 0028 与 ADR 0027): 无框线表格区域以什么对象进入索引.

确定性切分器只把 lines 策略能证明网格的表切成 Table。无框线表格区域有两种去向:
- 版式不规整(表头只占右侧、科目折行、脚注行)时 ``column_layout`` 判为 ``ambiguous``,
  整页回退模型版面, 模型标出的 Table 照常按行收录;
- 每行都是"科目 + 数值"的规整表格按行读, 被 ``body_blocks`` 收进一个 Text 块: 每一
  印刷行的科目与数值落在同一 TextLine、同一 Text 对象, 描述逐字按印刷顺序保留它们,
  仍是同一检索单元(按行收录只作用于 Table 对象, 在这里不触发, 也不需要触发)。
"""

from pathlib import Path

import pytest
from pydantic import TypeAdapter

from enterprise_pdf_rag.adapters.pdf_ingestion import IngestionSummary, ingest_pdf
from enterprise_pdf_rag.adapters.processing_store import ProcessingStore
from ragspine.extraction.evidence.objects.typed_ir import ObjectDescription
from ragspine.extraction.evidence.page.models import ObjectKind
from tests.enterprise_pdf_rag.adapters.table_rows_helpers import (
    STATEMENT,
    model_env,
    statement_pdf,
)

_ROWS = (
    ("Revenue", "1,234,567", "1,100,200"),
    ("Cost of sales", "(456,789)", "(400,100)"),
    ("Operating profit", "790,123", "690,224"),
    ("Total", "790,123", "690,224"),
)
# 每一行都是"科目 + 两期数值"的规整无框线表格(无表头、无折行、无脚注).
_REGULAR = tuple(line for line in STATEMENT if line[2] in {cell for row in _ROWS for cell in row})


def _ingest(pdf: Path, root: Path) -> IngestionSummary:
    return ingest_pdf(
        pdf=pdf,
        output_dir=root,
        stage="semantics",
        max_live_calls=10,
        layout_policy="deterministic-text-pages",
        unverified_tables_as_rows=True,
    )


def test_an_irregular_unruled_table_falls_back_to_the_model_layout_and_is_indexed_as_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model_env(monkeypatch)
    summary = _ingest(statement_pdf(tmp_path / "statement.pdf"), tmp_path / "output")
    assert summary.pages_partitioned_deterministically == 1  # 标题页
    assert summary.partition_fallback_reasons == {"ambiguous_columns": 1}
    assert summary.table_row_transcriptions == 1
    assert summary.table_row_lines == 9


def test_a_regular_unruled_table_on_a_text_page_keeps_each_row_in_one_text_object(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = model_env(monkeypatch)
    pdf = statement_pdf(tmp_path / "statement.pdf", lines=_REGULAR)
    summary = _ingest(pdf, tmp_path / "output")
    assert summary.pages_partitioned_deterministically == 2
    assert summary.pages_partition_model_fallback == 0
    assert all(b"Source text observations:" not in payload for payload in calls)
    # 确定性页上没有 Table 对象, 按行收录无从触发.
    assert summary.table_row_transcriptions == 0

    outputs = ProcessingStore(Path(summary.processing_store))
    page = outputs.load(summary.processing_id).pages[-1]
    assert ObjectKind.TABLE not in {item.kind for item in page.objects}
    descriptions: list[str] = []
    for item in page.objects:
        stage = {outcome.stage: outcome for outcome in item.stages}["description"]
        assert stage.artifact is not None
        descriptions.append(
            TypeAdapter(ObjectDescription).validate_json(outputs.assets.get(stage.artifact)).text
        )
    for row in _ROWS:
        owners = [text for text in descriptions if all(cell in text for cell in row)]
        assert len(owners) == 1, row
        # 科目与它的两期数值在描述里按印刷顺序紧邻.
        assert " ".join(row) in " ".join(owners[0].split()), row
