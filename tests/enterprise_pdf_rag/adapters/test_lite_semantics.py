"""Lite ingest (ADR 0025): images and formulas send no call, a chart description derives from its IR."""

from pathlib import Path

import pytest
from pydantic import TypeAdapter

from enterprise_pdf_rag.adapters.chart_semantics import CHART_DESCRIPTION_FROM_IR
from enterprise_pdf_rag.adapters.processing_retrieval import eligibility
from enterprise_pdf_rag.adapters.processing_store import ProcessingStore
from ragspine.extraction.evidence.figures.models import TextDescription
from ragspine.extraction.evidence.page.models import ObjectKind, StageState
from tests.enterprise_pdf_rag.adapters.lite_ingest_helpers import (
    artifact_of,
    lite_env,
    mixed_folder,
    processing_store_of,
    published_manifest,
    records_of,
    run_mode,
    stages_of,
)


def test_a_lite_image_is_registered_skipped_and_still_not_retrievable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lite_env(monkeypatch)
    mixed_folder(tmp_path)

    manifest = published_manifest(run_mode(tmp_path, "lite"))

    (image,) = records_of(manifest, ObjectKind.IMAGE)
    stages = stages_of(image)
    for name in ("ir", "description"):
        assert stages[name].state is StageState.NOT_APPLICABLE
        assert "skipped_by_ingest_mode" in (stages[name].diagnostic or "")
    assert eligibility(image) == (False, "Image objects are not retrievable")


def test_a_lite_formula_qualifies_exactly_as_a_budget_exhausted_one_would(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lite_env(monkeypatch)
    mixed_folder(tmp_path)
    full = published_manifest(run_mode(tmp_path, "full", root="full"))

    lite = published_manifest(run_mode(tmp_path, "lite", root="lite"))

    lite_formulas, full_formulas = (
        records_of(lite, ObjectKind.FORMULA),
        records_of(full, ObjectKind.FORMULA),
    )
    assert len(lite_formulas) == len(full_formulas) == 2
    for lite_record, full_record in zip(lite_formulas, full_formulas, strict=True):
        assert eligibility(lite_record) == eligibility(full_record) == (True, None)
        lite_stages, full_stages = stages_of(lite_record), stages_of(full_record)
        # The proof reads no model byte: the qualified products are the same bytes.
        for name in ("qualified_ir", "qualified_description", "formula_observation"):
            assert lite_stages[name].artifact == full_stages[name].artifact
        assert lite_stages["ir"].state is StageState.NOT_APPLICABLE


def test_a_lite_chart_description_is_derived_from_its_ir_and_qualifies_the_same_points(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lite_env(monkeypatch)
    mixed_folder(tmp_path)
    full = published_manifest(run_mode(tmp_path, "full", root="full"))

    lite = published_manifest(run_mode(tmp_path, "lite", root="lite"))

    (lite_chart,), (full_chart,) = (
        records_of(lite, ObjectKind.CHART),
        records_of(full, ObjectKind.CHART),
    )
    assert eligibility(lite_chart) == (True, None)
    lite_stages, full_stages = stages_of(lite_chart), stages_of(full_chart)
    assert lite_stages["description"].producer.startswith("semantic-object-v2:")
    outputs = ProcessingStore(Path(processing_store_of(tmp_path, "lite")))
    raw = TypeAdapter(TextDescription).validate_json(
        outputs.assets.get(artifact_of(lite_stages["description"]))
    )
    assert raw.producer.startswith(CHART_DESCRIPTION_FROM_IR)
    assert all(claim.value is None for claim in raw.claims)
    # The IR call is the very same request in both modes, so the qualified projection — the
    # only source of a chart's numbers — is the same bytes.
    assert lite_stages["ir"].artifact == full_stages["ir"].artifact
    assert lite_stages["qualified_ir"].artifact == full_stages["qualified_ir"].artifact
    assert lite_chart.qualified_claim_count == full_chart.qualified_claim_count == 2
    full_outputs = ProcessingStore(Path(processing_store_of(tmp_path, "full")))
    texts = [
        sorted(
            claim.text
            for claim in TypeAdapter(TextDescription)
            .validate_json(store.assets.get(artifact_of(stages["qualified_description"])))
            .claims
        )
        for store, stages in ((outputs, lite_stages), (full_outputs, full_stages))
    ]
    assert texts[0] == texts[1]
