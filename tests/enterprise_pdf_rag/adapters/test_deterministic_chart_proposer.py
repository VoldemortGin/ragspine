"""ADR 0037: a fully labelled simple chart is proposed from its own print, never from a model.

Every fixture is self-authored: hand-written native SVG paths plus a synthetic text sidecar,
or the authored donut PDF of ``test_source_paint``. Nothing is real-world data.
"""

import json
from collections.abc import Iterator
from dataclasses import replace
from decimal import Decimal
from hashlib import sha256
from math import pi
from pathlib import Path
from typing import Literal, cast

import pdfspine
import pytest
from pydantic import TypeAdapter

from enterprise_pdf_rag.adapters import semantic_objects
from enterprise_pdf_rag.adapters.chart_publication import resolve_chart_member
from enterprise_pdf_rag.adapters.deterministic_chart_proposer import (
    PROPOSER_PRODUCER,
    ProposalRejected,
    admit_proposal,
    propose_chart,
)
from enterprise_pdf_rag.adapters.document_store import LocalDocumentStore
from enterprise_pdf_rag.adapters.figure_reasoning import PreparedFigure, prepare_figure
from enterprise_pdf_rag.adapters.folder_pipeline import run_folder_pipeline
from enterprise_pdf_rag.adapters.ingest_mode import ingest_plan
from enterprise_pdf_rag.adapters.offline import OfflineDescriptionEmbedder
from enterprise_pdf_rag.adapters.processing_store import ProcessingStore
from enterprise_pdf_rag.adapters.semantic_objects import SemanticObjectAdapter
from enterprise_pdf_rag.processing.index_text import member_index_text
from ragspine.common.evidence.configs import get_settings
from ragspine.common.evidence.providers.json_completion import JsonCompletionClient
from ragspine.common.evidence.providers.providers import load_llm_config
from ragspine.extraction.evidence.document.models import AssetRef, Bounds, TextSidecar, TextSpan
from ragspine.extraction.evidence.figures.models import (
    ChartIR,
    Confidence,
    TextDescription,
    ValueKind,
)
from ragspine.extraction.evidence.page.models import (
    LayoutObject,
    ObjectKind,
    ObjectProcessingRecord,
    PageInput,
    StageState,
)
from tests.enterprise_pdf_rag.adapters.lite_ingest_helpers import (
    LLM_ENV,
    _extent,
    _inside,
    _region,
    _reply,
    published_manifest,
    records_of,
)
from tests.enterprise_pdf_rag.adapters.test_donut_qualification import _sector
from tests.enterprise_pdf_rag.adapters.test_source_paint import _FontInsertionPage, authored_donut

type Label = tuple[str, Bounds]

# A synthetic cost-ratio bar chart: three bars on one baseline, a period below each and a
# percent label above each, and a caption above them all.
BAR_TITLE: Label = ("Cost Ratio", (70.0, 10.0, 130.0, 20.0))
BAR_COLUMNS = (
    ("1H21", "15", 30.0, 50.0),
    ("1H22", "9", 85.0, 70.0),
    ("1H23", "6", 140.0, 90.0),
)
DONUT_LABELS: tuple[Label, ...] = (
    ("Channel Mix", (50.0, 10.0, 170.0, 22.0)),
    ("Sales", (88.0, 71.0, 114.0, 78.0)),
    ("1H26", (88.0, 83.0, 114.0, 90.0)),
    ("Agency", (3.0, 74.0, 40.0, 83.0)),
    ("72%", (55.0, 74.0, 72.0, 83.0)),
    ("28%", (129.0, 74.0, 146.0, 83.0)),
    ("Partners", (160.0, 74.0, 229.0, 83.0)),
)


def _bar_native(tops: tuple[float, ...], extra: str = "") -> bytes:
    bars = "".join(
        f'<path d="M{x} {top}L{x + 30} {top}L{x + 30} 120L{x} 120Z" fill="#336699"/>'
        for (_, _, x, _), top in zip(BAR_COLUMNS, tops, strict=True)
    )
    axis = '<path d="M20 120L180 120" fill="none" stroke="#000000" stroke-width="0.5"/>'
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="200" height="150" viewBox="0 0 200 150">'
        f"{bars}{axis}{extra}</svg>"
    ).encode()


TOPS = tuple(top for *_, top in BAR_COLUMNS)


def _bar_labels(
    tops: tuple[float, ...] = TOPS, *, unlabelled: str | None = None
) -> tuple[Label, ...]:
    labels: list[Label] = [BAR_TITLE]
    for (category, value, x, _), top in zip(BAR_COLUMNS, tops, strict=True):
        labels.append((category, (x + 6, 122.0, x + 24, 130.0)))
        if category != unlabelled:
            labels.append((f"{value}%", (x + 7, top - 10, x + 23, top - 2)))
    return tuple(labels)


def _bar_figure(tops: tuple[float, ...] = TOPS) -> tuple[bytes, tuple[Label, ...]]:
    return _bar_native(tops), _bar_labels(tops)


def _donut_figure() -> tuple[bytes, tuple[Label, ...]]:
    split = pi * 0.28
    paths = (
        f'<path fill="#d31145" d="{_sector(split, 2 * pi - split)}"/>'
        f'<path fill="#333d47" d="{_sector(2 * pi - split, 2 * pi + split)}"/>'
    )
    native = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="240" height="160" viewBox="0 0 240 160">'
        f"{paths}</svg>"
    ).encode()
    return native, DONUT_LABELS


def _page(native: bytes, labels: tuple[Label, ...], width: float, height: float) -> PageInput:
    source = sha256(b"deterministic-chart-proposer-fixture").hexdigest()
    return PageInput(
        "a" * 64,
        source,
        0,
        width,
        height,
        AssetRef(sha256(native).hexdigest(), "image/svg+xml", len(native)),
        TextSidecar(
            "source-text-v1",
            source,
            0,
            tuple(
                TextSpan(f"span-{index}", text, bbox) for index, (text, bbox) in enumerate(labels)
            ),
        ),
    )


def _prepared(native: bytes, labels: tuple[Label, ...], region: Bounds) -> PreparedFigure:
    return prepare_figure(
        page=_page(native, labels, *_size(region)),
        native_svg=native,
        bbox=region,
        region_id="synthetic-chart",
    )


def _size(region: Bounds) -> tuple[float, float]:
    return (200.0, 150.0) if region == BAR_REGION else (240.0, 160.0)


BAR_REGION: Bounds = (0.0, 0.0, 200.0, 150.0)
DONUT_REGION: Bounds = (0.0, 0.0, 240.0, 150.0)


@pytest.fixture
def proposer_on(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("APP_CHART_DETERMINISTIC_FIRST", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def proposer_off(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("APP_CHART_DETERMINISTIC_FIRST", "false")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _observation(view: dict[str, object], text: str, span_of: str | None = None) -> str:
    rows = view["observations"]
    assert isinstance(rows, list)
    if span_of is not None:
        span = next(row["source_span_id"] for row in rows if row["text"] == span_of)
        return str(
            next(row["id"] for row in rows if row["text"] == text and row["source_span_id"] == span)
        )
    return str(next(row["id"] for row in rows if row["text"] == text))


def _model_reply(prompt: str, title: str, points: tuple[tuple[str, str], ...]) -> bytes:
    """What a well-behaved model answers: the same printed fields the proposer would cite."""
    view = json.loads(prompt.rsplit("\n", 1)[1])
    printed = {row["text"] for row in view["observations"]}
    # An unlabelled bar has no value to report; a careful model leaves it out.
    points = tuple((category, value) for category, value in points if f"{value}%" in printed)

    def evidence(*ids: str) -> dict[str, object]:
        return {"element_ids": list(ids), "confidence": "high"}

    def field(text: str) -> dict[str, object]:
        return {"text": text, "evidence": evidence(_observation(view, text))}

    content: dict[str, object]
    if "chart-observations-v1" in prompt:
        content = {
            "schema_version": "chart-observations-v1",
            "svg_digest": view["svg_digest"],
            "grammar": "bar",
            "title": field(title),
            "period": None,
            "axes": [],
            "points": [
                {
                    "point_id": f"p{index + 1}",
                    "series": field(title),
                    "category": field(category),
                    "unit": {
                        "text": "%",
                        "evidence": evidence(_observation(view, "%", f"{value}%")),
                    },
                    "value": {
                        "value": value,
                        "kind": "explicit",
                        "evidence": evidence(_observation(view, value, f"{value}%")),
                    },
                }
                for index, (category, value) in enumerate(points)
            ],
            "marks": [],
            "diagnostics": [],
        }
    else:
        content = {
            "schema_version": "figure-description-v1",
            "svg_digest": view["svg_digest"],
            "claims": [
                {
                    "text": title,
                    "evidence": evidence(_observation(view, title)),
                    "series": None,
                    "category": None,
                    "unit": None,
                    "value": None,
                    "period": None,
                }
            ],
            "diagnostics": [],
        }
    return json.dumps(
        {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(content)}}]}
    ).encode()


def _client(tmp_path: Path, calls: list[str]) -> JsonCompletionClient:
    def sender(url: str, *, api_key: str, payload: bytes, timeout: float) -> bytes:
        prompt = json.loads(payload)["messages"][1]["content"][0]["text"]
        calls.append("chart" if "chart-observations-v1" in prompt else "description")
        return _model_reply(
            prompt,
            BAR_TITLE[0],
            tuple((category, value) for category, value, *_ in BAR_COLUMNS),
        )

    return JsonCompletionClient(
        load_llm_config(
            {
                "APP_LLM_API_KEY": "test",
                "APP_LLM_BASE_URL": "https://example.invalid",
                "APP_LLM_MODEL": "test",
            }
        ),
        cache_dir=tmp_path / "cache",
        max_live_calls=4,
        sender=sender,
    )


def _process(
    tmp_path: Path,
    native: bytes,
    labels: tuple[Label, ...],
    region: Bounds,
    *,
    lite: bool = False,
    policy: Literal["none", "source-labels-only", "donut"] = "none",
) -> tuple[ObjectProcessingRecord, ProcessingStore, list[str], SemanticObjectAdapter]:
    width, height = _size(region)
    sources = LocalDocumentStore(tmp_path / "source")
    stored = sources.put(native, media_type="image/svg+xml")
    page = _page(native, labels, width, height)
    assert page.native_svg == stored
    item = LayoutObject(
        "chart",
        ObjectKind.CHART,
        region,
        tuple(span.span_id for span in page.text.spans),
        "chart",
        Confidence(None, "test"),
    )
    calls: list[str] = []
    outputs = ProcessingStore(tmp_path / "processing")
    adapter = SemanticObjectAdapter(
        sources,
        outputs,
        _client(tmp_path, calls),
        qualification_policy=policy,
        plan=ingest_plan("lite" if lite else "full"),
    )
    return adapter.process(page, item), outputs, calls, adapter


def _stage_json[T](
    outputs: ProcessingStore, record: ObjectProcessingRecord, name: str, kind: type[T]
) -> T:
    stage = next(stage for stage in record.stages if stage.stage == name)
    assert stage.state is StageState.SUCCEEDED and stage.artifact is not None
    return TypeAdapter(kind).validate_json(outputs.assets.get(stage.artifact))


# ---- (a) / (b): a fully labelled chart sends no model call at all -------------------------


@pytest.mark.usefixtures("proposer_on")
def test_fully_labelled_bar_chart_sends_no_model_call(tmp_path: Path) -> None:
    native, labels = _bar_figure()
    record, outputs, calls, adapter = _process(tmp_path, native, labels, BAR_REGION)
    assert calls == []
    stages = {stage.stage: stage for stage in record.stages}
    assert "ir_raw" not in stages and "description_raw" not in stages
    chart = _stage_json(outputs, record, "ir", ChartIR)
    assert chart.producer.startswith(PROPOSER_PRODUCER + ":")
    assert chart.grammar == "bar"
    assert [
        (p.category.text, p.series.text, p.unit.text, p.value.kind, p.value.value)
        for p in chart.points
    ] == [
        ("1H21", "Cost Ratio", "%", ValueKind.EXPLICIT, Decimal("15")),
        ("1H22", "Cost Ratio", "%", ValueKind.EXPLICIT, Decimal("9")),
        ("1H23", "Cost Ratio", "%", ValueKind.EXPLICIT, Decimal("6")),
    ]
    description = _stage_json(outputs, record, "description", TextDescription)
    assert all(claim.value is None for claim in description.claims)
    assert stages["qualification"].state is StageState.UNAVAILABLE
    assert adapter.chart_proposals == {"proposed": 1}
    # Deterministic, and its own stage producer: a rerun is the same record, no cache conflict.
    again, *_ = _process(tmp_path, native, labels, BAR_REGION)
    assert again == record


@pytest.mark.usefixtures("proposer_on")
def test_fully_labelled_donut_sends_no_model_call(tmp_path: Path) -> None:
    native, labels = _donut_figure()
    record, outputs, calls, adapter = _process(tmp_path, native, labels, DONUT_REGION)
    assert calls == []
    chart = _stage_json(outputs, record, "ir", ChartIR)
    assert chart.grammar == "donut"
    assert chart.title is not None and chart.title.text == "Channel Mix"
    assert chart.period is not None and chart.period.text == "1H26"
    assert {
        p.category.text: (p.series.text, p.unit.text, p.value.kind, p.value.value)
        for p in chart.points
    } == {
        "Agency": ("Sales", "%", ValueKind.EXPLICIT, Decimal("72")),
        "Partners": ("Sales", "%", ValueKind.EXPLICIT, Decimal("28")),
    }
    assert adapter.chart_proposals == {"proposed": 1}


def test_bar_values_are_read_from_labels_never_from_bar_heights() -> None:
    first = propose_chart(_prepared(*_bar_figure(), BAR_REGION), policy="none").chart
    second = propose_chart(
        _prepared(*_bar_figure((80.0, 40.0, 60.0)), BAR_REGION), policy="none"
    ).chart
    assert [p.value.value for p in first.points] == [p.value.value for p in second.points]


def test_authored_pdf_donut_needs_the_source_paint_proof_for_its_glyph_outlines() -> None:
    source, prepared, _, _ = authored_donut()
    with pytest.raises(ProposalRejected, match="unexplained_source_paint_requires_review"):
        propose_chart(prepared, policy="none")
    chart = propose_chart(prepared, policy="none", source_pdf=lambda: source).chart
    assert chart.period is not None and chart.period.text == "1H26"
    assert {p.category.text: (p.series.text, p.value.value) for p in chart.points} == {
        "Agency": ("VONB", Decimal("72")),
        "Partnerships": ("VONB", Decimal("28")),
    }


# ---- (c): anything the rules cannot settle falls back to the model, once ------------------


@pytest.mark.parametrize(
    ("native", "labels", "code"),
    [
        (_bar_native(TOPS), _bar_labels(unlabelled="1H22"), "bar_value_label_missing"),
        (
            _bar_native(TOPS),
            (*_bar_labels(), ("restated", (150.0, 10.0, 190.0, 20.0))),
            "bar_title_scope_ambiguous",
        ),
        (
            _bar_native(TOPS, '<path d="M30 30L60 30L60 50L30 50Z" fill="#99aacc"/>'),
            _bar_labels(),
            "bar_fill_overlaps_text",
        ),
    ],
    ids=["missing-label", "unexplained-text", "stacked-segment"],
)
@pytest.mark.usefixtures("proposer_on")
def test_unsettled_bar_chart_falls_back_to_one_model_call(
    tmp_path: Path, native: bytes, labels: tuple[Label, ...], code: str
) -> None:
    with pytest.raises(ProposalRejected, match=code):
        propose_chart(_prepared(native, labels, BAR_REGION), policy="none")
    record, outputs, calls, adapter = _process(tmp_path, native, labels, BAR_REGION, lite=True)
    assert calls == ["chart"]
    stages = {stage.stage for stage in record.stages}
    assert "ir_raw" in stages
    assert _stage_json(outputs, record, "ir", ChartIR).producer.startswith("model-chart-v1:")
    assert adapter.chart_proposals == {"fallback": 1}


# ---- (d): switched off, or fallen back, the record is the one this code wrote before -------


def test_switch_off_never_proposes_and_matches_the_fallback_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, proposer_off: None
) -> None:
    native, labels = _bar_figure()

    def forbidden(*_: object, **__: object) -> None:
        raise AssertionError("the proposer must not run when switched off")

    monkeypatch.setattr(semantic_objects, "propose_chart", forbidden)
    off, _, off_calls, adapter = _process(tmp_path / "off", native, labels, BAR_REGION)
    assert off_calls == ["chart", "description"]
    assert adapter.chart_proposals == {}

    def rejected(*_: object, **__: object) -> None:
        raise ProposalRejected("forced")

    monkeypatch.setenv("APP_CHART_DETERMINISTIC_FIRST", "true")
    get_settings.cache_clear()
    monkeypatch.setattr(semantic_objects, "propose_chart", rejected)
    fallback, _, fallback_calls, _ = _process(tmp_path / "on", native, labels, BAR_REGION)
    assert fallback_calls == ["chart", "description"]
    assert fallback == off


# ---- (e): a proposed IR qualifies into the same shape the model's IR does ------------------


def test_proposed_ir_qualifies_like_the_model_ir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    native, labels = _bar_figure()
    qualified = {}
    for name, enabled in (("model", "false"), ("proposed", "true")):
        monkeypatch.setenv("APP_CHART_DETERMINISTIC_FIRST", enabled)
        get_settings.cache_clear()
        record, outputs, calls, _ = _process(
            tmp_path / name, native, labels, BAR_REGION, lite=True, policy="source-labels-only"
        )
        assert calls == (["chart"] if name == "model" else [])
        chart = _stage_json(outputs, record, "qualified_ir", ChartIR)
        description = _stage_json(outputs, record, "qualified_description", TextDescription)
        qualified[name] = (chart, description, record.qualified_claim_count)
    get_settings.cache_clear()
    (model, model_text, model_count), (proposed, proposed_text, proposed_count) = (
        qualified["model"],
        qualified["proposed"],
    )
    assert model.producer.startswith("verbatim-source-points-v1:model-chart-v1:")
    assert proposed.producer.startswith("verbatim-source-points-v1:" + PROPOSER_PRODUCER)
    assert replace(proposed, producer="") == replace(model, producer="")
    assert [claim.text for claim in proposed_text.claims] == [
        claim.text for claim in model_text.claims
    ]
    assert proposed_count == model_count == 3
    assert member_index_text(proposed, proposed_text.text) == member_index_text(
        model, model_text.text
    )
    assert member_index_text(proposed, proposed_text.text) == (
        "Cost Ratio bar chart figure 1H21 Cost Ratio 15% 1H22 Cost Ratio 9% 1H23 Cost Ratio 6%"
    )


# ---- (f): a point whose value is not explicit printed text is refused ----------------------


@pytest.mark.parametrize(
    "tamper",
    ["derived", "estimated", "cites-category", "other-number"],
)
def test_admission_refuses_any_value_not_printed_as_explicit_text(tamper: str) -> None:
    native, labels = _bar_figure()
    prepared = _prepared(native, labels, BAR_REGION)
    proposal = propose_chart(prepared, policy="none")
    point = proposal.chart.points[0]
    if tamper in ("derived", "estimated"):
        value = replace(point.value, kind=ValueKind(tamper))
    elif tamper == "cites-category":
        value = replace(point.value, evidence=point.category.evidence)
    else:
        value = replace(point.value, value=Decimal("16"))
    chart = replace(
        proposal.chart, points=(replace(point, value=value), *proposal.chart.points[1:])
    )
    with pytest.raises(ProposalRejected, match="value_not_"):
        admit_proposal(prepared, chart, proposal.description, policy="none")


def test_donut_with_an_unpaired_category_is_refused() -> None:
    native, labels = _donut_figure()
    labels = (*labels, ("Other", (41.0, 74.0, 49.0, 83.0)))
    with pytest.raises(ProposalRejected, match="donut:donut_"):
        propose_chart(_prepared(native, labels, DONUT_REGION), policy="none")


# ---- end to end: an authored PDF through run-folder publishes the proposal ---------------

PDF_CHART_REGION = (20.0, 20.0, 280.0, 175.0)


def _cost_ratio_pdf(path: Path) -> Path:
    font = Path(__file__).parents[1].joinpath("fixtures/authored-donut-ascii.ttf").read_bytes()
    with pdfspine.open() as document:
        page = document.new_page(width=300, height=220)
        cast(_FontInsertionPage, page).insert_font(fontname="Authored", fontbuffer=font)
        for left, top in ((45.0, 65.0), (125.0, 80.0), (205.0, 95.0)):
            page.draw_rect((left, top, left + 24.0, 145.0), color=None, fill=(0.2, 0.4, 0.6))
        for text, origin, size in (
            ("Cost Ratio", (110.0, 40.0), 9.0),
            ("15%", (49.0, 61.0), 8.0),
            ("9%", (130.0, 76.0), 8.0),
            ("6%", (212.0, 91.0), 8.0),
            ("1H21", (47.0, 155.0), 8.0),
            ("1H22", (127.0, 155.0), 8.0),
            ("1H23", (207.0, 155.0), 8.0),
            ("Group results", (20.0, 205.0), 8.0),
        ):
            page.insert_text(origin, text, fontname="Authored", fontsize=size)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(document.tobytes())
    return path


@pytest.mark.usefixtures("proposer_on")
def test_folder_run_publishes_a_proposed_chart_without_a_chart_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _cost_ratio_pdf(tmp_path / "pdfs" / "cost.pdf")
    for key, value in LLM_ENV.items():
        monkeypatch.setenv(key, value)
    tasks: list[str] = []

    def sender(url: str, *, api_key: str, payload: bytes, timeout: float) -> bytes:
        prompt = str(json.loads(payload)["messages"][1]["content"][0]["text"])
        if "Source text observations:" not in prompt:
            tasks.append("other")
            raise AssertionError("only the page layout may be asked")
        tasks.append("page-layout")
        observations = json.loads(prompt.split("Source text observations:\n", 1)[1])
        chart = [item for item in observations if _inside(item, PDF_CHART_REGION)]
        rest = [item for item in observations if item not in chart]
        return _reply(
            {
                "regions": [
                    _region("chart", "Chart", list(PDF_CHART_REGION), chart),
                    _region("body", "Text", _extent(rest, (1e9, 1e9, -1e9, -1e9)), rest),
                ],
                "unassigned_span_ids": [],
                "diagnostics": [],
            }
        )

    monkeypatch.setattr("ragspine.common.evidence.providers.json_completion._send_once", sender)
    result = run_folder_pipeline(
        tmp_path / "pdfs",
        ingestion_root=tmp_path / "ingestion",
        max_live_calls_per_pdf=20,
        ingest_mode="lite",
        build_tree=False,
        embedder=OfflineDescriptionEmbedder(),
    )
    assert tasks == ["page-layout"]
    manifest = published_manifest(result)
    (document,) = result.documents
    assert document.ingestion is not None
    sources = LocalDocumentStore(Path(document.ingestion.source_store), activate_on_publish=False)
    outputs = ProcessingStore(Path(document.ingestion.processing_store))
    (record,) = records_of(manifest, ObjectKind.CHART)
    chart = _stage_json(outputs, record, "qualified_ir", ChartIR)
    assert PROPOSER_PRODUCER in chart.producer
    assert [(p.category.text, p.value.value) for p in chart.points] == [
        ("1H21", Decimal("15")),
        ("1H22", Decimal("9")),
        ("1H23", Decimal("6")),
    ]
    assert manifest.retrieval is not None
    plan, _ = outputs.load_retrieval(manifest.retrieval)
    (member,) = tuple(item for item in plan.members if item.kind is ObjectKind.CHART)
    # Publication re-derives the member from the stored raw IR and description, no model.
    resolved = resolve_chart_member(sources, outputs.assets, manifest.scope, member)
    assert resolved.chart == chart


def test_a_bar_chart_never_replays_its_page_for_a_donut_proof() -> None:
    def unread() -> bytes:
        raise AssertionError("no donut proof is attempted without two coloured sectors")

    native, labels = _bar_figure()
    labels = (*labels, ("restated", (150.0, 10.0, 190.0, 20.0)))
    # No axis stroke, and one neutral glyph-like outline inside the caption: exactly the
    # paint that sends a donut to the source replay proof.
    native = native.replace(
        b'<path d="M20 120L180 120" fill="none" stroke="#000000" stroke-width="0.5"/>',
        b'<path d="M70 12L72 12L72 18L70 18Z" fill="#000000"/>',
    )
    with pytest.raises(ProposalRejected, match="two_coloured_sectors_required"):
        propose_chart(_prepared(native, labels, BAR_REGION), policy="none", source_pdf=unread)
