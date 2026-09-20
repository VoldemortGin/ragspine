"""Prompt context blocks built only from stored, re-verified evidence.

A block renders a member's typed IR field by field so a model can cite exact
span, cell or chart-field paths. Nothing here infers, summarises or truncates:
blocks are dropped whole when a budget is exceeded.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from enterprise_pdf_rag.documents.models import Bounds
from enterprise_pdf_rag.figures.models import ChartIR, TextField, ValueKind, Verification
from enterprise_pdf_rag.processing.diagram_description import EDGE_ARROW
from enterprise_pdf_rag.processing.models import ObjectKind
from enterprise_pdf_rag.processing.retrieval import RetrievalContext
from enterprise_pdf_rag.processing.table_models import CellContentState, TableIR
from enterprise_pdf_rag.processing.typed_ir import (
    DiagramIR,
    FormulaIR,
    GroupIR,
    ListIR,
    ObservedText,
    TextIR,
)


class BlockKind(StrEnum):
    TEXT = "text"
    LIST = "list"
    GROUP = "group"
    TABLE = "table"
    CHART = "chart"
    DIAGRAM = "diagram"
    FORMULA = "formula"


_KIND_OF_OBJECT = {
    ObjectKind.TEXT: BlockKind.TEXT,
    ObjectKind.LIST: BlockKind.LIST,
    ObjectKind.GROUP: BlockKind.GROUP,
    ObjectKind.TABLE: BlockKind.TABLE,
    ObjectKind.CHART: BlockKind.CHART,
    ObjectKind.DIAGRAM: BlockKind.DIAGRAM,
    ObjectKind.FORMULA: BlockKind.FORMULA,
}


@dataclass(frozen=True, slots=True)
class SpanEvidence:
    source_span_id: str
    page_index: int
    bbox: Bounds
    text: str


@dataclass(frozen=True, slots=True)
class CellEvidence:
    cell_id: str
    row: int
    col: int
    row_span: int
    col_span: int
    bbox: Bounds
    text: str | None
    content_state: CellContentState
    source_span_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ChartFieldEvidence:
    """One exact chart IR field: ``title``, ``period`` or ``points.<id>.<role>``."""

    field_path: str
    text: str
    value: Decimal | None
    value_kind: ValueKind | None
    element_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DiagramNodeEvidence:
    """One proven diagram node: ``nodes.<node_id>.label`` is its citable path."""

    node_id: str
    label: str
    bbox: Bounds
    source_span_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DiagramEdgeEvidence:
    """One proven drawn edge: ``edges.<index>`` is its citable path, printed as a pair."""

    edge_index: int
    source_node_id: str
    target_node_id: str
    source_label: str
    target_label: str
    bbox: Bounds

    @property
    def value(self) -> str:
        return f"{self.source_label}{EDGE_ARROW}{self.target_label}"


@dataclass(frozen=True, slots=True)
class FormulaTokenEvidence:
    """One proven formula token: ``tokens.<index>`` is its citable path."""

    index: int
    text: str
    role: str
    script: str
    proof: str | None
    source_span_id: str
    bbox: Bounds


@dataclass(frozen=True, slots=True)
class ContextBlock:
    snapshot_id: str
    member_id: str
    kind: BlockKind
    page_index: int
    scope: str
    verification: Verification
    description_text: str
    spans: tuple[SpanEvidence, ...] = ()
    list_items: tuple[tuple[str, ...], ...] = ()
    cells: tuple[CellEvidence, ...] = ()
    row_count: int = 0
    col_count: int = 0
    grammar: str | None = None
    chart_fields: tuple[ChartFieldEvidence, ...] = ()
    nodes: tuple[DiagramNodeEvidence, ...] = ()
    edges: tuple[DiagramEdgeEvidence, ...] = ()
    formula_linear: str | None = None
    formula_readable: str | None = None
    formula_proof_level: str | None = None
    formula_tokens: tuple[FormulaTokenEvidence, ...] = ()

    def prompt_text(self) -> str:
        """Deterministic rendering; every citable path appears verbatim as a line prefix."""
        lines = [
            f"[member {self.member_id}] kind={self.kind.value} page_index={self.page_index} "
            f"scope={self.scope} verification={self.verification.value}"
        ]
        if self.kind is BlockKind.CHART:
            lines.append(f"chart grammar={self.grammar}")
            fields = {field.field_path: field for field in self.chart_fields}
            for name in ("title", "period"):
                field = fields.get(name)
                lines.append(f"{name}: {field.text if field is not None else '<NONE>'}")
            for field in self.chart_fields:
                if not field.field_path.endswith(".value"):
                    continue
                prefix = field.field_path.removesuffix(".value")
                value = "<UNAVAILABLE>" if field.value is None else str(field.value)
                lines.append(
                    f"{field.field_path}: series={fields[prefix + '.series'].text} "
                    f"category={fields[prefix + '.category'].text} "
                    f"unit={fields[prefix + '.unit'].text} value={value}"
                )
        elif self.kind is BlockKind.FORMULA:
            lines.append(f"formula proof_level={self.formula_proof_level}")
            lines.append(f"formula.linear: {self.formula_linear}")
            lines.append(f"formula.readable: {self.formula_readable}")
            lines.extend(
                f"tokens.{token.index}: {token.text}  "
                f"(role={token.role}, script={token.script}, proof={token.proof or 'none'})"
                for token in self.formula_tokens
            )
        elif self.kind is BlockKind.TABLE:
            lines.append(f"table rows={self.row_count} cols={self.col_count}")
            for cell in self.cells:
                if cell.content_state is CellContentState.PRESENT:
                    shown = cell.text if cell.text is not None else "<UNAVAILABLE>"
                else:
                    shown = f"<{cell.content_state.name}>"
                lines.append(f"cells.{cell.cell_id} ({cell.row},{cell.col}): {shown}")
        elif self.kind is BlockKind.DIAGRAM:
            lines.append(f"diagram nodes={len(self.nodes)} edges={len(self.edges)}")
            lines.extend(f"nodes.{node.node_id}.label: {node.label}" for node in self.nodes)
            lines.extend(f"edges.{edge.edge_index}: {edge.value}" for edge in self.edges)
        else:
            lines.extend(f"fragments.{span.source_span_id}: {span.text}" for span in self.spans)
            lines.extend(
                f"items.{index}: {', '.join(item)}" for index, item in enumerate(self.list_items)
            )
        return "\n".join(lines)


def _spans(fragments: tuple[ObservedText, ...]) -> tuple[SpanEvidence, ...]:
    return tuple(
        SpanEvidence(
            fragment.source_span_id,
            fragment.source.page_index,
            fragment.source.bbox,
            fragment.text,
        )
        for fragment in fragments
    )


def _text_field(path: str, field: TextField | None) -> tuple[ChartFieldEvidence, ...]:
    if field is None:
        return ()
    return (ChartFieldEvidence(path, field.text, None, None, field.evidence.element_ids),)


def _chart_fields(chart: ChartIR) -> tuple[ChartFieldEvidence, ...]:
    fields = [*_text_field("title", chart.title), *_text_field("period", chart.period)]
    for point in chart.points:
        prefix = f"points.{point.point_id}"
        fields.extend(_text_field(f"{prefix}.series", point.series))
        fields.extend(_text_field(f"{prefix}.category", point.category))
        fields.extend(_text_field(f"{prefix}.unit", point.unit))
        value = point.value
        fields.append(
            ChartFieldEvidence(
                f"{prefix}.value",
                "" if value.value is None else str(value.value),
                value.value,
                value.kind,
                value.evidence.element_ids,
            )
        )
    return tuple(fields)


def _union(first: Bounds, second: Bounds) -> Bounds:
    return (
        min(first[0], second[0]),
        min(first[1], second[1]),
        max(first[2], second[2]),
        max(first[3], second[3]),
    )


def build_context_block(context: RetrievalContext) -> ContextBlock:
    """Project one hydrated member into a block; unsupported or mismatched kinds are refused."""
    ir = context.ir
    member = context.member
    common = (context.snapshot_id, member.member_id)
    if isinstance(ir, TextIR | ListIR | GroupIR):
        kind = {TextIR: BlockKind.TEXT, ListIR: BlockKind.LIST, GroupIR: BlockKind.GROUP}[type(ir)]
        if _KIND_OF_OBJECT.get(member.kind) is not kind:
            raise ValueError("Retrieval member kind does not match its typed IR")
        return ContextBlock(
            *common,
            kind,
            member.page_index,
            context.scope,
            context.description.verification,
            context.description.text,
            spans=_spans(ir.fragments),
            list_items=ir.item_groups if isinstance(ir, ListIR) else (),
        )
    if isinstance(ir, TableIR):
        if member.kind is not ObjectKind.TABLE:
            raise ValueError("Retrieval member kind does not match its typed IR")
        # The inferred grid is pinned PENDING; what a table member verifies is its
        # literal transcription, exactly like text members.
        return ContextBlock(
            *common,
            BlockKind.TABLE,
            member.page_index,
            context.scope,
            context.description.verification,
            context.description.text,
            cells=tuple(
                CellEvidence(
                    cell.cell_id,
                    cell.row,
                    cell.col,
                    cell.row_span,
                    cell.col_span,
                    cell.bbox,
                    cell.text,
                    cell.content_state,
                    cell.source_span_ids,
                )
                for cell in ir.cells
            ),
            row_count=ir.row_count,
            col_count=ir.col_count,
        )
    if isinstance(ir, ChartIR):
        if member.kind is not ObjectKind.CHART:
            raise ValueError("Retrieval member kind does not match its typed IR")
        return ContextBlock(
            *common,
            BlockKind.CHART,
            member.page_index,
            context.scope,
            ir.verification,
            context.description.text,
            grammar=ir.grammar,
            chart_fields=_chart_fields(ir),
        )
    if isinstance(ir, DiagramIR):
        if member.kind is not ObjectKind.DIAGRAM:
            raise ValueError("Retrieval member kind does not match its typed IR")
        by_id = {node.node_id: node for node in ir.nodes}
        return ContextBlock(
            *common,
            BlockKind.DIAGRAM,
            member.page_index,
            context.scope,
            ir.verification,
            context.description.text,
            nodes=tuple(
                DiagramNodeEvidence(node.node_id, node.label, node.bbox, node.source_span_ids)
                for node in ir.nodes
            ),
            edges=tuple(
                DiagramEdgeEvidence(
                    index,
                    edge.source_node_id,
                    edge.target_node_id,
                    by_id[edge.source_node_id].label,
                    by_id[edge.target_node_id].label,
                    _union(
                        by_id[edge.source_node_id].bbox,
                        by_id[edge.target_node_id].bbox,
                    ),
                )
                for index, edge in enumerate(ir.edges)
            ),
        )
    if isinstance(ir, FormulaIR):
        if member.kind is not ObjectKind.FORMULA:
            raise ValueError("Retrieval member kind does not match its typed IR")
        if not ir.tokens:
            raise ValueError("Formula members need their proven token IR")
        return ContextBlock(
            *common,
            BlockKind.FORMULA,
            member.page_index,
            context.scope,
            ir.verification,
            context.description.text,
            formula_linear=ir.linear,
            formula_readable=ir.readable,
            formula_proof_level=ir.proof_level,
            formula_tokens=tuple(
                FormulaTokenEvidence(
                    token.index,
                    token.text,
                    token.role.value,
                    token.script.value,
                    token.script_proof,
                    token.source_span_id,
                    token.bbox,
                )
                for token in ir.tokens
            ),
        )
    raise ValueError(f"{type(ir).__name__} members are not supported as answer context")


def budget_blocks(blocks: Sequence[ContextBlock], *, max_chars: int) -> tuple[ContextBlock, ...]:
    """Keep blocks in fused order while their rendered size fits; never truncate a block."""
    if max_chars < 1:
        raise ValueError("Context budget must allow at least one character")
    kept: list[ContextBlock] = []
    used = 0
    for block in blocks:
        size = len(block.prompt_text())
        if used + size > max_chars:
            continue
        kept.append(block)
        used += size
    return tuple(kept)
