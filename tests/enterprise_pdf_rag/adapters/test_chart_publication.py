"""Publication admits a chart only from its pinned source and raw branch closure."""

from dataclasses import replace
from pathlib import Path

import pytest
from pydantic import TypeAdapter

from enterprise_pdf_rag.adapters.chart_publication import (
    ChartPublicationReceipt,
    NumericLabelPublicationReceipt,
    parse_chart_receipt,
    promote_numeric_label_member,
    resolve_chart_member,
    validate_chart_member,
)
from enterprise_pdf_rag.adapters.document_store import LocalDocumentStore
from enterprise_pdf_rag.adapters.donut_qualification import DonutQualification
from enterprise_pdf_rag.adapters.figure_label_qualification import (
    FIGURE_LABEL_SCOPE,
    FIGURE_POINT_SCOPE,
    qualify_source_labels,
)
from enterprise_pdf_rag.adapters.figure_reasoning import (
    ContextualFigureModelView,
    FigureModelView,
    prepare_figure,
)
from enterprise_pdf_rag.adapters.pdfspine_document import PdfspineDocumentAdapter
from enterprise_pdf_rag.adapters.processing_retrieval import ProcessingRetrieval
from enterprise_pdf_rag.adapters.processing_store import ProcessingStore
from enterprise_pdf_rag.adapters.source_paint import (
    SourcePaintProof,
    build_source_paint_proof,
)
from enterprise_pdf_rag.documents.models import (
    AssetRef,
    DocumentManifest,
    PageRecord,
    RegionRecord,
    TextSidecar,
    TextSpan,
)
from enterprise_pdf_rag.figures.models import (
    ChartIR,
    DescriptionClaim,
    QualifiedFigurePair,
    SvgElement,
    TextDescription,
    Verification,
)
from enterprise_pdf_rag.processing.index_text import chart_index_text
from enterprise_pdf_rag.processing.models import (
    ObjectKind,
    ObjectProcessingRecord,
    PageInput,
    ProcessingScope,
    StageOutcome,
    StageState,
)
from enterprise_pdf_rag.processing.retrieval import (
    RetrievalContext,
    RetrievalMember,
    require_financial_qualification,
)
from tests.enterprise_pdf_rag.adapters.test_donut_qualification import sample
from tests.enterprise_pdf_rag.adapters.test_source_paint import authored_donut


def test_numeric_promotion_cannot_trust_an_invented_pdf_behind_valid_source_labels(
    tmp_path: Path,
) -> None:
    sources, assets, scope, member, _ = member_fixture(tmp_path, label_scope=True)
    with pytest.raises((ValueError, RuntimeError)):
        promote_numeric_label_member(sources, assets, scope, member)


def test_resolved_member_exposes_its_revalidated_source_svg(tmp_path: Path) -> None:
    sources, assets, scope, member, pair = member_fixture(tmp_path, label_scope=True)

    resolved = resolve_chart_member(sources, assets, scope, member)

    assert resolved.chart == pair.chart
    assert resolved.description == pair.description
    assert resolved.qualification == pair.receipt
    assert resolved.svg.binding == pair.chart.binding
    assert resolved.svg.svg.encode() == assets.get(member.source_svg)


def test_numeric_promotion_reuses_the_exact_label_description_and_vector(
    tmp_path: Path,
) -> None:
    sources, assets, scope, member, _ = member_fixture(tmp_path, label_scope=True, actual_pdf=True)
    prior = sources.load_current().manifest_id

    numeric = promote_numeric_label_member(sources, assets, scope, member)

    assert numeric.description == member.description
    assert numeric.embedding == member.embedding
    assert numeric.source_svg == member.source_svg
    assert numeric.member_id != member.member_id
    assert sources.load_current().manifest_id == prior
    assert not (assets.root / "current-processing").exists()
    receipt = parse_chart_receipt(assets.get(numeric.qualification))
    assert isinstance(receipt, NumericLabelPublicationReceipt)
    assert set(numeric.lineage_refs) == {
        *member.lineage_refs,
        receipt.source_paint_proof,
    }
    resolved = resolve_chart_member(sources, assets, scope, numeric)
    assert resolved.description.text == "Distribution Mix"
    assert resolved.qualification.semantic_scope == "explicit-distribution-shares"
    assert {point.category.text: str(point.value.value) for point in resolved.chart.points} == {
        "Agency": "72",
        "Partnerships": "28",
    }


def test_a_verbatim_points_member_resolves_and_promotes_under_its_own_scope(
    tmp_path: Path,
) -> None:
    """The newer label policy must replay, and must not disturb the numeric promotion."""
    sources, assets, scope, member, _ = member_fixture(
        tmp_path, label_scope=True, actual_pdf=True, label_policy=FIGURE_POINT_SCOPE
    )

    resolved = resolve_chart_member(sources, assets, scope, member)

    assert resolved.qualification.semantic_scope == FIGURE_POINT_SCOPE
    assert resolved.chart.points
    assert all(point.value.value is not None for point in resolved.chart.points)
    assert resolved.chart.verification is Verification.PENDING
    with pytest.raises(ValueError, match="financial"):
        require_financial_qualification(
            RetrievalContext(
                "f" * 64, member, resolved.chart, resolved.description, resolved.qualification
            )
        )

    numeric = promote_numeric_label_member(sources, assets, scope, member)
    promoted = resolve_chart_member(sources, assets, scope, numeric)

    # The numeric receipt pins the description asset, not the policy that projected it.
    assert promoted.qualification.semantic_scope == "explicit-distribution-shares"
    assert promoted.description == resolved.description


def test_self_reported_proof_bytes_cannot_replace_recomputed_font_provenance(
    tmp_path: Path,
) -> None:
    sources, assets, scope, member, _ = member_fixture(tmp_path)
    receipt = parse_chart_receipt(assets.get(member.qualification))
    assert isinstance(receipt, NumericLabelPublicationReceipt)
    proof = TypeAdapter(SourcePaintProof).validate_json(
        assets.get(receipt.source_paint_proof), strict=True
    )
    false_proof = replace(
        proof,
        glyphs=(replace(proof.glyphs[0], font_sha256="f" * 64), *proof.glyphs[1:]),
    )
    replacement = assets.put(
        TypeAdapter(SourcePaintProof).dump_json(false_proof),
        media_type="application/json",
    )
    changed = receipt.model_copy(update={"source_paint_proof": replacement})
    changed_member = replace(
        member,
        qualification=assets.put(changed.model_dump_json().encode(), media_type="application/json"),
        lineage_refs=tuple(
            replacement if ref == receipt.source_paint_proof else ref for ref in member.lineage_refs
        ),
    )
    with pytest.raises(ValueError, match="source_paint_proof_revalidation_mismatch"):
        resolve_chart_member(sources, assets, scope, changed_member)


def test_legacy_numeric_receipt_cannot_bypass_the_source_paint_policy(
    tmp_path: Path,
) -> None:
    sources, assets, scope, member, _ = member_fixture(tmp_path)
    receipt = parse_chart_receipt(assets.get(member.qualification))
    assert isinstance(receipt, NumericLabelPublicationReceipt)
    legacy = ChartPublicationReceipt.model_validate_json(
        receipt.model_dump_json(exclude={"schema_version", "source_paint_proof"})
    )
    changed = replace(
        member,
        qualification=assets.put(legacy.model_dump_json().encode(), media_type="application/json"),
        lineage_refs=tuple(ref for ref in member.lineage_refs if ref != receipt.source_paint_proof),
    )
    with pytest.raises(ValueError, match="source-paint publication policy"):
        resolve_chart_member(sources, assets, scope, changed)


def member_fixture(
    tmp_path: Path,
    *,
    label_scope: bool = False,
    context: bool = False,
    actual_pdf: bool = False,
    label_policy: str = FIGURE_LABEL_SCOPE,
) -> tuple[
    LocalDocumentStore,
    LocalDocumentStore,
    ProcessingScope,
    RetrievalMember,
    QualifiedFigurePair,
]:
    actual_pdf = actual_pdf or not label_scope
    if not actual_pdf:
        initial, raw_chart, raw_description = sample()
        pdf_bytes = b"independently-authored-source"
    else:
        pdf_bytes, initial, raw_chart, raw_description = authored_donut()
    sources, assets = (
        LocalDocumentStore(tmp_path / "source"),
        LocalDocumentStore(tmp_path / "semantic"),
    )
    pdf = sources.put(pdf_bytes, media_type="application/pdf")
    native = initial.crop_svg.split(">", 1)[1][:-6].encode()
    if actual_pdf:
        native = PdfspineDocumentAdapter().extract_document(pdf_bytes).pages[0].native_svg.encode()
    native_ref = sources.put(native, media_type="image/svg+xml")
    longest: dict[str, SvgElement] = {}
    for element in initial.svg.elements:
        assert element.source_span_id is not None
        old = longest.get(element.source_span_id)
        if old is None or len(old.text) < len(element.text):
            longest[element.source_span_id] = element
    sidecar = TextSidecar(
        "source-text-v1",
        pdf.sha256,
        0,
        tuple(TextSpan(key, element.text, element.anchor.bbox) for key, element in longest.items()),
    )
    if actual_pdf:
        sidecar = replace(sidecar, spans=initial.paint_text_spans)
    if context:
        sidecar = replace(
            sidecar,
            spans=(
                *sidecar.spans,
                TextSpan("footer", "Comparisons use constant FX.", (5.0, 152.0, 230.0, 158.0)),
            ),
        )
    text_ref = sources.put(
        TypeAdapter(TextSidecar).dump_json(sidecar), media_type="application/json"
    )
    manifest_id = sources.publish(
        DocumentManifest(
            "source-ingestion-v1",
            "authored.pdf",
            pdf,
            "authored-fixture",
            (PageRecord(0, 240.0, 160.0, 0, native_ref, text_ref, len(sidecar.spans), ()),),
            RegionRecord(0, initial.svg.source.bbox, native_ref, native_ref, text_ref, ()),
        )
    )
    prepared = prepare_figure(
        page=PageInput(manifest_id, pdf.sha256, 0, 240.0, 160.0, native_ref, sidecar),
        native_svg=native,
        bbox=initial.svg.source.bbox,
        region_id="authored-distribution",
        context_span_ids=("footer",) if context else (),
    )
    raw_chart = replace(raw_chart, binding=prepared.svg.binding)
    raw_description = replace(raw_description, binding=prepared.svg.binding)
    assert raw_chart.title is not None
    raw_description = replace(
        raw_description,
        claims=(
            *raw_description.claims,
            DescriptionClaim(raw_chart.title.text, raw_chart.title.evidence),
        ),
    )
    labels = qualify_source_labels(prepared.svg, raw_chart, raw_description, scope=label_policy)
    proof_ref = None
    if label_scope:
        pair = QualifiedFigurePair(
            labels.chart,
            labels.description,
            labels.receipt,
            labels.raw_chart_id,
            labels.raw_description_id,
            labels.excluded_claim_paths,
        )
    else:
        proof = build_source_paint_proof(pdf_bytes, prepared=prepared)
        proof_ref = assets.put(
            TypeAdapter(SourcePaintProof).dump_json(proof),
            media_type="application/json",
        )
        numeric = DonutQualification(prepared, source_paint=proof).qualify_pair(
            prepared.svg, raw_chart, raw_description
        )
        pair = replace(numeric, description=labels.description)
    raw_chart_ref = assets.put(
        TypeAdapter(ChartIR).dump_json(raw_chart), media_type="application/json"
    )
    raw_description_ref = assets.put(
        TypeAdapter(TextDescription).dump_json(raw_description),
        media_type="application/json",
    )
    view_ref = assets.put(
        TypeAdapter[object](type(prepared.model_view)).dump_json(prepared.model_view),
        media_type="application/json",
    )
    chart_ref = assets.put(
        TypeAdapter(ChartIR).dump_json(pair.chart), media_type="application/json"
    )
    description_ref = assets.put(
        TypeAdapter(TextDescription).dump_json(pair.description),
        media_type="application/json",
    )
    svg_ref = assets.put(prepared.svg.svg.encode(), media_type="image/svg+xml")
    receipt_fields = dict(
        object_id="chart",
        source_manifest_id=manifest_id,
        region_id="authored-distribution",
        ir=chart_ref,
        description=description_ref,
        source_svg=svg_ref,
        raw_chart=raw_chart_ref,
        raw_description=raw_description_ref,
        view=view_ref,
        qualification=pair.receipt,
    )
    receipt = (
        ChartPublicationReceipt.model_validate(receipt_fields)
        if proof_ref is None
        else NumericLabelPublicationReceipt.model_validate(
            {**receipt_fields, "source_paint_proof": proof_ref}
        )
    )
    receipt_ref = assets.put(receipt.model_dump_json().encode(), media_type="application/json")
    member = RetrievalMember(
        "chart",
        ObjectKind.CHART,
        0,
        chart_ref,
        description_ref,
        receipt_ref,
        description_ref,
        svg_ref,
        "test-embedding-not-called",
        1,
        lineage_refs=(raw_chart_ref, raw_description_ref, view_ref)
        + ((proof_ref,) if proof_ref else ()),
    )
    return (
        sources,
        assets,
        ProcessingScope(manifest_id, pdf.sha256, 1, (0,)),
        member,
        pair,
    )


def test_source_aware_chart_preflight_rebuilds_the_actual_view_and_projection(
    tmp_path: Path,
) -> None:
    sources, assets, scope, member, pair = member_fixture(tmp_path)
    chart, description, receipt = validate_chart_member(sources, assets, scope, member)
    assert (chart, description, receipt) == (pair.chart, pair.description, pair.receipt)


@pytest.mark.parametrize(
    "change", ["raw-branch", "view", "missing-raw", "qualified-value", "receipt"]
)
def test_plausible_immutable_references_cannot_hide_tampered_source_lineage(
    tmp_path: Path, change: str
) -> None:
    sources, assets, scope, member, _ = member_fixture(tmp_path)
    receipt = parse_chart_receipt(assets.get(member.qualification))
    lineage = member.lineage_refs
    if change == "raw-branch":
        raw = TypeAdapter(ChartIR).validate_json(assets.get(receipt.raw_chart), strict=True)
        first, second = raw.points
        raw = replace(raw, points=(replace(first, category=second.category), second))
        new_ref = assets.put(TypeAdapter(ChartIR).dump_json(raw), media_type="application/json")
        lineage = tuple(new_ref if ref == receipt.raw_chart else ref for ref in lineage)
        receipt = receipt.model_copy(update={"raw_chart": new_ref})
    elif change == "view":
        view = TypeAdapter(FigureModelView).validate_json(assets.get(receipt.view), strict=True)
        new_ref = assets.put(
            TypeAdapter(FigureModelView).dump_json(replace(view, source_manifest_id="f" * 64)),
            media_type="application/json",
        )
        lineage = tuple(new_ref if ref == receipt.view else ref for ref in lineage)
        receipt = receipt.model_copy(update={"view": new_ref})
    elif change == "missing-raw":
        assets.asset_path(receipt.raw_chart).unlink()
    elif change == "qualified-value":
        chart = TypeAdapter(ChartIR).validate_json(assets.get(member.ir), strict=True)
        first, second = chart.points
        assert first.value.value is not None
        chart = replace(
            chart,
            points=(
                replace(first, value=replace(first.value, value=first.value.value + 1)),
                second,
            ),
        )
        new_ref = assets.put(TypeAdapter(ChartIR).dump_json(chart), media_type="application/json")
        member = replace(member, ir=new_ref)
        receipt = receipt.model_copy(update={"ir": new_ref})
    else:
        receipt = receipt.model_copy(
            update={
                "qualification": replace(
                    receipt.qualification, method="self-reported model verified"
                )
            }
        )
    ref = assets.put(receipt.model_dump_json().encode(), media_type="application/json")
    member = replace(member, qualification=ref, lineage_refs=lineage)
    with pytest.raises((ValueError, FileNotFoundError)):
        validate_chart_member(sources, assets, scope, member)


def test_omitting_raw_inputs_from_manifest_closure_fails_before_admission(
    tmp_path: Path,
) -> None:
    sources, assets, scope, member, _ = member_fixture(tmp_path)
    with pytest.raises(ValueError, match="complete raw branch"):
        validate_chart_member(sources, assets, scope, replace(member, lineage_refs=()))


@pytest.mark.parametrize("label_scope,with_context", [(False, False), (True, False), (True, True)])
def test_durable_chart_search_embeds_only_qualified_description_and_hydrates_its_snapshot(
    tmp_path: Path,
    label_scope: bool,
    with_context: bool,
) -> None:
    sources, assets, scope, member, pair = member_fixture(
        tmp_path, label_scope=label_scope, context=with_context
    )
    receipt = parse_chart_receipt(assets.get(member.qualification))
    refs: tuple[tuple[str, AssetRef], ...] = (
        ("qualified_ir", member.ir),
        ("qualified_description", member.description),
        ("qualification", member.qualification),
        ("svg", member.source_svg),
        ("ir", receipt.raw_chart),
        ("description", receipt.raw_description),
        ("model_view", receipt.view),
    )
    if isinstance(receipt, NumericLabelPublicationReceipt):
        refs = (*refs, ("source_paint_proof", receipt.source_paint_proof))
    stages = tuple(
        StageOutcome(name, str(index) * 64, StageState.SUCCEEDED, "test-source-bound", ref)
        for index, (name, ref) in enumerate(refs, start=1)
    )
    record = ObjectProcessingRecord(member.object_id, ObjectKind.CHART, stages)

    class EmbeddingSpy:
        fingerprint = "test-qualified-description-only"
        texts: list[str]

        def __init__(self) -> None:
            self.texts = []

        def embed_description(self, text: str) -> tuple[float, ...]:
            self.texts.append(text)
            return (1.0, 0.0)

        def embed_query(self, text: str) -> tuple[float, ...]:
            return (1.0, 0.0)

    embedding = EmbeddingSpy()
    retrieval = ProcessingRetrieval(sources, ProcessingStore(assets.root), embedding)
    publication = retrieval.build(scope, ((0, record),))
    # Only qualified content is embedded: the index-text projection of the qualified IR
    # (title / period / grammar / points) when values are citable, else the description.
    expected_text = chart_index_text(pair.chart, fallback=pair.description.text)
    assert embedding.texts == [expected_text]
    if label_scope:
        assert expected_text == pair.description.text
    else:
        assert (
            expected_text.startswith(pair.description.text) and "Agency VONB 72%" in expected_text
        )
    reopened = ProcessingRetrieval(sources, ProcessingStore(assets.root), embedding)
    (hit,) = reopened.search(publication, "Agency share", limit=1)
    context = reopened.resolve(publication, hit)
    assert context.ir == pair.chart and context.description == pair.description
    assert context.snapshot_id == publication.snapshot_id
    if label_scope:
        assert context.scope == FIGURE_LABEL_SCOPE
        assert isinstance(context.ir, ChartIR)
        assert context.ir.verification is Verification.PENDING
        assert context.ir.axes == ()
        assert context.ir.points == ()
        assert context.ir.marks == ()
        assert context.ir.title is context.ir.period is None
        with pytest.raises(ValueError, match="financial"):
            require_financial_qualification(context)
    else:
        assert context.scope == "explicit-distribution-shares"
        require_financial_qualification(context)
    assert set(member.lineage_refs).issubset(set(publication.dependencies))
    with pytest.raises(ValueError, match="snapshot"):
        reopened.resolve(publication, replace(hit, snapshot_id="f" * 64))


@pytest.mark.parametrize(
    "change",
    [
        "numeric-projection",
        "unknown-scope",
        "context-text",
        "context-id",
        "context-page",
    ],
)
def test_label_scope_does_not_admit_forged_projection_or_context(
    tmp_path: Path, change: str
) -> None:
    sources, assets, scope, member, _ = member_fixture(tmp_path, label_scope=True, context=True)
    receipt = parse_chart_receipt(assets.get(member.qualification))
    if change == "numeric-projection":
        raw_chart = TypeAdapter(ChartIR).validate_json(assets.get(receipt.raw_chart), strict=True)
        new_ir = assets.put(
            TypeAdapter(ChartIR).dump_json(raw_chart), media_type="application/json"
        )
        receipt = receipt.model_copy(update={"ir": new_ir})
        member = replace(member, ir=new_ir)
    elif change == "unknown-scope":
        receipt = receipt.model_copy(
            update={
                "qualification": replace(
                    receipt.qualification, semantic_scope="self-promoted-financial-v1"
                )
            }
        )
    else:
        view = TypeAdapter(ContextualFigureModelView).validate_json(
            assets.get(receipt.view), strict=True
        )
        (old,) = view.page_context
        context = (
            replace(old, text="Comparisons use actual FX.")
            if change == "context-text"
            else replace(old, source_span_id="missing")
            if change == "context-id"
            else replace(old, source=replace(old.source, page_index=1))
        )
        changed = replace(view, page_context=(context,))
        view_ref = assets.put(
            TypeAdapter(ContextualFigureModelView).dump_json(changed),
            media_type="application/json",
        )
        member = replace(
            member,
            lineage_refs=tuple(
                view_ref if ref == receipt.view else ref for ref in member.lineage_refs
            ),
        )
        receipt = receipt.model_copy(update={"view": view_ref})
    receipt_ref = assets.put(receipt.model_dump_json().encode(), media_type="application/json")
    member = replace(member, qualification=receipt_ref)
    with pytest.raises(ValueError):
        validate_chart_member(sources, assets, scope, member)
