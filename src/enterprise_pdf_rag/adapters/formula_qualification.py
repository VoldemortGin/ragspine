"""Orchestrate, receipt and replay the model-free formula proof.

``qualify_formula`` observes the pinned PDF and runs ``check_formula``; nothing a model
returned reaches the qualified products. The model's own literal and description are
compared against the proof only to record agreement, never to admit or withhold.
"""

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, TypeAdapter

from enterprise_pdf_rag.adapters.aia_ingestion import read_text_sidecar
from enterprise_pdf_rag.adapters.document_store import LocalDocumentStore
from enterprise_pdf_rag.adapters.pdfspine_formula import observe_formula
from enterprise_pdf_rag.adapters.pdfspine_svg import crop_native_svg
from enterprise_pdf_rag.figures.models import Confidence, SourceAnchor, Verification
from enterprise_pdf_rag.processing.formula_models import (
    FormulaQualification,
    FormulaSourceObservation,
    TokenRole,
)
from enterprise_pdf_rag.processing.formula_rules import check_formula, role_of
from enterprise_pdf_rag.processing.models import (
    LayoutObject,
    ObjectKind,
    PageInput,
    ProcessingScope,
)
from enterprise_pdf_rag.processing.retrieval import RetrievalMember
from enterprise_pdf_rag.processing.typed_ir import FormulaIR, ObjectDescription

FORMULA_SCOPE = "formula-source-tokens-v1"
FORMULA_PRODUCER = "exact-formula-transcription-v1"
FORMULA_CONFIDENCE = Confidence(
    None,
    "deterministic formula token transcription; structure from source paths; "
    "typographic scripts marked derived when unproven",
)
FORMULA_RECEIPT_SCHEMA = "source-formula-qualification-v1"

type LiteralAgreement = Literal["agrees", "disagrees", "unavailable"]

_SYMBOL_ROLES = frozenset({TokenRole.RELATION, TokenRole.OPERATOR, TokenRole.GREEK})


class FormulaPublicationReceipt(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    schema_version: Literal["source-formula-qualification-v1"] = "source-formula-qualification-v1"
    qualification: FormulaQualification


def parse_formula_receipt(data: bytes) -> FormulaPublicationReceipt:
    return TypeAdapter(FormulaPublicationReceipt).validate_json(data, strict=True)


@dataclass(frozen=True, slots=True)
class FormulaQualificationResult:
    observation: FormulaSourceObservation
    ir: FormulaIR | None
    description: ObjectDescription | None
    agreement: LiteralAgreement
    diagnostics: tuple[str, ...]


def _folded(text: str) -> str:
    return "".join(text.split())


def literal_agreement(model_ir: FormulaIR | None, proven: FormulaIR) -> LiteralAgreement:
    """Informational only: the model's cited literal against the proven span concatenation."""
    if model_ir is None or model_ir.source_literal is None:
        return "unavailable"
    if _folded(model_ir.source_literal) == _folded(proven.source_literal or ""):
        return "agrees"
    return "disagrees"


def check_model_description(
    description: ObjectDescription | None, ir: FormulaIR
) -> tuple[str, ...]:
    """Diagnostics only: every symbol-like word a model wrote must be a proven token text."""
    if description is None:
        return ("formula_model_description_unavailable",)
    texts = {token.text for token in ir.tokens}
    diagnostics: list[str] = []
    for word in description.text.split():
        if word in texts:
            continue
        if any(char.isdigit() or role_of(char) in _SYMBOL_ROLES for char in word):
            diagnostics.append(f"formula_model_symbol_not_a_token:{word}")
    return tuple(dict.fromkeys(diagnostics))


def qualify_formula(
    pdf: bytes, *, page: PageInput, item: LayoutObject, model_ir: FormulaIR | None
) -> FormulaQualificationResult:
    """Observe, prove and project one Formula object; a refused proof yields no products."""
    observation = observe_formula(pdf, page=page, item=item)
    anchor = SourceAnchor(page.source_sha256, page.source_sha256, page.page_index, item.bbox)
    check = check_formula(observation, object_id=item.object_id, anchor=anchor)
    if check.ir is None:
        return FormulaQualificationResult(observation, None, None, "unavailable", check.diagnostics)
    assert check.ir.readable is not None  # a proven IR always carries its readable form
    description = ObjectDescription(
        item.object_id,
        anchor,
        check.ir.source_span_ids,
        check.ir.readable,
        FORMULA_PRODUCER,
        FORMULA_CONFIDENCE,
        Verification.VERIFIED,
    )
    return FormulaQualificationResult(
        observation,
        check.ir,
        description,
        literal_agreement(model_ir, check.ir),
        check.diagnostics,
    )


def validate_formula_member(
    sources: LocalDocumentStore,
    assets: LocalDocumentStore,
    scope: ProcessingScope,
    member: RetrievalMember,
) -> tuple[FormulaIR, ObjectDescription, FormulaQualification]:
    """No model/network calls: re-observe the pinned PDF, repeat the proof, compare everything."""
    receipt = parse_formula_receipt(assets.get(member.qualification)).qualification
    ir = TypeAdapter(FormulaIR).validate_json(assets.get(member.ir), strict=True)
    description = TypeAdapter(ObjectDescription).validate_json(
        assets.get(member.description), strict=True
    )
    if member.kind is not ObjectKind.FORMULA or (
        receipt.object_id,
        receipt.source_manifest_id,
        receipt.ir,
        receipt.description,
        receipt.source_svg,
        receipt.scope,
    ) != (
        member.object_id,
        scope.source_manifest_id,
        member.ir,
        member.description,
        member.source_svg,
        FORMULA_SCOPE,
    ):
        raise ValueError("Formula qualification does not match its retrieval member")
    if description.producer != FORMULA_PRODUCER or description.confidence != FORMULA_CONFIDENCE:
        raise ValueError("Formula qualification requires the deterministic transcription producer")
    source = sources.load(scope.source_manifest_id)
    if (
        source.manifest.source.sha256 != scope.source_sha256
        or member.page_index not in scope.selected_page_indices
    ):
        raise ValueError("Formula projection is outside the pinned source scope")
    source_page = source.manifest.pages[member.page_index]
    page = PageInput(
        scope.source_manifest_id,
        scope.source_sha256,
        member.page_index,
        source_page.width,
        source_page.height,
        source_page.svg,
        read_text_sidecar(sources, source, member.page_index),
    )
    item = LayoutObject(
        member.object_id,
        ObjectKind.FORMULA,
        receipt.source.bbox,
        receipt.source_span_ids,
        "replay",
        Confidence(None, "replay of a pinned formula object"),
    )
    observation = observe_formula(sources.get(source.manifest.source), page=page, item=item)
    if observation != TypeAdapter(FormulaSourceObservation).validate_json(
        assets.get(receipt.observation), strict=True
    ):
        raise ValueError("Formula source observation differs from the pinned PDF")
    check = check_formula(observation, object_id=member.object_id, anchor=receipt.source)
    expected_description = (
        None
        if check.ir is None or check.ir.readable is None
        else ObjectDescription(
            member.object_id,
            receipt.source,
            check.ir.source_span_ids,
            check.ir.readable,
            FORMULA_PRODUCER,
            FORMULA_CONFIDENCE,
            Verification.VERIFIED,
        )
    )
    if (
        check.ir != ir
        or expected_description != description
        or (
            receipt.proof_level,
            receipt.token_count,
            receipt.structure_count,
            receipt.derived_script_token_indices,
        )
        != (
            ir.proof_level,
            len(ir.tokens),
            len(ir.structures),
            tuple(token.index for token in ir.tokens if token.script_proof == "derived"),
        )
    ):
        raise ValueError(
            "Formula projection or receipt differs from independent source qualification"
        )
    expected_crop = crop_native_svg(
        sources.get(source_page.svg).decode(),
        width=source_page.width,
        height=source_page.height,
        bbox=receipt.source.bbox,
    ).encode()
    if assets.get(member.source_svg) != expected_crop:
        raise ValueError("Formula SVG crop does not derive from the pinned source page and anchor")
    if set(member.lineage_refs) != {receipt.observation, *receipt.lineage} or len(
        member.lineage_refs
    ) != 1 + len(receipt.lineage):
        raise ValueError("Formula publication requires its observation and recorded model lineage")
    return ir, description, receipt
