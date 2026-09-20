"""An authored formula page travels ingest → qualify → index → publish → retrieve → answer."""

import re
from pathlib import Path

import pytest
from pydantic import TypeAdapter

from enterprise_pdf_rag.adapters.answer_service import AnswerService
from enterprise_pdf_rag.adapters.document_store import LocalDocumentStore
from enterprise_pdf_rag.adapters.draft_publication import index_draft, publish_draft, qualify_draft
from enterprise_pdf_rag.adapters.formula_qualification import (
    FORMULA_SCOPE,
    parse_formula_receipt,
)
from enterprise_pdf_rag.adapters.offline import OfflineDescriptionEmbedder
from enterprise_pdf_rag.adapters.pdf_ingestion import IngestionSummary, ingest_pdf
from enterprise_pdf_rag.adapters.processing_retrieval import ProcessingRetrieval, member_text
from enterprise_pdf_rag.adapters.processing_store import ProcessingStore
from enterprise_pdf_rag.answers.models import AbstainReason, AnswerRequest, AnswerStatus, ClaimKind
from enterprise_pdf_rag.answers.prompt import ModelAnswer, ModelClaim
from enterprise_pdf_rag.figures.models import Verification
from enterprise_pdf_rag.processing.context_builder import BlockKind, build_context_block
from enterprise_pdf_rag.processing.formula_models import FormulaQualification
from enterprise_pdf_rag.processing.models import (
    ObjectKind,
    ObjectProcessingRecord,
    ProcessingManifest,
    StageState,
)
from enterprise_pdf_rag.processing.retrieval import RetrievalPlan
from enterprise_pdf_rag.processing.typed_ir import FormulaIR
from tests.enterprise_pdf_rag.adapters.formula_fixture import rise_formula_pdf
from tests.enterprise_pdf_rag.adapters.generic_publication_helpers import (
    PROVIDER_BASE_URL,
    ingest_generic_semantics,
    text_partition_sender,
)
from tests.enterprise_pdf_rag.answers.fake_llm import answered, scripted_client
from tests.enterprise_pdf_rag.answers.store_mounted_document import StoreMountedDocument

_MEMBER_LINE = re.compile(r"^\[member ([0-9a-f]{64})\] kind=(\w+)", re.MULTILINE)
LINEAR = "ROE = \\frac{Net\\ profit}{Equity}"
READABLE = "ROE 等于 Net profit 除以 Equity"
INDEX_TEXT = f"{READABLE} {LINEAR} formula ROE = Net profit Equity"
QUESTION = "ROE 怎么算?"
FORMULA_STAGES = (
    "formula_observation",
    "qualified_ir",
    "qualified_description",
    "qualification",
    "qualification_exclusions",
)


def _formula_records(manifest: ProcessingManifest) -> tuple[ObjectProcessingRecord, ...]:
    return tuple(
        item for page in manifest.pages for item in page.objects if item.kind is ObjectKind.FORMULA
    )


def _stage_state(record: ObjectProcessingRecord, stage: str) -> StageState:
    return next(item.state for item in record.stages if item.stage == stage)


def _stage_diagnostic(record: ObjectProcessingRecord, stage: str) -> str:
    return next(item.diagnostic or "" for item in record.stages if item.stage == stage)


def _ir_of(outputs: ProcessingStore, record: ObjectProcessingRecord) -> FormulaIR:
    ref = next(item.artifact for item in record.stages if item.stage == "qualified_ir")
    assert ref is not None
    return TypeAdapter(FormulaIR).validate_json(outputs.assets.get(ref))


def _published(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    max_live_calls: int | None = None,
) -> tuple[LocalDocumentStore, ProcessingStore, str, ProcessingManifest, RetrievalPlan]:
    ingest, _ = ingest_generic_semantics(
        tmp_path, monkeypatch, formula_page=True, max_live_calls=max_live_calls
    )
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
    return sources, outputs, published.published_processing_id, manifest, plan


def test_formula_pdf_ingest_qualify_index_publish_retrieve_offline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ingest, calls = ingest_generic_semantics(tmp_path, monkeypatch, formula_page=True)
    assert ingest.failed_stage_count == 0
    # Three layout calls plus the typed and natural-language branch of each formula region.
    assert len(calls) == 7
    source_store = Path(ingest.source_store)
    processing_store = Path(ingest.processing_store)
    outputs = ProcessingStore(processing_store)
    records = _formula_records(outputs.load(ingest.processing_id))
    assert len(records) == 2
    for record in records:
        for stage in FORMULA_STAGES:
            assert _stage_state(record, stage) is StageState.SUCCEEDED, (record.object_id, stage)
    levels = {_ir_of(outputs, record).proof_level for record in records}
    assert levels == {"full", "literal"}
    fraction = next(record for record in records if _ir_of(outputs, record).linear == LINEAR)
    power = next(record for record in records if record is not fraction)
    # The fraction is proven end to end; the typographic superscript is only literal.
    assert _ir_of(outputs, fraction).verification is Verification.VERIFIED
    assert _ir_of(outputs, power).verification is Verification.PENDING
    assert _ir_of(outputs, power).linear == "x^{2}"
    assert fraction.qualified_claim_count == 5

    draft = qualify_draft(
        source_store=source_store,
        processing_store=processing_store,
        processing_id=ingest.processing_id,
    )
    assert draft.kinds == {"Formula": 2, "Text": 3}
    assert draft.skipped_reasons == {}
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
    manifest = outputs.load(published.published_processing_id)
    assert manifest.retrieval is not None
    plan, _ = outputs.load_retrieval(manifest.retrieval)
    assert plan.qualification_policy == "source-transcription-and-scoped-chart-qualification-v5"
    member = next(
        item
        for item in plan.members
        if item.kind is ObjectKind.FORMULA
        and TypeAdapter(FormulaIR).validate_json(outputs.assets.get(item.ir)).linear == LINEAR
    )
    assert member_text(outputs.assets, plan, member) == INDEX_TEXT

    retrieval = ProcessingRetrieval(sources, outputs, OfflineDescriptionEmbedder())
    hits = retrieval.search(manifest.retrieval, QUESTION, limit=5)
    assert hits[0].member_id == member.member_id
    context = retrieval.resolve(manifest.retrieval, hits[0])
    assert isinstance(context.qualification, FormulaQualification)
    assert context.scope == FORMULA_SCOPE
    block = build_context_block(context)
    assert block.prompt_text().split("\n") == [
        f"[member {member.member_id}] kind=formula page_index=2 "
        f"scope={FORMULA_SCOPE} verification=verified",
        "formula proof_level=full",
        f"formula.linear: {LINEAR}",
        f"formula.readable: {READABLE}",
        "tokens.0: ROE  (role=operand, script=base, proof=none)",
        "tokens.1: =  (role=relation, script=base, proof=none)",
        "tokens.2: Net  (role=operand, script=base, proof=none)",
        "tokens.3: profit  (role=operand, script=base, proof=none)",
        "tokens.4: Equity  (role=operand, script=base, proof=none)",
    ]


def _formula_member_id(prompt: str) -> str:
    members = [str(member) for member, kind in _MEMBER_LINE.findall(prompt) if kind == "formula"]
    return members[0]


def _answer(
    tmp_path: Path,
    document: StoreMountedDocument,
    claim: tuple[str, str],
    *,
    answer: str,
    name: str,
) -> tuple[AnswerStatus, object]:
    field_path, text = claim

    def script(prompt: str) -> ModelAnswer:
        return answered(
            answer,
            ModelClaim(
                claim_id="c1",
                member_id=_formula_member_id(prompt),
                kind="formula",
                field_path=field_path,
                text=text,
            ),
        )

    client, prompts = scripted_client(tmp_path / name, script)
    service = AnswerService({document.source_sha256: document}, client)
    result = service.answer(AnswerRequest(QUESTION))
    assert len(prompts) == 1 and result.llm_live_calls == 1
    return result.status, result


def test_formula_answer_cites_linear_and_rejects_non_verbatim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sources, outputs, processing_id, _manifest, _plan = _published(tmp_path, monkeypatch)
    document = StoreMountedDocument(
        sources,
        outputs,
        processing_id=processing_id,
        embedder=OfflineDescriptionEmbedder(),
    )

    status, result = _answer(
        tmp_path,
        document,
        ("formula.linear", LINEAR),
        answer=f"公式是 {LINEAR}。",
        name="llm-linear",
    )
    assert status is AnswerStatus.ANSWERED
    (claim,) = result.claims  # type: ignore[attr-defined]
    assert claim.kind is ClaimKind.FORMULA
    (citation,) = claim.citations
    assert citation.kind is BlockKind.FORMULA
    assert len(citation.evidence_ids) == 3
    assert all(item.startswith("span-v1-") for item in citation.evidence_ids)
    assert citation.bbox is None and citation.quote == LINEAR

    status, rejected = _answer(
        tmp_path,
        document,
        ("formula.linear", "ROE = Net profit / Equity"),
        answer="公式是 ROE = Net profit / Equity。",
        name="llm-loose",
    )
    assert status is AnswerStatus.ABSTAINED
    assert rejected.abstain_reason is AbstainReason.CLAIM_NOT_IN_EVIDENCE  # type: ignore[attr-defined]
    assert rejected.rejected[0].detail == "claim text differs from the formula line"  # type: ignore[attr-defined]

    status, token = _answer(
        tmp_path,
        document,
        ("tokens.4", "Equity"),
        answer="分母是 Equity。",
        name="llm-token",
    )
    assert status is AnswerStatus.ANSWERED
    (token_claim,) = token.claims  # type: ignore[attr-defined]
    assert token_claim.citations[0].bbox is not None
    assert token_claim.citations[0].field_path == "tokens.4"


def test_formula_without_rule_is_withheld_and_stays_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ingest, _ = ingest_generic_semantics(
        tmp_path, monkeypatch, formula_page=True, formula_rule=False
    )
    outputs = ProcessingStore(Path(ingest.processing_store))
    records = _formula_records(outputs.load(ingest.processing_id))
    withheld = next(
        record
        for record in records
        if _stage_state(record, "qualification") is StageState.UNAVAILABLE
    )
    assert "formula_multiline_unsupported" in _stage_diagnostic(withheld, "qualification")
    assert all(stage.stage != "qualified_ir" for stage in withheld.stages)

    draft = qualify_draft(
        source_store=Path(ingest.source_store),
        processing_store=Path(ingest.processing_store),
        processing_id=ingest.processing_id,
    )
    assert draft.skipped_reasons == {
        "Formula tokens are not source-proven; only proven formulas are retrievable": 1
    }
    indexed = index_draft(
        source_store=Path(ingest.source_store),
        processing_store=Path(ingest.processing_store),
        processing_id=ingest.processing_id,
        embedder=OfflineDescriptionEmbedder(),
    )
    assert indexed.member_count == 4


def test_formula_qualifies_when_model_branches_are_budget_exhausted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The proof reads no model, so a starved visual branch cannot withhold a formula."""
    _sources, outputs, processing_id, _manifest, plan = _published(
        tmp_path, monkeypatch, max_live_calls=3
    )
    records = _formula_records(outputs.load(processing_id))
    assert len(records) == 2
    for record in records:
        assert _stage_state(record, "ir") is StageState.FAILED
        assert _stage_state(record, "description") is StageState.FAILED
        for stage in FORMULA_STAGES:
            assert _stage_state(record, stage) is StageState.SUCCEEDED

    member = next(item for item in plan.members if item.kind is ObjectKind.FORMULA)
    receipt = parse_formula_receipt(outputs.assets.get(member.qualification)).qualification
    assert receipt.model_literal_agreement == "unavailable"
    # Only the locally rendered model view survived the budget; both model branches failed.
    assert len(receipt.lineage) == 1
    assert set(member.lineage_refs) == {receipt.observation, *receipt.lineage}


def _ingest_rise_formula(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> IngestionSummary:
    for key, value in {
        "OPENAI_API_KEY": "offline-secret",
        "OPENAI_BASE_URL": PROVIDER_BASE_URL,
        "OPENAI_MODEL": "offline-test",
    }.items():
        monkeypatch.setenv(key, value)
    pdf = rise_formula_pdf(tmp_path / "rise-formula.pdf")
    calls: list[bytes] = []
    monkeypatch.setattr(
        "enterprise_pdf_rag.adapters.json_completion._send_once",
        text_partition_sender(calls, formula_page=True),
    )
    return ingest_pdf(
        pdf=pdf, stage="semantics", max_live_calls=5, output_dir=tmp_path / "ingestion"
    )


def test_rise_formula_pdf_reaches_full_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ingest = _ingest_rise_formula(tmp_path, monkeypatch)
    outputs = ProcessingStore(Path(ingest.processing_store))
    records = _formula_records(outputs.load(ingest.processing_id))
    power = next(record for record in records if _ir_of(outputs, record).linear == "x^{2}")
    ir = _ir_of(outputs, power)
    assert ir.proof_level == "full"
    assert ir.verification is Verification.VERIFIED
    assert ir.tokens[1].script_proof == "text_rise"
    assert ir.readable == "x 的 2 次方"
