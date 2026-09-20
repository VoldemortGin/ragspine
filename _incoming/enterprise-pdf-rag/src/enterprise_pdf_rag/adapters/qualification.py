"""Trusted, process-local receipts for an internally authored demo source.

This adapter never derives eligibility from a producer's ChartIR or description.
Its only registration entry generates the known fixture itself and checks source
label positions. Arbitrary SVGs cannot be registered through the CLI or API.
"""

from xml.etree import ElementTree

from enterprise_pdf_rag.adapters.demo_source import make_demo_figure
from enterprise_pdf_rag.figures.models import (
    FieldOccurrence,
    FigureQualification,
    SvgArtifact,
)

_EXPECTED_ORIGINS = {
    "Revenue": (35.0, 35.0),
    "USDm": (190.0, 35.0),
    "2024": (60.0, 210.0),
    "10": (80.0, 100.0),
    "2025": (170.0, 210.0),
    "15": (190.0, 60.0),
}


class AuthoredFixtureQualifier:
    def __init__(self) -> None:
        self._receipts: dict[str, FigureQualification] = {}

    def create_source(self) -> tuple[bytes, SvgArtifact]:
        pdf, svg = make_demo_figure()
        root = ElementTree.fromstring(svg.svg)
        labels: dict[str, str] = {}
        for node in root.findall("{http://www.w3.org/2000/svg}text"):
            if node.text not in _EXPECTED_ORIGINS or node.text in labels:
                raise ValueError(
                    "Source contains an unexpected or ambiguous fixture label"
                )
            origin = (float(node.attrib["x"]), float(node.attrib["y"]))
            if origin != _EXPECTED_ORIGINS[node.text]:
                raise ValueError("Fixture label is not at its authored source location")
            labels[node.text] = node.attrib["id"]
        if labels.keys() != _EXPECTED_ORIGINS.keys():
            raise ValueError("Fixture source is missing an authored label")
        fields = [
            FieldOccurrence("axes.y.label", (labels["Revenue"],)),
            FieldOccurrence("axes.y.unit", (labels["USDm"],)),
        ]
        for year, value in (("2024", "10"), ("2025", "15")):
            for name, label in (
                ("series", "Revenue"),
                ("category", year),
                ("unit", "USDm"),
                ("value", value),
            ):
                fields.append(
                    FieldOccurrence(f"points.revenue-{year}.{name}", (labels[label],))
                )
        self._receipts[svg.artifact_id] = FigureQualification(
            svg.binding, svg.source, tuple(fields), "offline-demo/authored-positions-v1"
        )
        return pdf, svg

    def qualification_for(self, svg: SvgArtifact) -> FigureQualification | None:
        return self._receipts.get(svg.artifact_id)
