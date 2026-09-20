"""Read-only smoke over the pinned AIA run: one diagram qualifies, one is refused."""

from pathlib import Path

import pytest
from pydantic import TypeAdapter

from enterprise_pdf_rag.adapters.diagram_qualification import (
    DiagramQualificationError,
    qualify_diagram,
)
from enterprise_pdf_rag.core.settings import ROOT_DIR
from enterprise_pdf_rag.documents.models import TextSidecar, TextSpan
from enterprise_pdf_rag.processing.typed_ir import DiagramIR

_RUN = ROOT_DIR / (
    "data/output/aia-2026-interim/pages-001-020/runs/"
    "00d5c714c8059e9c74da32b030ec56813eb3affe6a8414af85ff78af63ae2076"
)
_P6 = _RUN / "page-006/objects/object-b7c9e77d6cf0ea933193"
_P5 = _RUN / "page-005/objects/object-0bf4bbbb1af632182d06"
pytestmark = pytest.mark.skipif(
    not _P6.is_dir() or not _P5.is_dir(), reason="AIA run dump is not present"
)


def _inputs(folder: Path) -> tuple[bytes, tuple[TextSpan, ...], DiagramIR]:
    svg = (folder / "svg.svg").read_bytes()
    sidecar = TypeAdapter(TextSidecar).validate_json((folder / "source_text.json").read_bytes())
    ir = TypeAdapter(DiagramIR).validate_json((folder / "ir.json").read_bytes())
    return svg, sidecar.spans, ir


def test_p6_three_stage_pathway_qualifies_nodes_only() -> None:
    svg, spans, ir = _inputs(_P6)
    qualified = qualify_diagram(svg=svg, spans=spans, ir=ir, source_manifest_id="x" * 64)

    assert tuple(node.node_id for node in qualified.qualification.nodes) == (
        "node-foundation",
        "node-growth",
        "node-intelligence",
    )
    assert qualified.qualification.edges == ()
    assert qualified.description.text.startswith(
        "Diagram with 3 nodes: Foundation: 100% Digitalised Agency; "
    )
    assert qualified.description.text.endswith("No connecting edges.")


def test_p5_technology_flow_is_rejected_for_empty_labels() -> None:
    svg, spans, ir = _inputs(_P5)
    with pytest.raises(DiagramQualificationError) as failure:
        qualify_diagram(svg=svg, spans=spans, ir=ir, source_manifest_id="x" * 64)

    assert str(failure.value) == (
        "node node-industry-leading-technology: empty_label_without_source_occurrence"
    )
