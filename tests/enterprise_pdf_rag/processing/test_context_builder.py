"""Context blocks carry only stored evidence and are dropped whole under budget."""

from decimal import Decimal
from pathlib import Path

import pytest

from enterprise_pdf_rag.adapters.offline import OfflineDescriptionEmbedder
from enterprise_pdf_rag.documents.models import AssetRef
from enterprise_pdf_rag.figures.models import (
    ChartIR,
    ChartPoint,
    Confidence,
    DescriptionClaim,
    Evidence,
    FieldOccurrence,
    FigureQualification,
    NumericObservation,
    SourceAnchor,
    SvgBinding,
    TextDescription,
    TextField,
    ValueKind,
    Verification,
)
from enterprise_pdf_rag.processing.context_builder import (
    BlockKind,
    ContextBlock,
    budget_blocks,
    build_context_block,
)
from enterprise_pdf_rag.processing.diagram_description import describe_diagram
from enterprise_pdf_rag.processing.diagram_models import (
    DiagramQualification,
    NodeEvidence,
    PathEvidence,
)
from enterprise_pdf_rag.processing.formula_models import (
    FormulaQualification,
    FormulaStructure,
    FormulaToken,
    StructureKind,
    TokenRole,
)
from enterprise_pdf_rag.processing.formula_models import PathEvidence as FormulaPathEvidence
from enterprise_pdf_rag.processing.models import ObjectKind
from enterprise_pdf_rag.processing.retrieval import RetrievalContext, RetrievalMember
from enterprise_pdf_rag.processing.table_models import (
    CellContentState,
    SlotState,
    TableCell,
    TableIR,
    TableSlot,
)
from enterprise_pdf_rag.processing.typed_ir import (
    DiagramEdge,
    DiagramIR,
    DiagramNode,
    FormulaIR,
    ImageIR,
    ListIR,
    LiteralQualification,
    ObjectDescription,
    ObservedText,
    TextIR,
)
from tests.enterprise_pdf_rag.adapters.generic_publication_helpers import (
    DOCUMENT_LABEL,
    publish_generic_document,
    resolve_table_member,
)

_SHA = "c" * 64
_SNAPSHOT = "s" * 64
_REF = AssetRef("a" * 64, "application/json", 1)
_ANCHOR = SourceAnchor(_SHA, _SHA, 2, (0.0, 0.0, 100.0, 50.0))


def _member(object_id: str, kind: ObjectKind) -> RetrievalMember:
    return RetrievalMember(object_id, kind, 2, _REF, _REF, _REF, _REF, _REF, "fp", 2)


def _span(span_id: str, text: str, top: float) -> ObservedText:
    return ObservedText(span_id, text, SourceAnchor(_SHA, _SHA, 2, (1.0, top, 90.0, top + 9.0)))


def _description(object_id: str, spans: tuple[ObservedText, ...]) -> ObjectDescription:
    return ObjectDescription(
        object_id,
        _ANCHOR,
        tuple(span.source_span_id for span in spans),
        "\n".join(span.text for span in spans),
        "exact-source-transcription-v1",
        Confidence(None, "deterministic source occurrence transcription; no semantic inference"),
        Verification.VERIFIED,
    )


def _literal(object_id: str, spans: tuple[ObservedText, ...]) -> LiteralQualification:
    return LiteralQualification(
        object_id, _ANCHOR, _SHA, tuple(s.source_span_id for s in spans), _REF, _REF, _REF
    )


def _text_context() -> RetrievalContext:
    spans = (_span("sp-1", "Revenue grew 12% in 2025.", 10.0), _span("sp-2", "Costs fell.", 20.0))
    return RetrievalContext(
        _SNAPSHOT,
        _member("text-1", ObjectKind.TEXT),
        TextIR("text-1", _ANCHOR, spans),
        _description("text-1", spans),
        _literal("text-1", spans),
    )


def _list_context() -> RetrievalContext:
    spans = (_span("li-1", "First item", 10.0), _span("li-2", "Second item", 20.0))
    return RetrievalContext(
        _SNAPSHOT,
        _member("list-1", ObjectKind.LIST),
        ListIR("list-1", _ANCHOR, spans, (("li-1",), ("li-2",)), True, ()),
        _description("list-1", spans),
        _literal("list-1", spans),
    )


def _evidence(*ids: str) -> Evidence:
    return Evidence(ids, Verification.VERIFIED, Confidence(None, "fixture"))


def _chart_context() -> RetrievalContext:
    binding = SvgBinding("fig", _SHA, "svg-v2:" + "e" * 64, "f" * 64)
    points = (
        ChartPoint(
            "p-1h21",
            TextField("Expense Ratio", _evidence("e-series")),
            TextField("1H21", _evidence("e-cat-1")),
            TextField("%", _evidence("e-unit-1")),
            NumericObservation(Decimal("15"), ValueKind.EXPLICIT, _evidence("e-val-1")),
        ),
        ChartPoint(
            "p-1h22",
            TextField("Expense Ratio", _evidence("e-series")),
            TextField("1H22", _evidence("e-cat-2")),
            TextField("%", _evidence("e-unit-2")),
            NumericObservation(None, ValueKind.UNAVAILABLE, _evidence("e-cat-2")),
        ),
    )
    chart = ChartIR(
        binding,
        "bar",
        (),
        points,
        "fixture",
        Verification.VERIFIED,
        title=TextField("Expense Ratio", _evidence("e-title")),
        period=TextField("1H21-1H22", _evidence("e-period")),
    )
    description = TextDescription(
        binding,
        (DescriptionClaim("Expense Ratio for 1H21: 15 %.", _evidence("e-val-1")),),
        "fixture",
        Verification.VERIFIED,
    )
    receipt = FigureQualification(
        binding,
        _ANCHOR,
        (FieldOccurrence("title", ("e-title",)),),
        "fixture",
        semantic_scope="displayed-percent-bar-lookup-v1",
    )
    return RetrievalContext(
        _SNAPSHOT, _member("chart-1", ObjectKind.CHART), chart, description, receipt
    )


def _table_context() -> RetrievalContext:
    cells = (
        TableCell(
            "c-1", 0, 0, 1, 1, (1.0, 1.0, 40.0, 20.0), ("t-1",), "Revenue", CellContentState.PRESENT
        ),
        TableCell("c-2", 0, 1, 1, 1, (41.0, 1.0, 90.0, 20.0), (), "", CellContentState.BLANK),
        TableCell(
            "c-3", 1, 0, 1, 2, (1.0, 21.0, 90.0, 40.0), (), None, CellContentState.UNAVAILABLE
        ),
    )
    slots = (
        (TableSlot(SlotState.ORIGIN, "c-1"), TableSlot(SlotState.ORIGIN, "c-2")),
        (TableSlot(SlotState.ORIGIN, "c-3"), TableSlot(SlotState.CONTINUATION, "c-3")),
    )
    spans = (_span("t-1", "Revenue", 1.0),)
    return RetrievalContext(
        _SNAPSHOT,
        _member("table-1", ObjectKind.TABLE),
        TableIR("table-1", _ANCHOR, 2, 2, cells, slots),
        _description("table-1", spans),
        _literal("table-1", spans),
    )


def test_text_block_cites_each_source_span_verbatim() -> None:
    block = build_context_block(_text_context())
    assert block.kind is BlockKind.TEXT
    member_id = _member("text-1", ObjectKind.TEXT).member_id
    assert (block.snapshot_id, block.member_id, block.page_index) == (_SNAPSHOT, member_id, 2)
    assert block.scope == "literal-source-transcription-v1"
    assert block.verification is Verification.VERIFIED
    assert [(s.source_span_id, s.text, s.page_index) for s in block.spans] == [
        ("sp-1", "Revenue grew 12% in 2025.", 2),
        ("sp-2", "Costs fell.", 2),
    ]
    assert block.spans[0].bbox == (1.0, 10.0, 90.0, 19.0)
    assert block.cells == () and block.chart_fields == () and block.grammar is None
    rendered = block.prompt_text()
    assert rendered.startswith(f"[member {member_id}] kind=text page_index=2")
    assert "fragments.sp-1: Revenue grew 12% in 2025." in rendered
    assert "fragments.sp-2: Costs fell." in rendered


def test_list_block_keeps_item_groups() -> None:
    block = build_context_block(_list_context())
    assert block.kind is BlockKind.LIST
    assert block.list_items == (("li-1",), ("li-2",))
    assert tuple(s.source_span_id for s in block.spans) == ("li-1", "li-2")
    assert "items.0: li-1" in block.prompt_text()


def test_chart_block_exposes_every_ir_field_with_its_element_ids() -> None:
    block = build_context_block(_chart_context())
    assert block.kind is BlockKind.CHART
    assert block.grammar == "bar"
    assert block.scope == "displayed-percent-bar-lookup-v1"
    fields = {field.field_path: field for field in block.chart_fields}
    assert set(fields) == {
        "title",
        "period",
        "points.p-1h21.series",
        "points.p-1h21.category",
        "points.p-1h21.unit",
        "points.p-1h21.value",
        "points.p-1h22.series",
        "points.p-1h22.category",
        "points.p-1h22.unit",
        "points.p-1h22.value",
    }
    value = fields["points.p-1h21.value"]
    assert (value.value, value.value_kind, value.element_ids) == (
        Decimal("15"),
        ValueKind.EXPLICIT,
        ("e-val-1",),
    )
    assert fields["points.p-1h22.value"].value is None
    assert fields["points.p-1h22.value"].value_kind is ValueKind.UNAVAILABLE
    assert fields["title"].text == "Expense Ratio" and fields["title"].value is None
    rendered = block.prompt_text()
    assert "chart grammar=bar" in rendered
    assert "points.p-1h21.value: series=Expense Ratio category=1H21 unit=% value=15" in rendered
    assert (
        "points.p-1h22.value: series=Expense Ratio category=1H22 unit=% value=<UNAVAILABLE>"
        in rendered
    )


def test_table_block_lists_cells_with_content_state() -> None:
    block = build_context_block(_table_context())
    assert block.kind is BlockKind.TABLE
    assert (block.row_count, block.col_count) == (2, 2)
    cells = {cell.cell_id: cell for cell in block.cells}
    assert cells["c-1"].text == "Revenue" and cells["c-1"].source_span_ids == ("t-1",)
    assert cells["c-2"].content_state is CellContentState.BLANK
    assert cells["c-3"].content_state is CellContentState.UNAVAILABLE
    assert cells["c-3"].col_span == 2
    rendered = block.prompt_text()
    assert "table rows=2 cols=2" in rendered
    assert "cells.c-1 (0,0): Revenue" in rendered
    assert "cells.c-2 (0,1): <BLANK>" in rendered
    assert "cells.c-3 (1,0): <UNAVAILABLE>" in rendered


def _diagram_ir() -> DiagramIR:
    nodes = (
        DiagramNode("n1", "PLAN", (20.0, 70.0, 90.0, 100.0), ("sp-plan",)),
        DiagramNode("n2", "BUILD", (150.0, 70.0, 220.0, 100.0), ("sp-build",)),
    )
    edges = (DiagramEdge("n1", "n2", None, "leads to", Verification.VERIFIED),)
    return DiagramIR("diagram-1", _ANCHOR, nodes, edges, (), Verification.VERIFIED)


def _diagram_context() -> RetrievalContext:
    ir = _diagram_ir()
    shape = PathEvidence(0, "shape", ((20.0, 70.0), (90.0, 100.0)), (20.0, 70.0, 90.0, 100.0))
    qualification = DiagramQualification(
        "diagram-1",
        _ANCHOR,
        _SHA,
        ("sp-plan", "sp-build"),
        (NodeEvidence("n1", ("sp-plan",), shape, (20.0, 70.0, 90.0, 100.0)),),
        (),
    )
    return RetrievalContext(
        _SNAPSHOT,
        _member("diagram-1", ObjectKind.DIAGRAM),
        ir,
        describe_diagram(ir),
        qualification,
    )


def test_diagram_block_prints_node_labels_and_edges() -> None:
    block = build_context_block(_diagram_context())
    assert block.kind is BlockKind.DIAGRAM
    assert block.scope == "diagram-structure-source-geometry-v1"
    assert block.verification is Verification.VERIFIED
    assert block.description_text == "Diagram with 2 nodes and 1 edge: PLAN; BUILD. PLAN -> BUILD."
    (edge,) = block.edges
    assert edge.value == "PLAN -> BUILD"
    # The citable box is the union of both endpoints.
    assert edge.bbox == (20.0, 70.0, 220.0, 100.0)
    assert block.prompt_text().split("\n")[1:] == [
        "diagram nodes=2 edges=1",
        "nodes.n1.label: PLAN",
        "nodes.n2.label: BUILD",
        "edges.0: PLAN -> BUILD",
    ]


def test_diagram_member_kind_mismatch_is_refused() -> None:
    context = _diagram_context()
    mismatched = RetrievalContext(
        context.snapshot_id,
        _member("diagram-1", ObjectKind.IMAGE),
        context.ir,
        context.description,
        context.qualification,
    )
    with pytest.raises(ValueError, match="kind"):
        build_context_block(mismatched)


_FORMULA_LINEAR = "ROE = \\frac{Net\\ profit}{Equity}"
_FORMULA_READABLE = "ROE 等于 Net profit 除以 Equity"


def _formula_ir(*, proven: bool = True) -> FormulaIR:
    if not proven:
        return FormulaIR("formula-1", _ANCHOR, "ROE =", "ROE = x", ("sp-roe",), ())
    tokens = (
        FormulaToken(0, "ROE", "sp-roe", 0, 3, (20.0, 72.4, 41.6, 84.4), TokenRole.OPERAND),
        FormulaToken(1, "=", "sp-roe", 4, 5, (48.8, 72.4, 56.0, 84.4), TokenRole.RELATION),
        FormulaToken(2, "Net", "sp-num", 0, 3, (62.0, 65.2, 81.8, 76.2), TokenRole.OPERAND),
        FormulaToken(3, "profit", "sp-num", 4, 10, (88.4, 65.2, 128.0, 76.2), TokenRole.OPERAND),
        FormulaToken(4, "Equity", "sp-den", 0, 6, (72.0, 85.2, 111.6, 96.2), TokenRole.OPERAND),
    )
    structure = FormulaStructure(
        StructureKind.FRACTION,
        FormulaPathEvidence(0, "line", ((60.0, 78.0), (120.0, 78.0)), 0.8),
        (2, 3),
        (4,),
    )
    return FormulaIR(
        "formula-1",
        _ANCHOR,
        "ROE =\nNet profit\nEquity",
        None,
        ("sp-roe", "sp-num", "sp-den"),
        ("proof_level=full",),
        Verification.VERIFIED,
        tokens,
        (structure,),
        _FORMULA_LINEAR,
        _FORMULA_READABLE,
        "full",
    )


def _formula_context(*, proven: bool = True) -> RetrievalContext:
    ir = _formula_ir(proven=proven)
    qualification = FormulaQualification(
        "formula-1",
        _ANCHOR,
        _SHA,
        ("sp-roe", "sp-num", "sp-den"),
        _REF,
        _REF,
        _REF,
        _REF,
        "full",
        len(ir.tokens) or 1,
        len(ir.structures),
        (),
        "unavailable",
    )
    description = ObjectDescription(
        "formula-1",
        _ANCHOR,
        ("sp-roe", "sp-num", "sp-den"),
        _FORMULA_READABLE,
        "exact-formula-transcription-v1",
        Confidence(None, "deterministic formula token transcription"),
        Verification.VERIFIED,
    )
    return RetrievalContext(
        _SNAPSHOT,
        _member("formula-1", ObjectKind.FORMULA),
        ir,
        description,
        qualification,
    )


def test_formula_member_renders_linear_readable_and_token_paths() -> None:
    block = build_context_block(_formula_context())
    assert block.kind is BlockKind.FORMULA
    assert block.scope == "formula-source-tokens-v1"
    assert block.verification is Verification.VERIFIED
    assert block.formula_proof_level == "full"
    assert tuple(token.text for token in block.formula_tokens) == (
        "ROE",
        "=",
        "Net",
        "profit",
        "Equity",
    )
    assert block.formula_tokens[1].source_span_id == "sp-roe"
    assert block.prompt_text().split("\n")[1:] == [
        "formula proof_level=full",
        f"formula.linear: {_FORMULA_LINEAR}",
        f"formula.readable: {_FORMULA_READABLE}",
        "tokens.0: ROE  (role=operand, script=base, proof=none)",
        "tokens.1: =  (role=relation, script=base, proof=none)",
        "tokens.2: Net  (role=operand, script=base, proof=none)",
        "tokens.3: profit  (role=operand, script=base, proof=none)",
        "tokens.4: Equity  (role=operand, script=base, proof=none)",
    ]


def test_model_only_formula_ir_is_refused() -> None:
    with pytest.raises(ValueError, match="need their proven token IR"):
        build_context_block(_formula_context(proven=False))
    proven = _formula_context()
    mismatched = RetrievalContext(
        proven.snapshot_id,
        _member("formula-1", ObjectKind.IMAGE),
        proven.ir,
        proven.description,
        proven.qualification,
    )
    with pytest.raises(ValueError, match="kind"):
        build_context_block(mismatched)


def test_kind_mismatch_and_unsupported_ir_are_refused() -> None:
    text = _text_context()
    mismatched = RetrievalContext(
        text.snapshot_id,
        _member("text-1", ObjectKind.LIST),
        text.ir,
        text.description,
        text.qualification,
    )
    with pytest.raises(ValueError, match="kind"):
        build_context_block(mismatched)
    image = RetrievalContext(
        text.snapshot_id,
        _member("image-1", ObjectKind.IMAGE),
        ImageIR("image-1", _ANCHOR, (), (), ()),
        text.description,
        text.qualification,
    )
    with pytest.raises(ValueError, match="not supported"):
        build_context_block(image)


def test_budget_drops_whole_blocks_and_keeps_order() -> None:
    small = build_context_block(_list_context())
    large = build_context_block(_chart_context())
    text = build_context_block(_text_context())
    sizes = [len(block.prompt_text()) for block in (small, large, text)]
    assert sizes[1] > sizes[2]
    kept = budget_blocks((small, large, text), max_chars=sizes[0] + sizes[2])
    assert kept == (small, text)
    assert budget_blocks((small, large, text), max_chars=sum(sizes)) == (small, large, text)
    assert budget_blocks((), max_chars=10) == ()
    with pytest.raises(ValueError, match="budget"):
        budget_blocks((small,), max_chars=0)


def test_blocks_are_immutable_values() -> None:
    block = build_context_block(_text_context())
    assert isinstance(block, ContextBlock)
    with pytest.raises(AttributeError):
        block.member_id = "other"  # type: ignore[misc]


def test_published_native_table_member_yields_citable_cell_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    published = publish_generic_document(
        tmp_path,
        monkeypatch,
        filename="meridian-semiannual.pdf",
        label=DOCUMENT_LABEL,
        page_count=3,
        embedder=OfflineDescriptionEmbedder(),
        table_page=True,
    )
    context = resolve_table_member(published)
    assert isinstance(context.ir, TableIR)
    block = build_context_block(context)
    assert block.kind is BlockKind.TABLE and block.page_index == 2
    assert block.snapshot_id == published.retrieval_snapshot_id
    assert block.scope == "literal-source-transcription-v1"
    assert block.verification is Verification.VERIFIED
    assert (block.row_count, block.col_count) == (3, 2)
    assert block.description_text == "Metric\nValue\nRevenue\n1,234\nMargin"
    by_position = {(cell.row, cell.col): cell for cell in block.cells}
    assert set(by_position) == {(r, c) for r in range(3) for c in range(2)}
    assert {cell.cell_id for cell in block.cells} == {cell.cell_id for cell in context.ir.cells}
    value = by_position[(1, 1)]
    assert value.text == "1,234" and value.content_state is CellContentState.PRESENT
    assert len(value.source_span_ids) == 1 and value.bbox == (120.0, 86.0, 220.0, 113.0)
    blank = by_position[(2, 1)]
    assert blank.content_state is CellContentState.BLANK and blank.source_span_ids == ()
    rendered = block.prompt_text()
    assert f"cells.{value.cell_id} (1,1): 1,234" in rendered
    assert f"cells.{blank.cell_id} (2,1): <BLANK>" in rendered
    assert "verification=verified" in rendered.splitlines()[0]
