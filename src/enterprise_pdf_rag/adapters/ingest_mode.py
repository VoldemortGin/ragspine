"""Ingest modes (ADR 0025): which model calls an ingest sends, and what stands in for the rest.

``full`` is the pipeline as it has always run. ``lite`` keeps every call a fact is read from —
the page layout, a chart's IR and both diagram branches — and replaces the calls nothing
retrievable reads: an image's two branches (images are never retrievable), a formula's two
branches (its proof reads the PDF, never the model), a chart's description (derived from its
IR's printed labels instead), the page-metadata call (derived from the page's own text
geometry instead) and the document tree. It also writes no review pages.

Every switch is one field of ``IngestPlan`` so each can be tested and moved on its own; the
mode name only picks a preset. ``layout`` is the selection point for the page partitioner
(``make_partitioner``): both presets use the model layout; ``"deterministic-text-pages"``
(ADR 0028) partitions pages without figures or images from pdfspine blocks and is chosen
explicitly until it is validated on long reports; ``"onnx-layout"`` (ADR 0030) additionally
partitions the remaining pages with pdfspine's local PP-DocLayoutV3 model (deterministic text
pages -> onnx -> per-page model fallback), also chosen explicitly only.
``unverified_tables_as_rows`` (ADR 0027) indexes a Table with no detected grid as its verbatim
printed rows; lite turns it on. ``table_row_index_units`` / ``drop_running_lines_from_index``
lay out the index text (row units for a long row table, nothing for a running header /
footer); lite turns both on. ``lexical_only_kinds`` keeps the members of those kinds out of
the vector channel (BM25 alone scores them); no preset sets it — ``run_folder_pipeline``
reads ``APP_INDEX_LEXICAL_ONLY_KINDS`` (empty by default).
``unverified_table_structure="tsr"`` (ADR 0031) first asks a local table-structure model for
that table's grid, kept pending, and falls back to the rows; both presets keep ``"rows"``.
"""

from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from typing import Final, Literal, get_args

from enterprise_pdf_rag.adapters.deterministic_partition import make_text_page_partitioner
from enterprise_pdf_rag.adapters.document_store import LocalDocumentStore
from enterprise_pdf_rag.adapters.onnx_partition import (
    make_onnx_page_partitioner,
    onnx_layout_unavailable,
)
from enterprise_pdf_rag.adapters.page_metadata_extraction import PAGE_METADATA_DETERMINISTIC
from enterprise_pdf_rag.adapters.page_partition import ModelPagePartitioner
from enterprise_pdf_rag.adapters.pdfspine_tsr import table_structure_unavailable
from enterprise_pdf_rag.processing.index_text import IndexTextOptions
from ragspine.common.evidence.providers.json_completion import JsonCompletionClient
from ragspine.extraction.evidence.document.models import DocumentSnapshot
from ragspine.extraction.evidence.page.models import ObjectKind, ProcessingManifest
from ragspine.extraction.evidence.page.ports import PagePartitioner

type IngestMode = Literal["full", "lite"]
# Which partitioner proposes a page's objects: the model on every page; pdfspine blocks on
# pages without figures or images with a per-page model fallback (ADR 0028); or those two plus
# pdfspine's local ONNX layout model (PP-DocLayoutV3) on the remaining pages (ADR 0030).
type LayoutPolicy = Literal["model", "deterministic-text-pages", "onnx-layout"]
# How a Table with no detected grid is structured when it is indexed: ADR 0027's verbatim
# printed rows, or a grid a local SLANet-plus model infers (kept pending, rows on fallback).
type UnverifiedTableStructure = Literal["rows", "tsr"]
INGEST_MODES: Final[tuple[str, ...]] = get_args(IngestMode.__value__)
LAYOUT_POLICIES: Final[tuple[str, ...]] = get_args(LayoutPolicy.__value__)
UNVERIFIED_TABLE_STRUCTURES: Final[tuple[str, ...]] = get_args(UnverifiedTableStructure.__value__)
# The notebook's ``LAYOUT_POLICY = "auto"``: in lite, the local ONNX layout when its weights and
# runtime are present, else the deterministic text pages (ADR 0030); never a library preset.
AUTO_LAYOUT: Final = "auto"
# The notebook's ``UNVERIFIED_TABLE_STRUCTURE = "auto"``: in lite, "tsr" when the SLANet-plus
# weights and runtime are present, else "rows" (ADR 0031); never a library preset.
AUTO_TABLE_STRUCTURE: Final = "auto"
# The call categories a mode may leave unsent, as ``IngestionSummary.skipped_calls`` keys.
SKIPPED_CALL_KINDS: Final = ("image", "formula", "chart_description", "page_metadata")
# What a stage a mode chose not to run says, so it reads apart from a failure or a budget.
SKIPPED_CODE: Final = "skipped_by_ingest_mode"


@dataclass(frozen=True, slots=True)
class IngestPlan:
    """Every switch an ingest mode sets; the defaults are exactly ``full``."""

    mode: IngestMode
    layout: LayoutPolicy = "model"
    # Send an Image object's two model branches (images are never retrievable either way).
    image_semantics: bool = True
    # Send a Formula object's two model branches (lineage only; the proof reads the PDF).
    formula_semantics: bool = True
    # A chart's description: its own model call, or derived from its IR's printed labels.
    chart_description: Literal["model", "from-ir"] = "model"
    # Page metadata: one text-only model call per page, or derived from the page's geometry.
    page_metadata: Literal["model", "deterministic"] = "model"
    # Write the per-run review pages (source review and processing review).
    review_exports: bool = True
    # Build the document tree after publication when the caller does not say.
    build_tree: bool = True
    # Index a Table with no detected grid as its verbatim printed rows (ADR 0027); its rows
    # carry their own producer, so every other stage keeps its bytes either way.
    unverified_tables_as_rows: bool = False
    # ADR 0031: ``"tsr"`` gives such a table a model-inferred grid (PENDING, cell citations only)
    # and falls back to the rows when the model's grid fails its self-check; it indexes the
    # table even when ``unverified_tables_as_rows`` is off. ``"rows"`` changes nothing.
    unverified_table_structure: UnverifiedTableStructure = "rows"
    # Index a long verbatim-rows table as one scoring unit per figure row, its header rows
    # repeated, instead of one unit for the whole table (ADR 0027 Amendment 1).
    table_row_index_units: bool = False
    # Keep a Text member that prints only running header / footer lines out of both
    # retrieval channels; it stays citable and in its page window (ADR 0028 Amendment 1).
    drop_running_lines_from_index: bool = False
    # Keep the members of these kinds out of the vector channel: BM25 alone scores them, a
    # chart its PDF text layer. Empty in both presets (``APP_INDEX_LEXICAL_ONLY_KINDS``).
    lexical_only_kinds: frozenset[ObjectKind] = field(default_factory=frozenset)

    @property
    def index_options(self) -> IndexTextOptions:
        """The index-text layout the index stage builds; all off keeps full's bytes."""
        return IndexTextOptions(
            table_row_units=self.table_row_index_units,
            drop_running_lines=self.drop_running_lines_from_index,
            lexical_only_kinds=self.lexical_only_kinds,
        )

    @property
    def object_variant(self) -> str | None:
        """The suffix lite adds to the object producer; ``None`` keeps full's bytes."""
        default = IngestPlan(self.mode)
        changed = (
            self.image_semantics,
            self.formula_semantics,
            self.chart_description,
        ) != (
            default.image_semantics,
            default.formula_semantics,
            default.chart_description,
        )
        return "lite-v1" if changed else None


_PLANS: Final[dict[str, IngestPlan]] = {
    "full": IngestPlan("full"),
    "lite": IngestPlan(
        "lite",
        image_semantics=False,
        formula_semantics=False,
        chart_description="from-ir",
        page_metadata="deterministic",
        review_exports=False,
        build_tree=False,
        unverified_tables_as_rows=True,
        table_row_index_units=True,
        drop_running_lines_from_index=True,
    ),
}


def check_ingest_mode(value: str) -> IngestMode:
    """``value`` as an ``IngestMode``, or a ``ValueError`` naming the accepted modes."""
    if value not in _PLANS:
        raise ValueError(f"ingest_mode must be one of {list(INGEST_MODES)}, not {value!r}")
    return _PLANS[value].mode


def lexical_only_kinds(value: str | Iterable[str]) -> frozenset[ObjectKind]:
    """Kind names (comma separated, any case; ``APP_INDEX_LEXICAL_ONLY_KINDS``) as kinds.

    Blank is the empty set. An unknown name, or ``Text`` (a Text member without a vector is a
    running line), is a ``ValueError`` naming the setting and the accepted kinds.
    """
    names = value.split(",") if isinstance(value, str) else list(value)
    accepted = {kind.value.lower(): kind for kind in ObjectKind if kind is not ObjectKind.TEXT}
    kinds: set[ObjectKind] = set()
    for name in (name.strip() for name in names):
        if not name:
            continue
        if name.lower() not in accepted:
            raise ValueError(
                f"APP_INDEX_LEXICAL_ONLY_KINDS: {name!r} is not one of "
                f"{sorted(kind.value for kind in accepted.values())}"
            )
        kinds.add(accepted[name.lower()])
    return frozenset(kinds)


def check_layout_policy(value: str) -> LayoutPolicy:
    """``value`` as a ``LayoutPolicy``, or a ``ValueError`` naming the accepted policies."""
    if value == "model":
        return "model"
    if value == "deterministic-text-pages":
        return "deterministic-text-pages"
    if value == "onnx-layout":
        return "onnx-layout"
    raise ValueError(f"layout_policy must be one of {list(LAYOUT_POLICIES)}, not {value!r}")


def choose_layout_policy(
    requested: str, *, ingest_mode: str, onnx_layout_model: str | None
) -> tuple[LayoutPolicy, str]:
    """The layout a run uses for ``requested`` (a policy or ``"auto"``), and a one-line reason.

    Full always keeps the model layout (its bytes never change). In lite an explicit policy is
    used as written; ``"auto"`` picks ``"onnx-layout"`` when ``onnx_layout_unavailable`` finds
    the weights and the runtime, else ``"deterministic-text-pages"`` naming what is missing.
    Only files and importability are checked; no weights are read and no model is loaded.
    """
    if requested != AUTO_LAYOUT and requested not in LAYOUT_POLICIES:
        raise ValueError(
            f"layout_policy must be {AUTO_LAYOUT!r} or one of {list(LAYOUT_POLICIES)}, "
            f"not {requested!r}"
        )
    if check_ingest_mode(ingest_mode) != "lite":
        return "model", (
            f'INGEST_MODE = "full" 时忽略 LAYOUT_POLICY = {requested!r}, '
            "版面每页都用模型(full 保持原样)"
        )
    if requested != AUTO_LAYOUT:
        policy = check_layout_policy(requested)
        problem = onnx_layout_unavailable(onnx_layout_model) if policy == "onnx-layout" else None
        return policy, "显式指定" + ("" if problem is None else f"; 注意, 入库前会报错: {problem}")
    problem = onnx_layout_unavailable(onnx_layout_model)
    if problem is None:
        return "onnx-layout", "auto: 已配置本地 ONNX 版面权重且 onnxruntime 可用"
    return "deterministic-text-pages", (
        "auto: 本地 ONNX 版面不可用, 改用确定性文本页(含图页回退模型版面)。"
        f"{problem} 启用方法: .env 设 APP_ONNX_LAYOUT_MODEL 指向 pp_doc_layoutv3.onnx, "
        "并 pip install 'pdfspine[onnx]'"
    )


def check_unverified_table_structure(value: str) -> UnverifiedTableStructure:
    """``value`` as an ``UnverifiedTableStructure``, or a ``ValueError`` naming the choices."""
    if value == "rows":
        return "rows"
    if value == "tsr":
        return "tsr"
    raise ValueError(
        f"unverified_table_structure must be one of {list(UNVERIFIED_TABLE_STRUCTURES)}, "
        f"not {value!r}"
    )


def choose_unverified_table_structure(
    requested: str, *, ingest_mode: str
) -> tuple[UnverifiedTableStructure, str]:
    """The table structure a run uses for ``requested`` (a choice or ``"auto"``), and a reason.

    Full always keeps ``"rows"`` (its bytes never change). In lite an explicit choice is used as
    written; ``"auto"`` picks ``"tsr"`` when ``table_structure_unavailable`` finds the weights
    and the runtime, else ``"rows"`` naming what is missing. Nothing is hashed or loaded.
    """
    if requested != AUTO_TABLE_STRUCTURE and requested not in UNVERIFIED_TABLE_STRUCTURES:
        raise ValueError(
            f"unverified_table_structure must be {AUTO_TABLE_STRUCTURE!r} or one of "
            f"{list(UNVERIFIED_TABLE_STRUCTURES)}, not {requested!r}"
        )
    if check_ingest_mode(ingest_mode) != "lite":
        return "rows", (
            f'INGEST_MODE = "full" 时忽略 UNVERIFIED_TABLE_STRUCTURE = {requested!r}, '
            "无网格表格保持原样(full 保持原样)"
        )
    if requested != AUTO_TABLE_STRUCTURE:
        structure = check_unverified_table_structure(requested)
        problem = table_structure_unavailable() if structure == "tsr" else None
        return structure, "显式指定" + (
            "" if problem is None else f"; 注意, 入库前会报错: {problem}"
        )
    problem = table_structure_unavailable()
    if problem is None:
        return "tsr", "auto: 已配置本地 SLANet-plus 表格结构权重且 onnxruntime 可用"
    return "rows", (
        f"auto: 表格结构模型不可用, 无网格表格按印刷行逐字收录。{problem} 启用方法: "
        "把 slanet-plus.onnx 放到 APP_ONNX_LAYOUT_MODEL 所在目录(与版面权重同一目录), "
        "并 pip install 'pdfspine[onnx]'"
    )


def ingest_plan(
    mode: IngestMode,
    *,
    layout_policy: LayoutPolicy | None = None,
    unverified_tables_as_rows: bool | None = None,
    table_row_index_units: bool | None = None,
    drop_running_lines_from_index: bool | None = None,
    unverified_table_structure: UnverifiedTableStructure | None = None,
    lexical_only: str | Iterable[str] | None = None,
) -> IngestPlan:
    """The switches one mode sets; a non-``None`` override replaces that one switch."""
    plan = _PLANS[check_ingest_mode(mode)]
    if lexical_only is not None:
        plan = replace(plan, lexical_only_kinds=lexical_only_kinds(lexical_only))
    if table_row_index_units is not None:
        plan = replace(plan, table_row_index_units=table_row_index_units)
    if drop_running_lines_from_index is not None:
        plan = replace(plan, drop_running_lines_from_index=drop_running_lines_from_index)
    if layout_policy is not None:
        plan = replace(plan, layout=check_layout_policy(layout_policy))
    if unverified_tables_as_rows is not None:
        plan = replace(plan, unverified_tables_as_rows=unverified_tables_as_rows)
    if unverified_table_structure is not None:
        plan = replace(
            plan,
            unverified_table_structure=check_unverified_table_structure(unverified_table_structure),
        )
    return plan


def make_partitioner(
    plan: IngestPlan,
    client: JsonCompletionClient,
    sources: LocalDocumentStore,
    snapshot: DocumentSnapshot,
) -> PagePartitioner:
    """The page partitioner a plan selects: the one layout seam."""
    model = ModelPagePartitioner(client, sources)
    if plan.layout == "model":
        return model
    if plan.layout == "deterministic-text-pages":
        return make_text_page_partitioner(model, sources, snapshot)
    if plan.layout == "onnx-layout":
        # 组合顺序: 纯文字页确定性(零调用、零推理) -> 其余页本地 ONNX 版面 -> 回退页模型版面.
        onnx = make_onnx_page_partitioner(model, sources, snapshot)
        return make_text_page_partitioner(onnx, sources, snapshot)
    raise ValueError(f"unknown layout policy {plan.layout!r}")


def published_ingest_mode(manifest: ProcessingManifest) -> IngestMode | None:
    """The mode a saved snapshot was ingested in, read from its page-metadata producers.

    Lite derives page metadata deterministically and full asks the model, so the producer
    of any page's metadata stage names the mode; ``None`` for a snapshot without one.
    """
    producers = {page.metadata.producer for page in manifest.pages if page.metadata is not None}
    if not producers:
        return None
    return "lite" if PAGE_METADATA_DETERMINISTIC in producers else "full"
