"""A table member is verified only when every present cell repeats its source occurrences."""

from dataclasses import replace

import pytest

from ragspine.extraction.evidence.document.models import TextSpan
from ragspine.extraction.evidence.figures.models import SourceAnchor, Verification
from ragspine.extraction.evidence.objects.tables.table_models import (
    CellContentState,
    SlotState,
    TableCell,
    TableIR,
    TableSlot,
)
from ragspine.extraction.evidence.objects.tables.table_transcription import (
    check_table_transcription,
    table_span_ids,
)

_SHA = "d" * 64
_ANCHOR = (15.0, 55.0, 225.0, 145.0)


def _table(*, header_text: str = "Metric", blank_spans: tuple[str, ...] = ()) -> TableIR:
    cells = (
        TableCell(
            "c-0-0",
            0,
            0,
            1,
            1,
            (20.0, 60.0, 120.0, 86.0),
            ("s-metric",),
            header_text,
            CellContentState.PRESENT,
        ),
        TableCell(
            "c-0-1",
            0,
            1,
            1,
            1,
            (120.0, 60.0, 220.0, 86.0),
            ("s-gross", "s-margin"),
            "Gross margin",
            CellContentState.PRESENT,
        ),
        TableCell(
            "c-1-0", 1, 0, 1, 2, (20.0, 86.0, 220.0, 113.0), blank_spans, "", CellContentState.BLANK
        ),
    )
    slots = (
        (TableSlot(SlotState.ORIGIN, "c-0-0"), TableSlot(SlotState.ORIGIN, "c-0-1")),
        (TableSlot(SlotState.ORIGIN, "c-1-0"), TableSlot(SlotState.CONTINUATION, "c-1-0")),
    )
    return TableIR(
        "table", SourceAnchor(_SHA, _SHA, 2, (20.0, 60.0, 220.0, 140.0)), 2, 2, cells, slots
    )


def _spans(**overrides: TextSpan) -> dict[str, TextSpan]:
    spans = {
        "s-metric": TextSpan("s-metric", "Metric", (26.0, 69.0, 66.0, 80.0)),
        "s-gross": TextSpan("s-gross", "Gross", (126.0, 64.0, 150.0, 72.0)),
        "s-margin": TextSpan("s-margin", "margin", (126.0, 74.0, 160.0, 82.0)),
        "s-stray": TextSpan("s-stray", "note", (26.0, 90.0, 60.0, 100.0)),
    }
    spans.update(overrides)
    return spans


def test_span_ids_follow_cell_order_and_exact_cells_pass() -> None:
    table = _table()
    assert table.verification is Verification.PENDING  # structure stays inferred
    assert table_span_ids(table) == ("s-metric", "s-gross", "s-margin")
    check_table_transcription(table, _spans(), anchor=_ANCHOR)


def test_present_cell_text_must_repeat_its_occurrences_modulo_whitespace() -> None:
    check_table_transcription(_table(), _spans(), anchor=_ANCHOR)
    with pytest.raises(ValueError, match="exact transcription"):
        check_table_transcription(_table(header_text="Metrics"), _spans(), anchor=_ANCHOR)
    with pytest.raises(ValueError, match="exact transcription"):
        check_table_transcription(
            _table(),
            _spans(**{"s-metric": TextSpan("s-metric", "1,234", (26.0, 69.0, 66.0, 80.0))}),
            anchor=_ANCHOR,
        )


def test_blank_cells_and_geometry_are_checked() -> None:
    with pytest.raises(ValueError, match="hides source occurrences"):
        check_table_transcription(_table(blank_spans=("s-stray",)), _spans(), anchor=_ANCHOR)
    with pytest.raises(ValueError, match="unknown source occurrence"):
        spans = _spans()
        del spans["s-margin"]
        check_table_transcription(_table(), spans, anchor=_ANCHOR)
    with pytest.raises(ValueError, match="outside its cell"):
        check_table_transcription(
            _table(),
            _spans(**{"s-metric": TextSpan("s-metric", "Metric", (126.0, 69.0, 166.0, 80.0))}),
            anchor=_ANCHOR,
        )
    with pytest.raises(ValueError, match="outside its qualified anchor"):
        check_table_transcription(_table(), _spans(), anchor=(20.0, 60.0, 200.0, 140.0))
    with pytest.raises(ValueError, match="outside its qualified anchor"):
        check_table_transcription(
            _table(),
            _spans(**{"s-metric": TextSpan("s-metric", "Metric", (10.0, 69.0, 66.0, 80.0))}),
            anchor=_ANCHOR,
        )


def test_anchor_tolerates_model_rendered_float_noise_but_not_real_overreach() -> None:
    # The layout anchor is the model's echo of canonical coordinates; the native grid
    # and the spans keep pdfspine's float noise.
    anchor = (20.0, 60.0, 220.0, 140.0)
    noisy_grid = replace(
        _table(), source=SourceAnchor(_SHA, _SHA, 2, (20.0, 60.0, 220.0, 140.00000000000003))
    )
    spans = _spans(
        **{"s-metric": TextSpan("s-metric", "Metric", (19.999999999999996, 69.0, 66.0, 80.0))}
    )
    check_table_transcription(noisy_grid, spans, anchor=anchor)
    with pytest.raises(ValueError, match="outside its qualified anchor"):
        check_table_transcription(noisy_grid, spans, anchor=(20.0, 60.0, 220.0, 139.5))
    with pytest.raises(ValueError, match="outside its qualified anchor"):
        check_table_transcription(
            noisy_grid,
            _spans(**{"s-metric": TextSpan("s-metric", "Metric", (19.5, 69.0, 66.0, 80.0))}),
            anchor=anchor,
        )
