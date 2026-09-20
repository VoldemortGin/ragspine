"""A narrow source geometry oracle, independent of model confidence and marks."""

from dataclasses import replace
from decimal import Decimal
from hashlib import sha256
from math import ceil, cos, pi, sin, tan

import pytest

from enterprise_pdf_rag.adapters.donut_qualification import DonutQualification
from enterprise_pdf_rag.adapters.figure_reasoning import (
    PageContextObservation,
    PreparedFigure,
    prepare_figure,
)
from enterprise_pdf_rag.adapters.memory import (
    MemoryDescriptionIndex,
    MemoryFigureRepository,
)
from enterprise_pdf_rag.documents.models import AssetRef, Bounds, TextSidecar, TextSpan
from enterprise_pdf_rag.figures.models import (
    ChartIR,
    ChartMark,
    ChartPoint,
    Confidence,
    DescriptionClaim,
    Evidence,
    ExecutionMode,
    FigureError,
    NumericObservation,
    SvgArtifact,
    TextDescription,
    TextField,
    ValueKind,
    Verification,
)
from enterprise_pdf_rag.figures.service import FiguresService
from enterprise_pdf_rag.processing.models import PageInput


def _sector(start: float, stop: float) -> str:
    def point(radius: float, angle: float) -> tuple[float, float]:
        return (100 + radius * cos(angle), 80 + radius * sin(angle))

    def curve(radius: float, first: float, last: float) -> str:
        count = ceil(abs(last - first) / (pi / 4))
        parts = []
        for index in range(count):
            a = first + (last - first) * index / count
            b = first + (last - first) * (index + 1) / count
            k = 4 / 3 * tan((b - a) / 4)
            x0, y0 = point(radius, a)
            x3, y3 = point(radius, b)
            parts.append(
                f"C{x0 - radius * sin(a) * k} {y0 + radius * cos(a) * k} {x3 + radius * sin(b) * k} {y3 - radius * cos(b) * k} {x3} {y3}"
            )
        return "".join(parts)

    x, y = point(50, start)
    ix, iy = point(25, stop)
    return f"M{x} {y}" + curve(50, start, stop) + f"L{ix} {iy}" + curve(25, stop, start) + "Z"


def sample(
    *, extra_category: bool = False, geometry: str = "supported"
) -> tuple[PreparedFigure, ChartIR, TextDescription]:
    split = pi * 0.28
    left = _sector(split, 2 * pi - split)
    right = _sector(2 * pi - split, 2 * pi + split)
    if geometry == "rectangles":
        left, right = "M45 40L80 40L80 120L45 120Z", "M120 40L155 40L155 120L120 120Z"
    paths = f'<path fill="#d31145" d="{left}"/><path fill="#333d47" d="{right}"/>'
    if geometry == "skew":
        paths = f'<g transform="matrix(1 .1 0 1 0 0)">{paths}</g>'
    elif geometry == "clipped":
        paths = (
            '<defs><clipPath id="cut"><path d="M0 0L240 0L240 90L0 90Z"/></clipPath></defs><g clip-path="url(#cut)">'
            + paths
            + "</g>"
        )
    elif geometry == "unsupported-third":
        paths += '<path fill="#00aa00" d="M90 45H110V55H90Z"/>'
    elif geometry == "small-third":
        paths += '<path fill="#00aa00" d="M90 45L110 45L110 55L90 55Z"/>'
    elif geometry == "transformed-third":
        paths += '<path fill="#00aa00" transform="matrix(1 .1 0 1 0 0)" d="M90 35L110 35L110 45L90 45Z"/>'
    elif geometry == "unresolved-paint":
        paths = f'<g opacity="0.4">{paths}</g>'
    elif geometry == "white-in-textbox":
        paths += '<path fill="#ffffff" d="M56 75L62 75L62 81L56 81Z"/>'
    elif geometry == "rect-in-textbox":
        paths += '<rect fill="#ffffff" x="56" y="75" width="6" height="6"/>'
    elif geometry == "colored-in-textbox":
        paths += '<path fill="#00aa00" d="M56 75L62 75L62 81L56 81Z"/>'
    elif geometry == "fake-glyph-occluder":
        paths += '<g transform="matrix(1 0 0 -1 0 166)"><path fill="#ffffff" transform="matrix(10 0 0 10 55 83)" d="M0 0L1 0L1 .9L.5 .45L0 .9Z"/></g>'
    native = f'<svg xmlns="http://www.w3.org/2000/svg" width="240" height="160" viewBox="0 0 240 160">{paths}</svg>'.encode()
    number_dx = -4.2 if geometry == "near-boundary" else 25.0 if geometry == "center-hole" else 0.0
    labels: tuple[tuple[str, Bounds], ...] = (
        ("Distribution Mix", (50.0, 10.0, 170.0, 22.0)),
        ("VONB", (88.0, 71.0, 114.0, 78.0)),
        ("1H26", (88.0, 83.0, 114.0, 90.0)),
        ("Agency", (3.0, 74.0, 40.0, 83.0)),
        ("72%", (55.0 + number_dx, 74.0, 72.0 + number_dx, 83.0)),
        ("28%", (129.0, 74.0, 146.0, 83.0)),
        ("Partnerships", (160.0, 74.0, 229.0, 83.0)),
    )
    if extra_category:
        labels = (*labels, ("Other", (41.0, 74.0, 49.0, 83.0)))
    source = sha256(b"independently-authored-source").hexdigest()
    sidecar = TextSidecar(
        "source-text-v1",
        source,
        17,
        tuple(
            TextSpan(f"source-{i}", text, bbox, (55.0, 83.0), "Arial", 10.0)
            if geometry == "fake-glyph-occluder" and text == "72%"
            else TextSpan(f"source-{i}", text, bbox)
            for i, (text, bbox) in enumerate(labels)
        ),
    )
    page = PageInput(
        "a" * 64,
        source,
        17,
        240.0,
        160.0,
        AssetRef(sha256(native).hexdigest(), "image/svg+xml", len(native)),
        sidecar,
    )
    prepared = prepare_figure(
        page=page,
        native_svg=native,
        bbox=(0.0, 0.0, 240.0, 90.0 if geometry == "crop-cut" else 150.0),
        region_id="authored-distribution",
    )
    elements = {element.text: element for element in prepared.svg.elements}

    def ev(*labels: str) -> Evidence:
        return Evidence(
            tuple(elements[label].element_id for label in labels),
            Verification.PENDING,
            Confidence(None, "model-declared ordinal=high; uncalibrated"),
        )

    def field(text: str) -> TextField:
        return TextField(text, ev(text))

    points = tuple(
        ChartPoint(
            category,
            field("VONB"),
            field(category),
            field("%"),
            NumericObservation(Decimal(value), ValueKind.EXPLICIT, ev(value)),
        )
        for category, value in (("Agency", "72"), ("Partnerships", "28"))
    )
    # Each percent occurrence must come from the corresponding value's own span.
    points = tuple(
        replace(
            point,
            unit=TextField(
                "%",
                Evidence(
                    (
                        next(
                            element.element_id
                            for element in prepared.svg.elements
                            if element.text == "%"
                            and element.source_span_id
                            == elements[str(point.value.value)].source_span_id
                        ),
                    ),
                    Verification.PENDING,
                    Confidence(None, "unknown"),
                ),
            ),
        )
        for point in points
    )
    chart = ChartIR(
        prepared.svg.binding,
        "donut",
        (),
        points,
        "independent-chart-model",
        Verification.PENDING,
        ExecutionMode.PRODUCTION,
        field("Distribution Mix"),
        field("1H26"),
        (
            ChartMark(
                "model-mark",
                "arc",
                (0.0, 0.0, 240.0, 150.0),
                "#aaaaaa",
                ("Agency",),
                ev("72"),
            ),
        ),
    )
    claims = tuple(
        DescriptionClaim(
            f"During 1H26, VONB for {point.category.text}: {point.value.value} %.",
            Evidence(
                (
                    *point.series.evidence.element_ids,
                    *point.category.evidence.element_ids,
                    *point.unit.evidence.element_ids,
                    *point.value.evidence.element_ids,
                    *field("1H26").evidence.element_ids,
                ),
                Verification.PENDING,
                Confidence(None, "model-declared high"),
            ),
            "VONB",
            point.category.text,
            "%",
            point.value.value,
            "1H26",
        )
        for point in points
    )
    description = TextDescription(
        prepared.svg.binding,
        claims,
        "independent-description-model",
        Verification.PENDING,
        ExecutionMode.PRODUCTION,
    )
    return prepared, chart, description


def test_source_geometry_qualifies_only_explicit_share_claims_not_model_marks() -> None:
    prepared, chart, description = sample()
    result = DonutQualification(prepared).qualify_pair(prepared.svg, chart, description)
    assert result.chart.verification is Verification.VERIFIED
    assert result.chart.marks == ()
    assert prepared.svg.verification is Verification.PENDING
    assert chart.verification is description.verification is Verification.PENDING
    assert result.raw_chart_id == chart.artifact_id
    assert result.raw_description_id == description.artifact_id
    assert result.receipt.semantic_scope == "explicit-distribution-shares"
    assert len(result.receipt.source_geometry_refs) == 3
    assert (
        result.description.text
        == "During 1H26, VONB distribution share for Agency: 72 %. During 1H26, VONB distribution share for Partnerships: 28 %."
    )
    assert result.excluded_fields == ("marks.model-mark",)


def test_adjacent_label_cannot_replace_a_category_even_when_both_models_agree() -> None:
    prepared, chart, description = sample(extra_category=True)
    other = next(e for e in prepared.svg.elements if e.text == "Other")
    original = chart.points[0].category
    category = TextField("Other", replace(original.evidence, element_ids=(other.element_id,)))
    chart = replace(chart, points=(replace(chart.points[0], category=category), chart.points[1]))
    first = description.claims[0]
    first = replace(
        first,
        text="During 1H26, VONB for Other: 72 %.",
        category="Other",
        evidence=replace(
            first.evidence,
            element_ids=tuple(
                other.element_id if value in original.evidence.element_ids else value
                for value in first.evidence.element_ids
            ),
        ),
    )
    description = replace(description, claims=(first, description.claims[1]))
    with pytest.raises(FigureError, match=r"category.*ambiguous"):
        DonutQualification(prepared).qualify_pair(prepared.svg, chart, description)


def test_rejected_source_cannot_be_promoted_by_valid_numbers_and_geometry() -> None:
    prepared, chart, description = sample()
    svg = replace(prepared.svg, verification=Verification.REJECTED)
    prepared = replace(prepared, svg=svg)
    chart = replace(chart, binding=svg.binding)
    description = replace(description, binding=svg.binding)
    with pytest.raises(FigureError, match="rejected_source"):
        DonutQualification(prepared).qualify_pair(svg, chart, description)


@pytest.mark.parametrize(
    "geometry", ["skew", "clipped", "crop-cut", "near-boundary", "center-hole"]
)
def test_unsupported_source_geometry_or_unsafe_label_location_is_unavailable(
    geometry: str,
) -> None:
    prepared, chart, description = sample(geometry=geometry)
    with pytest.raises(FigureError, match=r"sector|scope_ambiguous|source_transform|source_paint"):
        DonutQualification(prepared).qualify_pair(prepared.svg, chart, description)


def test_swapping_categories_in_both_branches_cannot_qualify_the_wrong_source_relation() -> None:
    prepared, chart, description = sample()
    first, second = chart.points
    swapped = replace(
        chart,
        points=(
            replace(first, category=second.category),
            replace(second, category=first.category),
        ),
    )
    claims = tuple(
        replace(
            claim,
            category=new.category.text,
            text=f"During 1H26, VONB for {new.category.text}: {claim.value} %.",
            evidence=replace(
                claim.evidence,
                element_ids=tuple(
                    new.category.evidence.element_ids[0]
                    if element in old.category.evidence.element_ids
                    else element
                    for element in claim.evidence.element_ids
                ),
            ),
        )
        for claim, old, new in zip(description.claims, chart.points, swapped.points, strict=True)
    )
    description = replace(description, claims=claims)
    with pytest.raises(FigureError, match="value_category_pair"):
        DonutQualification(prepared).qualify_pair(prepared.svg, swapped, description)


def test_correct_values_do_not_qualify_an_added_market_or_causal_conclusion() -> None:
    prepared, chart, description = sample()
    claim = replace(
        description.claims[0],
        text=description.claims[0].text + " Agency leads the market because of policy changes.",
    )
    description = replace(description, claims=(claim, description.claims[1]))
    with pytest.raises(FigureError, match="unqualified_text"):
        DonutQualification(prepared).qualify_pair(prepared.svg, chart, description)


def test_unit_occurrence_must_belong_to_the_same_source_number() -> None:
    prepared, chart, description = sample()
    first, second = chart.points
    chart = replace(chart, points=(replace(first, unit=second.unit), second))
    with pytest.raises(FigureError, match="field_source_occurrence_mismatch"):
        DonutQualification(prepared).qualify_pair(prepared.svg, chart, description)


def test_qualification_cannot_read_paths_from_a_crop_other_than_the_model_source() -> None:
    prepared, chart, description = sample()
    prepared = replace(prepared, crop_svg=prepared.crop_svg.replace("#d31145", "#abcdef"))
    with pytest.raises(FigureError, match="source_view_binding"):
        DonutQualification(prepared).qualify_pair(prepared.svg, chart, description)


def test_scope_does_not_silently_ignore_shared_page_qualifiers() -> None:
    prepared, chart, description = sample()
    context = PageContextObservation(
        "footer",
        "Comparisons use constant FX.",
        replace(prepared.svg.source, bbox=(1.0, 152.0, 239.0, 158.0)),
    )
    prepared = replace(prepared, page_context=(context,))
    with pytest.raises(FigureError, match="page_context_semantics_not_qualified"):
        DonutQualification(prepared).qualify_pair(prepared.svg, chart, description)


def test_two_colored_rectangles_do_not_prove_donut_topology() -> None:
    prepared, chart, description = sample(geometry="rectangles")
    with pytest.raises(FigureError, match="annular"):
        DonutQualification(prepared).qualify_pair(prepared.svg, chart, description)


@pytest.mark.parametrize(
    "geometry",
    [
        "unsupported-third",
        "small-third",
        "transformed-third",
        "unresolved-paint",
        "white-in-textbox",
        "rect-in-textbox",
        "colored-in-textbox",
        "fake-glyph-occluder",
    ],
)
def test_unexplained_visible_paint_cannot_be_skipped_when_qualifying_two_sectors(
    geometry: str,
) -> None:
    prepared, chart, description = sample(geometry=geometry)
    with pytest.raises(FigureError, match=r"source_.*(paint|path|transform)"):
        DonutQualification(prepared).qualify_pair(prepared.svg, chart, description)


@pytest.mark.parametrize("rejected", ["chart", "description", "value", "claim"])
def test_scoped_qualifier_itself_cannot_revive_rejected_branches_or_claims(
    rejected: str,
) -> None:
    prepared, chart, description = sample()
    if rejected == "chart":
        chart = replace(chart, verification=Verification.REJECTED)
    elif rejected == "description":
        description = replace(description, verification=Verification.REJECTED)
    elif rejected == "value":
        first, second = chart.points
        chart = replace(
            chart,
            points=(
                replace(
                    first,
                    value=replace(
                        first.value,
                        evidence=replace(first.value.evidence, verification=Verification.REJECTED),
                    ),
                ),
                second,
            ),
        )
    else:
        first_claim, second_claim = description.claims
        description = replace(
            description,
            claims=(
                replace(
                    first_claim,
                    evidence=replace(first_claim.evidence, verification=Verification.REJECTED),
                ),
                second_claim,
            ),
        )
    with pytest.raises(FigureError, match="rejected_prior"):
        DonutQualification(prepared).qualify_pair(prepared.svg, chart, description)


def test_production_preflight_has_no_embedding_side_effect_and_pair_hydrates_same_snapshot() -> (
    None
):
    prepared, chart, description = sample()

    class Extractor:
        def extract(self, svg: SvgArtifact) -> ChartIR:
            raise AssertionError("Use the existing immutable model outputs")

    class Describer:
        def generate(self, svg: SvgArtifact) -> TextDescription:
            raise AssertionError("Do not rewrite descriptions from ChartIR")

    class Embedder:
        fingerprint = "test-only-embedding"

        def __init__(self) -> None:
            self.texts: list[str] = []

        def embed_description(self, text: str) -> tuple[float, ...]:
            self.texts.append(text)
            return (1.0, 0.0)

        def embed_query(self, text: str) -> tuple[float, ...]:
            return (1.0, 0.0)

    embedder = Embedder()
    repository = MemoryFigureRepository()
    service = FiguresService(
        Extractor(),
        Describer(),
        embedder,
        repository,
        MemoryDescriptionIndex(),
        qualifier=DonutQualification(prepared),
        mode=ExecutionMode.PRODUCTION,
    )
    qualified = service.qualify_pair(prepared.svg, chart, description)
    assert not embedder.texts
    assert repository.get_svg(prepared.svg.artifact_id) is None
    bundle = service.pair(
        prepared.svg,
        qualified.chart,
        qualified.description,
        snapshot_id="fixed-release",
    )
    (hit,) = service.search("Agency distribution share", snapshot_id="fixed-release")
    view = service.resolve(hit, snapshot_id="fixed-release")
    assert view.chart_ir == qualified.chart
    assert bundle.qualification_id == qualified.receipt.artifact_id
    assert embedder.texts == [qualified.description.text]
    assert service.search("Agency", snapshot_id="different-release") == ()
    assert {entry.field_path for entry in view.evidence} == {
        field.field_path for field in qualified.receipt.fields
    }
