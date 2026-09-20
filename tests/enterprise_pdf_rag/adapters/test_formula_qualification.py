"""The formula qualification is model-free, and a published one only remounts by replaying it."""

from dataclasses import replace
from hashlib import sha256
from pathlib import Path

import pytest
from pydantic import TypeAdapter

from enterprise_pdf_rag.adapters.document_store import LocalDocumentStore
from enterprise_pdf_rag.adapters.draft_publication import index_draft, publish_draft, qualify_draft
from enterprise_pdf_rag.adapters.formula_qualification import (
    FORMULA_CONFIDENCE,
    FORMULA_PRODUCER,
    FORMULA_SCOPE,
    check_model_description,
    literal_agreement,
    parse_formula_receipt,
    qualify_formula,
    validate_formula_member,
)
from enterprise_pdf_rag.adapters.offline import OfflineDescriptionEmbedder
from enterprise_pdf_rag.adapters.pdfspine_document import PdfspineDocumentAdapter
from enterprise_pdf_rag.adapters.processing_store import ProcessingStore
from enterprise_pdf_rag.documents.models import AssetRef, Bounds, TextSidecar
from enterprise_pdf_rag.figures.models import Confidence, SourceAnchor, Verification
from enterprise_pdf_rag.processing.formula_models import FormulaSourceObservation
from enterprise_pdf_rag.processing.models import (
    LayoutObject,
    ObjectKind,
    PageInput,
    ProcessingScope,
)
from enterprise_pdf_rag.processing.retrieval import RetrievalMember
from enterprise_pdf_rag.processing.typed_ir import FormulaIR
from tests.enterprise_pdf_rag.adapters.formula_fixture import rise_formula_pdf
from tests.enterprise_pdf_rag.adapters.generic_publication_helpers import (
    ingest_generic_semantics,
)
from tests.enterprise_pdf_rag.adapters.test_pdf_ingestion import (
    FORMULA_FRACTION_BBOX,
    FORMULA_POWER_BBOX,
    authored_pdf,
)

WHOLE_FORMULA_BBOX: Bounds = (
    FORMULA_FRACTION_BBOX[0],
    FORMULA_FRACTION_BBOX[1],
    FORMULA_POWER_BBOX[2],
    FORMULA_FRACTION_BBOX[3],
)
LINEAR = "ROE = \\frac{Net\\ profit}{Equity}"
LITERAL = "ROE =\nNet profit\nEquity\nx\n2"


def _page_input(pdf: bytes) -> PageInput:
    extracted = PdfspineDocumentAdapter().extract_document(pdf).pages[0]
    svg = extracted.native_svg.encode()
    digest = sha256(pdf).hexdigest()
    return PageInput(
        "a" * 64,
        digest,
        0,
        extracted.width,
        extracted.height,
        AssetRef(sha256(svg).hexdigest(), "image/svg+xml", len(svg)),
        TextSidecar("source-text-v1", digest, 0, extracted.text_spans),
    )


def _object(page: PageInput, bbox: Bounds) -> LayoutObject:
    span_ids = tuple(
        span.span_id
        for span in page.text.spans
        if bbox[0] <= span.bbox[0]
        and bbox[1] <= span.bbox[1]
        and span.bbox[2] <= bbox[2]
        and span.bbox[3] <= bbox[3]
    )
    return LayoutObject(
        "formula-object",
        ObjectKind.FORMULA,
        bbox,
        span_ids,
        "Model-proposed formula region",
        Confidence(None, "layout inference pending"),
    )


def _model_ir(page: PageInput, literal: str | None) -> FormulaIR:
    return FormulaIR(
        "formula-object",
        SourceAnchor(page.source_sha256, page.source_sha256, 0, WHOLE_FORMULA_BBOX),
        literal,
        "ROE = \\frac{Net profit}{Equity}",
        tuple(span.span_id for span in page.text.spans),
        ("Normalized form state=inferred",),
    )


def test_qualify_formula_fixture_a_is_literal_level(tmp_path: Path) -> None:
    pdf = authored_pdf(
        tmp_path / "formula.pdf",
        page_count=1,
        label="Formula",
        embedded_font=True,
        formula_page=True,
    ).read_bytes()
    page = _page_input(pdf)

    result = qualify_formula(pdf, page=page, item=_object(page, WHOLE_FORMULA_BBOX), model_ir=None)

    assert result.diagnostics == ()
    ir = result.ir
    assert ir is not None and result.description is not None
    assert ir.proof_level == "literal"
    assert ir.verification is Verification.PENDING
    assert tuple(token.text for token in ir.tokens) == (
        "ROE",
        "=",
        "Net",
        "profit",
        "Equity",
        "x",
        "2",
    )
    # Only the typographic superscript is derived; every other token sits on the baseline.
    assert tuple(token.index for token in ir.tokens if token.script_proof == "derived") == (6,)
    assert ir.linear == LINEAR + " x^{2}"
    assert ir.readable == "ROE 等于 Net profit 除以 Equity x 上标 2"
    assert ir.source_literal == LITERAL
    assert result.description.text == ir.readable
    assert result.description.producer == FORMULA_PRODUCER
    assert result.description.confidence == FORMULA_CONFIDENCE
    assert result.description.verification is Verification.VERIFIED
    assert result.agreement == "unavailable"


def test_qualify_formula_fixture_b_is_full_level(tmp_path: Path) -> None:
    pdf = rise_formula_pdf(tmp_path / "rise.pdf").read_bytes()
    page = _page_input(pdf)

    result = qualify_formula(pdf, page=page, item=_object(page, WHOLE_FORMULA_BBOX), model_ir=None)

    ir = result.ir
    assert ir is not None
    assert ir.proof_level == "full"
    assert ir.verification is Verification.VERIFIED
    assert ir.tokens[6].script_proof == "text_rise"
    assert ir.tokens[6].script_evidence is not None
    assert ir.tokens[6].script_evidence.rise == 5.0
    assert ir.readable == "ROE 等于 Net profit 除以 Equity x 的 2 次方"


def test_literal_agreement_three_states(tmp_path: Path) -> None:
    pdf = authored_pdf(
        tmp_path / "formula.pdf",
        page_count=1,
        label="Formula",
        embedded_font=True,
        formula_page=True,
    ).read_bytes()
    page = _page_input(pdf)
    item = _object(page, WHOLE_FORMULA_BBOX)
    proven = qualify_formula(pdf, page=page, item=item, model_ir=None).ir
    assert proven is not None

    assert literal_agreement(_model_ir(page, "ROE = Net profit Equity x 2"), proven) == "agrees"
    assert literal_agreement(_model_ir(page, "ROE = Net income / Equity"), proven) == "disagrees"
    assert literal_agreement(_model_ir(page, None), proven) == "unavailable"
    assert literal_agreement(None, proven) == "unavailable"


def test_check_model_description_flags_symbols_not_in_tokens(tmp_path: Path) -> None:
    pdf = authored_pdf(
        tmp_path / "formula.pdf",
        page_count=1,
        label="Formula",
        embedded_font=True,
        formula_page=True,
    ).read_bytes()
    page = _page_input(pdf)
    item = _object(page, WHOLE_FORMULA_BBOX)
    result = qualify_formula(pdf, page=page, item=item, model_ir=None)
    assert result.ir is not None and result.description is not None

    written = replace(result.description, text="ROE = Net profit ÷ Equity")
    assert check_model_description(written, result.ir) == ("formula_model_symbol_not_a_token:÷",)
    # The proof's own readable transcription only uses proven token texts and fixed words.
    assert check_model_description(result.description, result.ir) == ()
    assert check_model_description(None, result.ir) == ("formula_model_description_unavailable",)


def _published(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[LocalDocumentStore, ProcessingStore, ProcessingScope, RetrievalMember]:
    """Ingest, qualify, index and publish the two authored formula regions, offline."""
    ingest, _ = ingest_generic_semantics(tmp_path, monkeypatch, formula_page=True)
    source_store = Path(ingest.source_store)
    processing_store = Path(ingest.processing_store)
    qualify_draft(
        source_store=source_store,
        processing_store=processing_store,
        processing_id=ingest.processing_id,
    )
    indexed = index_draft(
        source_store=source_store,
        processing_store=processing_store,
        processing_id=ingest.processing_id,
        embedder=OfflineDescriptionEmbedder(),
    )
    published = publish_draft(
        source_store=source_store,
        processing_store=processing_store,
        processing_id=indexed.indexed_processing_id,
    )
    sources = LocalDocumentStore(source_store)
    outputs = ProcessingStore(processing_store)
    manifest = outputs.load(published.published_processing_id)
    assert manifest.retrieval is not None
    plan, _ = outputs.load_retrieval(manifest.retrieval)
    fraction = next(
        item
        for item in plan.members
        if item.kind is ObjectKind.FORMULA
        and TypeAdapter(FormulaIR).validate_json(outputs.assets.get(item.ir)).linear == LINEAR
    )
    return sources, outputs, plan.scope, fraction


def test_validate_formula_member_replays_and_refuses_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pinned PDF bytes are content addressed, so drift is injected into the receipt's
    own pinned observation instead: the replay re-derives it and refuses the difference."""
    sources, outputs, scope, member = _published(tmp_path, monkeypatch)

    ir, description, qualification = validate_formula_member(sources, outputs.assets, scope, member)
    assert ir.linear == LINEAR
    assert description.text == "ROE 等于 Net profit 除以 Equity"
    assert qualification.scope == FORMULA_SCOPE
    assert qualification.proof_level == "full"
    assert validate_formula_member(sources, outputs.assets, scope, member) == (
        ir,
        description,
        qualification,
    )

    receipt = parse_formula_receipt(outputs.assets.get(member.qualification))
    token = ir.tokens[0]
    rewritten = outputs.assets.put(
        TypeAdapter(FormulaIR).dump_json(
            replace(ir, tokens=(replace(token, text="ROI"), *ir.tokens[1:]))
        ),
        media_type="application/json",
    )
    forged = outputs.assets.put(
        receipt.model_copy(
            update={
                "qualification": replace(receipt.qualification, ir=rewritten),
            }
        )
        .model_dump_json()
        .encode(),
        media_type="application/json",
    )
    with pytest.raises(ValueError, match="differs from independent source qualification"):
        validate_formula_member(
            sources, outputs.assets, scope, replace(member, ir=rewritten, qualification=forged)
        )

    observed = TypeAdapter(FormulaSourceObservation).validate_json(
        outputs.assets.get(receipt.qualification.observation)
    )
    drifted = outputs.assets.put(
        TypeAdapter(FormulaSourceObservation).dump_json(replace(observed, paths=())),
        media_type="application/json",
    )
    with_drift = outputs.assets.put(
        receipt.model_copy(
            update={"qualification": replace(receipt.qualification, observation=drifted)}
        )
        .model_dump_json()
        .encode(),
        media_type="application/json",
    )
    with pytest.raises(ValueError, match="observation differs from the pinned PDF"):
        validate_formula_member(
            sources,
            outputs.assets,
            scope,
            replace(
                member,
                qualification=with_drift,
                lineage_refs=(drifted, *receipt.qualification.lineage),
            ),
        )

    with pytest.raises(ValueError, match="observation and recorded model lineage"):
        validate_formula_member(
            sources, outputs.assets, scope, replace(member, lineage_refs=member.lineage_refs[:1])
        )
