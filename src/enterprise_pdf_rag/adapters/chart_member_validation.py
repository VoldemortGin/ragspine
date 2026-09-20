"""Select a versioned source validator; a policy name alone never grants admission."""

from typing import Literal

from pydantic import BaseModel, ConfigDict

from enterprise_pdf_rag.adapters.bar_publication import resolve_displayed_bar_member
from enterprise_pdf_rag.adapters.chart_publication import validate_chart_member
from enterprise_pdf_rag.adapters.document_store import LocalDocumentStore
from enterprise_pdf_rag.figures.models import (
    ChartIR,
    FigureQualification,
    TextDescription,
)
from enterprise_pdf_rag.processing.models import ProcessingScope
from enterprise_pdf_rag.processing.retrieval import RetrievalMember

BAR_RECEIPT_SCHEMA = "source-displayed-bar-qualification-v1"


class _PolicyHeader(BaseModel):
    # This reads only a discriminator. The selected validator strictly checks
    # the complete receipt, replays source evidence and compares every projection.
    model_config = ConfigDict(strict=True, frozen=True, extra="ignore")
    schema_version: Literal[
        "source-chart-qualification-v1",
        "source-chart-numeric-label-index-v1",
        "source-displayed-bar-qualification-v1",
    ]


def uses_displayed_bar_policy(payload: bytes) -> bool:
    return (
        _PolicyHeader.model_validate_json(payload).schema_version == BAR_RECEIPT_SCHEMA
    )


def validate_retrieval_chart_member(
    sources: LocalDocumentStore,
    assets: LocalDocumentStore,
    scope: ProcessingScope,
    member: RetrievalMember,
) -> tuple[ChartIR, TextDescription, FigureQualification]:
    if uses_displayed_bar_policy(assets.get(member.qualification)):
        validated = resolve_displayed_bar_member(sources, assets, scope, member)
        return validated.chart, validated.description, validated.qualification
    return validate_chart_member(sources, assets, scope, member)
