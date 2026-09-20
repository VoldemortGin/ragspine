"""Diagram relation labels retain exact source occurrences in stored typed IR."""

from dataclasses import FrozenInstanceError

import pytest
from pydantic import TypeAdapter

from enterprise_pdf_rag.processing.typed_ir import DiagramEdge


def test_diagram_edge_source_occurrences_survive_serialization_and_are_immutable() -> (
    None
):
    edge = DiagramEdge(
        "left", "right", "Flow", "directed", source_span_ids=("label-occurrence",)
    )
    assert (
        TypeAdapter(DiagramEdge).validate_json(TypeAdapter(DiagramEdge).dump_json(edge))
        == edge
    )
    with pytest.raises(FrozenInstanceError):
        type(edge).__setattr__(edge, "source_span_ids", ("other",))
