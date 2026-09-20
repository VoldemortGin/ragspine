"""Capture expectations are rebuilt from publication/source before any HTTP."""

from dataclasses import replace
from pathlib import Path

import pytest
from tests.adapters.chart_qa_bar_fixture import published_bar_input
from tests.processing.test_persistent_retrieval import RecordingEmbedding

from enterprise_pdf_rag.adapters.bar_publication import parse_displayed_bar_receipt
from enterprise_pdf_rag.adapters.chart_qa_bar_promotion import (
    create_displayed_bar_draft,
)
from enterprise_pdf_rag.adapters.chart_qa_v2_targets import (
    source_qualified_capture_target,
)


def test_target_requalifies_the_exact_source_member_and_raw_normalization_lineage(
    tmp_path: Path,
) -> None:
    sources, outputs, prior, item, source = published_bar_input(tmp_path)
    release = create_displayed_bar_draft(
        sources,
        outputs,
        RecordingEmbedding(),
        processing_id=prior,
        page_index=0,
        object_id=item.object_id,
    )
    target = source_qualified_capture_target(
        sources, outputs, pin=release.current, endpoint="http://127.0.0.1:18766"
    )
    checked = target.release
    assert checked is not None
    assert checked.pin.processing_id == release.current.processing_id
    assert checked.pin.member_id == release.current.member_id
    assert checked.raw_chart_ir_artifact_id == source.chart.artifact_id
    assert (
        checked.normalization.original_typed_description_artifact_id
        == source.previous_description.artifact_id
    )
    assert checked.normalization.binding == checked.binding
    assert checked.publication_receipt_sha256 == release.publication_receipt.sha256
    assert checked.source_paint_proof_sha256 == release.source_paint_proof.sha256
    assert checked.qualification_id == release.qualification_id
    assert outputs.load_current()[0] == prior
    with pytest.raises(ValueError, match="snapshot"):
        source_qualified_capture_target(
            sources,
            outputs,
            pin=replace(release.current, snapshot_id="f" * 64),
            endpoint="http://127.0.0.1:18766",
        )
    with pytest.raises(ValueError, match="loopback"):
        source_qualified_capture_target(
            sources, outputs, pin=release.current, endpoint="http://example.com:18766"
        )


def test_missing_raw_branch_cannot_be_hidden_by_an_existing_qualified_receipt(
    tmp_path: Path,
) -> None:
    sources, outputs, prior, item, _ = published_bar_input(tmp_path)
    release = create_displayed_bar_draft(
        sources,
        outputs,
        RecordingEmbedding(),
        processing_id=prior,
        page_index=0,
        object_id=item.object_id,
    )
    receipt = parse_displayed_bar_receipt(
        outputs.assets.get(release.publication_receipt)
    )
    outputs.assets.asset_path(receipt.raw_description_json).unlink()
    with pytest.raises((OSError, ValueError)):
        source_qualified_capture_target(
            sources, outputs, pin=release.current, endpoint="http://127.0.0.1:18766"
        )
