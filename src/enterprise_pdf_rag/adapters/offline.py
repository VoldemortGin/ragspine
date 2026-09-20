"""Deterministic adapters for one declared fixture grammar, never production AI.

Each branch reads the same source SVG independently. These adapters demonstrate
the ports and evidence flow, not general chart recognition or model quality.
"""

import hashlib
import re
from decimal import Decimal
from math import sqrt
from xml.etree import ElementTree

from enterprise_pdf_rag.figures.models import (
    ChartAxis,
    ChartIR,
    ChartPoint,
    Confidence,
    DescriptionClaim,
    Evidence,
    NumericObservation,
    SvgArtifact,
    TextDescription,
    TextField,
    ValueKind,
    Verification,
)


def _labels(svg: SvgArtifact) -> dict[str, str]:
    root = ElementTree.fromstring(svg.svg)
    result: dict[str, str] = {}
    for node in root.findall("{http://www.w3.org/2000/svg}text"):
        if node.text and "id" in node.attrib:
            if node.text in result:
                raise ValueError("The demo grammar requires unique explicit labels")
            result[node.text] = node.attrib["id"]
    if set(result) != {"Revenue", "USDm", "2024", "10", "2025", "15"}:
        raise ValueError("Only the known labelled demo fixture is supported")
    return result


def _evidence(*element_ids: str) -> Evidence:
    return Evidence(
        element_ids,
        Verification.VERIFIED,
        Confidence(None, "offline-demo: explicit source labels; no calibrated model score"),
    )


class OfflineChartExtractor:
    def extract(self, svg: SvgArtifact) -> ChartIR:
        labels = _labels(svg)
        series = TextField("Revenue", _evidence(labels["Revenue"]))
        unit = TextField("USDm", _evidence(labels["USDm"]))
        points = tuple(
            ChartPoint(
                f"revenue-{year}",
                series,
                TextField(year, _evidence(labels[year])),
                unit,
                NumericObservation(Decimal(value), ValueKind.EXPLICIT, _evidence(labels[value])),
            )
            for year, value in (("2024", "10"), ("2025", "15"))
        )
        return ChartIR(
            svg.binding,
            "explicit-labelled-demo-bars-v1",
            (ChartAxis("y", series, unit, "linear"),),
            points,
            "offline-demo/label-grammar-v1",
            Verification.VERIFIED,
        )


class OfflineDescriptionGenerator:
    def generate(self, svg: SvgArtifact) -> TextDescription:
        labels = _labels(svg)
        claims = tuple(
            DescriptionClaim(
                f"Revenue for {year}: {value} USDm.",
                _evidence(labels["Revenue"], labels["USDm"], labels[year], labels[value]),
                series="Revenue",
                category=year,
                unit="USDm",
                value=Decimal(value),
            )
            for year, value in (("2024", "10"), ("2025", "15"))
        )
        return TextDescription(
            svg.binding,
            claims,
            "offline-demo/source-template-v1",
            Verification.VERIFIED,
        )


class OfflineDescriptionEmbedder:
    """A reproducible token-hash demo vector, not a semantic embedding model."""

    @property
    def fingerprint(self) -> str:
        return "offline-demo/token-hash-64-v1"

    def embed_description(self, text: str) -> tuple[float, ...]:
        return self._vector(text)

    def embed_query(self, text: str) -> tuple[float, ...]:
        return self._vector(text)

    @staticmethod
    def _vector(text: str) -> tuple[float, ...]:
        values = [0.0] * 64
        for token in re.findall(r"\w+", text.casefold()):
            digest = hashlib.sha256(token.encode()).digest()
            values[int.from_bytes(digest[:2]) % len(values)] += 1.0
        norm = sqrt(sum(value * value for value in values))
        if not norm:
            raise ValueError("Embedding text must contain a token")
        return tuple(value / norm for value in values)
