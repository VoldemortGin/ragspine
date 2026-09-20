"""In-memory ``MountedDocument`` whose members resolve to real context blocks.

Each member is a text (one span quoting its index text) or a chart (a ``ChartIR``);
the vector channel returns the configured order, so tests can place a chart at any
fused position without a store or an embedder. ``chart_context`` is not part of
ranking or seat selection and stays unavailable.
"""

from dataclasses import dataclass
from decimal import Decimal

from enterprise_pdf_rag.answers.ports import MemberText, MountedDocument
from enterprise_pdf_rag.documents.models import AssetRef
from enterprise_pdf_rag.figures.chart_qa.displayed_models import DisplayedLookupContext
from enterprise_pdf_rag.figures.chart_qa.models import ChartContext
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
from enterprise_pdf_rag.processing.diagram_description import describe_diagram
from enterprise_pdf_rag.processing.diagram_models import (
    DiagramQualification,
    NodeEvidence,
    PathEvidence,
)
from enterprise_pdf_rag.processing.index_text import member_index_text
from enterprise_pdf_rag.processing.models import ObjectKind, ProcessingManifest
from enterprise_pdf_rag.processing.retrieval import (
    PinnedRetrievalHit,
    RetrievalContext,
    RetrievalMember,
)
from enterprise_pdf_rag.processing.typed_ir import (
    DiagramEdge,
    DiagramIR,
    DiagramNode,
    LiteralQualification,
    ObjectDescription,
    ObservedText,
    TextIR,
)

SNAPSHOT = "1" * 64
DONUT_TITLE = "Distribution Mix"
_SHA = "c" * 64
_REF = AssetRef("a" * 64, "application/json", 1)
_ANCHOR = SourceAnchor(_SHA, _SHA, 0, (0.0, 0.0, 100.0, 50.0))
_BINDING = SvgBinding("fig", _SHA, "svg-v2:" + "e" * 64, "f" * 64)


def _evidence(*ids: str) -> Evidence:
    return Evidence(ids, Verification.VERIFIED, Confidence(None, "fake"))


def donut_chart(*shares: tuple[str, str]) -> ChartIR:
    """A verified 1H26 ``Distribution Mix`` donut; ``shares`` are (category, percent)."""
    points = tuple(
        ChartPoint(
            f"point-{category.lower()}",
            TextField("VONB", _evidence("e-series")),
            TextField(category, _evidence(f"e-cat-{category}")),
            TextField("%", _evidence(f"e-unit-{category}")),
            NumericObservation(Decimal(value), ValueKind.EXPLICIT, _evidence(f"e-val-{category}")),
        )
        for category, value in shares
    )
    return ChartIR(
        _BINDING,
        "donut",
        (),
        points,
        "fake",
        Verification.VERIFIED,
        title=TextField(DONUT_TITLE, _evidence("e-title")),
        period=TextField("1H26", _evidence("e-period")),
    )


def pending_chart() -> ChartIR:
    """A pending bar with no points, like the untitled AIA bars."""
    return ChartIR(_BINDING, "bar", (), (), "fake", Verification.PENDING)


DIAGRAM_MEMBER = "diagram-1"
DIAGRAM_ANCHOR = SourceAnchor(_SHA, _SHA, 2, (15.0, 65.0, 225.0, 105.0))
DIAGRAM_LABELS = ("Foundation: 100% Digitalised Agency", "Growth")


def diagram_ir() -> DiagramIR:
    """A proven two-node, one-edge diagram; the first label carries a percentage."""
    nodes = (
        DiagramNode("n1", DIAGRAM_LABELS[0], (20.0, 70.0, 90.0, 100.0), ("sp-plan",)),
        DiagramNode("n2", DIAGRAM_LABELS[1], (150.0, 70.0, 220.0, 100.0), ("sp-build",)),
    )
    edges = (DiagramEdge("n1", "n2", None, "leads to", Verification.VERIFIED),)
    return DiagramIR(DIAGRAM_MEMBER, DIAGRAM_ANCHOR, nodes, edges, (), Verification.VERIFIED)


def diagram_member(member_id: str = DIAGRAM_MEMBER) -> RetrievalContext:
    """The hydrated diagram member a store-backed ``resolve`` returns, without a store."""
    ir = diagram_ir()
    shape = PathEvidence(0, "shape", ((20.0, 70.0), (90.0, 100.0)), (20.0, 70.0, 90.0, 100.0))
    qualification = DiagramQualification(
        DIAGRAM_MEMBER,
        DIAGRAM_ANCHOR,
        _SHA,
        ("sp-plan", "sp-build"),
        tuple(
            NodeEvidence(node.node_id, node.source_span_ids, shape, node.bbox) for node in ir.nodes
        ),
        (),
    )
    member = _NamedMember(member_id, ObjectKind.DIAGRAM, 2, _REF, _REF, _REF, _REF, _REF, "fp", 2)
    return RetrievalContext(SNAPSHOT, member, ir, describe_diagram(ir), qualification)


@dataclass(frozen=True, slots=True)
class _NamedMember(RetrievalMember):
    """A retrieval member whose id is its object id, so blocks name the fake member."""

    @property
    def member_id(self) -> str:
        return self.object_id


@dataclass(frozen=True, slots=True)
class FakeMember:
    member_id: str
    text: str
    chart: ChartIR | None = None
    # Verified page metadata as ``member_texts`` would report it (ADR 0013).
    page_title: str | None = None
    page_type: str | None = None
    periods: tuple[str, ...] = ()
    regions: tuple[str, ...] = ()

    @property
    def kind(self) -> ObjectKind:
        return ObjectKind.TEXT if self.chart is None else ObjectKind.CHART

    @property
    def index_text(self) -> str:
        return self.text if self.chart is None else member_index_text(self.chart, self.text)


class FakeDocument:
    def __init__(
        self,
        members: tuple[FakeMember, ...],
        vector_order: tuple[str, ...],
        *,
        snapshot_id: str = SNAPSHOT,
    ) -> None:
        self._members = {member.member_id: member for member in members}
        self._vector_order = vector_order
        self._snapshot_id = snapshot_id
        self.member_texts_calls = 0
        self.search_calls: list[tuple[str, int]] = []
        self.resolved: list[str] = []

    @property
    def source_sha256(self) -> str:
        return "d" * 64

    @property
    def processing_id(self) -> str:
        return "p" * 64

    @property
    def retrieval_snapshot_id(self) -> str:
        return self._snapshot_id

    @property
    def embedding_fingerprint(self) -> str:
        return "fake-fingerprint"

    def manifest(self) -> ProcessingManifest:
        raise AssertionError("manifest is not needed by ranking")

    def member_texts(self) -> tuple[MemberText, ...]:
        self.member_texts_calls += 1
        return tuple(
            MemberText(
                member.member_id,
                member.kind,
                0,
                member.index_text,
                page_title=member.page_title,
                page_type=member.page_type,
                periods=member.periods,
                regions=member.regions,
            )
            for member in sorted(self._members.values(), key=lambda item: item.member_id)
        )

    def search(self, query: str, *, limit: int) -> tuple[PinnedRetrievalHit, ...]:
        self.search_calls.append((query, limit))
        return tuple(
            PinnedRetrievalHit(self._snapshot_id, member_id, 1.0 - 0.01 * rank)
            for rank, member_id in enumerate(self._vector_order[:limit])
        )

    def resolve(self, hit: PinnedRetrievalHit) -> RetrievalContext:
        if hit.snapshot_id != self._snapshot_id:
            raise ValueError("Retrieval hit belongs to another semantic snapshot")
        member = self._members[hit.member_id]
        self.resolved.append(member.member_id)
        retrieval_member = _NamedMember(
            member.member_id, member.kind, 0, _REF, _REF, _REF, _REF, _REF, "fp", 2
        )
        if member.chart is None:
            span = ObservedText(f"{member.member_id}-span", member.text, _ANCHOR)
            return RetrievalContext(
                self._snapshot_id,
                retrieval_member,
                TextIR(member.member_id, _ANCHOR, (span,)),
                ObjectDescription(
                    member.member_id,
                    _ANCHOR,
                    (span.source_span_id,),
                    member.text,
                    "exact-source-transcription-v1",
                    Confidence(None, "fake"),
                    Verification.VERIFIED,
                ),
                LiteralQualification(
                    member.member_id, _ANCHOR, _SHA, (span.source_span_id,), _REF, _REF, _REF
                ),
            )
        evidence = Evidence(("e-title",), Verification.VERIFIED, Confidence(None, "fake"))
        return RetrievalContext(
            self._snapshot_id,
            retrieval_member,
            member.chart,
            TextDescription(
                member.chart.binding,
                (DescriptionClaim(member.text, evidence),),
                "fake",
                Verification.VERIFIED,
            ),
            FigureQualification(
                member.chart.binding,
                _ANCHOR,
                (FieldOccurrence("title", ("e-title",)),),
                "fake",
                semantic_scope="explicit-distribution-shares",
            ),
        )

    def chart_context(self, hit: PinnedRetrievalHit) -> ChartContext:
        raise AssertionError("chart evidence is not part of ranking or seat selection")

    def displayed_context(self, hit: PinnedRetrievalHit) -> DisplayedLookupContext:
        raise AssertionError("displayed evidence is not part of ranking or seat selection")


def satisfies_port(document: FakeDocument) -> bool:
    return isinstance(document, MountedDocument)
