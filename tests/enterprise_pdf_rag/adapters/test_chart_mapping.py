"""Cached chart DTOs retain valid slices without inventing missing occurrences."""

import json
from decimal import Decimal
from pathlib import Path

import pytest

from enterprise_pdf_rag.adapters.chart_semantic_schemas import (
    AxisDTO,
    ChartObservationsDTO,
    ChartPointDTO,
    EvidenceDTO,
    MarkDTO,
    NumericDTO,
    TextFieldDTO,
)
from enterprise_pdf_rag.adapters.chart_semantics import (
    ChartInference,
    ModelChartExtractor,
)
from enterprise_pdf_rag.adapters.figure_reasoning import PreparedFigure
from enterprise_pdf_rag.figures.models import ValueKind, Verification
from ragspine.common.evidence.providers.json_completion import JsonCompletionClient
from ragspine.common.evidence.providers.providers import load_llm_config
from tests.enterprise_pdf_rag.adapters.test_chart_semantics import _prepared


def _field(prepared: PreparedFigure, text: str) -> TextFieldDTO:
    element = next(element for element in prepared.svg.elements if element.text == text)
    return TextFieldDTO(
        text=text,
        evidence=EvidenceDTO(element_ids=(element.element_id,), confidence="high"),
    )


def _point(prepared: PreparedFigure, point_id: str = "good") -> ChartPointDTO:
    return ChartPointDTO(
        point_id=point_id,
        series=_field(prepared, "VONB"),
        category=_field(prepared, "Agency"),
        unit=_field(prepared, "%"),
        value=NumericDTO(value="72", kind="explicit", evidence=_field(prepared, "72").evidence),
    )


def _dto(prepared: PreparedFigure, *points: ChartPointDTO) -> ChartObservationsDTO:
    return ChartObservationsDTO(
        schema_version="chart-observations-v1",
        svg_digest=prepared.svg.digest,
        grammar="bar",
        title=None,
        period=None,
        axes=(),
        points=points,
        marks=(),
        diagnostics=(),
    )


def _infer(tmp_path: Path, prepared: PreparedFigure, dto: ChartObservationsDTO) -> ChartInference:
    sends: list[bytes] = []

    def sender(url: str, *, api_key: str, payload: bytes, timeout: float) -> bytes:
        sends.append(payload)
        return json.dumps(
            {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": dto.model_dump_json()},
                    }
                ]
            }
        ).encode()

    client = JsonCompletionClient(
        load_llm_config(
            {
                "OPENAI_API_KEY": "test",
                "OPENAI_BASE_URL": "https://test.invalid",
                "OPENAI_MODEL": "test",
            }
        ),
        cache_dir=tmp_path,
        max_live_calls=1,
        sender=sender,
    )
    extractor = ModelChartExtractor(client, prepared)
    result = extractor.infer(prepared.svg)
    replay = extractor.infer(prepared.svg)
    assert len(sends) == 1 and replay.completion.cache_hit
    assert replay.chart == result.chart
    assert result.completion.parsed == dto
    return result


def test_one_missing_occurrence_omits_only_affected_members_without_borrowing_evidence(
    tmp_path: Path,
) -> None:
    prepared = _prepared()
    good = _point(prepared)
    missing = EvidenceDTO(element_ids=("unknown-source-occurrence",), confidence="high")
    bad = _point(prepared, "bad").model_copy(
        update={"unit": TextFieldDTO(text="%", evidence=missing)}
    )
    axis = AxisDTO(
        axis_id="y",
        label=TextFieldDTO(text="Unknown", evidence=missing),
        unit=good.unit,
        scale="unknown",
    )
    mark = MarkDTO(
        mark_id="bad-mark",
        kind="bar",
        bbox=(1.0, 1.0, 2.0, 2.0),
        color="#ff0000",
        point_ids=("bad",),
        evidence=good.value.evidence,
    )
    dto = _dto(prepared, good, bad).model_copy(update={"axes": (axis,), "marks": (mark,)})
    result = _infer(tmp_path, prepared, dto)
    assert tuple(point.point_id for point in result.chart.points) == ("good",)
    assert result.chart.axes == ()
    assert result.chart.marks == ()
    assert any("points.bad.unit:missing_source_evidence" in item for item in result.diagnostics)
    assert any("axes.y.label:missing_source_evidence" in item for item in result.diagnostics)
    assert any("marks.bad-mark:unmapped_point_relation" in item for item in result.diagnostics)
    assert result.chart.verification is Verification.PENDING


@pytest.mark.parametrize(
    "literal,unit,expected",
    [("3,212", "%", "3212"), ("$3.2b", "$b", "3.2"), ("72%", "%", "72")],
)
def test_lexical_numeric_normalization_retains_scale_unit_and_raw_literal(
    tmp_path: Path, literal: str, unit: str, expected: str
) -> None:
    prepared = _prepared(additional_literal=literal)
    observed = _field(prepared, literal)
    base = _point(prepared)
    value = NumericDTO(value=literal, kind="explicit", evidence=observed.evidence)
    point = base.model_copy(
        update={
            "value": value,
            "unit": TextFieldDTO(text=unit, evidence=observed.evidence),
        }
    )
    result = _infer(tmp_path, prepared, _dto(prepared, point))
    assert result.chart.points[0].value.value == Decimal(expected)
    assert result.chart.points[0].unit.text == unit
    assert result.completion.parsed.points[0].value.value == literal
    assert result.chart.points[0].value.evidence.verification is Verification.PENDING
    assert any("lexical_numeric_normalization" in item for item in result.diagnostics)


@pytest.mark.parametrize(
    "literal,unit", [("3,21", "%"), ("$3.2b", "%"), ("unknown", "%"), ("NaN", "%")]
)
def test_uninterpretable_numeric_literal_is_unavailable_without_losing_the_point(
    tmp_path: Path, literal: str, unit: str
) -> None:
    prepared = _prepared(additional_literal=literal)
    observed = _field(prepared, literal)
    point = _point(prepared).model_copy(
        update={
            "value": NumericDTO(value=literal, kind="explicit", evidence=observed.evidence),
            "unit": TextFieldDTO(text=unit, evidence=observed.evidence),
        }
    )
    result = _infer(tmp_path, prepared, _dto(prepared, point))
    assert len(result.chart.points) == 1
    assert result.chart.points[0].value.value is None
    assert result.chart.points[0].value.kind is ValueKind.UNAVAILABLE
    assert result.chart.points[0].category.text == "Agency"
    assert any("unsupported_numeric_literal" in item for item in result.diagnostics)
