"""Public figure use-case contracts with boundary doubles only."""

from dataclasses import replace
from decimal import Decimal

import pytest

from enterprise_pdf_rag.figures.models import (
    ChartAxis,
    ChartIR,
    ChartPoint,
    Confidence,
    DescriptionClaim,
    DescriptionEmbedding,
    DescriptionIndexRecord,
    Evidence,
    ExecutionMode,
    FailureCode,
    FieldOccurrence,
    FigureBundle,
    FigureError,
    FigureHit,
    FigureQualification,
    NumericObservation,
    SourceAnchor,
    SvgArtifact,
    SvgElement,
    TextDescription,
    TextField,
    ValueKind,
    Verification,
)
from enterprise_pdf_rag.figures.service import FiguresService
from enterprise_pdf_rag.figures.validation import validate_pair


def figure() -> SvgArtifact:
    source = SourceAnchor("source-v1", "a" * 64, 0, (0.0, 0.0, 200.0, 100.0))
    labels = (
        ("series", "Revenue"),
        ("category", "2024"),
        ("unit", "USD million"),
        ("value", "42"),
    )
    body = "".join(f'<text id="{key}">{text}</text>' for key, text in labels)
    return SvgArtifact(
        "figure-1",
        source,
        f'<svg xmlns="http://www.w3.org/2000/svg">{body}</svg>',
        tuple(SvgElement(key, text, source) for key, text in labels),
        Verification.VERIFIED,
    )


def evidence(*ids: str) -> Evidence:
    return Evidence(
        ids, Verification.VERIFIED, Confidence(None, "fixture exact labels")
    )


def test_numeric_claim_binds_period_and_rejects_extra_conclusions() -> None:
    base = figure()
    svg = replace(
        base,
        svg=base.svg.replace("</svg>", '<text id="period">1H26</text></svg>'),
        elements=(*base.elements, SvgElement("period", "1H26", base.source)),
    )
    chart = replace(
        Extractor().extract(svg), period=TextField("1H26", evidence("period"))
    )
    old = Describer().generate(svg)
    claim = replace(
        old.claims[0],
        text="During 1H26, Revenue for 2024: 42 USD million.",
        period="1H26",
        evidence=evidence("series", "category", "unit", "value", "period"),
    )
    description = replace(old, claims=(claim,))
    validate_pair(svg, chart, description)
    for changed in (
        replace(claim, period=None),
        replace(claim, period="1H25"),
        replace(
            claim, text=claim.text + " Agency is market-leading due to policy changes."
        ),
    ):
        with pytest.raises(FigureError):
            validate_pair(svg, chart, replace(description, claims=(changed,)))


class FixtureQualifier:
    """Authored source oracle; its mappings never consume either branch output."""

    def __init__(
        self,
        svg: SvgArtifact | None = None,
        *,
        extra_fields: tuple[FieldOccurrence, ...] = (),
    ) -> None:
        authored = figure() if svg is None else svg
        self.receipt = FigureQualification(
            authored.binding,
            authored.source,
            (
                FieldOccurrence("axes.y.label", ("series",)),
                FieldOccurrence("axes.y.unit", ("unit",)),
                FieldOccurrence("points.p1.series", ("series",)),
                FieldOccurrence("points.p1.category", ("category",)),
                FieldOccurrence("points.p1.unit", ("unit",)),
                FieldOccurrence("points.p1.value", ("value",)),
                *extra_fields,
            ),
            "independently authored fixture labels and field occurrences",
        )

    def qualification_for(self, svg: SvgArtifact) -> FigureQualification | None:
        return self.receipt if svg.binding == self.receipt.binding else None


class Extractor:
    def __init__(self) -> None:
        self.inputs: list[SvgArtifact] = []

    def extract(self, svg: SvgArtifact) -> ChartIR:
        self.inputs.append(svg)
        return ChartIR(
            svg.binding,
            "bar",
            (
                ChartAxis(
                    "y",
                    TextField("Revenue", evidence("series")),
                    TextField("USD million", evidence("unit")),
                    "linear",
                ),
            ),
            (
                ChartPoint(
                    "p1",
                    TextField("Revenue", evidence("series")),
                    TextField("2024", evidence("category")),
                    TextField("USD million", evidence("unit")),
                    NumericObservation(
                        Decimal("42"), ValueKind.EXPLICIT, evidence("value")
                    ),
                ),
            ),
            "fixture-chart@1",
            Verification.VERIFIED,
        )


class Describer:
    def __init__(self) -> None:
        self.inputs: list[SvgArtifact] = []

    def generate(self, svg: SvgArtifact) -> TextDescription:
        self.inputs.append(svg)
        return TextDescription(
            svg.binding,
            (
                DescriptionClaim(
                    "Revenue for 2024: 42 USD million.",
                    evidence("series", "category", "unit", "value"),
                    "Revenue",
                    "2024",
                    "USD million",
                    Decimal("42"),
                ),
            ),
            "fixture-description@1",
            Verification.VERIFIED,
        )


class Embedder:
    fingerprint = "fixture-embedding@1"

    def __init__(self) -> None:
        self.description_inputs: list[str] = []

    def embed_description(self, text: str) -> tuple[float, ...]:
        self.description_inputs.append(text)
        return (1.0, 0.0)

    def embed_query(self, text: str) -> tuple[float, ...]:
        return (1.0, 0.0)


class Repository:
    def __init__(self) -> None:
        self.bundles: dict[tuple[str, str], FigureBundle] = {}
        self.svgs: dict[str, SvgArtifact] = {}
        self.charts: dict[str, ChartIR] = {}
        self.descriptions: dict[str, TextDescription] = {}
        self.embeddings: dict[str, DescriptionEmbedding] = {}

    def save(
        self,
        bundle: FigureBundle,
        svg: SvgArtifact,
        chart: ChartIR,
        description: TextDescription,
    ) -> None:
        self.bundles[(bundle.snapshot_id, bundle.bundle_id)] = bundle
        self.svgs[svg.artifact_id] = svg
        self.charts[chart.artifact_id] = chart
        self.descriptions[description.artifact_id] = description

    def get_bundle(self, snapshot_id: str, bundle_id: str) -> FigureBundle | None:
        return self.bundles.get((snapshot_id, bundle_id))

    def get_svg(self, artifact_id: str) -> SvgArtifact | None:
        return self.svgs.get(artifact_id)

    def get_chart(self, artifact_id: str) -> ChartIR | None:
        return self.charts.get(artifact_id)

    def get_description(self, artifact_id: str) -> TextDescription | None:
        return self.descriptions.get(artifact_id)

    def get_embedding(self, key: str) -> DescriptionEmbedding | None:
        return self.embeddings.get(key)

    def save_embedding(self, embedding: DescriptionEmbedding) -> None:
        self.embeddings[embedding.key] = embedding


class Index:
    def __init__(self) -> None:
        self.records: list[DescriptionIndexRecord] = []

    def add(self, record: DescriptionIndexRecord) -> None:
        self.records.append(record)

    def search(
        self, vector: tuple[float, ...], *, snapshot_id: str, limit: int
    ) -> tuple[FigureHit, ...]:
        return tuple(
            replace(record.hit, score=1.0)
            for record in self.records
            if record.hit.snapshot_id == snapshot_id
        )[:limit]


def test_same_svg_branches_embed_only_description_and_hydrate_chart() -> None:
    svg, extractor, describer, embedder = figure(), Extractor(), Describer(), Embedder()
    service = FiguresService(
        extractor,
        describer,
        embedder,
        Repository(),
        Index(),
        qualifier=FixtureQualifier(),
        mode=ExecutionMode.OFFLINE_DEMO,
    )

    bundle = service.build(svg, snapshot_id="release-1")
    (hit,) = service.search("How much revenue?", snapshot_id="release-1")
    context = service.resolve(hit, snapshot_id="release-1")

    assert extractor.inputs == describer.inputs == [svg]
    assert extractor.inputs[0] is describer.inputs[0] is svg
    assert embedder.description_inputs == ["Revenue for 2024: 42 USD million."]
    assert context.bundle_id == bundle.bundle_id
    assert context.snapshot_id == "release-1"
    assert context.chart_ir.points[0].value.value == Decimal("42")
    assert any(
        item.field_path == "points.p1.value"
        and item.elements[0].anchor.source_revision == "source-v1"
        for item in context.evidence
    )


@pytest.mark.parametrize("branch", ["chart", "description"])
@pytest.mark.parametrize(
    "field", ["figure_id", "source_revision", "svg_artifact_id", "svg_digest"]
)
def test_pair_rejects_wrong_svg_binding_before_embedding(
    branch: str, field: str
) -> None:
    svg, embedder = figure(), Embedder()
    chart, description = Extractor().extract(svg), Describer().generate(svg)
    wrong = replace(svg.binding, **{field: "wrong"})
    if branch == "chart":
        chart = replace(chart, binding=wrong)
    else:
        description = replace(description, binding=wrong)
    service = FiguresService(
        Extractor(),
        Describer(),
        embedder,
        Repository(),
        Index(),
        qualifier=FixtureQualifier(),
        mode=ExecutionMode.OFFLINE_DEMO,
    )

    with pytest.raises(FigureError) as error:
        service.pair(svg, chart, description, snapshot_id="release-1")

    assert error.value.code is FailureCode.BINDING_MISMATCH
    assert not embedder.description_inputs


@pytest.mark.parametrize("part", ["svg", "chart", "description", "field", "production"])
def test_unqualified_inputs_never_reach_embedding(part: str) -> None:
    svg, embedder = figure(), Embedder()
    chart, description = Extractor().extract(svg), Describer().generate(svg)
    mode = ExecutionMode.OFFLINE_DEMO
    if part == "svg":
        svg = replace(svg, verification=Verification.PENDING)
        chart = replace(chart, binding=svg.binding)
        description = replace(description, binding=svg.binding)
    elif part == "chart":
        chart = replace(chart, verification=Verification.REJECTED)
    elif part == "description":
        description = replace(description, verification=Verification.PENDING)
    elif part == "field":
        rejected = Evidence(
            ("value",),
            Verification.REJECTED,
            Confidence(Decimal("0.99"), "model self-score"),
        )
        point = replace(
            chart.points[0], value=replace(chart.points[0].value, evidence=rejected)
        )
        chart = replace(chart, points=(point,))
    else:
        mode = ExecutionMode.PRODUCTION
        chart = replace(chart, execution_mode=ExecutionMode.PRODUCTION)
        description = replace(description, execution_mode=ExecutionMode.PRODUCTION)
    service = FiguresService(
        Extractor(),
        Describer(),
        embedder,
        Repository(),
        Index(),
        qualifier=FixtureQualifier(),
        mode=mode,
    )

    with pytest.raises(FigureError):
        service.pair(svg, chart, description, snapshot_id="release-1")

    assert not embedder.description_inputs


@pytest.mark.parametrize(
    "error_kind",
    [
        "number",
        "series",
        "unit",
        "text_number",
        "missing_evidence",
        "json",
        "svg",
        "negated",
        "unsupported_trend",
    ],
)
def test_description_claim_must_match_source_fields_before_embedding(
    error_kind: str,
) -> None:
    svg, embedder = figure(), Embedder()
    chart, description = Extractor().extract(svg), Describer().generate(svg)
    claim = description.claims[0]
    if error_kind == "number":
        claim = replace(
            claim, value=Decimal("420"), text="Revenue for 2024: 420 USD million."
        )
    elif error_kind == "series":
        claim = replace(claim, series="Profit", text="Profit for 2024: 42 USD million.")
    elif error_kind == "unit":
        claim = replace(
            claim, unit="EUR million", text="Revenue for 2024: 42 EUR million."
        )
    elif error_kind == "text_number":
        claim = replace(claim, text="Revenue for 2024: 420 USD million.")
    elif error_kind == "missing_evidence":
        claim = replace(claim, evidence=evidence("missing"))
    elif error_kind == "json":
        claim = replace(
            claim, text='{"Revenue": "2024", "value": 42, "unit": "USD million"}'
        )
    elif error_kind == "negated":
        claim = replace(claim, text="Revenue for 2024: not 42 USD million.")
    elif error_kind == "unsupported_trend":
        claim = replace(
            claim, text="Revenue for 2024: 42 USD million, a rapid decline."
        )
    else:
        claim = replace(claim, text="<svg>Revenue for 2024: 42 USD million.</svg>")
    description = replace(description, claims=(claim,))
    service = FiguresService(
        Extractor(),
        Describer(),
        embedder,
        Repository(),
        Index(),
        qualifier=FixtureQualifier(),
        mode=ExecutionMode.OFFLINE_DEMO,
    )

    with pytest.raises(FigureError):
        service.pair(svg, chart, description, snapshot_id="release-1")

    assert not embedder.description_inputs


@pytest.mark.parametrize(
    "corruption", ["xml_text", "pdf_revision", "pdf_hash", "duplicate_element"]
)
def test_svg_mapping_cannot_disagree_with_xml_or_pdf_source(corruption: str) -> None:
    svg, embedder = figure(), Embedder()
    if corruption == "xml_text":
        svg = replace(svg, svg=svg.svg.replace(">42<", ">420<"))
    elif corruption in ("pdf_revision", "pdf_hash"):
        source = (
            replace(svg.source, source_revision="other")
            if corruption == "pdf_revision"
            else replace(svg.source, document_sha256="b" * 64)
        )
        svg = replace(
            svg, elements=(*svg.elements[:-1], replace(svg.elements[-1], anchor=source))
        )
    else:
        svg = replace(svg, elements=(*svg.elements, svg.elements[0]))
    service = FiguresService(
        Extractor(),
        Describer(),
        embedder,
        Repository(),
        Index(),
        qualifier=FixtureQualifier(),
        mode=ExecutionMode.OFFLINE_DEMO,
    )

    with pytest.raises(FigureError):
        service.build(svg, snapshot_id="release-1")

    assert not embedder.description_inputs


@pytest.mark.parametrize(
    "field",
    [
        "snapshot_id",
        "source_revision",
        "chart_ir_artifact_id",
        "svg_artifact_id",
        "svg_digest",
        "description_id",
        "text",
    ],
)
def test_context_rejects_tampered_hit_membership(field: str) -> None:
    service = FiguresService(
        Extractor(),
        Describer(),
        Embedder(),
        Repository(),
        Index(),
        qualifier=FixtureQualifier(),
        mode=ExecutionMode.OFFLINE_DEMO,
    )
    service.build(figure(), snapshot_id="release-1")
    (hit,) = service.search("revenue", snapshot_id="release-1")

    if field == "snapshot_id":
        wrong = replace(hit, snapshot_id="other")
    elif field == "source_revision":
        wrong = replace(hit, source_revision="other")
    elif field == "chart_ir_artifact_id":
        wrong = replace(hit, chart_ir_artifact_id="other")
    elif field == "svg_artifact_id":
        wrong = replace(hit, svg_artifact_id="other")
    elif field == "svg_digest":
        wrong = replace(hit, svg_digest="other")
    elif field == "description_id":
        wrong = replace(hit, description_id="other")
    else:
        wrong = replace(hit, text="other")
    with pytest.raises(FigureError):
        service.resolve(wrong, snapshot_id="release-1")


@pytest.mark.parametrize("corruption", ["missing_chart", "replaced_chart"])
def test_context_never_falls_back_to_summary_when_chart_is_missing_or_replaced(
    corruption: str,
) -> None:
    repository = Repository()
    service = FiguresService(
        Extractor(),
        Describer(),
        Embedder(),
        repository,
        Index(),
        qualifier=FixtureQualifier(),
        mode=ExecutionMode.OFFLINE_DEMO,
    )
    bundle = service.build(figure(), snapshot_id="release-1")
    (hit,) = service.search("revenue", snapshot_id="release-1")
    chart = repository.charts.pop(bundle.chart_ir_artifact_id)
    if corruption == "replaced_chart":
        repository.charts[bundle.chart_ir_artifact_id] = replace(
            chart, producer="other@2"
        )

    with pytest.raises(FigureError):
        service.resolve(hit, snapshot_id="release-1")


@pytest.mark.parametrize(
    "kind", [ValueKind.ESTIMATED, ValueKind.UNAVAILABLE, ValueKind.DERIVED]
)
def test_nonexact_observations_cannot_supply_exact_reasoning_context(
    kind: ValueKind,
) -> None:
    svg = figure()
    chart = Extractor().extract(svg)
    value = None if kind is ValueKind.UNAVAILABLE else Decimal("42")
    chart = replace(
        chart,
        points=(
            replace(
                chart.points[0],
                value=NumericObservation(value, kind, evidence("value")),
            ),
        ),
    )
    description = replace(
        Describer().generate(svg),
        claims=(DescriptionClaim("Revenue", evidence("series")),),
    )
    service = FiguresService(
        Extractor(),
        Describer(),
        Embedder(),
        Repository(),
        Index(),
        qualifier=FixtureQualifier(),
        mode=ExecutionMode.OFFLINE_DEMO,
    )
    service.pair(svg, chart, description, snapshot_id="release-1")
    (hit,) = service.search("revenue", snapshot_id="release-1")

    with pytest.raises(FigureError) as error:
        service.resolve(hit, snapshot_id="release-1")

    assert error.value.code is FailureCode.UNSUPPORTED_VALUE


@pytest.mark.parametrize("label", ["(42)", "42e2", "42,00", ""])
def test_blank_or_different_numeric_label_cannot_be_used_as_explicit_42(
    label: str,
) -> None:
    svg = figure()
    svg = replace(
        svg,
        svg=svg.svg.replace(">42<", f">{label}<"),
        elements=(*svg.elements[:-1], replace(svg.elements[-1], text=label)),
    )
    embedder = Embedder()
    service = FiguresService(
        Extractor(),
        Describer(),
        embedder,
        Repository(),
        Index(),
        qualifier=FixtureQualifier(svg),
        mode=ExecutionMode.OFFLINE_DEMO,
    )

    with pytest.raises(FigureError):
        service.build(svg, snapshot_id="release-1")

    assert not embedder.description_inputs


def test_repair_reuses_description_embedding_and_keeps_old_snapshot() -> None:
    svg, embedder, repository = figure(), Embedder(), Repository()
    chart, description = Extractor().extract(svg), Describer().generate(svg)
    service = FiguresService(
        Extractor(),
        Describer(),
        embedder,
        repository,
        Index(),
        qualifier=FixtureQualifier(),
        mode=ExecutionMode.OFFLINE_DEMO,
    )
    original = service.pair(svg, chart, description, snapshot_id="release-1")
    updated_chart = replace(chart, producer="fixture-chart@2")

    updated = service.pair(svg, updated_chart, description, snapshot_id="release-2")
    (old_hit,) = service.search("revenue", snapshot_id="release-1")
    (new_hit,) = service.search("revenue", snapshot_id="release-2")

    assert original.bundle_id != updated.bundle_id
    assert original.embedding_key == updated.embedding_key
    assert original.description_id == updated.description_id
    assert embedder.description_inputs == [description.text]
    assert service.resolve(old_hit, snapshot_id="release-1").chart_ir == chart
    assert service.resolve(new_hit, snapshot_id="release-2").chart_ir == updated_chart
    assert figure().artifact_id == svg.artifact_id
    assert Extractor().extract(figure()).artifact_id == chart.artifact_id
    assert Describer().generate(figure()).artifact_id == description.artifact_id


def test_same_number_in_different_series_still_requires_matched_point_evidence() -> (
    None
):
    svg = figure()
    extra = '<text id="profit">Profit</text><text id="profit-value">42</text>'
    svg = replace(
        svg,
        svg=svg.svg.replace("</svg>", extra + "</svg>"),
        elements=(
            *svg.elements,
            SvgElement("profit", "Profit", svg.source),
            SvgElement("profit-value", "42", svg.source),
        ),
    )
    chart = Extractor().extract(svg)
    profit = replace(
        chart.points[0],
        point_id="profit",
        series=TextField("Profit", evidence("profit")),
        value=NumericObservation(
            Decimal("42"), ValueKind.EXPLICIT, evidence("profit-value")
        ),
    )
    chart = replace(chart, points=(*chart.points, profit))
    description = Describer().generate(svg)
    wrong = replace(
        description.claims[0], text="Profit for 2024: 42 USD million.", series="Profit"
    )
    embedder = Embedder()
    service = FiguresService(
        Extractor(),
        Describer(),
        embedder,
        Repository(),
        Index(),
        qualifier=FixtureQualifier(
            svg,
            extra_fields=(
                FieldOccurrence("points.profit.series", ("profit",)),
                FieldOccurrence("points.profit.category", ("category",)),
                FieldOccurrence("points.profit.unit", ("unit",)),
                FieldOccurrence("points.profit.value", ("profit-value",)),
            ),
        ),
        mode=ExecutionMode.OFFLINE_DEMO,
    )

    with pytest.raises(FigureError) as error:
        service.pair(
            svg, chart, replace(description, claims=(wrong,)), snapshot_id="release-1"
        )

    assert error.value.code is FailureCode.INVALID_EVIDENCE
    assert not embedder.description_inputs


class EmptyExtractor:
    def extract(self, svg: SvgArtifact) -> None:
        return None


def test_missing_chart_branch_never_publishes_only_a_description() -> None:
    embedder, index = Embedder(), Index()
    service = FiguresService(
        EmptyExtractor(),
        Describer(),
        embedder,
        Repository(),
        index,
        qualifier=FixtureQualifier(),
        mode=ExecutionMode.OFFLINE_DEMO,
    )

    with pytest.raises(FigureError) as error:
        service.build(figure(), snapshot_id="release-1")

    assert error.value.code is FailureCode.MISSING_CHART
    assert not embedder.description_inputs
    assert not service.search("revenue", snapshot_id="release-1")


@pytest.mark.parametrize("field", ["figure_id", "source_revision"])
def test_bundle_and_matching_hit_cannot_relabel_resolved_source(field: str) -> None:
    repository = Repository()
    service = FiguresService(
        Extractor(),
        Describer(),
        Embedder(),
        repository,
        Index(),
        qualifier=FixtureQualifier(),
        mode=ExecutionMode.OFFLINE_DEMO,
    )
    bundle = service.build(figure(), snapshot_id="release-1")
    (hit,) = service.search("revenue", snapshot_id="release-1")
    if field == "figure_id":
        forged_bundle = replace(bundle, figure_id="figure-DECOY")
        forged_hit = replace(hit, figure_id="figure-DECOY")
    else:
        forged_bundle = replace(bundle, source_revision="source-DECOY")
        forged_hit = replace(hit, source_revision="source-DECOY")
    repository.bundles[("release-1", forged_bundle.bundle_id)] = forged_bundle
    forged_hit = replace(forged_hit, bundle_id=forged_bundle.bundle_id)

    with pytest.raises(FigureError) as error:
        service.resolve(forged_hit, snapshot_id="release-1")

    assert error.value.code is FailureCode.BINDING_MISMATCH


def test_same_text_at_another_occurrence_cannot_replace_series_evidence() -> None:
    svg = figure()
    svg = replace(
        svg,
        svg=svg.svg.replace("</svg>", '<text id="decoy-series">Revenue</text></svg>'),
        elements=(*svg.elements, SvgElement("decoy-series", "Revenue", svg.source)),
    )
    chart = Extractor().extract(svg)
    chart = replace(
        chart,
        points=(
            replace(
                chart.points[0], series=TextField("Revenue", evidence("decoy-series"))
            ),
        ),
    )
    description = Describer().generate(svg)
    description = replace(
        description,
        claims=(
            replace(
                description.claims[0],
                evidence=evidence("decoy-series", "category", "unit", "value"),
            ),
        ),
    )
    embedder, index = Embedder(), Index()
    service = FiguresService(
        Extractor(),
        Describer(),
        embedder,
        Repository(),
        index,
        qualifier=FixtureQualifier(svg),
        mode=ExecutionMode.OFFLINE_DEMO,
    )

    with pytest.raises(FigureError) as error:
        service.pair(svg, chart, description, snapshot_id="release-1")

    assert error.value.code is FailureCode.INVALID_EVIDENCE
    assert not embedder.description_inputs
    assert not index.records


class ProvidedQualification:
    def __init__(self, receipt: FigureQualification | None) -> None:
        self.receipt = receipt

    def qualification_for(self, svg: SvgArtifact) -> FigureQualification | None:
        return self.receipt


@pytest.mark.parametrize("corruption", ["missing", "source", "bbox", "svg"])
def test_self_verified_svg_requires_independent_exact_source_receipt(
    corruption: str,
) -> None:
    svg = figure()
    original = FixtureQualifier().receipt
    receipt: FigureQualification | None
    if corruption == "missing":
        receipt = None
    elif corruption == "source":
        receipt = replace(
            original, source=replace(original.source, document_sha256="b" * 64)
        )
    elif corruption == "bbox":
        receipt = replace(
            original, source=replace(original.source, bbox=(0.0, 0.0, 100.0, 50.0))
        )
    else:
        receipt = replace(
            original, binding=replace(original.binding, svg_digest="b" * 64)
        )
    extractor, describer, embedder, index = (
        Extractor(),
        Describer(),
        Embedder(),
        Index(),
    )
    service = FiguresService(
        extractor,
        describer,
        embedder,
        Repository(),
        index,
        qualifier=ProvidedQualification(receipt),
        mode=ExecutionMode.OFFLINE_DEMO,
    )

    with pytest.raises(FigureError) as error:
        service.build(svg, snapshot_id="release-1")

    assert error.value.code is FailureCode.UNVERIFIED
    assert not extractor.inputs
    assert not describer.inputs
    assert not embedder.description_inputs
    assert not index.records
