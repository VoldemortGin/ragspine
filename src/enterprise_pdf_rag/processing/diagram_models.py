"""Deterministic proof records for a diagram's nodes and edges; nothing here infers."""

from dataclasses import dataclass
from typing import Literal

from enterprise_pdf_rag.documents.models import Bounds
from enterprise_pdf_rag.figures.models import SourceAnchor

DIAGRAM_SCOPE = "diagram-structure-source-geometry-v1"
DIAGRAM_METHOD = (
    "exact-span-label + native-shape-bbox + connector-endpoint + filled-arrowhead-tip; "
    "no relationship semantics, no financial relations"
)
# Tolerances in points, page-top-left. All are constants; none is injectable.
NODE_BBOX_TOLERANCE = 2.0
"""Per-edge slack between a node bbox and its real shape bounds clipped to the object."""
SPAN_INSIDE_TOLERANCE = 0.5
"""Per-edge slack for a cited span lying inside the object and its node bbox."""
CONNECT_TOLERANCE = 2.0
"""How far outside a node bbox a connector endpoint or arrow tip may still touch it."""
ARROW_JOIN_TOLERANCE = 3.0
"""Maximum distance between an arrowhead base midpoint and its connector endpoint."""
ARROWHEAD_MAX_AREA = 400.0
"""Bounding-box area ceiling (pt²) for a filled triangle to count as an arrowhead."""
SHAPE_MIN_FILL_RATIO = 0.85
"""Flattened polygon area over bounding-box area for a node frame (rounded rect: 0.997)."""
READING_ROW_QUANTUM = 4.0
"""Reading order quantises ``y0`` to this many points before sorting by ``x0``."""
NODE_ID_PATTERN = r"^[A-Za-z0-9_-]{1,80}$"
"""Character set keeping the citable path ``nodes.<id>.label`` unambiguous."""

type Point = tuple[float, float]


@dataclass(frozen=True, slots=True)
class PathEvidence:
    """One native SVG <path>, located by its document order; points are page-top-left."""

    path_index: int
    kind: Literal["shape", "line", "arrowhead"]
    points: tuple[Point, ...]
    bounds: Bounds


@dataclass(frozen=True, slots=True)
class NodeEvidence:
    """The verbatim spans and the real drawn frame that prove one node."""

    node_id: str
    label_span_ids: tuple[str, ...]
    shape: PathEvidence
    clipped_bounds: Bounds


@dataclass(frozen=True, slots=True)
class EdgeEvidence:
    """The connector and the filled arrowhead that prove one directed edge."""

    edge_index: int
    source_node_id: str
    target_node_id: str
    line: PathEvidence
    arrowhead: PathEvidence
    tip: Point
    base_mid: Point
    line_end_at_target: Point


@dataclass(frozen=True, slots=True)
class DiagramQualification:
    """Independent geometry/verbatim proof of a DiagramIR; produced and replayed by one pure function."""

    object_id: str
    source: SourceAnchor
    source_manifest_id: str
    source_span_ids: tuple[str, ...]
    nodes: tuple[NodeEvidence, ...]
    edges: tuple[EdgeEvidence, ...]
    method: str = DIAGRAM_METHOD
    scope: str = DIAGRAM_SCOPE

    def __post_init__(self) -> None:
        if not self.nodes:
            raise ValueError("diagram qualification requires at least one proven node")
        ids = tuple(node.node_id for node in self.nodes)
        if len(set(ids)) != len(ids):
            raise ValueError("diagram qualification requires unique node ids")
