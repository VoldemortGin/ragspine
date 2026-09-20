"""A retrieval snapshot freezes all qualified semantic and vector dependencies."""

import json
from dataclasses import asdict, dataclass
from hashlib import sha256
from math import isfinite

from enterprise_pdf_rag.documents.models import AssetRef
from enterprise_pdf_rag.figures.models import FigureQualification, TextDescription
from enterprise_pdf_rag.processing.diagram_models import DiagramQualification
from enterprise_pdf_rag.processing.models import ObjectKind, ProcessingScope
from enterprise_pdf_rag.processing.typed_ir import (
    LiteralQualification,
    ObjectDescription,
    TypedIR,
)


def _identity(value: object) -> str:
    return sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class RetrievalMember:
    object_id: str
    kind: ObjectKind
    page_index: int
    ir: AssetRef
    description: AssetRef
    qualification: AssetRef
    embedding: AssetRef
    source_svg: AssetRef
    embedding_fingerprint: str
    embedding_dimensions: int
    lineage_refs: tuple[AssetRef, ...] = ()

    def __post_init__(self) -> None:
        if not self.object_id or not self.embedding_fingerprint or self.embedding_dimensions <= 0:
            raise ValueError("Retrieval members require explicit source and embedding identities")
        if len(set(self.lineage_refs)) != len(self.lineage_refs):
            raise ValueError("Retrieval lineage references must be unique")

    @property
    def member_id(self) -> str:
        return _identity({"schema": "retrieval-member-v1", "member": asdict(self)})


@dataclass(frozen=True, slots=True)
class RetrievalPlan:
    scope: ProcessingScope
    members: tuple[RetrievalMember, ...]
    qualification_policy: str
    index_version: str

    def __post_init__(self) -> None:
        if not self.qualification_policy or not self.index_version:
            raise ValueError("Retrieval qualification and index policies are required")
        if any(
            member.page_index not in self.scope.selected_page_indices for member in self.members
        ):
            raise ValueError("Retrieval member is outside selected processing pages")
        ids = tuple(member.member_id for member in self.members)
        if len(set(ids)) != len(ids):
            raise ValueError("Retrieval members must be unique")

    @property
    def snapshot_id(self) -> str:
        return _identity(
            {
                "schema": "retrieval-snapshot-v1",
                "scope": asdict(self.scope),
                "members": [
                    asdict(member)
                    for member in sorted(self.members, key=lambda item: item.member_id)
                ],
                "qualification_policy": self.qualification_policy,
                "index_version": self.index_version,
            }
        )


def retrieval_dependencies(plan: RetrievalPlan) -> tuple[AssetRef, ...]:
    refs = {
        ref
        for member in plan.members
        for ref in (
            member.ir,
            member.description,
            member.qualification,
            member.embedding,
            member.source_svg,
            *member.lineage_refs,
        )
    }
    return tuple(sorted(refs, key=lambda ref: (ref.sha256, ref.media_type, ref.byte_length)))


@dataclass(frozen=True, slots=True)
class PinnedRetrievalHit:
    snapshot_id: str
    member_id: str
    score: float

    def __post_init__(self) -> None:
        if not isfinite(self.score):
            raise ValueError("Retrieval score must be finite")


def resolve_member(plan: RetrievalPlan, hit: PinnedRetrievalHit) -> RetrievalMember:
    if hit.snapshot_id != plan.snapshot_id:
        raise ValueError("Retrieval hit belongs to another semantic snapshot")
    member = next((item for item in plan.members if item.member_id == hit.member_id), None)
    if member is None:
        raise ValueError("Retrieval hit is not a member of the pinned snapshot")
    return member


@dataclass(frozen=True, slots=True)
class RetrievalEmbedding:
    description_sha256: str
    fingerprint: str
    vector: tuple[float, ...]

    def __post_init__(self) -> None:
        if not self.vector or not all(isfinite(value) for value in self.vector):
            raise ValueError("Retrieval embeddings require finite nonempty vectors")


@dataclass(frozen=True, slots=True)
class IndexEntry:
    member_id: str
    vector: tuple[float, ...]


@dataclass(frozen=True, slots=True)
class RetrievalIndex:
    snapshot_id: str
    index_version: str
    entries: tuple[IndexEntry, ...]


@dataclass(frozen=True, slots=True)
class RetrievalContext:
    snapshot_id: str
    member: RetrievalMember
    ir: TypedIR
    description: ObjectDescription | TextDescription
    qualification: LiteralQualification | FigureQualification | DiagramQualification

    @property
    def scope(self) -> str:
        if isinstance(self.qualification, FigureQualification):
            return self.qualification.semantic_scope
        return self.qualification.scope


def require_financial_qualification(context: RetrievalContext) -> None:
    if context.scope not in (
        "financial-field-relations-v1",
        "explicit-distribution-shares",
    ):
        raise ValueError(
            "Literal source transcription does not qualify financial relationships or numerical answers"
        )
